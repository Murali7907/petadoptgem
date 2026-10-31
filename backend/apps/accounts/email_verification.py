import logging
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)

EMAIL_VERIFICATION_SALT = 'auth-email-verification'


def generate_email_verification_token(user) -> str:
    """
    Generates a signed, timestamped token containing the user's primary key and current email address.
    """
    payload = {
        'user_id': user.id,
        'email': (user.email or '').strip().lower(),
    }
    return signing.dumps(payload, salt=EMAIL_VERIFICATION_SALT)


def verify_email_token(token: str) -> dict:
    """
    Validates the email verification token against EMAIL_VERIFY_MAX_AGE (default 86400 seconds).
    Returns the decoded payload dict {'user_id': ..., 'email': ...} on success.
    Raises ValueError('expired') or ValueError('invalid') on signature errors.
    """
    max_age = int(getattr(settings, 'EMAIL_VERIFY_MAX_AGE', 86400))
    try:
        data = signing.loads(token, salt=EMAIL_VERIFICATION_SALT, max_age=max_age)
        if not isinstance(data, dict) or 'user_id' not in data or 'email' not in data:
            raise ValueError('invalid')
        return data
    except signing.SignatureExpired:
        raise ValueError('expired')
    except (signing.BadSignature, Exception):
        raise ValueError('invalid')


def send_verification_email(user):
    """
    Dispatches a verification email with a signed link to the user's registered email address.
    """
    email = (getattr(user, 'email', '') or '').strip()
    if not email:
        return

    token = generate_email_verification_token(user)
    frontend_url = getattr(settings, 'FRONTEND_BASE_URL', 'http://localhost:3000').rstrip('/')
    verify_link = f"{frontend_url}/verify-email?token={token}"
    max_age_hours = int(getattr(settings, 'EMAIL_VERIFY_MAX_AGE', 86400)) // 3600

    subject = "Verify Your Email Address - KindHeart Pet Adoption"
    message = (
        f"Hello {user.first_name or user.username},\n\n"
        f"Thank you for joining KindHeart Pet Adoption Platform. Please verify your email by clicking the link below:\n"
        f"{verify_link}\n\n"
        f"This link will expire in {max_age_hours} hours.\n"
        f"If you did not register for an account, please safely disregard this email.\n"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("Failed to dispatch verification email: %s", exc)
