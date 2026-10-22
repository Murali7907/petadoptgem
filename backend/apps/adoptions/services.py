from decimal import Decimal
from rest_framework import status
from rest_framework.exceptions import APIException
from apps.adoptions.models import AdoptionApplication


class RefundRequiredError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "refund_required"
    default_detail = (
        "Cannot modify or cancel application: online payment has already been captured "
        "or cash collected. A refund must be processed before cancellation."
    )

    def __init__(self, detail=None, code=None):
        error_detail = {
            "code": code or self.default_code,
            "detail": detail or self.default_detail,
        }
        super().__init__(detail=error_detail, code=code or self.default_code)


def check_application_refund_safe(application: AdoptionApplication) -> tuple[bool, str]:
    """
    Returns (is_safe, error_message).
    Checks if an application has any captured/paid online Payment or cash collected on PetDelivery.
    """
    from apps.logistics.models import Payment, PaymentStatus, PetDelivery

    deliv = PetDelivery.objects.filter(application=application).first()
    has_cash = bool(deliv and deliv.cash_collected and deliv.cash_collected > Decimal('0.00'))
    has_online_payment = Payment.objects.filter(
        application=application,
        status__in=[PaymentStatus.CAPTURED, PaymentStatus.PAID, 'captured', 'paid']
    ).exclude(payment_method='cod').exists()

    if has_online_payment or has_cash:
        msg = (
            "Cannot modify or cancel application: online payment has already been captured "
            "or cash collected. A refund must be processed before cancellation."
        )
        return False, msg

    return True, ""


def assert_application_refund_safe(application: AdoptionApplication):
    """
    Raises RefundRequiredError (HTTP 409 Conflict with code 'refund_required')
    if the application cannot be moved out of 'approved' or into 'cancelled'/'rejected'/'reviewing'
    due to captured online payment or collected cash.
    """
    is_safe, msg = check_application_refund_safe(application)
    if not is_safe:
        raise RefundRequiredError(detail=msg)
