from django.contrib import admin
from .models import Pet, PetImage, PetMedicalRecord

@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    list_display = ('name', 'species', 'breed', 'shelter', 'adoption_fee', 'status', 'created_at')
    list_filter = ('species', 'status', 'is_vaccinated', 'is_neutered', 'shelter')
    search_fields = ('name', 'breed', 'description', 'shelter__organization_name')
    readonly_fields = ('public_id',)

@admin.register(PetImage)
class PetImageAdmin(admin.ModelAdmin):
    list_display = ('pet', 'caption', 'is_primary', 'created_at')
    list_filter = ('is_primary',)
    readonly_fields = ('public_id',)

@admin.register(PetMedicalRecord)
class PetMedicalRecordAdmin(admin.ModelAdmin):
    list_display = ('title', 'pet', 'vet_name', 'record_date', 'created_at')
    search_fields = ('title', 'vet_name', 'pet__name')
    readonly_fields = ('public_id',)
