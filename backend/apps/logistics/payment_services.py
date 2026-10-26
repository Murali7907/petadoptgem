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
from django.db.models import Q
from rest_framework.exceptions import APIException, NotFound, ValidationError

from apps.adoptions.models import AdoptionApplication
from .models import Payment, PaymentStatus, PetDelivery, Refund, RefundStatus, RefundSource

logger = logging.getLogger(__name__)


class ConflictError(APIException):
    status_code = 409
    default_detail = "Conflict with current resource state."


class RefundFailedError(APIException):
    status_code = 502
    default_code = "refund_failed"
    default_detail = "Refund gateway call failed."


def get_razorpay_client():
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
    if not key_id or not key_secret:
        raise ValidationError("Razorpay API credentials are not configured on the server.")
    return razorpay.Client(auth=(key_id, key_secret))


def _execute_late_capture_refund(payment_id: int, reason: str, source: str):
    try:
        pay = Payment.objects.get(id=payment_id)
        issue_refund(payment=pay, reason=reason, initiated_by=None, source=source)
    except Exception as exc:
        logger.error(f"Late capture refund dispatch failed for Payment #{payment_id}: {exc}", exc_info=True)


def mark_payment_captured(payment: Payment, razorpay_payment_id: str = '') -> bool:
    """
    Atomically transitions payment from a non-terminal state to 'captured'.
    Under row lock (select_for_update), sets:
      - payment.status = 'captured'
      - payment.razorpay_payment_id = razorpay_payment_id
    When application is no longer 'approved' (cancelled, rejected, or completed by another path)
    or delivery is 'failed'/'cancelled':
      - Records capture and schedules issue_refund(source='late_capture') after commit.
    Else:
      - application.payment_status = 'paid'
      - delivery.payment_status = 'paid'
    Idempotent: if payment is already 'captured', 'paid', or 'refunded', returns True without duplicate writes.
    If payment is in terminal 'failed' state, does not overwrite and returns False.
    Concurrent verify + webhook creates exactly one refund.
    """
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(id=payment.id)

        if locked_payment.status in (PaymentStatus.CAPTURED, PaymentStatus.PAID, PaymentStatus.REFUNDED):
            return True

        if locked_payment.status == PaymentStatus.FAILED:
            logger.warning(f"Cannot capture payment #{locked_payment.id} in terminal failed status.")
            return False

        locked_payment.status = PaymentStatus.CAPTURED
        if razorpay_payment_id:
            locked_payment.razorpay_payment_id = razorpay_payment_id
            locked_payment.transaction_id = razorpay_payment_id
        locked_payment.save(update_fields=['status', 'razorpay_payment_id', 'transaction_id', 'updated_at'])

        app = None
        if locked_payment.application_id:
            app = AdoptionApplication.objects.select_for_update().get(id=locked_payment.application_id)
        elif locked_payment.delivery and locked_payment.delivery.application_id:
            app = AdoptionApplication.objects.select_for_update().get(id=locked_payment.delivery.application_id)

        deliv = None
        if locked_payment.delivery_id:
            deliv = PetDelivery.objects.select_for_update().get(id=locked_payment.delivery_id)
        elif app and hasattr(app, 'delivery') and app.delivery:
            deliv = PetDelivery.objects.select_for_update().get(id=app.delivery.id)

        # Check for late capture: application not approved or delivery failed/cancelled
        is_late_capture = False
        if app and app.status != 'approved':
            is_late_capture = True
        if deliv and deliv.status in ('failed', 'cancelled'):
            is_late_capture = True

        if is_late_capture:
            if app and razorpay_payment_id:
                app.razorpay_payment_id = razorpay_payment_id
                app.save(update_fields=['razorpay_payment_id', 'updated_at'])

            active_refund_exists = Refund.objects.filter(payment=locked_payment).exclude(status=RefundStatus.FAILED).exists()
            if not active_refund_exists:
                pay_id = locked_payment.id
                transaction.on_commit(
                    lambda: _execute_late_capture_refund(
                        pay_id,
                        reason="Late capture after cancellation/rejection",
                        source=RefundSource.LATE_CAPTURE
                    )
                )
        else:
            if app:
                app.payment_status = 'paid'
                if razorpay_payment_id:
                    app.razorpay_payment_id = razorpay_payment_id
                app.save(update_fields=['payment_status', 'razorpay_payment_id', 'updated_at'])

            if deliv:
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

    elif event_type in ('refund.processed', 'refund.failed'):
        refund_entity = event_payload.get('payload', {}).get('refund', {}).get('entity', {})
        rzp_refund_id = refund_entity.get('id', '')
        refund_amount = refund_entity.get('amount')
        rzp_payment_id = refund_entity.get('payment_id', '')

        if not rzp_refund_id:
            logger.warning(f"Webhook {event_type} missing refund entity id.")
            return {'status': 'ignored', 'detail': 'Missing refund id.'}

        with transaction.atomic():
            refund = Refund.objects.select_for_update().filter(razorpay_refund_id=rzp_refund_id).first()
            if not refund and rzp_payment_id:
                # Match by payment id if refund id is unknown
                payment = Payment.objects.filter(razorpay_payment_id=rzp_payment_id).first()
                if payment:
                    candidates = Refund.objects.select_for_update().filter(
                        payment=payment
                    ).filter(
                        Q(razorpay_refund_id__isnull=True) | Q(razorpay_refund_id='')
                    ).exclude(
                        status=RefundStatus.FAILED
                    )
                    if candidates.count() == 1:
                        refund = candidates.first()
                        refund.razorpay_refund_id = rzp_refund_id
                        refund.save(update_fields=['razorpay_refund_id', 'updated_at'])
                        logger.info(f"Webhook {event_type} matched unlinked Refund #{refund.id} via payment {rzp_payment_id} to {rzp_refund_id}")
                    else:
                        logger.warning(
                            f"Webhook {event_type} unknown refund {rzp_refund_id} matched payment {rzp_payment_id} with {candidates.count()} unlinked non-failed refund rows (expected 1)."
                        )
                        return {'status': 'ignored', 'detail': 'Multiple or zero candidate refunds found for payment.'}

            if not refund:
                logger.warning(f"Webhook {event_type} for unknown razorpay_refund_id: {rzp_refund_id}")
                return {'status': 'ignored', 'detail': 'Refund not found.'}

            # Compare payload amount with Refund row; never trust payload alone
            if refund_amount is not None and int(refund_amount) != refund.amount_in_paise:
                logger.warning(f"Webhook refund amount mismatch for {rzp_refund_id}: payload={refund_amount}, db={refund.amount_in_paise}")
                return {'status': 'ignored', 'detail': 'Amount mismatch.'}

            if event_type == 'refund.processed':
                # Idempotent check
                if refund.status == RefundStatus.PROCESSED:
                    return {'status': 'success', 'event': event_type, 'idempotent': True}

                refund.status = RefundStatus.PROCESSED
                refund.save(update_fields=['status', 'updated_at'])
                mark_refund_processed_state(refund)

            elif event_type == 'refund.failed':
                # If already processed, do not mark as failed
                if refund.status == RefundStatus.PROCESSED:
                    return {'status': 'ignored', 'detail': 'Refund already processed.'}

                error_desc = refund_entity.get('error_description') or refund_entity.get('error_reason') or 'Webhook received refund.failed'
                refund.status = RefundStatus.FAILED
                refund.failure_reason = str(error_desc)
                refund.save(update_fields=['status', 'failure_reason', 'updated_at'])

        return {'status': 'success', 'event': event_type}


