from django.contrib import admin
from django import forms
from .models import AdoptionApplication, AdoptionReview
from .services import check_application_refund_safe, assert_application_refund_safe, RefundRequiredError


class AdoptionApplicationAdminForm(forms.ModelForm):
    class Meta:
        model = AdoptionApplication
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        if self.instance and self.instance.pk:
            orig = AdoptionApplication.objects.filter(pk=self.instance.pk).first()
            if orig and orig.status == 'approved':
                new_status = cleaned_data.get('status')
                if new_status != 'approved':
                    safe, msg = check_application_refund_safe(orig)
                    if not safe:
                        raise forms.ValidationError({'status': msg})
        return cleaned_data


@admin.register(AdoptionApplication)
class AdoptionApplicationAdmin(admin.ModelAdmin):
    form = AdoptionApplicationAdminForm
    list_display = ('id', 'pet', 'applicant', 'status', 'total_amount', 'payment_method', 'payment_status', 'submitted_at')
    list_filter = ('status', 'payment_method', 'payment_status')
    search_fields = ('applicant__username', 'pet__name', 'contact_phone')
    readonly_fields = ('public_id', 'updated_at')

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.status == 'approved':
            safe, _ = check_application_refund_safe(obj)
            if not safe:
                if 'status' not in readonly:
                    readonly.append('status')
        return readonly

    def save_model(self, request, obj, form, change):
        if change and 'status' in form.changed_data:
            orig = AdoptionApplication.objects.get(pk=obj.pk)
            if orig.status == 'approved' and obj.status != 'approved':
                assert_application_refund_safe(orig)
        super().save_model(request, obj, form, change)


@admin.register(AdoptionReview)
class AdoptionReviewAdmin(admin.ModelAdmin):
    list_display = ('application', 'reviewer', 'rating', 'created_at')
    list_filter = ('rating',)
    search_fields = ('reviewer__username', 'comment')
    readonly_fields = ('public_id',)

