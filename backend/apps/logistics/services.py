import hmac
import logging
import secrets
import sys
from datetime import timedelta
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import PetDelivery
from .otp import generate_otp, check_otp, hash_otp, issue_otp
from apps.accounts.models import DeliveryProfile
from apps.adoptions.models import AdoptionApplication

logger = logging.getLogger(__name__)

def generate_secure_otp() -> str:
    """
    Returns a cryptographically secure 6-digit zero-padded numeric string via apps.logistics.otp.
    """
    return generate_otp()

def assign_delivery_task(application: AdoptionApplication, driver: DeliveryProfile, payment_method: str = 'unset') -> PetDelivery:
    """
    Assigns an approved application to an available driver inside transaction.atomic().
    """
    with transaction.atomic():
        if application.status != 'approved':
            raise ValidationError(f"Cannot assign delivery for application with status '{application.get_status_display()}'. Must be 'approved'.")

        if driver.shelter_id != application.pet.shelter_id:
            raise ValidationError("Driver belongs to a different shelter organization.")

        if not driver.is_available:
            raise ValidationError("Driver is currently unavailable or already assigned to another task.")

        active_driver_delivery = PetDelivery.objects.filter(
            driver=driver,
            status__in=['assigned', 'out_for_delivery']
        ).exists()
        if active_driver_delivery:
            raise ValidationError("Driver already has an active delivery in progress.")

        existing_delivery = getattr(application, 'delivery', None)
        pickup_code = generate_secure_otp()
        doorstep_code = generate_secure_otp()
        expiry_time = timezone.now() + timedelta(hours=12)

        if existing_delivery:
            delivery = existing_delivery
            delivery.driver = driver
            delivery.status = 'assigned'
            delivery.pickup_otp = pickup_code
            delivery.doorstep_otp = doorstep_code
            delivery.secret_otp = doorstep_code
            delivery.expires_at = expiry_time
            delivery.doorstep_otp_expires_at = expiry_time
            delivery.pickup_otp_attempts = 0
            delivery.doorstep_otp_attempts = 0
            delivery.save()
        else:
            delivery = PetDelivery.objects.create(
                application=application,
                driver=driver,
                status='assigned',
                pickup_otp=pickup_code,
                doorstep_otp=doorstep_code,
                secret_otp=doorstep_code,
                expires_at=expiry_time,
                doorstep_otp_expires_at=expiry_time,
                pickup_otp_attempts=0,
                doorstep_otp_attempts=0
            )

        if payment_method and payment_method != 'unset':
            application.payment_method = payment_method
            application.save()

        driver.is_available = False
        driver.save()

        return delivery


def verify_pickup_otp(delivery: PetDelivery, submitted_otp: str) -> bool:
    """
    Verifies pickup OTP submitted by driver at shelter.
    """
    if delivery.status != 'assigned':
        raise ValidationError(f"Cannot verify pickup for delivery in '{delivery.get_status_display()}' state.")

    if delivery.expires_at and timezone.now() > delivery.expires_at:
        raise ValidationError("Pickup OTP has expired. Please contact shelter admin.")

    if delivery.pickup_otp_attempts >= 5:
        raise ValidationError("Maximum pickup OTP attempts exceeded. Contact support.")

    is_valid = False
    if delivery.pickup_otp_hash:
        is_valid = check_otp(delivery.pickup_otp_hash, delivery.id, 'pickup', submitted_otp)
    elif delivery.pickup_otp:
        is_valid = hmac.compare_digest(str(submitted_otp).strip(), str(delivery.pickup_otp).strip())

    if not is_valid:
        delivery.pickup_otp_attempts += 1
        delivery.save()
        raise ValidationError("Invalid pickup OTP.")

    delivery.status = 'out_for_delivery'
    delivery.dispatched_at = timezone.now()
    delivery.save()
    return True


def verify_doorstep_otp(delivery: PetDelivery, submitted_otp: str) -> bool:
    """
    Verifies doorstep 6-digit OTP provided by customer to driver upon delivery arrival.
    """
    if delivery.status != 'out_for_delivery':
        raise ValidationError(f"Cannot verify doorstep OTP for delivery in '{delivery.get_status_display()}' state.")

    if delivery.doorstep_otp_expires_at and timezone.now() > delivery.doorstep_otp_expires_at:
        raise ValidationError("Doorstep OTP has expired. Contact support.")

    if delivery.doorstep_otp_attempts >= 5:
        raise ValidationError("Maximum OTP attempts exceeded. Contact support.")

    is_valid = False
    if delivery.doorstep_otp_hash:
        is_valid = check_otp(delivery.doorstep_otp_hash, delivery.id, 'doorstep', submitted_otp)
    elif delivery.doorstep_otp:
        is_valid = hmac.compare_digest(str(submitted_otp).strip(), str(delivery.doorstep_otp).strip())
    elif delivery.secret_otp:
        is_valid = hmac.compare_digest(str(submitted_otp).strip(), str(delivery.secret_otp).strip())

    if not is_valid:
        delivery.doorstep_otp_attempts += 1
        delivery.save()
        if delivery.doorstep_otp_attempts >= 5:
            raise ValidationError("Maximum OTP attempts exceeded. Contact support.")
        raise ValidationError("Invalid OTP.")

    with transaction.atomic():
        delivery.status = 'delivered'
        delivery.delivered_at = timezone.now()
        delivery.save()

        if delivery.driver:
            driver = delivery.driver
            driver.is_available = True
            driver.save()

        application = delivery.application
        application.status = 'completed'
        application.payment_status = 'paid'
        application.save()

    return True


