import hashlib
import hmac
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import razorpay
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import APIException, NotFound, ValidationError

from apps.adoptions.models import AdoptionApplication
from .models import Payment, PaymentStatus, PetDelivery

logger = logging.getLogger(__name__)


class ConflictError(APIException):
    status_code = 409
    default_detail = "Conflict with current resource state."


def get_razorpay_client():
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    if not key_id or not key_secret:
        raise ValidationError("Razorpay API credentials are not configured on the server.")
    return razorpay.Client(auth=(key_id, key_secret))


def mark_payment_captured(payment: Payment, razorpay_payment_id: str = '') -> bool:
    """
    Atomically transitions payment from a non-terminal state to 'captured'.
    Under row lock (select_for_update), sets:
      - payment.status = 'captured'
      - payment.razorpay_payment_id = razorpay_payment_id
      - application.payment_status = 'paid'
      - delivery.payment_status = 'paid'
    Idempotent: if payment is already 'captured' or 'paid', returns True without duplicate writes.
    If payment is in terminal 'failed' state, does not overwrite and returns False.
    """
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(id=payment.id)

        if locked_payment.status in (PaymentStatus.CAPTURED, PaymentStatus.PAID):
            return True

        if locked_payment.status == PaymentStatus.FAILED:
            logger.warning(f"Cannot capture payment #{locked_payment.id} in terminal failed status.")
            return False

        locked_payment.status = PaymentStatus.CAPTURED
        if razorpay_payment_id:
            locked_payment.razorpay_payment_id = razorpay_payment_id
            locked_payment.transaction_id = razorpay_payment_id
        locked_payment.save(update_fields=['status', 'razorpay_payment_id', 'transaction_id', 'updated_at'])

        if locked_payment.application_id:
            app = AdoptionApplication.objects.select_for_update().get(id=locked_payment.application_id)
            app.payment_status = 'paid'
            if razorpay_payment_id:
                app.razorpay_payment_id = razorpay_payment_id
            app.save(update_fields=['payment_status', 'razorpay_payment_id', 'updated_at'])

        if locked_payment.delivery_id:
            deliv = PetDelivery.objects.select_for_update().get(id=locked_payment.delivery_id)
            deliv.payment_status = 'paid'
            deliv.save(update_fields=['payment_status'])

        return True


def create_razorpay_order(delivery: PetDelivery, user) -> Payment:
    """
    Creates an official Razorpay Order for an approved pet delivery task.
    Enforces:
    - Ownership: user must be delivery.application.applicant.
    - Amount derived strictly server-side from application.total_amount with Decimal.
    - Precondition checks (total_amount > 0, approved status, non-cod, unpaid, valid delivery state).
    - Idempotent: reuses existing unpaid order with the same amount if already created.
    - Locks rows under select_for_update strictly for database operations (network call happens outside locks).
    """
    application = getattr(delivery, 'application', None)
    if not application:
        raise NotFound("Delivery has no linked application.")

    # Ownership check: logged-in user must be application.applicant
    if application.applicant_id != user.id:
        raise NotFound("Delivery not found.")

    # Precondition checks prior to network call
    total_amount = application.total_amount or Decimal('0.00')
    amount_in_paise = int(round(Decimal(str(total_amount)) * 100))

    if amount_in_paise <= 0:
        raise ValidationError("Total amount must be greater than zero to initiate online payment.")

    if application.status != 'approved':
        raise ConflictError(f"Cannot initiate payment for application with status '{application.get_status_display()}'. Must be 'approved'.")

    if application.payment_method == 'cod':
        raise ConflictError("Cannot initiate online payment for Cash on Delivery (COD) application.")

    if application.payment_status == 'paid':
        raise ConflictError("Payment has already been completed for this application.")

    if delivery.status not in ('assigned', 'out_for_delivery'):
        raise ConflictError(f"Cannot initiate payment for delivery in '{delivery.status}' state.")

    # Check for existing unpaid order with the same amount (fast path before network call)
    existing_payment = Payment.objects.filter(
        delivery=delivery,
        status__in=[PaymentStatus.CREATED, PaymentStatus.PENDING],
        amount_in_paise=amount_in_paise
    ).order_by('-created_at').first()

    if existing_payment and existing_payment.razorpay_order_id:
        return existing_payment

    # 1. External network call to Razorpay (performed OUTSIDE database locks)
    client = get_razorpay_client()
    order_payload = {
        'amount': amount_in_paise,
        'currency': 'INR',
        'receipt': f"rcpt_del_{str(delivery.public_id)[:8]}",
        'notes': {
            'delivery_public_id': str(delivery.public_id),
            'application_public_id': str(application.public_id),
        }
    }

    try:
        razorpay_order = client.order.create(data=order_payload)
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {e}", exc_info=True)
        raise ValidationError(f"Razorpay order creation failed: {str(e)}")

    order_id = razorpay_order.get('id', '')

    # 2. Database lock and record insertion
    with transaction.atomic():
        locked_application = AdoptionApplication.objects.select_for_update().get(id=application.id)
        locked_delivery = PetDelivery.objects.select_for_update().get(id=delivery.id)

        # Re-check under lock for concurrent creation
        concurrent_payment = Payment.objects.filter(
            delivery=locked_delivery,
            status__in=[PaymentStatus.CREATED, PaymentStatus.PENDING],
            amount_in_paise=amount_in_paise
        ).order_by('-created_at').first()

        if concurrent_payment and concurrent_payment.razorpay_order_id:
            return concurrent_payment

        # Set application.payment_method='online' only if it is 'unset' or 'online'
        if locked_application.payment_method in ('unset', 'online'):
            locked_application.payment_method = 'online'
            locked_application.save(update_fields=['payment_method', 'updated_at'])

        payment = Payment.objects.create(
            delivery=locked_delivery,
            application=locked_application,
            amount=Decimal(amount_in_paise) / Decimal(100),
            amount_in_paise=amount_in_paise,
            currency='INR',
            payment_method='razorpay',
            status=PaymentStatus.CREATED,
            razorpay_order_id=order_id,
            raw_payload=razorpay_order
        )

        locked_application.razorpay_order_id = order_id
        locked_application.save(update_fields=['razorpay_order_id', 'updated_at'])

        return payment


