import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from decimal import Decimal
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field, OpenApiTypes
from .models import PetDelivery, DeliveryLocationPing, Payment
from apps.accounts.models import DeliveryProfile
from apps.adoptions.models import AdoptionApplication


class DeliveryAssignSerializer(serializers.Serializer):
    application_public_id = serializers.UUIDField(required=False)
    application_id = serializers.CharField(required=False)
    application_ref = serializers.CharField(required=False)
    driver_public_id = serializers.UUIDField(required=False)
    driver_id = serializers.CharField(required=False)
    driver_ref = serializers.CharField(required=False)
    payment_method = serializers.CharField(required=False, default='unset')

    def validate(self, attrs):
        app_ref = attrs.get('application_ref') or attrs.get('application_public_id') or attrs.get('application_id')
        driver_ref = attrs.get('driver_ref') or attrs.get('driver_public_id') or attrs.get('driver_id')
        if not app_ref:
            raise serializers.ValidationError("Either application_id, application_public_id, or application_ref is required.")
        if not driver_ref:
            raise serializers.ValidationError("Either driver_id, driver_public_id, or driver_ref is required.")
        attrs['application_ref'] = str(app_ref)
        attrs['driver_ref'] = str(driver_ref)
        return attrs


class DriverTaskPetSummarySerializer(serializers.Serializer):
    public_id = serializers.UUIDField(help_text="Public UUID of the pet")
    name = serializers.CharField(help_text="Pet name")
    species = serializers.CharField(help_text="Pet species (e.g. dog, cat)")
    breed = serializers.CharField(help_text="Pet breed")
    age_months = serializers.IntegerField(help_text="Pet age in months")


class CustomerTrackingPetSummarySerializer(serializers.Serializer):
    public_id = serializers.UUIDField(help_text="Public UUID of the pet")
    name = serializers.CharField(help_text="Pet name")
    species = serializers.CharField(help_text="Pet species")
    breed = serializers.CharField(help_text="Pet breed")


class DeliveryDriverDetailsSerializer(serializers.Serializer):
    public_id = serializers.UUIDField(help_text="Public UUID of the delivery driver")
    driver_name = serializers.CharField(help_text="Driver full name or username")
    driver_phone = serializers.CharField(allow_blank=True, help_text="Driver contact phone")
    vehicle_type = serializers.CharField(help_text="Driver vehicle type display")
    vehicle_number = serializers.CharField(allow_blank=True, help_text="Driver vehicle registration number")


