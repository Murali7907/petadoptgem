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


def send_driver_assigned_notice(delivery: PetDelivery) -> bool:
    """
    Sends an email and in-app notification to the adopter informing them that a driver
    has been assigned to their pet's delivery, including driver contact and vehicle info.
    Does NOT include any OTP.
    """
    try:
        application = getattr(delivery, 'application', None)
        if not application:
            return False

        customer = getattr(application, 'customer', None) or getattr(application, 'applicant', None)
        if not customer:
            return False

        pet = getattr(application, 'pet', None)
        driver = getattr(delivery, 'driver', None)
        driver_user = getattr(driver, 'user', None) if driver else None

        customer_email = getattr(customer, 'email', None)
        customer_username = getattr(customer, 'username', 'Valued Adopter')
        pet_name = getattr(pet, 'name', 'your pet')
        driver_username = getattr(driver_user, 'username', 'Assigned Delivery Partner')
        driver_phone = getattr(driver_user, 'phone', '') or 'Not provided'
        vehicle_type = driver.get_vehicle_type_display() if driver else 'Vehicle'
        vehicle_number = getattr(driver, 'vehicle_number', '') or 'Not provided'

        if customer_email:
            subject = f"Delivery Scheduled for {pet_name} - Driver Assigned"
            message = (
                f"Hello {customer_username},\n\n"
                f"Great news! A delivery driver has been assigned for {pet_name}'s delivery.\n\n"
                f"Driver Details:\n"
                f"- Driver Name: {driver_username}\n"
                f"- Contact Phone: {driver_phone}\n"
                f"- Vehicle: {vehicle_type} ({vehicle_number})\n\n"
                f"Your pet is currently being prepared for pickup at the shelter. "
                f"When the driver picks up {pet_name} and is on the way to your doorstep, "
                f"you will receive a separate confidential verification OTP.\n\n"
                f"Thank you for adopting through KindHeart Pet Adoption Platform!\n"
            )
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@kindheart.org')
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[customer_email],
                fail_silently=False,
            )

        # In-app notification (without OTP)
        try:
            from apps.accounts.models import Notification
            Notification.objects.create(
                recipient=customer,
                title=f"Delivery Scheduled for {pet_name}",
                message=f"Driver {driver_username} ({driver_phone}) has been assigned to deliver {pet_name}. You will receive a verification OTP when the driver is on the way.",
                notification_type='delivery'
            )
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"Error sending driver assigned notice for delivery #{delivery.id}: {e}", exc_info=True)
        return False