def verify_razorpay_payment(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str, user) -> Payment:
    """
    Verifies Razorpay payment signature under row lock.
    Enforces:
    - Unknown order -> 404
    - Non-owner -> 404
    - Already captured -> 200 idempotent
    - Confirms amount_in_paise == paise of application.total_amount
    - Verifies signature, marks captured
    """
    if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
        raise ValidationError("Missing order_id, payment_id, or signature for verification.")

    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(razorpay_order_id=razorpay_order_id).first()
        if not payment:
            raise NotFound("Payment not found.")

        if not payment.application or payment.application.applicant_id != user.id:
            raise NotFound("Payment not found.")

        # Already captured -> idempotent return
        if payment.status in (PaymentStatus.CAPTURED, PaymentStatus.PAID):
            return payment

        # Amount safety: confirm payment.amount_in_paise equals paise of application.total_amount
        expected_paise = int(round(Decimal(str(payment.application.total_amount)) * 100))
        if payment.amount_in_paise != expected_paise:
            raise ValidationError("Payment amount does not match application total amount.")

        # Verify signature
        client = get_razorpay_client()
        params_dict = {
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        }

        try:
            client.utility.verify_payment_signature(params_dict)
        except razorpay.errors.SignatureVerificationError:
            logger.warning("Razorpay signature verification failed for order %s", razorpay_order_id)
            raise ValidationError("Invalid payment signature.")

        payment.razorpay_signature = razorpay_signature
        payment.save(update_fields=['razorpay_signature', 'updated_at'])

        mark_payment_captured(payment, razorpay_payment_id)
        payment.refresh_from_db()
        return payment


def process_razorpay_webhook(raw_body: bytes, signature: str) -> dict:
    """
    Verifies webhook signature over raw request.body against RAZORPAY_WEBHOOK_SECRET.
    Processes payment.captured and payment.failed events.
    """
    secret = getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', None)
    if not secret:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not configured.")
        raise ValidationError("Webhook secret is not configured.")

    client = get_razorpay_client()
    try:
        body_str = raw_body.decode('utf-8') if isinstance(raw_body, bytes) else raw_body
        client.utility.verify_webhook_signature(body_str, signature, secret)
    except Exception as e:
        logger.warning(f"Webhook signature verification failed: {e}")
        raise ValidationError("Invalid webhook signature.")

    try:
        event_payload = json.loads(raw_body)
    except Exception as e:
        logger.warning(f"Invalid JSON in webhook payload: {e}")
        raise ValidationError("Invalid JSON payload.")

    event_type = event_payload.get('event', '')
    payload_entity = event_payload.get('payload', {}).get('payment', {}).get('entity', {})
    order_id = payload_entity.get('order_id') or event_payload.get('payload', {}).get('order', {}).get('entity', {}).get('id')
    payment_id = payload_entity.get('id', '')
    entity_amount = payload_entity.get('amount')
    entity_currency = payload_entity.get('currency')

    if event_type == 'payment.captured' and order_id:
        with transaction.atomic():
            payment = Payment.objects.select_for_update().filter(razorpay_order_id=order_id).first()
            if not payment:
                logger.warning(f"Webhook payment.captured for unknown order_id: {order_id}")
                return {'status': 'ignored', 'detail': 'Order not found.'}

            # Compare entity amount and currency with Payment row
            if entity_amount is not None and entity_amount != payment.amount_in_paise:
                logger.warning(f"Webhook amount mismatch for order {order_id}: payload={entity_amount}, db={payment.amount_in_paise}")
                return {'status': 'ignored', 'detail': 'Amount mismatch.'}

            if entity_currency and entity_currency != payment.currency:
                logger.warning(f"Webhook currency mismatch for order {order_id}: payload={entity_currency}, db={payment.currency}")
                return {'status': 'ignored', 'detail': 'Currency mismatch.'}

            payment.raw_payload = event_payload
            payment.save(update_fields=['raw_payload', 'updated_at'])

            mark_payment_captured(payment, payment_id)

    elif event_type == 'payment.failed' and order_id:
        with transaction.atomic():
            payment = Payment.objects.select_for_update().filter(razorpay_order_id=order_id).first()
            if payment:
                # payment.failed must not overwrite a captured payment
                if payment.status not in (PaymentStatus.CAPTURED, PaymentStatus.PAID):
                    payment.status = PaymentStatus.FAILED
                    payment.raw_payload = event_payload
                    if payment_id:
                        payment.razorpay_payment_id = payment_id
                    payment.save(update_fields=['status', 'raw_payload', 'razorpay_payment_id', 'updated_at'])

    return {'status': 'success', 'event': event_type}
