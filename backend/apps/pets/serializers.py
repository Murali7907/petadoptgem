import sys
from pathlib import Path

# Ensure backend directory is in sys.path for IDE module resolution
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field, OpenApiTypes
from .models import Pet, PetImage, PetMedicalRecord, FavoritePet
from apps.accounts.serializers import ShelterProfileSerializer


class MedicalOverviewRecordSerializer(serializers.Serializer):
    title = serializers.CharField(help_text="Title or procedure name of the medical record")
    description = serializers.CharField(allow_null=True, required=False, help_text="Clinical description or notes")
    record_date = serializers.DateField(help_text="Date the record or treatment was administered")
    vet_name = serializers.CharField(allow_null=True, required=False, help_text="Administering veterinarian or clinic name")
    document_url = serializers.CharField(allow_null=True, required=False, help_text="Direct document link (null for anonymous users or non-applicants)")


class MedicalOverviewSerializer(serializers.Serializer):
    is_vaccinated = serializers.BooleanField(help_text="Whether the pet is fully vaccinated")
    is_neutered = serializers.BooleanField(help_text="Whether the pet is spayed or neutered")
    medical_records = MedicalOverviewRecordSerializer(many=True, help_text="List of medical records")


class PublicPetShelterSerializer(serializers.Serializer):
    public_id = serializers.UUIDField(help_text="Public UUID of the shelter organization")
    organization_name = serializers.CharField(help_text="Name of the shelter organization")
    city = serializers.CharField(allow_blank=True, default='', help_text="City of the shelter")
    pickup_address = serializers.CharField(allow_null=True, required=False, help_text="Physical pickup address (unmasked only for approved adopters, owning shelter, and admins)")
    latitude = serializers.CharField(allow_null=True, required=False, help_text="Shelter latitude coordinates (unmasked only for approved adopters, owning shelter, and admins)")
    longitude = serializers.CharField(allow_null=True, required=False, help_text="Shelter longitude coordinates (unmasked only for approved adopters, owning shelter, and admins)")
    phone = serializers.CharField(allow_null=True, required=False, help_text="Contact phone number (unmasked only for approved adopters, owning shelter, and admins)")


class PetImageSerializer(serializers.ModelSerializer):
    image = serializers.FileField(required=False, write_only=True)

    class Meta:
        model = PetImage
        fields = ['id', 'public_id', 'image', 'image_url', 'caption', 'is_primary', 'created_at']
        read_only_fields = ['id', 'public_id', 'created_at']
        extra_kwargs = {
            'image_url': {'required': False}
        }

    def validate_image(self, value):
        from .validators import validate_pet_image
        validate_pet_image(value)
        return value

    def create(self, validated_data):
        image_file = validated_data.pop('image', None)
        if image_file:
            from django.core.files.storage import default_storage
            pet = validated_data.get('pet')
            prefix = f"pets/{pet.id if pet else 'gallery'}"
            saved_path = default_storage.save(f"{prefix}/{image_file.name}", image_file)
            validated_data['image_url'] = default_storage.url(saved_path)
        elif not validated_data.get('image_url'):
            raise serializers.ValidationError({"image": "Either image file or image_url is required."})
        return super().create(validated_data)


def user_can_access_medical_documents(user, pet) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False) or getattr(user, 'role', '') == 'admin':
        return True
    if hasattr(user, 'shelter_profile') and user.shelter_profile and pet and getattr(pet, 'shelter_id', None) == user.shelter_profile.id:
        return True
    if pet:
        from apps.adoptions.models import AdoptionApplication
        return AdoptionApplication.objects.filter(
            applicant=user,
            pet=pet,
            status__in=['approved', 'completed']
        ).exists()
    return False


user_can_access_confidential_pet_details = user_can_access_medical_documents


