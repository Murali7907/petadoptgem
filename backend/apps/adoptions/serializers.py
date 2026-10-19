import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from django.db import IntegrityError
from .models import AdoptionApplication, AdoptionReview
from apps.pets.models import Pet
from apps.accounts.models import User

class PetSummarySerializer(serializers.ModelSerializer):
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = ['public_id', 'name', 'species', 'breed', 'adoption_fee', 'status', 'primary_image']
        ref_name = 'AdoptionPetSummary'

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_primary_image(self, obj):
        primary = obj.images.filter(is_primary=True).first()
        if primary:
            return primary.image_url
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        first_img = obj.images.first()
        return first_img.image_url if first_img else None


class ApplicantSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'public_id', 'username', 'email', 'phone', 'first_name', 'last_name']


class ApplicationCreateSerializer(serializers.Serializer):
    pet_public_id = serializers.UUIDField(write_only=True, required=False)
    pet_id = serializers.CharField(write_only=True, required=False)
    living_situation = serializers.CharField(max_length=255)
    has_other_pets = serializers.BooleanField(default=False)
    other_pets_description = serializers.CharField(required=False, allow_blank=True, default='')
    id_proof_url = serializers.URLField(max_length=500, required=False, allow_blank=True, default='')
    residence_proof_url = serializers.URLField(max_length=500, required=False, allow_blank=True, default='')
    residence_type = serializers.CharField(max_length=50, required=False, allow_blank=True, default='own_house')
    has_fenced_yard = serializers.BooleanField(required=False, default=False)
    dropoff_address = serializers.CharField(required=False, allow_blank=True, default='')
    delivery_address = serializers.CharField(required=False, allow_blank=True, default='')
    delivery_city = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    delivery_pincode = serializers.CharField(max_length=10, required=False, allow_blank=True, default='')
    destination_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    destination_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    customer_latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    customer_longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    contact_phone = serializers.CharField(max_length=20)
    application_notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            raise serializers.ValidationError("Authentication required.")

        pet_ref = attrs.get('pet_public_id') or attrs.get('pet_id')
        if not pet_ref:
            raise serializers.ValidationError({"pet_id": "Either pet_id or pet_public_id is required."})

        pet = None
        import uuid
        try:
            val = uuid.UUID(str(pet_ref))
            pet = Pet.objects.filter(public_id=val).first()
        except (ValueError, TypeError, AttributeError):
            pass
        if not pet and str(pet_ref).isdigit():
            pet = Pet.objects.filter(id=int(pet_ref)).first()
        if not pet:
            pet = Pet.objects.filter(public_id=pet_ref).first()

        if not pet:
            raise serializers.ValidationError({"pet_id": "Target pet not found."})

        err_key = 'pet_public_id' if 'pet_public_id' in attrs else 'pet_id'
        if pet.status != 'available':
            raise serializers.ValidationError({err_key: f"Pet '{pet.name}' is currently unavailable for adoption (status: {pet.get_status_display()})."})

        active_app = AdoptionApplication.objects.filter(
            applicant=request.user,
            pet=pet,
            status__in=['submitted', 'reviewing', 'approved']
        ).exists()

        if active_app:
            raise serializers.ValidationError({err_key: "You already have an active application for this pet."})

        # Ensure either delivery_address or dropoff_address is supplied
        addr = attrs.get('delivery_address') or attrs.get('dropoff_address')
        if not addr:
            raise serializers.ValidationError({"delivery_address": "Delivery address is required."})
        if not attrs.get('delivery_address') and addr:
            attrs['delivery_address'] = addr
        if not attrs.get('dropoff_address') and addr:
            attrs['dropoff_address'] = addr

        # Sync coordinates
        dest_lat = attrs.get('destination_latitude') if attrs.get('destination_latitude') is not None else attrs.get('customer_latitude')
        dest_lon = attrs.get('destination_longitude') if attrs.get('destination_longitude') is not None else attrs.get('customer_longitude')
        if dest_lat is not None:
            attrs['destination_latitude'] = dest_lat
            attrs['customer_latitude'] = dest_lat
        if dest_lon is not None:
            attrs['destination_longitude'] = dest_lon
            attrs['customer_longitude'] = dest_lon

        attrs['pet_obj'] = pet
        return attrs

    def create(self, validated_data):
        pet = validated_data.pop('pet_obj')
        validated_data.pop('pet_public_id', None)
        validated_data.pop('pet_id', None)
        user = self.context['request'].user

        try:
            application = AdoptionApplication.objects.create(
                pet=pet,
                applicant=user,
                **validated_data
            )
            application.calculate_total()
            application.save()
            return application
        except IntegrityError:
            raise serializers.ValidationError({"pet_public_id": "You already have an active application for this pet."})