def mark_refund_processed_state(refund: Refund):
    """
    Transitions Payment, application.payment_status and delivery.payment_status to 'refunded'.
    Dispatches adopter email and in-app notification inside transaction.on_commit.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(id=refund.payment_id)
        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=['status', 'updated_at'])

        app = refund.application
        if app:
            app = AdoptionApplication.objects.select_for_update().get(id=app.id)
            app.payment_status = 'refunded'
            app.save(update_fields=['payment_status', 'updated_at'])

        deliv = payment.delivery or getattr(app, 'delivery', None)
        if deliv:
            deliv = PetDelivery.objects.select_for_update().get(id=deliv.id)
            deliv.payment_status = 'refunded'
            deliv.save(update_fields=['payment_status'])

    def _notify():
        try:
            adopter = getattr(refund.application, 'applicant', None)
            pet = getattr(refund.application, 'pet', None)
            pet_name = pet.name if pet else "your pet"
            amount_in_rupees = Decimal(refund.amount_in_paise) / Decimal(100)

            if adopter:
                from apps.accounts.models import Notification
                Notification.objects.create(
                    recipient=adopter,
                    title=f"Refund Processed for {pet_name}",
                    message=f"A full refund of ₹{amount_in_rupees:.2f} has been processed for your adoption application for {pet_name}.",
                    notification_type='payment'
                )
                if adopter.email:
                    from django.core.mail import send_mail
                    send_mail(
                        subject=f"Refund Processed: Adoption Application for {pet_name}",
                        message=(
                            f"Hello {adopter.get_full_name() or adopter.username},\n\n"
                            f"A full refund of ₹{amount_in_rupees:.2f} has been processed for your adoption application for {pet_name}.\n"
                            f"Reason: {refund.reason}\n"
                            f"Refund ID: {refund.razorpay_refund_id or refund.public_id}\n\n"
                            f"The amount should reflect in your account within 5-7 business days.\n\n"
                            f"Kind regards,\nPet Adoption Team"
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@kindheart.org'),
                        recipient_list=[adopter.email],
                        fail_silently=True,
                    )
        except Exception as exc:
            logger.error(f"Failed to send refund notification for Refund #{refund.id}: {exc}", exc_info=True)

    transaction.on_commit(_notify)


def issue_refund(payment: Payment, reason: str = "", initiated_by=None, source: str = 'manual', refund_obj: Refund = None) -> Refund:
    """
    Executes a full refund for a captured payment:
    1. Under select_for_update on the Payment row, finds or creates the Refund row in 'created'.
       If a processed refund already exists, returns it idempotently.
    2. Releases row locks before calling Razorpay API.
    3. Pre-reconciliation: queries Razorpay for existing refunds for this payment using
       the installed SDK method `client.payment.fetch_multiple_refund(payment_id)`.
       If a full refund already exists, adopts its id and status and does not call refund again.
    4. Calls client.payment.refund for the full captured amount with our refund id in notes/receipt
       and X-Razorpay-Idempotency-Key.
    5. Success: stores razorpay_refund_id and sets processing/processed per response.
       On 'processed': marks Payment, application.payment_status and delivery.payment_status as 'refunded'.
    6. API error: marks Refund as 'failed' with reason, raises RefundFailedError; retry allowed.
    """
    # Phase 1: DB row lock and creation or adoption of unlinked Refund row
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(id=payment.id)

        # Idempotency check: if a processed refund already exists, return it
        processed_refund = Refund.objects.filter(payment=locked_payment, status=RefundStatus.PROCESSED).first()
        if processed_refund:
            return processed_refund

        if locked_payment.status not in (PaymentStatus.CAPTURED, PaymentStatus.PAID, PaymentStatus.REFUNDED):
            raise ValidationError(f"Cannot refund payment in '{locked_payment.status}' status. Only captured payments can be refunded.")

        amount_in_paise = locked_payment.amount_in_paise or int(round(Decimal(str(locked_payment.amount)) * 100))
        if amount_in_paise <= 0:
            raise ValidationError("Payment amount must be greater than zero to refund.")

        app = locked_payment.application
        if not app and locked_payment.delivery and locked_payment.delivery.application:
            app = locked_payment.delivery.application
            locked_payment.application = app
            locked_payment.save(update_fields=['application'])

        refund = refund_obj
        if refund:
            refund = Refund.objects.select_for_update().get(id=refund.id)
            if refund.status == RefundStatus.FAILED:
                refund.status = RefundStatus.CREATED
                refund.failure_reason = ''
                refund.save(update_fields=['status', 'failure_reason', 'updated_at'])
        else:
            # Check if there is an existing Refund row without razorpay_refund_id in CREATED or FAILED
            existing_unlinked = Refund.objects.select_for_update().filter(
                payment=locked_payment
            ).filter(
                Q(razorpay_refund_id__isnull=True) | Q(razorpay_refund_id='')
            ).filter(
                status__in=[RefundStatus.CREATED, RefundStatus.FAILED]
            ).order_by('-created_at').first()

            if existing_unlinked:
                refund = existing_unlinked
                if refund.status == RefundStatus.FAILED:
                    refund.status = RefundStatus.CREATED
                    refund.failure_reason = ''
                    refund.save(update_fields=['status', 'failure_reason', 'updated_at'])
            else:
                # Check if refund already in processing
                existing_processing = Refund.objects.filter(payment=locked_payment, status=RefundStatus.PROCESSING).first()
                if existing_processing:
                    return existing_processing

                refund = Refund.objects.create(
                    payment=locked_payment,
                    application=app,
                    amount_in_paise=amount_in_paise,
                    status=RefundStatus.CREATED,
                    reason=reason,
                    initiated_by=initiated_by,
                    source=source,
                )

    # Phase 2: Call gateway with locks released
    client = get_razorpay_client()

    # Pre-reconciliation with Razorpay before creating duplicate refund
    if not refund.razorpay_refund_id:
        try:
            rzp_list = None
            if hasattr(client.payment, 'fetch_multiple_refund'):
                rzp_list = client.payment.fetch_multiple_refund(locked_payment.razorpay_payment_id)
            elif hasattr(client, 'refund') and hasattr(client.refund, 'all'):
                rzp_list = client.refund.all({'payment_id': locked_payment.razorpay_payment_id})

            if rzp_list:
                items = rzp_list.get('items', []) if isinstance(rzp_list, dict) else (rzp_list if isinstance(rzp_list, list) else [])
                for item in items:
                    item_amt = item.get('amount')
                    item_id = item.get('id')
                    if item_id and item_amt is not None and int(item_amt) == refund.amount_in_paise:
                        rzp_status = item.get('status', 'processed')
                        with transaction.atomic():
                            refund.razorpay_refund_id = item_id
                            if rzp_status == 'processed':
                                refund.status = RefundStatus.PROCESSED
                            elif rzp_status == 'failed':
                                refund.status = RefundStatus.FAILED
                            else:
                                refund.status = RefundStatus.PROCESSING
                            refund.save(update_fields=['razorpay_refund_id', 'status', 'updated_at'])

                            if refund.status == RefundStatus.PROCESSED:
                                mark_refund_processed_state(refund)

                        return refund
        except Exception as query_exc:
            logger.warning(f"Failed to query Razorpay refunds before issuing for Payment #{locked_payment.id}: {query_exc}")

    # No existing full refund found at gateway: call Razorpay refund endpoint
    refund_data = {
        'amount': refund.amount_in_paise,
        'notes': {
            'refund_id': str(refund.public_id),
            'receipt': str(refund.public_id),
            'reason': str(reason)[:500],
        }
    }
    refund_options = {
        'headers': {
            'X-Razorpay-Idempotency-Key': str(refund.public_id)
        }
    }

    try:
        rzp_response = client.payment.refund(
            locked_payment.razorpay_payment_id,
            data=refund_data,
            **refund_options
        )
    except Exception as exc:
        err_msg = str(exc)
        logger.error(f"Razorpay refund API call failed for Payment #{locked_payment.id}: {err_msg}", exc_info=True)
        refund.status = RefundStatus.FAILED
        refund.failure_reason = err_msg
        refund.save(update_fields=['status', 'failure_reason', 'updated_at'])
        raise RefundFailedError(detail={"code": "refund_failed", "detail": f"Refund gateway call failed: {err_msg}"})

    rzp_refund_id = rzp_response.get('id', '')
    rzp_status = rzp_response.get('status', 'processed')

    with transaction.atomic():
        refund.razorpay_refund_id = rzp_refund_id
        if rzp_status == 'processed':
            refund.status = RefundStatus.PROCESSED
        else:
            refund.status = RefundStatus.PROCESSING
        refund.save(update_fields=['razorpay_refund_id', 'status', 'updated_at'])

        if refund.status == RefundStatus.PROCESSED:
            mark_refund_processed_state(refund)

    return refund
