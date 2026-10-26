from django.contrib import admin, messages
from django.utils.timesince import timesince
from .models import PetDelivery, Payment, DeliveryLocationPing, Refund, RefundStatus, PaymentStatus
from .payment_services import issue_refund

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
    """Read-only Django admin interface for inspecting payment refunds with admin retry action."""
    list_display = (
        'id',
        'status',
        'reason',
        'source',
        'failure_reason',
        'age',
        'public_id',
        'payment',
        'application',
        'amount_in_paise',
        'razorpay_refund_id',
        'created_at',
    )
    list_filter = ('status', 'source', 'created_at')
    search_fields = ('razorpay_refund_id', 'payment__transaction_id', 'application__applicant__username', 'reason', 'failure_reason')
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
    actions = ['retry_selected_refunds']

    @admin.display(description="Age", ordering="created_at")
    def age(self, obj):
        return timesince(obj.created_at)

    @admin.action(description="Retry selected refunds")
    def retry_selected_refunds(self, request, queryset):
        """Platform admin only: retries selected pending or failed refunds using idempotent issue_refund."""
        if not (request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, 'role', None) == 'admin')):
            self.message_user(request, "Permission denied: Platform admin only.", level=messages.ERROR)
            return

        success_count = 0
        failed_count = 0
        skipped_count = 0

        for refund in queryset:
            payment = refund.payment
            if refund.status == RefundStatus.PROCESSED or payment.status == PaymentStatus.REFUNDED:
                skipped_count += 1
                continue

            try:
                result = issue_refund(
                    payment=payment,
                    reason=refund.reason or "Admin triggered refund retry",
                    initiated_by=request.user,
                    source=refund.source or 'admin_retry',
                    refund_obj=refund,
                )
                if result.status == RefundStatus.PROCESSED:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                failed_count += 1

        msg = f"Refund retry completed: {success_count} succeeded, {failed_count} failed, {skipped_count} skipped."
        if failed_count > 0:
            self.message_user(request, msg, level=messages.WARNING)
        else:
            self.message_user(request, msg, level=messages.SUCCESS)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not (request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, 'role', None) == 'admin')):
            actions.pop('retry_selected_refunds', None)
        return actions

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

