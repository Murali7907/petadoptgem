import hmac
import logging
import secrets
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, APIException
from .models import PetDelivery, Payment, PaymentStatus


class ConflictError(APIException):
    status_code = 409
    default_detail = "Conflict with current delivery state."
    default_code = "conflict"

from .otp import generate_otp, check_otp, hash_otp, issue_otp
from apps.accounts.models import DeliveryProfile
from apps.adoptions.models import AdoptionApplication

from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class OTPVerificationOutcome(str, Enum):
    SUCCESS = "success"
    ALREADY_COMPLETED = "already_completed"
    WRONG_STATE = "wrong_state"
    EXPIRED = "expired"
    LOCKED = "locked"
    INVALID = "invalid"
    PAYMENT_REQUIRED = "payment_required"
    PAYMENT_METHOD_REQUIRED = "payment_method_required"
    CASH_NOT_COLLECTED = "cash_not_collected"


@dataclass
class OTPVerificationResult:
    outcome: OTPVerificationOutcome
    attempts_remaining: int = 0
    delivery: PetDelivery | None = None
    detail: str = ""
    code: str = ""


def is_payment_satisfied(application: AdoptionApplication, delivery: PetDelivery | None = None) -> tuple[bool, int, int]:
    """
    Returns (is_satisfied, total_paid_paise, expected_paise).
    Consolidates payment verification across online (Razorpay) and COD ledger rows.
    """
    total_amount = getattr(application, 'total_amount', Decimal('0.00')) or Decimal('0.00')
    expected_paise = int(round(Decimal(str(total_amount)) * 100))
    if expected_paise <= 0:
        return True, 0, 0

    paid_payments = application.payments.filter(status__in=[PaymentStatus.CAPTURED, PaymentStatus.PAID, 'captured', 'paid'])
    total_paid_paise = sum(
        p.amount_in_paise if p.amount_in_paise else int(round(Decimal(str(p.amount)) * 100))
        for p in paid_payments
    )

    if delivery and delivery.cash_collected and delivery.cash_collected >= total_amount and delivery.cash_collected > Decimal('0.00'):
        collected_paise = int(round(Decimal(str(delivery.cash_collected)) * 100))
        total_paid_paise = max(total_paid_paise, collected_paise)

    return total_paid_paise >= expected_paise, total_paid_paise, expected_paise


def check_pickup_payment_guard(delivery: PetDelivery) -> tuple[bool, str, str]:
    """
    Validates payment requirements prior to pickup OTP verification.
    Consumes NO OTP attempts.

    Rules:
    - total_amount == 0 -> allowed
    - payment_method 'cod' -> allowed
    - payment_method 'online' -> allowed only if a Payment is captured with amount >= total in paise, else 409 {"code": "payment_required"}
    - payment_method 'unset' with total > 0 -> 409 {"code": "payment_method_required"}

    Returns (allowed: bool, code: str, detail: str).
    """
    application = getattr(delivery, 'application', None)
    if not application:
        return True, "", ""

    total_amount = getattr(application, 'total_amount', Decimal('0.00')) or Decimal('0.00')
    if total_amount <= Decimal('0.00'):
        return True, "", ""

    payment_method = getattr(application, 'payment_method', 'unset')
    if payment_method == 'cod':
        return True, "", ""

    if payment_method == 'online':
        satisfied, _, _ = is_payment_satisfied(application, delivery)
        if satisfied:
            return True, "", ""
        return False, "payment_required", "Online payment must be captured before pet pickup."

    # payment_method 'unset' (or unknown) with total > 0
    return False, "payment_method_required", "Payment method must be selected before pet pickup."


