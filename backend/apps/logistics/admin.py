from django.contrib import admin
from .models import PetDelivery, Payment, DeliveryLocationPing

@admin.register(PetDelivery)
class PetDeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'application', 'driver', 'status', 'secret_otp', 'cash_collected', 'delivered_at')
    list_filter = ('status', 'is_cash_settled_with_shelter')
    search_fields = ('secret_otp', 'driver__user__username', 'application__pet__name')
    readonly_fields = ('public_id',)

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