class CustomerApplicationSerializer(serializers.ModelSerializer):
    pet = PetSummarySerializer(read_only=True)

    class Meta:
        model = AdoptionApplication
        fields = [
            'id',
            'public_id',
            'status',
            'living_situation',
            'has_other_pets',
            'other_pets_description',
            'id_proof_url',
            'residence_proof_url',
            'residence_type',
            'has_fenced_yard',
            'dropoff_address',
            'delivery_address',
            'delivery_city',
            'delivery_pincode',
            'destination_latitude',
            'destination_longitude',
            'contact_phone',
            'customer_latitude',
            'customer_longitude',
            'pet_care_fee',
            'base_delivery_fee',
            'distance_km',
            'distance_fee',
            'total_amount',
            'application_notes',
            'pet',
            'submitted_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'public_id', 'status', 'submitted_at', 'updated_at']


class ShelterApplicationSerializer(serializers.ModelSerializer):
    pet = PetSummarySerializer(read_only=True)
    applicant = ApplicantSummarySerializer(read_only=True)

    class Meta:
        model = AdoptionApplication
        fields = [
            'id',
            'public_id',
            'status',
            'living_situation',
            'has_other_pets',
            'other_pets_description',
            'id_proof_url',
            'residence_proof_url',
            'residence_type',
            'has_fenced_yard',
            'dropoff_address',
            'delivery_address',
            'delivery_city',
            'delivery_pincode',
            'destination_latitude',
            'destination_longitude',
            'contact_phone',
            'customer_latitude',
            'customer_longitude',
            'pet_care_fee',
            'base_delivery_fee',
            'distance_km',
            'distance_fee',
            'total_amount',
            'application_notes',
            'pet',
            'applicant',
            'submitted_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'public_id', 'status', 'submitted_at', 'updated_at']


class ApplicationDecisionSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class AdoptionReviewCreateSerializer(serializers.ModelSerializer):
    shelter_rating = serializers.IntegerField(min_value=1, max_value=5)
    shelter_review = serializers.CharField(required=False, allow_blank=True, default='')
    driver_rating = serializers.IntegerField(min_value=1, max_value=5)
    driver_review = serializers.CharField(required=False, allow_blank=True, default='')

    class Meta:
        model = AdoptionReview
        fields = [
            'shelter_rating',
            'shelter_review',
            'driver_rating',
            'driver_review',
        ]

    def validate_shelter_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Shelter rating must be between 1 and 5.")
        return value

    def validate_driver_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Driver rating must be between 1 and 5.")
        return value


class AdoptionReviewDetailSerializer(serializers.ModelSerializer):
    application_public_id = serializers.UUIDField(source='application.public_id', read_only=True)
    reviewer_name = serializers.SerializerMethodField()

    class Meta:
        model = AdoptionReview
        fields = [
            'public_id',
            'application_public_id',
            'reviewer_name',
            'shelter_rating',
            'shelter_review',
            'driver_rating',
            'driver_review',
            'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_reviewer_name(self, obj):
        if obj.reviewer:
            return obj.reviewer.get_full_name() or obj.reviewer.username
        return "Anonymous"