def check_doorstep_payment_guard(delivery: PetDelivery) -> tuple[bool, str, str]:
    """
    Validates payment requirements prior to doorstep OTP verification.
    Consumes NO OTP attempts.

    Rule:
    Doorstep verification for COD with total > 0 and cash_collected < total returns 409 {"code": "cash_not_collected"}.
    """
    application = getattr(delivery, 'application', None)
    if not application:
        return True, "", ""

    total_amount = getattr(application, 'total_amount', Decimal('0.00')) or Decimal('0.00')
    if total_amount <= Decimal('0.00'):
        return True, "", ""

    payment_method = getattr(application, 'payment_method', 'unset')
    if payment_method == 'cod':
        cash_collected = getattr(delivery, 'cash_collected', Decimal('0.00')) or Decimal('0.00')
        if cash_collected < total_amount:
            remaining = total_amount - cash_collected
            return False, "cash_not_collected", f"Cash on delivery balance must be collected in full before doorstep verification. Remaining balance: {remaining}."

    return True, "", ""

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
        applicant = getattr(application, 'applicant', None)
        adopter_email = (getattr(applicant, 'email', '') or '').strip()
        if not adopter_email:
            raise ConflictError(
                detail={"code": "adopter_email_missing", "detail": "Cannot assign delivery task: adopter has no email address."},
                code="adopter_email_missing"
            )

        existing_delivery = PetDelivery.objects.select_for_update().filter(application=application).first()

        if existing_delivery and existing_delivery.status in ['out_for_delivery', 'delivered']:
            raise ConflictError(f"Cannot reassign delivery in '{existing_delivery.status}' state.")

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

        if existing_delivery:

            # Free previous driver if different
            if existing_delivery.driver and existing_delivery.driver != driver:
                old_driver = existing_delivery.driver
                old_driver.is_available = True
                old_driver.save(update_fields=['is_available'])

            delivery = existing_delivery
            delivery.driver = driver
            delivery.status = 'assigned'
            delivery.dispatched_at = None
            delivery.delivered_at = None
            delivery.pickup_otp = ''
            delivery.doorstep_otp = ''
            delivery.secret_otp = ''
            delivery.pickup_otp_hash = None
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

        # Send adopter notice (driver contact info, NO OTP) via transaction.on_commit
        deliv_id = delivery.id
        def _send_assigned_notice():
            try:
                deliv = PetDelivery.objects.filter(id=deliv_id).first()
                if deliv:
                    send_driver_assigned_notice(deliv)
            except Exception as e:
                logger.error(f"Failed to send driver assigned notice for delivery #{deliv_id}: {e}", exc_info=True)

        transaction.on_commit(_send_assigned_notice)

        return delivery


