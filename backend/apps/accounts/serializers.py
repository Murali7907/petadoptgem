from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User, ShelterProfile, DeliveryProfile, Notification

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Custom JWT serializer that embeds user_id, public_id, email, and role into the JWT payload.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Add custom claims into token payload
        token['user_id'] = user.id
        token['public_id'] = str(user.public_id)
        token['email'] = user.email
        token['role'] = user.role

        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        # Add user identity details to the login response payload
        data['user'] = {
            'id': self.user.id,
            'public_id': str(self.user.public_id),
            'username': self.user.username,
            'email': self.user.email,
            'role': self.user.role,
        }
        return data


class ShelterProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShelterProfile
        fields = ['public_id', 'organization_name', 'license_no', 'pickup_address', 'latitude', 'longitude', 'is_active', 'created_at']
        read_only_fields = ['public_id', 'created_at']


class DeliveryProfileSerializer(serializers.ModelSerializer):
    shelter_name = serializers.ReadOnlyField(source='shelter.organization_name')

    class Meta:
        model = DeliveryProfile
        fields = ['public_id', 'shelter', 'shelter_name', 'vehicle_type', 'vehicle_number', 'is_available', 'created_at']
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
    password = serializers.CharField(write_only=True, min_length=6)

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
            'address'
        ]

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


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


