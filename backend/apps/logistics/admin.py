from django.contrib import admin
from .models import PetDelivery, Payment, DeliveryLocationPing, Refund

@admin.register(PetDelivery)
class PetDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'application',
        'driver',
        'status',
        'pickup_otp_attempts',
        'doorstep_otp_attempts',
        'expires_at',
        'doorstep_otp_expires_at',
        'doorstep_otp_sent_at',
        'cash_collected',
        'delivered_at',
    )
    list_filter = ('status', 'is_cash_settled_with_shelter')
    search_fields = ('driver__user__username', 'application__pet__name', 'application__applicant__username')
    readonly_fields = (
        'public_id',
        'pickup_otp_hash',
        'doorstep_otp_hash',
        'pickup_otp_attempts',
        'doorstep_otp_attempts',
    )

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'application', 'amount', 'payment_method', 'status', 'created_at')
    list_filter = ('payment_method', 'status')
    search_fields = ('transaction_id', 'application__applicant__username')
    readonly_fields = ('public_id',)

@admin.register(DeliveryLocationPing)
class DeliveryLocationPingAdmin(admin.ModelAdmin):
    list_display = ('id', 'delivery', 'latitude', 'longitude', 'pinged_at')
    readonly_fields = ('public_id',)


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """Read-only Django admin interface for inspecting payment refunds."""
    list_display = (
        'id',
        'public_id',
        'payment',
        'application',
        'amount_in_paise',
        'status',
        'razorpay_refund_id',
        'source',
        'initiated_by',
        'created_at',
        'updated_at',
    )
    list_filter = ('status', 'source', 'created_at')
    search_fields = ('razorpay_refund_id', 'payment__transaction_id', 'application__applicant__username', 'reason')
    readonly_fields = (
        'public_id',
        'payment',
        'application',
        'amount_in_paise',
        'status',
        'razorpay_refund_id',
        'reason',
        'initiated_by',
        'source',
        'failure_reason',
        'created_at',
        'updated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