class DeliveryDriverTaskSerializer(serializers.ModelSerializer):
    destination_address = serializers.SerializerMethodField()
    delivery_address = serializers.SerializerMethodField()
    delivery_city = serializers.SerializerMethodField()
    delivery_pincode = serializers.SerializerMethodField()
    destination_latitude = serializers.SerializerMethodField()
    destination_longitude = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    pet_summary = serializers.SerializerMethodField()

    class Meta:
        model = PetDelivery
        fields = [
            'public_id',
            'status',
            'destination_address',
            'delivery_address',
            'delivery_city',
            'delivery_pincode',
            'destination_latitude',
            'destination_longitude',
            'customer_phone',
            'customer_name',
            'pet_summary',
            'dispatched_at',
            'expires_at',
            'current_latitude',
            'current_longitude',
            'last_location_update',
            'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_destination_address(self, obj):
        if obj.application:
            app = obj.application
            addr = app.delivery_address or app.dropoff_address
            if addr:
                parts = [addr]
                if app.delivery_city:
                    parts.append(app.delivery_city)
                if app.delivery_pincode:
                    parts.append(app.delivery_pincode)
                return ", ".join(parts)
            if app.applicant and app.applicant.address:
                return app.applicant.address
        return "N/A"

    @extend_schema_field(serializers.CharField())
    def get_delivery_address(self, obj):
        if obj.application:
            return obj.application.delivery_address or obj.application.dropoff_address or ""
        return ""

    @extend_schema_field(serializers.CharField())
    def get_delivery_city(self, obj):
        if obj.application:
            return obj.application.delivery_city or ""
        return ""

    @extend_schema_field(serializers.CharField())
    def get_delivery_pincode(self, obj):
        if obj.application:
            return obj.application.delivery_pincode or ""
        return ""

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_destination_latitude(self, obj):
        if obj.application:
            return float(obj.application.destination_latitude) if obj.application.destination_latitude is not None else (float(obj.application.customer_latitude) if obj.application.customer_latitude is not None else None)
        return None

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_destination_longitude(self, obj):
        if obj.application:
            return float(obj.application.destination_longitude) if obj.application.destination_longitude is not None else (float(obj.application.customer_longitude) if obj.application.customer_longitude is not None else None)
        return None

    @extend_schema_field(serializers.CharField())
    def get_customer_phone(self, obj):
        if obj.application:
            if obj.application.contact_phone:
                return obj.application.contact_phone
            if obj.application.applicant and obj.application.applicant.phone:
                return obj.application.applicant.phone
        return ""

    @extend_schema_field(serializers.CharField())
    def get_customer_name(self, obj):
        if obj.application and obj.application.applicant:
            user = obj.application.applicant
            return user.get_full_name() or user.username
        return ""

    @extend_schema_field(DriverTaskPetSummarySerializer)
    def get_pet_summary(self, obj):
        if obj.application and obj.application.pet:
            pet = obj.application.pet
            return {
                'public_id': pet.public_id,
                'name': pet.name,
                'species': pet.species,
                'breed': pet.breed,
                'age_months': pet.age_months,
            }
        return {}


class DeliveryCustomerSerializer(serializers.ModelSerializer):
    driver_details = serializers.SerializerMethodField()
    pet_summary = serializers.SerializerMethodField()

    class Meta:
        model = PetDelivery
        fields = [
            'public_id',
            'status',
            'expires_at',
            'doorstep_otp_expires_at',
            'doorstep_otp_sent_at',
            'dispatched_at',
            'delivered_at',
            'current_latitude',
            'current_longitude',
            'last_location_update',
            'driver_details',
            'pet_summary',
            'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(DeliveryDriverDetailsSerializer)
    def get_driver_details(self, obj):
        driver = obj.driver or obj.delivery_partner
        if driver and driver.user:
            return {
                'public_id': driver.public_id,
                'driver_name': driver.user.get_full_name() or driver.user.username,
                'driver_phone': driver.user.phone,
                'vehicle_type': driver.get_vehicle_type_display(),
                'vehicle_number': driver.vehicle_number,
            }
        return None

    @extend_schema_field(CustomerTrackingPetSummarySerializer)
    def get_pet_summary(self, obj):
        if obj.application and obj.application.pet:
            pet = obj.application.pet
            return {
                'public_id': pet.public_id,
                'name': pet.name,
                'species': pet.species,
                'breed': pet.breed,
            }
        return {}


class ShelterDeliverySerializer(serializers.ModelSerializer):
    application_public_id = serializers.UUIDField(source='application.public_id', read_only=True)
    driver_public_id = serializers.UUIDField(source='driver.public_id', read_only=True, allow_null=True)
    driver_name = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    pet_name = serializers.SerializerMethodField()

    class Meta:
        model = PetDelivery
        fields = [
            'public_id',
            'application_public_id',
            'driver_public_id',
            'driver_name',
            'customer_name',
            'pet_name',
            'status',
            'pickup_otp_attempts',
            'doorstep_otp_attempts',
            'expires_at',
            'doorstep_otp_expires_at',
            'doorstep_otp_sent_at',
            'dispatched_at',
            'delivered_at',
            'current_latitude',
            'current_longitude',
            'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_driver_name(self, obj):
        if obj.driver and obj.driver.user:
            return obj.driver.user.get_full_name() or obj.driver.user.username
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_name(self, obj):
        if obj.application and obj.application.applicant:
            return obj.application.applicant.get_full_name() or obj.application.applicant.username
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_pet_name(self, obj):
        if obj.application and obj.application.pet:
            return obj.application.pet.name
        return None


class DriverListSerializer(serializers.ModelSerializer):
    user_public_id = serializers.UUIDField(source='user.public_id', read_only=True)
    name = serializers.SerializerMethodField()
    phone = serializers.CharField(source='user.phone', read_only=True)

    class Meta:
        model = DeliveryProfile
        fields = [
            'public_id',
            'user_public_id',
            'name',
            'phone',
            'vehicle_type',
            'vehicle_number',
            'is_available',
            'current_latitude',
            'current_longitude',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


class LocationPingSerializer(serializers.ModelSerializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)

    class Meta:
        model = DeliveryLocationPing
        fields = ['public_id', 'latitude', 'longitude', 'pinged_at']
        read_only_fields = ['public_id', 'pinged_at']

    def validate_latitude(self, value):
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value

    def validate_longitude(self, value):
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        return value


class OTPVerificationSerializer(serializers.Serializer):
    otp = serializers.CharField(max_length=6, min_length=6, required=False)
    secret_otp = serializers.CharField(max_length=6, min_length=6, required=False)
    code = serializers.CharField(max_length=6, min_length=6, required=False)

    def validate(self, attrs):
        otp = attrs.get('otp') or attrs.get('secret_otp') or attrs.get('code')
        if not otp:
            raise serializers.ValidationError({"otp": "OTP is required."})
        val = str(otp).strip()
        if not val.isdigit() or len(val) != 6:
            raise serializers.ValidationError({"otp": "OTP must be a 6-digit numeric string."})
        attrs['otp'] = val
        return attrs


class PaymentOrderCreateSerializer(serializers.Serializer):
    delivery_public_id = serializers.UUIDField(required=False)
    delivery_id = serializers.UUIDField(required=False)
    amount_in_paise = serializers.IntegerField(required=False, min_value=1)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)

    def validate(self, attrs):
        if not attrs.get('delivery_public_id') and not attrs.get('delivery_id'):
            raise serializers.ValidationError({"delivery_id": "delivery_id or delivery_public_id is required."})
        return attrs


class PaymentVerifySerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField()
    razorpay_payment_id = serializers.CharField()
    razorpay_signature = serializers.CharField()


class PaymentLedgerSerializer(serializers.ModelSerializer):
    delivery_public_id = serializers.UUIDField(source='delivery.public_id', read_only=True, allow_null=True)
    application_public_id = serializers.UUIDField(source='application.public_id', read_only=True, allow_null=True)

    class Meta:
        model = Payment
        fields = [
            'public_id',
            'delivery_public_id',
            'application_public_id',
            'status',
            'amount',
            'amount_in_paise',
            'currency',
            'payment_method',
            'razorpay_order_id',
            'razorpay_payment_id',
            'razorpay_signature',
            'transaction_id',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class DeliveryAssignResponseSerializer(ShelterDeliverySerializer):
    pickup_otp = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Plaintext 6-digit pickup OTP returned once upon assignment to shelter."
    )

    class Meta(ShelterDeliverySerializer.Meta):
        fields = ShelterDeliverySerializer.Meta.fields + ['pickup_otp']
        read_only_fields = fields


class DeliveryReissueOTPRequestSerializer(serializers.Serializer):
    purpose = serializers.ChoiceField(choices=['pickup', 'doorstep'])


class DeliveryReissueOTPResponseSerializer(serializers.Serializer):
    message = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)
    purpose = serializers.CharField()
    pickup_otp = serializers.CharField(required=False, allow_blank=True)
    doorstep_otp = serializers.CharField(required=False, allow_blank=True)
    email_dispatched = serializers.BooleanField(required=False)
    expires_at = serializers.DateTimeField()


class DeliveryStatusUpdateRequestSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['failed'])


class DeliveryPickupResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    delivery = DeliveryDriverTaskSerializer()


class DeliveryDoorstepResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    delivery = DeliveryDriverTaskSerializer()


class DeliveryLocationCurrentLocationSerializer(serializers.Serializer):
    latitude = serializers.CharField()
    longitude = serializers.CharField()
    updated_at = serializers.CharField()


class DeliveryPingResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    ping = LocationPingSerializer()
    current_location = DeliveryLocationCurrentLocationSerializer()


class DeliveryTrackingCoordinatesSerializer(serializers.Serializer):
    latitude = serializers.CharField(allow_null=True)
    longitude = serializers.CharField(allow_null=True)
    last_updated = serializers.DateTimeField(allow_null=True)


class DeliveryTrackingResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    delivery = serializers.DictField()
    current_coordinates = DeliveryTrackingCoordinatesSerializer()
    breadcrumbs = LocationPingSerializer(many=True)


class CollectCashRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'))


class CollectCashResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    cash_collected = serializers.DecimalField(max_digits=8, decimal_places=2)
    remaining_amount = serializers.DecimalField(max_digits=8, decimal_places=2)
    payment_status = serializers.CharField()
    payment_id = serializers.UUIDField(required=False)


class DeliveryCancelResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    delivery_id = serializers.CharField()
    status = serializers.CharField()
    idempotent = serializers.BooleanField(required=False)



