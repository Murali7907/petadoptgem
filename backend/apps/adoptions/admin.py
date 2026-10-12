from django.contrib import admin
from .models import AdoptionApplication, AdoptionReview

@admin.register(AdoptionApplication)
class AdoptionApplicationAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet', 'applicant', 'status', 'total_amount', 'payment_method', 'payment_status', 'submitted_at')
    list_filter = ('status', 'payment_method', 'payment_status')
    search_fields = ('applicant__username', 'pet__name', 'contact_phone')
    readonly_fields = ('public_id', 'updated_at')

@admin.register(AdoptionReview)
class AdoptionReviewAdmin(admin.ModelAdmin):
    list_display = ('application', 'reviewer', 'rating', 'created_at')
    list_filter = ('rating',)
    search_fields = ('reviewer__username', 'comment')
    readonly_fields = ('public_id',)
