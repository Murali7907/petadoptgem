import hashlib
import hmac
import json
import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import razorpay
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import Payment, PetDelivery


def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_razorpay_order(delivery: PetDelivery, amount_in_paise: int) -> Payment:
    """
    Creates a Razorpay order via Razorpay SDK and logs a Payment record.
    """
    if not amount_in_paise or int(amount_in_paise) <= 0:
        raise ValidationError("Amount must be greater than zero.")

    amount_in_paise = int(amount_in_paise)
    client = get_razorpay_client()

    order_payload = {
        'amount': amount_in_paise,
        'currency': 'INR',
        'receipt': f"rcpt_del_{str(delivery.public_id)[:8]}",
        'notes': {
            'delivery_public_id': str(delivery.public_id),
            'application_public_id': str(delivery.application.public_id) if delivery.application else '',
        }
    }

    try:
        razorpay_order = client.order.create(data=order_payload)
    except Exception as e:
        raise ValidationError(f"Razorpay order creation failed: {str(e)}")

    order_id = razorpay_order.get('id', '')

    with transaction.atomic():
        payment = Payment.objects.create(
            delivery=delivery,
            application=delivery.application if delivery else None,
            amount_in_paise=amount_in_paise,
            currency='INR',
            payment_method='razorpay',
            status='created',
            razorpay_order_id=order_id,
            raw_payload=razorpay_order
        )

        if delivery.application:
            delivery.application.razorpay_order_id = order_id
            delivery.application.save()

    return payment


def verify_razorpay_payment(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> Payment:
    """
    Verifies Razorpay payment signature using HMAC-SHA256 with RAZORPAY_KEY_SECRET.
    Updates Payment and PetDelivery payment_status.
    """
    if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
        raise ValidationError("Missing order_id, payment_id, or signature for verification.")

    msg = f"{razorpay_order_id}|{razorpay_payment_id}".encode('utf-8')
    secret = settings.RAZORPAY_KEY_SECRET.encode('utf-8')
    computed_signature = hmac.new(secret, msg, hashlib.sha256).hexdigest()

    is_valid = hmac.compare_digest(computed_signature, razorpay_signature)

    try:
        payment = Payment.objects.filter(razorpay_order_id=razorpay_order_id).latest('created_at')
    except Payment.DoesNotExist:
        payment = None

    if is_valid:
        if payment:
            with transaction.atomic():
                payment.status = 'captured'
                payment.razorpay_payment_id = razorpay_payment_id
                payment.razorpay_signature = razorpay_signature
                payment.transaction_id = razorpay_payment_id
                payment.save()

                if payment.delivery:
                    payment.delivery.payment_status = 'paid'
                    payment.delivery.save()

                if payment.application:
                    payment.application.payment_status = 'paid'
                    payment.application.razorpay_payment_id = razorpay_payment_id
                    payment.application.save()
        return payment
    else:
        if payment:
            payment.status = 'failed'
            payment.razorpay_payment_id = razorpay_payment_id
            payment.razorpay_signature = razorpay_signature
            payment.save()
        raise ValidationError("Invalid payment signature.")


def process_razorpay_webhook(event_payload: dict, signature: str, raw_body: bytes = None) -> None:
    """
    Verifies webhook signature against RAZORPAY_WEBHOOK_SECRET and records raw JSON payload into Payment ledger.
    """
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    client = get_razorpay_client()

    # Verify signature
    if raw_body is not None:
        try:
            client.utility.verify_webhook_signature(raw_body.decode('utf-8'), signature, secret)
        except Exception as e:
            raise ValidationError(f"Webhook signature verification failed: {str(e)}")
    else:
        # Fallback digest verification with stringified JSON payload
        msg = json.dumps(event_payload, separators=(',', ':')).encode('utf-8')
        computed = hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, signature):
            # Attempt uncompressed json dump
            msg_uncompressed = json.dumps(event_payload).encode('utf-8')
            computed_uncompressed = hmac.new(secret.encode('utf-8'), msg_uncompressed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(computed_uncompressed, signature):
                raise ValidationError("Webhook signature verification failed.")

    event_type = event_payload.get('event', '')
    payload_entity = event_payload.get('payload', {}).get('payment', {}).get('entity', {})
    order_id = payload_entity.get('order_id') or event_payload.get('payload', {}).get('order', {}).get('entity', {}).get('id')
    payment_id = payload_entity.get('id', '')

    if order_id:
        payment = Payment.objects.filter(razorpay_order_id=order_id).first()
        if payment:
            with transaction.atomic():
                payment.raw_payload = event_payload
                if event_type == 'payment.captured':
                    payment.status = 'captured'
                    payment.razorpay_payment_id = payment_id
                    payment.transaction_id = payment_id
                    if payment.delivery:
                        payment.delivery.payment_status = 'paid'
                        payment.delivery.save()
                    if payment.application:
                        payment.application.payment_status = 'paid'
                        payment.application.save()
                elif event_type == 'payment.failed':
                    payment.status = 'failed'
                    payment.razorpay_payment_id = payment_id
                    if payment.delivery:
                        payment.delivery.payment_status = 'failed'
                        payment.delivery.save()
                payment.save()