def verify_pickup_service(delivery_id: int | str, submitted_otp: str) -> OTPVerificationResult:
    """
    Authoritative service to verify pickup OTP under row lock.
    Transitions delivery from 'assigned' to 'out_for_delivery' upon successful OTP validation.
    Issues doorstep OTP and queues delivery alert email via transaction.on_commit.
    Never raises exceptions for invalid OTP, lockout, expiry, or wrong state; returns OTPVerificationResult.
    """
    with transaction.atomic():
        try:
            delivery = PetDelivery.objects.select_for_update().get(id=delivery_id)
        except PetDelivery.DoesNotExist:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.WRONG_STATE,
                detail=f"Delivery #{delivery_id} not found."
            )

        if delivery.status != 'assigned':
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.WRONG_STATE,
                delivery=delivery,
                detail=f"Cannot verify pickup for delivery in '{delivery.status}' state.",
                code="invalid_state"
            )

        max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 3)

        # Check pickup payment guard before OTP verification (consumes NO attempts)
        allowed, code, detail = check_pickup_payment_guard(delivery)
        if not allowed:
            outcome = OTPVerificationOutcome.PAYMENT_REQUIRED if code == "payment_required" else OTPVerificationOutcome.PAYMENT_METHOD_REQUIRED
            return OTPVerificationResult(
                outcome=outcome,
                attempts_remaining=max(0, max_attempts - delivery.pickup_otp_attempts),
                delivery=delivery,
                detail=detail,
                code=code
            )

        if delivery.pickup_otp_attempts >= max_attempts:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.LOCKED,
                attempts_remaining=0,
                delivery=delivery,
                detail="Maximum OTP verification attempts exceeded. Please contact the shelter to reissue an OTP.",
                code="otp_locked"
            )

        now = timezone.now()
        if delivery.expires_at and now > delivery.expires_at:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.EXPIRED,
                attempts_remaining=max(0, max_attempts - delivery.pickup_otp_attempts),
                delivery=delivery,
                detail="OTP has expired. Please contact the shelter to reissue an OTP.",
                code="otp_expired"
            )

        # Hash-only verification (no plaintext fallback)
        is_valid = False
        if delivery.pickup_otp_hash:
            is_valid = check_otp(delivery.pickup_otp_hash, delivery.id, 'pickup', submitted_otp)

        if not is_valid:
            delivery.pickup_otp_attempts += 1
            delivery.save(update_fields=['pickup_otp_attempts'])
            attempts_remaining = max(0, max_attempts - delivery.pickup_otp_attempts)
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.INVALID,
                attempts_remaining=attempts_remaining,
                delivery=delivery,
                detail="Invalid pickup OTP.",
                code="otp_invalid"
            )

        delivery.status = 'out_for_delivery'
        delivery.dispatched_at = now
        delivery.pickup_otp_hash = None
        delivery.pickup_otp = ''

        # Issue doorstep OTP
        raw_doorstep_otp = issue_otp(delivery, 'doorstep')
        delivery.save()

        # Send doorstep OTP email to customer via transaction.on_commit
        deliv_id = delivery.id
        def _send_doorstep_email():
            try:
                deliv = PetDelivery.objects.filter(id=deliv_id).first()
                if deliv:
                    sent = dispatch_doorstep_otp(deliv, raw_otp=raw_doorstep_otp)
                    if sent:
                        PetDelivery.objects.filter(id=deliv_id).update(doorstep_otp_sent_at=timezone.now())
                    else:
                        logger.error(f"Failed to dispatch doorstep OTP for delivery #{deliv_id}: dispatch returned False.")
            except Exception as e:
                logger.error(f"Failed to email doorstep OTP for delivery #{deliv_id}: {e}", exc_info=True)

        transaction.on_commit(_send_doorstep_email)

        return OTPVerificationResult(
            outcome=OTPVerificationOutcome.SUCCESS,
            delivery=delivery
        )


def verify_pickup_otp(delivery: PetDelivery, submitted_otp: str) -> bool:
    """Thin wrapper around verify_pickup_service for backwards compatibility."""
    res = verify_pickup_service(delivery.id, submitted_otp)
    if res.outcome == OTPVerificationOutcome.SUCCESS:
        return True
    raise ValidationError(res.detail or "Invalid pickup OTP.")


def resolve_delivery_payment_status(delivery: PetDelivery) -> bool:
    """
    Evaluates and updates application.payment_status upon doorstep delivery verification.
    Consolidates payment verification into a single authoritative source of truth.
    Returns True if payment was verified and marked 'paid', False otherwise.

    Unifies online captured payments and COD ledger rows through is_payment_satisfied with no dead branch.
    """
    application = getattr(delivery, 'application', None)
    if not application:
        return False

    satisfied, _, _ = is_payment_satisfied(application, delivery)
    if satisfied:
        application.payment_status = 'paid'
        return True

    return False