def assign_delivery_task(application: AdoptionApplication, driver: DeliveryProfile, payment_method: str = 'unset') -> PetDelivery:
    """
    Assigns an approved application to an available driver inside transaction.atomic().
    Issues pickup OTP (HMAC hash stored, plaintext returned via raw_pickup_otp attribute).
    Sends driver-assigned notice to adopter with driver details and NO OTP.
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

        if existing_delivery:
            delivery = existing_delivery
            delivery.driver = driver
            delivery.status = 'assigned'
            delivery.pickup_otp = ''
            delivery.doorstep_otp = ''
            delivery.secret_otp = ''
            delivery.doorstep_otp_hash = None
            delivery.doorstep_otp_sent_at = None
            delivery.doorstep_otp_expires_at = None
            delivery.pickup_otp_attempts = 0
            delivery.doorstep_otp_attempts = 0
            delivery.save()
        else:
            delivery = PetDelivery.objects.create(
                application=application,
                driver=driver,
                status='assigned',
                pickup_otp='',
                doorstep_otp='',
                secret_otp='',
                pickup_otp_attempts=0,
                doorstep_otp_attempts=0,
            )

        # Issue pickup OTP via cryptographic helper
        raw_pickup_otp = issue_otp(delivery, 'pickup')
        delivery.raw_pickup_otp = raw_pickup_otp

        if payment_method and payment_method != 'unset':
            application.payment_method = payment_method
            application.save()

        driver.is_available = False
        driver.save()

        # Send adopter notice (driver contact info, NO OTP)
        try:
            send_driver_assigned_notice(delivery)
        except Exception as e:
            logger.error(f"Failed to send driver assigned notice for delivery #{delivery.id}: {e}", exc_info=True)

        return delivery


def verify_pickup_otp(delivery: PetDelivery, submitted_otp: str) -> bool:
    """
    Verifies pickup OTP submitted by driver at shelter.
    On success:
      - Marks delivery status='out_for_delivery', dispatched_at=now.
      - Clears pickup_otp_hash and plaintext pickup_otp.
      - Issues doorstep OTP via issue_otp(delivery, 'doorstep').
      - Emails plaintext doorstep OTP to customer and sets doorstep_otp_sent_at.
      - In-app notification informs customer delivery is on the way without OTP code.
      - Email failure logs warning without aborting transition.
    """
    if delivery.status != 'assigned':
        raise ValidationError(f"Cannot verify pickup for delivery in '{delivery.get_status_display()}' state.")

    if delivery.expires_at and timezone.now() > delivery.expires_at:
        raise ValidationError("Pickup OTP has expired. Please contact shelter admin.")

    max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 3)
    if delivery.pickup_otp_attempts >= max_attempts:
        raise ValidationError("Maximum pickup OTP attempts exceeded. Contact support.")

    is_valid = False
    if delivery.pickup_otp_hash:
        is_valid = check_otp(delivery.pickup_otp_hash, delivery.id, 'pickup', submitted_otp)
    elif delivery.pickup_otp:
        is_valid = hmac.compare_digest(str(submitted_otp).strip(), str(delivery.pickup_otp).strip())

    if not is_valid:
        delivery.pickup_otp_attempts += 1
        delivery.save(update_fields=['pickup_otp_attempts'])
        raise ValidationError("Invalid pickup OTP.")

    delivery.status = 'out_for_delivery'
    delivery.dispatched_at = timezone.now()
    delivery.pickup_otp_hash = None
    delivery.pickup_otp = ''

    # Issue doorstep OTP
    raw_doorstep_otp = issue_otp(delivery, 'doorstep')
    delivery.raw_doorstep_otp = raw_doorstep_otp

    # Email customer doorstep OTP (idempotent: only if not already sent)
    if not delivery.doorstep_otp_sent_at:
        try:
            sent = dispatch_doorstep_otp(delivery, raw_otp=raw_doorstep_otp)
            if sent:
                delivery.doorstep_otp_sent_at = timezone.now()
        except Exception as e:
            logger.error(f"Failed to email doorstep OTP for delivery #{delivery.id}: {e}", exc_info=True)

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

    max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 3)
    if delivery.doorstep_otp_attempts >= max_attempts:
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
        delivery.save(update_fields=['doorstep_otp_attempts'])
        if delivery.doorstep_otp_attempts >= max_attempts:
            raise ValidationError("Maximum OTP attempts exceeded. Contact support.")
        raise ValidationError("Invalid OTP.")

    with transaction.atomic():
        delivery.status = 'delivered'
        delivery.delivered_at = timezone.now()
        delivery.doorstep_otp_hash = None
        delivery.doorstep_otp = ''
        delivery.secret_otp = ''
        delivery.save()

        if delivery.driver:
            driver = delivery.driver
            driver.is_available = True
            driver.save(update_fields=['is_available'])

        application = delivery.application
        application.status = 'completed'

        # Payment status evaluation: paid only if online Payment exists or COD collected
        total_amount = getattr(application, 'total_amount', Decimal('0.00')) or Decimal('0.00')
        payment_verified = False
        if application.payments.filter(status__in=['paid', 'captured']).exists():
            payment_verified = True
        elif application.payment_method == 'cod' and delivery.cash_collected >= total_amount and delivery.cash_collected > Decimal('0.00'):
            payment_verified = True

        if payment_verified:
            application.payment_status = 'paid'

        application.save()

    return True


def dispatch_doorstep_otp(delivery: PetDelivery, raw_otp: str = None) -> bool:
    """
    Automated doorstep verification OTP and status dispatch service for pet deliveries.
    Sends an email with the confidential doorstep verification OTP to the adopter.
    If raw_otp is not provided, falls back to delivery.raw_doorstep_otp or legacy delivery.secret_otp.
    Does NOT include OTP in the in-app notification.

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

        secret_otp = raw_otp or getattr(delivery, 'raw_doorstep_otp', None) or getattr(delivery, 'secret_otp', None) or getattr(delivery, 'doorstep_otp', None)

        if not secret_otp:
            logger.error(f"Cannot dispatch OTP: Delivery #{delivery.id} has no raw OTP.")
            return False

        if not customer_email:
            logger.warning(f"Delivery #{delivery.id}: Customer {customer_username} has no email address.")
            return False

        subject = f"Your Pet Delivery for {pet_name} is on the way! Doorstep Verification OTP"
        stage_message = f"Great news! Your pet delivery for {pet_name} is now on the way to your doorstep!"

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

        # In-App Notification (without OTP code!)
        try:
            from apps.accounts.models import Notification
            Notification.objects.create(
                recipient=customer,
                title=f"Delivery Alert: {pet_name} is on the way!",
                message=f"Driver {driver_username} ({driver_phone}) is on the way with {pet_name}. Check your email for your confidential verification OTP.",
                notification_type='delivery'
            )
        except Exception:
            pass

        return True

    except Exception as e:
        logger.error(f"Failed to dispatch doorstep OTP for delivery #{getattr(delivery, 'id', 'unknown')}: {str(e)}", exc_info=True)
        return False


