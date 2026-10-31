import hashlib
import hmac
from datetime import timedelta
from django.conf import settings
from django.db import migrations
from django.utils import timezone


def hash_otp_func(secret_key: str, delivery_id: int | str, purpose: str, otp: str) -> str:
    message = f"{delivery_id}:{purpose}:{otp}".encode('utf-8')
    key = secret_key.encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def forward_hash_inflight_otps(apps, schema_editor):
    PetDelivery = apps.get_model('logistics', 'PetDelivery')
    now = timezone.now()
    ttl_minutes = getattr(settings, 'OTP_TTL_MINUTES', 1440)
    secret_key = settings.SECRET_KEY

    # Target in-flight deliveries not in terminal states (delivered, failed)
    inflight_deliveries = PetDelivery.objects.exclude(status__in=['delivered', 'failed'])

    for delivery in inflight_deliveries:
        updated = False

        # Migrate pickup OTP
        if delivery.pickup_otp:
            delivery.pickup_otp_hash = hash_otp_func(secret_key, delivery.id, 'pickup', delivery.pickup_otp)
            delivery.pickup_otp = ''
            if not delivery.expires_at:
                delivery.expires_at = now + timedelta(minutes=ttl_minutes)
            updated = True

        # Migrate doorstep OTP (including secret_otp fallback)
        plain_doorstep = delivery.doorstep_otp or delivery.secret_otp
        if plain_doorstep:
            delivery.doorstep_otp_hash = hash_otp_func(secret_key, delivery.id, 'doorstep', plain_doorstep)
            delivery.doorstep_otp = ''
            delivery.secret_otp = ''
            if not delivery.doorstep_otp_expires_at:
                delivery.doorstep_otp_expires_at = now + timedelta(minutes=ttl_minutes)
            updated = True

        if updated:
            delivery.save()


def reverse_hash_inflight_otps(apps, schema_editor):
    """
    Documented no-op: Plaintext OTPs cannot and should not be reconstructed from HMAC-SHA256
    one-way cryptographic hashes. Reversing this migration preserves existing hashes safely.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('logistics', '0004_petdelivery_doorstep_otp_hash_and_more'),
    ]

    operations = [
        migrations.RunPython(forward_hash_inflight_otps, reverse_hash_inflight_otps),
    ]
