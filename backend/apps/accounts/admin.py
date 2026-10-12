from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, ShelterProfile, DeliveryProfile

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'role', 'phone', 'is_staff', 'is_active')
    list_filter = ('role', 'is_staff', 'is_active')
    fieldsets = UserAdmin.fieldsets + (
        ('Custom Profile Info', {'fields': ('public_id', 'role', 'phone', 'address')}),
    )
    readonly_fields = ('public_id',)

@admin.register(ShelterProfile)
class ShelterProfileAdmin(admin.ModelAdmin):
    list_display = ('organization_name', 'user', 'license_no', 'is_active', 'created_at')
    search_fields = ('organization_name', 'license_no', 'user__username')
    readonly_fields = ('public_id',)

@admin.register(DeliveryProfile)
class DeliveryProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'shelter', 'vehicle_type', 'vehicle_number', 'is_available', 'created_at')
    list_filter = ('vehicle_type', 'is_available')
    search_fields = ('user__username', 'vehicle_number', 'shelter__organization_name')
    readonly_fields = ('public_id',)
