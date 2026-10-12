import secrets
import sys
from datetime import timedelta
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import PetDelivery
from apps.accounts.models import DeliveryProfile
from apps.adoptions.models import AdoptionApplication

def generate_secure_otp() -> str:
    """
    Returns a cryptographically secure 6-digit zero-padded numeric string.
    """
    return f"{secrets.randbelow(900000) + 100000:06d}"

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

    if str(submitted_otp).strip() != str(delivery.pickup_otp).strip():
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

    if str(submitted_otp).strip() != str(delivery.doorstep_otp).strip():
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