class PetMedicalRecordSerializer(serializers.ModelSerializer):
    record_date = serializers.DateField(required=False)
    administered_date = serializers.DateField(required=False, write_only=True)
    document = serializers.FileField(required=False, write_only=True)
    document_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = PetMedicalRecord
        fields = ['id', 'public_id', 'title', 'description', 'vet_name', 'record_date', 'administered_date', 'document', 'document_url', 'created_at']
        read_only_fields = ['id', 'public_id', 'created_at']

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_document_url(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user_can_access_medical_documents(user, getattr(obj, 'pet', None)):
            return obj.document
        return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        can_access = user_can_access_medical_documents(user, getattr(instance, 'pet', None))
        if not can_access:
            data['document_url'] = None
            data['description'] = instance.clinical_description
        else:
            data['document_url'] = instance.document
            data['description'] = instance.clinical_description or instance.description
        return data

    def validate_document(self, value):
        from .validators import validate_medical_document
        validate_medical_document(value)
        return value

    def validate(self, attrs):
        if 'record_date' not in attrs:
            if 'administered_date' in attrs:
                attrs['record_date'] = attrs.pop('administered_date')
            else:
                from django.utils import timezone
                attrs['record_date'] = timezone.now().date()
        else:
            attrs.pop('administered_date', None)
        return attrs

    def create(self, validated_data):
        doc_file = validated_data.pop('document', None)
        if doc_file:
            from django.core.files.storage import default_storage
            pet = validated_data.get('pet')
            prefix = f"medical/{pet.id if pet else 'records'}"
            saved_path = default_storage.save(f"{prefix}/{doc_file.name}", doc_file)
            doc_url = default_storage.url(saved_path)
            desc = validated_data.get('description', '')
            if desc:
                validated_data['description'] = f"{desc}\nDocument: {doc_url}"
            else:
                validated_data['description'] = f"Document: {doc_url}"
        return super().create(validated_data)

    def update(self, instance, validated_data):
        doc_file = validated_data.pop('document', None)
        if doc_file:
            from django.core.files.storage import default_storage
            pet = instance.pet
            prefix = f"medical/{pet.id if pet else 'records'}"
            saved_path = default_storage.save(f"{prefix}/{doc_file.name}", doc_file)
            doc_url = default_storage.url(saved_path)
            desc = validated_data.get('description', instance.clinical_description)
            validated_data['description'] = f"{desc}\nDocument: {doc_url}".strip() if desc else f"Document: {doc_url}"
        elif 'description' in validated_data and instance.document:
            existing_doc = instance.document
            desc = validated_data['description']
            if existing_doc and "Document: " not in desc:
                validated_data['description'] = f"{desc}\nDocument: {existing_doc}".strip()
        return super().update(instance, validated_data)


class PetListSerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')
    age_display = serializers.CharField(read_only=True)
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

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_primary_image(self, obj):
        all_images = obj.images.all()
        primary = next((img for img in all_images if img.is_primary), None)
        if primary:
            return primary.image_url
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        first_img = next(iter(all_images), None)
        return first_img.image_url if first_img else None


class PetDetailSerializer(serializers.ModelSerializer):
    shelter = serializers.SerializerMethodField()
    images = PetImageSerializer(many=True, read_only=True)
    medical_records = PetMedicalRecordSerializer(many=True, read_only=True)
    medical_overview = serializers.SerializerMethodField()
    age_display = serializers.CharField(read_only=True)
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
            'medical_overview',
            'created_at'
        ]
        read_only_fields = ['id', 'public_id', 'created_at']

    @extend_schema_field(MedicalOverviewSerializer)
    def get_medical_overview(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        can_access = user_can_access_medical_documents(user, obj)

        records = obj.medical_records.all()
        return {
            'is_vaccinated': obj.is_vaccinated,
            'is_neutered': obj.is_neutered,
            'medical_records': [
                {
                    'title': r.title,
                    'description': r.clinical_description if not can_access else (r.clinical_description or r.description),
                    'record_date': r.record_date,
                    'vet_name': r.vet_name,
                    'document_url': r.document if can_access else None,
                } for r in records
            ]
        }

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

    @extend_schema_field(PublicPetShelterSerializer)
    def get_shelter(self, obj):
        if not obj.shelter:
            return None
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        can_access = user_can_access_medical_documents(user, obj)

        city_val = getattr(obj.shelter, 'city', '') or "location not provided"

        return {
            'public_id': obj.shelter.public_id,
            'organization_name': obj.shelter.organization_name,
            'city': city_val,
            'pickup_address': obj.shelter.pickup_address if can_access else None,
            'latitude': str(obj.shelter.latitude) if (can_access and obj.shelter.latitude) else None,
            'longitude': str(obj.shelter.longitude) if (can_access and obj.shelter.longitude) else None,
            'phone': (obj.shelter.user.phone if (obj.shelter.user and obj.shelter.user.phone) else None) if can_access else None,
        }


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

    @extend_schema_field(serializers.CharField())
    def get_shelter_city(self, obj):
        if obj.shelter and getattr(obj.shelter, 'city', None):
            return obj.shelter.city
        return "location not provided"

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_distance_km(self, obj):
        if hasattr(obj, 'distance_meters') and obj.distance_meters is not None:
            km = round(float(obj.distance_meters) / 1000.0)
            return max(1, int(km))
        return None


class PublicPetDetailSerializer(serializers.ModelSerializer):
    images = PetImageSerializer(many=True, read_only=True)
    primary_image = serializers.SerializerMethodField()
    shelter = serializers.SerializerMethodField()
    medical_overview = serializers.SerializerMethodField()
    medical_records = PetMedicalRecordSerializer(many=True, read_only=True)
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
            'medical_records',
            'shelter',
            'distance_km',
            'created_at',
        ]
        read_only_fields = fields

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

    @extend_schema_field(MedicalOverviewSerializer)
    def get_medical_overview(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        can_access = user_can_access_medical_documents(user, obj)

        records = obj.medical_records.all()
        return {
            'is_vaccinated': obj.is_vaccinated,
            'is_neutered': obj.is_neutered,
            'medical_records': [
                {
                    'title': r.title,
                    'description': r.clinical_description if not can_access else (r.clinical_description or r.description),
                    'record_date': r.record_date,
                    'vet_name': r.vet_name,
                    'document_url': r.document if can_access else None,
                } for r in records
            ]
        }

    @extend_schema_field(PublicPetShelterSerializer)
    def get_shelter(self, obj):
        if not obj.shelter:
            return None
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        can_access = user_can_access_medical_documents(user, obj)

        city_val = getattr(obj.shelter, 'city', '') or "location not provided"

        return {
            'public_id': obj.shelter.public_id,
            'organization_name': obj.shelter.organization_name,
            'city': city_val,
            'pickup_address': obj.shelter.pickup_address if can_access else None,
            'latitude': str(obj.shelter.latitude) if (can_access and obj.shelter.latitude) else None,
            'longitude': str(obj.shelter.longitude) if (can_access and obj.shelter.longitude) else None,
            'phone': (obj.shelter.user.phone if (obj.shelter.user and obj.shelter.user.phone) else None) if can_access else None,
        }

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_distance_km(self, obj):
        if hasattr(obj, 'distance_meters') and obj.distance_meters is not None:
            km = round(float(obj.distance_meters) / 1000.0)
            return max(1, int(km))
        return None


class PetSummarySerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')
    age_display = serializers.CharField(read_only=True)
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


class FavoritePetSerializer(serializers.ModelSerializer):
    pet = PetSummarySerializer(read_only=True)
    pet_public_id = serializers.UUIDField(source='pet.public_id', read_only=True)

    class Meta:
        model = FavoritePet
        fields = ['id', 'pet_public_id', 'pet', 'created_at']
        read_only_fields = ['id', 'pet_public_id', 'created_at']