def dispatch_doorstep_otp(delivery: PetDelivery) -> bool:
    """
    Automated doorstep verification OTP and status dispatch service for pet deliveries.
    Extracts customer.email, customer.username, pet.name, driver.user.username,
    driver.user.phone, and secret_otp, formatting and sending an email alerting the adopter
    that their delivery is on the way with their confidential verification OTP.

    Returns True if successfully dispatched, False otherwise.
    """
    try:
        application = getattr(delivery, 'application', None)
        if not application:
            logger.error(f"Cannot dispatch OTP: Delivery #{getattr(delivery, 'id', 'unknown')} has no linked application.")
            return False

        customer = getattr(application, 'customer', None) or getattr(application, 'applicant', None)
        if not customer:
            logger.error(f"Cannot dispatch OTP: Application #{application.id} has no customer.")
            return False

        pet = getattr(application, 'pet', None)
        driver = getattr(delivery, 'driver', None)
        driver_user = getattr(driver, 'user', None) if driver else None

        customer_email = getattr(customer, 'email', None)
        customer_username = getattr(customer, 'username', 'Valued Adopter')
        pet_name = getattr(pet, 'name', 'your pet')
        driver_username = getattr(driver_user, 'username', 'Assigned Delivery Partner')
        driver_phone = getattr(driver_user, 'phone', '') or 'Not provided'
        secret_otp = getattr(delivery, 'secret_otp', None) or getattr(delivery, 'doorstep_otp', None)

        if not secret_otp:
            logger.error(f"Cannot dispatch OTP: Delivery #{delivery.id} has no secret_otp.")
            return False

        if not customer_email:
            logger.warning(f"Delivery #{delivery.id}: Customer {customer_username} has no email address.")
            return False

        delivery_status = getattr(delivery, 'status', 'assigned')
        if delivery_status in ['out_for_delivery', 'in_transit']:
            subject = f"Your Pet Delivery for {pet_name} is on the way! Doorstep Verification OTP"
            stage_message = f"Great news! Your pet delivery for {pet_name} is now on the way to your doorstep!"
        elif delivery_status == 'assigned':
            subject = f"Delivery Scheduled for {pet_name} - Doorstep Verification OTP"
            stage_message = f"Great news! Driver {driver_username} has been assigned for {pet_name}'s delivery."
        else:
            subject = f"Delivery Update for {pet_name} - Doorstep Verification OTP"
            stage_message = f"Your delivery for {pet_name} has updated to status '{delivery_status}'."

        message = (
            f"Hello {customer_username},\n\n"
            f"{stage_message}\n\n"
            f"Delivery Driver Details:\n"
            f"- Driver Name: {driver_username}\n"
            f"- Contact Phone: {driver_phone}\n\n"
            f"Your confidential 6-digit doorstep verification OTP is: {secret_otp}\n\n"
            f"Please share this confidential verification OTP with your driver ONLY after they arrive "
            f"at your doorstep and you have verified the arrival and safety of {pet_name}.\n\n"
            f"Thank you for adopting through KindHeart Pet Adoption Platform!\n"
        )

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@kindheart.org')
        sent_count = send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[customer_email],
            fail_silently=False,
        )
        if sent_count <= 0:
            logger.warning(f"Delivery #{delivery.id}: send_mail dispatched 0 messages to {customer_email}")
            return False

        logger.info(f"Successfully dispatched doorstep OTP for delivery #{delivery.id} to {customer_email}")

        # In-App Notification (if Notification model is available)
        try:
            from apps.accounts.models import Notification
            Notification.objects.create(
                recipient=customer,
                title=f"Delivery Alert: {pet_name} is on the way!",
                message=f"Driver {driver_username} ({driver_phone}) is on the way with {pet_name}. Your 6-digit verification OTP is {secret_otp}.",
                notification_type='delivery'
            )
        except Exception:
            pass

        return True

    except Exception as e:
        logger.error(f"Failed to dispatch doorstep OTP for delivery #{getattr(delivery, 'id', 'unknown')}: {str(e)}", exc_info=True)
        return False
