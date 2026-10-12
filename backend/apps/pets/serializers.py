import sys
from pathlib import Path

# Ensure backend directory is in sys.path for IDE module resolution
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework import serializers
from .models import Pet, PetImage, PetMedicalRecord, FavoritePet
from apps.accounts.serializers import ShelterProfileSerializer

class PetImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PetImage
        fields = ['id', 'public_id', 'image_url', 'caption', 'is_primary', 'created_at']
        read_only_fields = ['id', 'public_id', 'created_at']


class PetMedicalRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PetMedicalRecord
        fields = ['id', 'public_id', 'title', 'description', 'vet_name', 'record_date', 'created_at']
        read_only_fields = ['id', 'public_id', 'created_at']


class PetListSerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')
    age_display = serializers.ReadOnlyField()
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = [
            'id',
            'public_id',
            'shelter',
            'shelter_name',
            'name',
            'species',
            'breed',
            'age_months',
            'age_display',
            'gender',
            'description',
            'adoption_fee',
            'image',
            'primary_image',
            'is_vaccinated',
            'is_neutered',
            'status',
            'created_at'
        ]
        read_only_fields = ['id', 'public_id', 'created_at']

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


class PetDetailSerializer(serializers.ModelSerializer):
    shelter = ShelterProfileSerializer(read_only=True)
    images = PetImageSerializer(many=True, read_only=True)
    medical_records = PetMedicalRecordSerializer(many=True, read_only=True)
    age_display = serializers.ReadOnlyField()
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = [
            'id',
            'public_id',
            'shelter',
            'name',
            'species',
            'breed',
            'age_months',
            'age_display',
            'gender',
            'description',
            'adoption_fee',
            'image',
            'primary_image',
            'is_vaccinated',
            'is_neutered',
            'status',
            'images',
            'medical_records',
            'created_at'
        ]
        read_only_fields = ['id', 'public_id', 'created_at']

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


class PetWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Pet
        fields = [
            'id',
            'public_id',
            'name',
            'species',
            'breed',
            'age_months',
            'gender',
            'description',
            'adoption_fee',
            'image',
            'is_vaccinated',
            'is_neutered',
            'status'
        ]
        read_only_fields = ['id', 'public_id']

    def validate_status(self, value):
        if value in ['adopted', 'in_transit']:
            raise serializers.ValidationError(f"Manual status change to '{value}' is restricted. Status changes automatically via adoption application workflows.")
        return value


class PublicPetListSerializer(serializers.ModelSerializer):
    primary_image = serializers.SerializerMethodField()
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')
    shelter_city = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = [
            'public_id',
            'name',
            'species',
            'breed',
            'age_months',
            'gender',
            'size',
            'primary_image',
            'shelter_name',
            'shelter_city',
            'distance_km',
        ]
        read_only_fields = fields

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

    def get_shelter_city(self, obj):
        if obj.shelter:
            return getattr(obj.shelter, 'city', '') or obj.shelter.pickup_address
        return ''

    def get_distance_km(self, obj):
        if hasattr(obj, 'distance_meters') and obj.distance_meters is not None:
            return round(float(obj.distance_meters) / 1000.0, 2)
        return None


class PublicPetDetailSerializer(serializers.ModelSerializer):
    images = PetImageSerializer(many=True, read_only=True)
    primary_image = serializers.SerializerMethodField()
    shelter = serializers.SerializerMethodField()
    medical_overview = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = [
            'public_id',
            'name',
            'species',
            'breed',
            'age_months',
            'gender',
            'size',
            'description',
            'adoption_fee',
            'status',
            'primary_image',
            'images',
            'medical_overview',
            'shelter',
            'distance_km',
            'created_at',
        ]
        read_only_fields = fields

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

    def get_medical_overview(self, obj):
        records = obj.medical_records.all()
        return {
            'is_vaccinated': obj.is_vaccinated,
            'is_neutered': obj.is_neutered,
            'medical_records': [
                {
                    'title': r.title,
                    'description': r.description,
                    'record_date': r.record_date
                } for r in records
            ]
        }

    def get_shelter(self, obj):
        if not obj.shelter:
            return None
        return {
            'public_id': obj.shelter.public_id,
            'organization_name': obj.shelter.organization_name,
            'pickup_address': obj.shelter.pickup_address,
            'city': getattr(obj.shelter, 'city', ''),
            'latitude': str(obj.shelter.latitude) if obj.shelter.latitude else None,
            'longitude': str(obj.shelter.longitude) if obj.shelter.longitude else None,
            'phone': obj.shelter.user.phone if obj.shelter.user else '',
        }

    def get_distance_km(self, obj):
        if hasattr(obj, 'distance_meters') and obj.distance_meters is not None:
            return round(float(obj.distance_meters) / 1000.0, 2)
        return None


class PetSummarySerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')
    age_display = serializers.ReadOnlyField()
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = [
            'public_id',
            'name',
            'species',
            'breed',
            'age_months',
            'age_display',
            'gender',
            'size',
            'description',
            'adoption_fee',
            'status',
            'primary_image',
            'shelter_name',
            'is_vaccinated',
            'is_neutered',
            'created_at',
        ]
        read_only_fields = fields

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


class FavoritePetSerializer(serializers.ModelSerializer):
    pet = PetSummarySerializer(read_only=True)
    pet_public_id = serializers.UUIDField(source='pet.public_id', read_only=True)

    class Meta:
        model = FavoritePet
        fields = ['id', 'pet_public_id', 'pet', 'created_at']
        read_only_fields = ['id', 'pet_public_id', 'created_at']