def verify_doorstep_service(delivery_id: int | str, submitted_otp: str) -> OTPVerificationResult:
    """
    Authoritative service to verify doorstep OTP under row lock.
    Transitions delivery from 'out_for_delivery' to 'delivered' upon successful OTP validation.
    Marks Pet 'adopted', Application 'completed', frees Driver, resolves payment, and
    queues completion notice email via transaction.on_commit.
    Never raises exceptions for invalid OTP, lockout, expiry, or wrong state; returns OTPVerificationResult.
    """
    with transaction.atomic():
        try:
            delivery = PetDelivery.objects.select_for_update().get(id=delivery_id)
        except PetDelivery.DoesNotExist:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.WRONG_STATE,
                detail=f"Delivery #{delivery_id} not found."
            )

        if delivery.status == 'delivered':
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.ALREADY_COMPLETED,
                delivery=delivery,
                detail="Delivery already completed."
            )

        if delivery.status != 'out_for_delivery':
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.WRONG_STATE,
                delivery=delivery,
                detail=f"Cannot verify doorstep OTP for delivery in '{delivery.status}' state.",
                code="invalid_state"
            )

        max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 3)

        # Check doorstep payment guard before OTP verification (consumes NO attempts)
        allowed, code, detail = check_doorstep_payment_guard(delivery)
        if not allowed:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.CASH_NOT_COLLECTED,
                attempts_remaining=max(0, max_attempts - delivery.doorstep_otp_attempts),
                delivery=delivery,
                detail=detail,
                code=code
            )

        if delivery.doorstep_otp_attempts >= max_attempts:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.LOCKED,
                attempts_remaining=0,
                delivery=delivery,
                detail="Maximum OTP verification attempts exceeded. Please contact the shelter to reissue an OTP.",
                code="otp_locked"
            )

        now = timezone.now()
        if delivery.doorstep_otp_expires_at and now > delivery.doorstep_otp_expires_at:
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.EXPIRED,
                attempts_remaining=max(0, max_attempts - delivery.doorstep_otp_attempts),
                delivery=delivery,
                detail="OTP has expired. Please contact the shelter to reissue an OTP.",
                code="otp_expired"
            )

        # Hash-only verification (no plaintext fallback)
        is_valid = False
        if delivery.doorstep_otp_hash:
            is_valid = check_otp(delivery.doorstep_otp_hash, delivery.id, 'doorstep', submitted_otp)

        if not is_valid:
            delivery.doorstep_otp_attempts += 1
            delivery.save(update_fields=['doorstep_otp_attempts'])
            attempts_remaining = max(0, max_attempts - delivery.doorstep_otp_attempts)
            return OTPVerificationResult(
                outcome=OTPVerificationOutcome.INVALID,
                attempts_remaining=attempts_remaining,
                delivery=delivery,
                detail="Invalid doorstep OTP.",
                code="otp_invalid"
            )

        delivery.status = 'delivered'
        delivery.delivered_at = now
        delivery.doorstep_otp_hash = None
        delivery.doorstep_otp = ''
        delivery.secret_otp = ''
        delivery.save()

        if delivery.driver:
            delivery.driver.is_available = True
            delivery.driver.save(update_fields=['is_available'])

        application = delivery.application
        if application:
            if application.pet:
                application.pet.status = 'adopted'
                application.pet.save(update_fields=['status'])

            application.status = 'completed'
            resolve_delivery_payment_status(delivery)
            application.save()

        deliv_id = delivery.id
        def _send_completed_notice():
            try:
                deliv = PetDelivery.objects.filter(id=deliv_id).first()
                if deliv:
                    send_delivery_completed_notice(deliv)
            except Exception as e:
                logger.error(f"Error sending delivery completed notice for delivery #{deliv_id}: {e}", exc_info=True)

        transaction.on_commit(_send_completed_notice)

        return OTPVerificationResult(
            outcome=OTPVerificationOutcome.SUCCESS,
            delivery=delivery
        )


def verify_doorstep_otp(delivery: PetDelivery, submitted_otp: str) -> bool:
    """Thin wrapper around verify_doorstep_service for backwards compatibility."""
    res = verify_doorstep_service(delivery.id, submitted_otp)
    if res.outcome in (OTPVerificationOutcome.SUCCESS, OTPVerificationOutcome.ALREADY_COMPLETED):
        return True
    raise ValidationError(res.detail or "Invalid doorstep OTP.")