def reissue_delivery_otp(delivery: PetDelivery, purpose: str) -> dict:
    """
    Reissues an OTP for either 'pickup' or 'doorstep' verification.
    Resets attempt counter and extends expiry time.
    For doorstep, sends plaintext OTP via email to adopter and updates doorstep_otp_sent_at.
    """
    if purpose not in ['pickup', 'doorstep']:
        raise ValidationError("Purpose must be 'pickup' or 'doorstep'.")

    from rest_framework.exceptions import APIException
    class Conflict(APIException):
        status_code = 409
        default_detail = "Action not valid for current delivery state."

    if purpose == 'pickup':
        if delivery.status != 'assigned':
            raise Conflict(detail=f"Cannot reissue pickup OTP for delivery in '{delivery.status}' status.")

        raw_pickup_otp = issue_otp(delivery, 'pickup')
        return {
            "detail": "Pickup OTP reissued successfully.",
            "purpose": "pickup",
            "pickup_otp": raw_pickup_otp,
            "expires_at": delivery.expires_at.isoformat() if delivery.expires_at else None
        }

    elif purpose == 'doorstep':
        if delivery.status != 'out_for_delivery':
            raise Conflict(detail=f"Cannot reissue doorstep OTP for delivery in '{delivery.status}' status.")

        raw_doorstep_otp = issue_otp(delivery, 'doorstep')
        email_sent = False
        try:
            email_sent = dispatch_doorstep_otp(delivery, raw_otp=raw_doorstep_otp)
            if email_sent:
                delivery.doorstep_otp_sent_at = timezone.now()
                delivery.save(update_fields=['doorstep_otp_sent_at'])
        except Exception as e:
            logger.error(f"Failed to email reissued doorstep OTP for delivery #{delivery.id}: {e}", exc_info=True)

        return {
            "detail": "Doorstep OTP reissued and emailed to adopter.",
            "purpose": "doorstep",
            "email_dispatched": email_sent,
            "expires_at": delivery.doorstep_otp_expires_at.isoformat() if delivery.doorstep_otp_expires_at else None
        }


def send_delivery_completed_notice(delivery: PetDelivery) -> bool:
    """
    Sends email and in-app notification to customer when delivery is completed.
    """
    try:
        application = getattr(delivery, 'application', None)
        if not application:
            return False
        customer = getattr(application, 'customer', None) or getattr(application, 'applicant', None)
        if not customer:
            return False
        pet = getattr(application, 'pet', None)
        pet_name = getattr(pet, 'name', 'your pet')
        customer_email = getattr(customer, 'email', None)

        if customer_email:
            subject = f"Delivery Completed: Welcome {pet_name} home!"
            message = (
                f"Hello {customer.username},\n\n"
                f"Congratulations! Your delivery for {pet_name} has been completed successfully.\n\n"
                f"We are thrilled to welcome {pet_name} to their forever home!\n\n"
                f"Thank you for adopting through KindHeart Pet Adoption Platform!\n"
            )
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@kindheart.org')
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[customer_email],
                fail_silently=False,
            )

        try:
            from apps.accounts.models import Notification
            Notification.objects.create(
                recipient=customer,
                title=f"Delivery Completed: Welcome {pet_name} home!",
                message=f"Congratulations! Delivery for {pet_name} has been completed successfully. Welcome {pet_name} home!",
                notification_type='delivery'
            )
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error(f"Failed to send completion notice for delivery #{delivery.id}: {e}", exc_info=True)
        return False


