import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING
from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from .models import PetDelivery


def generate_otp() -> str:
    """
    Generates a cryptographically secure 6-digit decimal OTP string using the secrets module.
    """
    return f"{secrets.randbelow(900000) + 100000:06d}"


def hash_otp(delivery_id: int | str, purpose: str, otp: str) -> str:
    """
    Computes an HMAC-SHA256 digest keyed by settings.SECRET_KEY.
    The message incorporates delivery_id, purpose ('pickup' or 'doorstep'), and the OTP code.
    Returns a 64-character lowercase hex digest.
    """
    normalized_otp = str(otp).strip()
    message = f"{delivery_id}:{purpose}:{normalized_otp}".encode('utf-8')
    key = settings.SECRET_KEY.encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def check_otp(stored_hash: str | None, delivery_id: int | str, purpose: str, submitted: str) -> bool:
    """
    Constant-time comparison between stored HMAC-SHA256 hash and submitted OTP.
    Returns False immediately if stored_hash or submitted is empty.
    """
    if not stored_hash or not submitted:
        return False
    candidate_hash = hash_otp(delivery_id, purpose, submitted)
    return hmac.compare_digest(stored_hash, candidate_hash)


def issue_otp(delivery: "PetDelivery", purpose: str) -> str:
    """
    Issues a new 6-digit OTP for the specified purpose ('pickup' or 'doorstep').
    Persists ONLY the cryptographic hash, resets attempt counter to 0, sets expiry timestamp,
    and returns the one-time plaintext code to the caller.
    Never persists or logs the plaintext OTP.
    """
    if purpose not in ('pickup', 'doorstep'):
        raise ValueError(f"Invalid OTP purpose '{purpose}'. Must be 'pickup' or 'doorstep'.")

    code = generate_otp()
    code_hash = hash_otp(delivery.id, purpose, code)
    now = timezone.now()
    ttl_minutes = getattr(settings, 'OTP_TTL_MINUTES', 1440)
    expiry = now + timedelta(minutes=ttl_minutes)

    if purpose == 'pickup':
        delivery.pickup_otp_hash = code_hash
        delivery.pickup_otp = ''
        delivery.pickup_otp_attempts = 0
        delivery.expires_at = expiry
        delivery.save(update_fields=[
            'pickup_otp_hash', 'pickup_otp', 'pickup_otp_attempts', 'expires_at'
        ])
    else:  # purpose == 'doorstep'
        delivery.doorstep_otp_hash = code_hash
        delivery.doorstep_otp = ''
        delivery.secret_otp = ''
        delivery.doorstep_otp_attempts = 0
        delivery.doorstep_otp_expires_at = expiry
        delivery.save(update_fields=[
            'doorstep_otp_hash', 'doorstep_otp', 'secret_otp',
            'doorstep_otp_attempts', 'doorstep_otp_expires_at'
        ])

    return code
