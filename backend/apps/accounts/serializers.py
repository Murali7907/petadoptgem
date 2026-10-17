from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User, ShelterProfile, DeliveryProfile, Notification

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Custom JWT serializer that embeds user_id, public_id, email, role, shelter_id, and delivery_id into the JWT payload.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Add custom claims into token payload
        token['user_id'] = user.id
        token['public_id'] = str(user.public_id)
        token['email'] = user.email
        token['role'] = user.role

        shelter_id = None
        if hasattr(user, 'shelter_profile') and user.shelter_profile:
            shelter_id = user.shelter_profile.id
        elif hasattr(user, 'delivery_profile') and user.delivery_profile:
            shelter_id = user.delivery_profile.shelter_id
        token['shelter_id'] = shelter_id

        delivery_id = None
        if hasattr(user, 'delivery_profile') and user.delivery_profile:
            delivery_id = user.delivery_profile.id
        token['delivery_id'] = delivery_id

        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        user = self.user
        shelter_id = None
        if hasattr(user, 'shelter_profile') and user.shelter_profile:
            shelter_id = user.shelter_profile.id
        elif hasattr(user, 'delivery_profile') and user.delivery_profile:
            shelter_id = user.delivery_profile.shelter_id

        delivery_id = None
        if hasattr(user, 'delivery_profile') and user.delivery_profile:
            delivery_id = user.delivery_profile.id

        # Add user identity details to the login response payload
        data['role'] = user.role
        data['shelter_id'] = shelter_id
        data['delivery_id'] = delivery_id
        data['user_id'] = user.id
        data['public_id'] = str(user.public_id)
        data['user'] = {
            'id': user.id,
            'public_id': str(user.public_id),
            'username': user.username,
            'email': user.email,
            'role': user.role,
            'shelter_id': shelter_id,
            'delivery_id': delivery_id,
        }
        return data


class ShelterProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShelterProfile
        fields = ['public_id', 'organization_name', 'license_no', 'pickup_address', 'city', 'latitude', 'longitude', 'is_active', 'is_verified', 'created_at']
        read_only_fields = ['public_id', 'created_at']


class DeliveryProfileSerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')

    class Meta:
        model = DeliveryProfile
        fields = ['public_id', 'shelter', 'shelter_name', 'vehicle_type', 'vehicle_number', 'is_available', 'current_latitude', 'current_longitude', 'created_at']
        read_only_fields = ['public_id', 'created_at']


class UserProfileSerializer(serializers.ModelSerializer):
    shelter_profile = ShelterProfileSerializer(read_only=True)
    delivery_profile = DeliveryProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 
            'public_id', 
            'username', 
            'email', 
            'role', 
            'first_name', 
            'last_name', 
            'phone', 
            'address', 
            'is_staff',
            'date_joined', 
            'shelter_profile', 
            'delivery_profile'
        ]
        read_only_fields = ['id', 'public_id', 'is_staff', 'date_joined']


class UserRegistrationSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True, allow_blank=False)
    password = serializers.CharField(write_only=True, min_length=6)
    organization_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    license_no = serializers.CharField(write_only=True, required=False, allow_blank=True)
    pickup_address = serializers.CharField(write_only=True, required=False, allow_blank=True)
    city = serializers.CharField(write_only=True, required=False, allow_blank=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, write_only=True, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, write_only=True, required=False, allow_null=True)

    class Meta:
        model = User
        fields = [
            'username', 
            'email', 
            'password', 
            'first_name', 
            'last_name', 
            'role', 
            'phone', 
            'address',
            'organization_name',
            'license_no',
            'pickup_address',
            'city',
            'latitude',
            'longitude',
        ]

    def validate_email(self, value):
        if not value or not str(value).strip():
            raise serializers.ValidationError("Email is required.")
        email_clean = str(value).strip().lower()
        if User.objects.filter(email__iexact=email_clean).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email_clean

    def validate_role(self, value):
        if value not in ('customer', 'shelter'):
            raise serializers.ValidationError("Public registration is only permitted for 'customer' and 'shelter' roles.")
        return value

    def create(self, validated_data):
        from django.db import transaction
        org_name = validated_data.pop('organization_name', None)
        lic_no = validated_data.pop('license_no', '')
        pickup_addr = validated_data.pop('pickup_address', '')
        city_val = validated_data.pop('city', '')
        lat_val = validated_data.pop('latitude', None)
        lon_val = validated_data.pop('longitude', None)
        password = validated_data.pop('password')

        with transaction.atomic():
            user = User(**validated_data)
            user.set_password(password)
            user.save()

            if user.role == 'shelter':
                ShelterProfile.objects.create(
                    user=user,
                    organization_name=org_name or f"{user.username}'s Shelter",
                    license_no=lic_no or '',
                    pickup_address=pickup_addr or user.address or 'Shelter Address',
                    city=city_val or '',
                    latitude=lat_val,
                    longitude=lon_val,
                    is_active=True,
                    is_verified=False
                )
        return user


class DriverCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=6)
    first_name = serializers.CharField(max_length=150, required=False, default='')
    last_name = serializers.CharField(max_length=150, required=False, default='')
    phone = serializers.CharField(max_length=20, required=False, default='')
    address = serializers.CharField(required=False, default='')
    vehicle_type = serializers.ChoiceField(choices=DeliveryProfile.VEHICLE_CHOICES, default='car')
    vehicle_number = serializers.CharField(max_length=30)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_email(self, value):
        if not value or not str(value).strip():
            raise serializers.ValidationError("Email is required.")
        email_clean = str(value).strip().lower()
        if User.objects.filter(email__iexact=email_clean).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email_clean


class AdminShelterDetailSerializer(serializers.ModelSerializer):
    user_id = serializers.ReadOnlyField(source='user.id')
    user_public_id = serializers.ReadOnlyField(source='user.public_id')
    user_email = serializers.ReadOnlyField(source='user.email')
    user_phone = serializers.ReadOnlyField(source='user.phone')
    user_username = serializers.ReadOnlyField(source='user.username')
    pet_inventory_count = serializers.SerializerMethodField()
    verification_status = serializers.SerializerMethodField()

    class Meta:
        model = ShelterProfile
        fields = [
            'id',
            'public_id',
            'user_id',
            'user_public_id',
            'user_username',
            'user_email',
            'user_phone',
            'organization_name',
            'license_no',
            'pickup_address',
            'city',
            'latitude',
            'longitude',
            'is_active',
            'is_verified',
            'verification_status',
            'pet_inventory_count',
            'created_at',
        ]
        read_only_fields = ['id', 'public_id', 'created_at']

    def get_pet_inventory_count(self, obj) -> int:
        return obj.pets.count()

    def get_verification_status(self, obj) -> str:
        if not obj.is_active:
            return "suspended"
        return "verified" if obj.is_verified else "pending"


class AdminUserStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    admin = serializers.IntegerField()
    shelter = serializers.IntegerField()
    delivery = serializers.IntegerField()
    customer = serializers.IntegerField()


class AdminPetStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    available = serializers.IntegerField()
    adopted = serializers.IntegerField()
    in_transit = serializers.IntegerField()
    pending = serializers.IntegerField()


class AdminPlatformStatsSerializer(serializers.Serializer):
    total_users = AdminUserStatsSerializer()
    total_pets = AdminPetStatsSerializer()
    active_deliveries = serializers.IntegerField()
    total_revenue_inr = serializers.FloatField()
    average_shelter_rating = serializers.FloatField()
    average_driver_rating = serializers.FloatField()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            'public_id',
            'title',
            'message',
            'notification_type',
            'is_read',
            'action_url',
            'created_at',
        ]
        read_only_fields = ['public_id', 'created_at']


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text="The refresh JWT token to blacklist.")