def dispatch_doorstep_otp(delivery: PetDelivery, raw_otp: str = None) -> bool:
    """
    Automated doorstep verification OTP and status dispatch service for pet deliveries.
    Sends an email with the confidential doorstep verification OTP to the adopter.
    If raw_otp is not provided, falls back to delivery.raw_doorstep_otp.
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

        secret_otp = raw_otp or getattr(delivery, 'raw_doorstep_otp', None)

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
        raise ValidationError({
            "code": "validation_error",
            "detail": "Purpose must be 'pickup' or 'doorstep'."
        })

    from rest_framework.exceptions import APIException
    class Conflict(APIException):
        status_code = 409
        default_detail = "Action not valid for current delivery state."
        default_code = "invalid_state"

    if purpose == 'pickup':
        if delivery.status != 'assigned':
            raise Conflict(detail={
                "code": "invalid_state",
                "detail": f"Cannot reissue pickup OTP for delivery in '{delivery.status}' status."
            })

        raw_pickup_otp = issue_otp(delivery, 'pickup')
        return {
            "detail": "Pickup OTP reissued successfully.",
            "purpose": "pickup",
            "pickup_otp": raw_pickup_otp,
            "expires_at": delivery.expires_at.isoformat() if delivery.expires_at else None
        }

    elif purpose == 'doorstep':
        if delivery.status != 'out_for_delivery':
            raise Conflict(detail={
                "code": "invalid_state",
                "detail": f"Cannot reissue doorstep OTP for delivery in '{delivery.status}' status."
            })

        raw_doorstep_otp = issue_otp(delivery, 'doorstep')
        email_sent = False
        try:
            email_sent = dispatch_doorstep_otp(delivery, raw_otp=raw_doorstep_otp)
            if email_sent:
                delivery.doorstep_otp_sent_at = timezone.now()
                delivery.save(update_fields=['doorstep_otp_sent_at'])
        except Exception as e:
            logger.error(f"Failed to email reissued doorstep OTP for delivery #{delivery.id}: {e}", exc_info=True)

        msg = "Doorstep OTP reissued and emailed to adopter." if email_sent else "Doorstep OTP reissued (email delivery failed)."
        return {
            "detail": msg,
            "message": msg,
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


def cancel_delivery_by_shelter(delivery: PetDelivery, user) -> dict:
    """
    Cancels an active delivery by verified shelter or admin.
    - Permitted only from 'assigned' or 'out_for_delivery'
    - Blocked if cash_collected > 0 (HTTP 409 {"code": "cash_collected"})
    - Idempotent if already 'failed' (HTTP 200)
    - Sets delivery status = 'failed'
    - Frees assigned driver (is_available = True)
    - Leaves pet status 'pending' and application status 'approved'
    - Dispatches notification to adopter on_commit
    """
    if delivery.status == 'failed':
        return {
            "detail": "Delivery is already marked as failed.",
            "delivery_id": str(delivery.public_id),
            "status": "failed",
            "idempotent": True
        }

    if delivery.status not in ['assigned', 'out_for_delivery']:
        raise ValidationError({
            "code": "validation_error",
            "detail": f"Cannot cancel delivery in '{delivery.status}' status. Only assigned or out_for_delivery deliveries can be cancelled."
        })

    if delivery.cash_collected and delivery.cash_collected > Decimal('0.00'):
        raise ConflictError({
            "code": "cash_collected",
            "detail": "Cannot cancel delivery after cash has been collected. Please reconcile with driver."
        })

    if delivery.application_id:
        has_captured_payment = Payment.objects.filter(
            application_id=delivery.application_id,
            status__in=[PaymentStatus.CAPTURED, PaymentStatus.PAID, 'captured', 'paid']
        ).exclude(payment_method='cod').exists()
        if has_captured_payment:
            raise ConflictError({
                "code": "refund_required",
                "detail": "Cannot cancel delivery: online payment has already been captured. A refund must be processed before cancellation.",
                "message": "Cannot cancel delivery: online payment has already been captured. A refund must be processed before cancellation."
            })

    with transaction.atomic():
        locked_delivery = PetDelivery.objects.select_for_update().get(id=delivery.id)
        if locked_delivery.status == 'failed':
            return {
                "detail": "Delivery is already marked as failed.",
                "delivery_id": str(locked_delivery.public_id),
                "status": "failed",
                "idempotent": True
            }

        if locked_delivery.status not in ['assigned', 'out_for_delivery']:
            raise ValidationError({
                "code": "validation_error",
                "detail": f"Cannot cancel delivery in '{locked_delivery.status}' status. Only assigned or out_for_delivery deliveries can be cancelled."
            })

        if locked_delivery.cash_collected and locked_delivery.cash_collected > Decimal('0.00'):
            raise ConflictError({
                "code": "cash_collected",
                "detail": "Cannot cancel delivery after cash has been collected. Please reconcile with driver."
            })

        if locked_delivery.application_id:
            has_captured_payment = Payment.objects.filter(
                application_id=locked_delivery.application_id,
                status__in=[PaymentStatus.CAPTURED, PaymentStatus.PAID, 'captured', 'paid']
            ).exclude(payment_method='cod').exists()
            if has_captured_payment:
                raise ConflictError({
                    "code": "refund_required",
                    "detail": "Cannot cancel delivery: online payment has already been captured. A refund must be processed before cancellation.",
                    "message": "Cannot cancel delivery: online payment has already been captured. A refund must be processed before cancellation."
                })

        locked_delivery.status = 'failed'
        locked_delivery.save(update_fields=['status'])

        # Free the driver
        if locked_delivery.driver:
            driver = locked_delivery.driver
            driver.is_available = True
            driver.save(update_fields=['is_available'])

        # Pet status remains 'pending'
        if locked_delivery.application and locked_delivery.application.pet:
            pet = locked_delivery.application.pet
            pet.status = 'pending'
            pet.save(update_fields=['status'])

        # Application status remains 'approved'
        if locked_delivery.application:
            app = locked_delivery.application
            app.status = 'approved'
            app.save(update_fields=['status'])

        # Adopter notification on_commit
        adopter = getattr(locked_delivery.application, 'customer', None) or getattr(locked_delivery.application, 'applicant', None)
        pet_name = getattr(locked_delivery.application.pet, 'name', 'your pet') if locked_delivery.application and locked_delivery.application.pet else 'your pet'

        def notify_adopter():
            if adopter:
                from apps.accounts.models import Notification
                try:
                    Notification.objects.create(
                        recipient=adopter,
                        title=f"Delivery Cancelled: Update on your adoption of {pet_name}",
                        message=f"The delivery for your adoption of {pet_name} has been cancelled by the shelter. Your adoption application remains approved.",
                        notification_type='delivery'
                    )
                except Exception as e:
                    logger.error(f"Failed to create cancellation notification: {e}")

                if adopter.email:
                    try:
                        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@kindheart.org')
                        send_mail(
                            subject=f"Delivery Cancelled: Update on your adoption of {pet_name}",
                            message=f"Hello {adopter.username},\n\nThe scheduled delivery for {pet_name} has been cancelled by the shelter. Your adoption application remains approved.\n\nThank you,\nKindHeart Team",
                            from_email=from_email,
                            recipient_list=[adopter.email],
                            fail_silently=True
                        )
                    except Exception as e:
                        logger.error(f"Failed to send cancellation email: {e}")

        transaction.on_commit(notify_adopter)

        return {
            "detail": "Delivery cancelled successfully.",
            "delivery_id": str(locked_delivery.public_id),
            "status": "failed",
            "idempotent": False
        }



