import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('shelter', 'Shelter'),
        ('delivery', 'Delivery'),
        ('customer', 'Customer'),
    )

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer')
    phone = models.CharField(max_length=20, blank=True, default='')
    address = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'users'
        managed = True
        constraints = [
            models.UniqueConstraint(
                Lower('email'),
                condition=~models.Q(email=''),
                name='unique_lower_email_non_empty',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.role = 'admin'
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.role == 'admin' or self.is_superuser

    @property
    def is_shelter(self):
        return self.role == 'shelter'

    @property
    def is_delivery(self):
        return self.role == 'delivery'

    @property
    def is_customer(self):
        return self.role == 'customer'

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class ShelterProfile(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, db_column='user_id', related_name='shelter_profile')
    organization_name = models.CharField(max_length=150)
    license_no = models.CharField(max_length=50, blank=True, default='')
    pickup_address = models.TextField()
    city = models.CharField(max_length=100, default='', blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'shelter_profiles'
        managed = True

    def __str__(self):
        return self.organization_name


class DeliveryProfile(models.Model):
    VEHICLE_CHOICES = (
        ('two_wheeler', 'Two Wheeler'),
        ('car', 'Car'),
        ('van', 'Van'),
    )

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, db_column='user_id', related_name='delivery_profile')
    shelter = models.ForeignKey(ShelterProfile, on_delete=models.CASCADE, db_column='shelter_id', related_name='delivery_team')
    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_CHOICES, default='car')
    vehicle_number = models.CharField(max_length=30)
    is_available = models.BooleanField(default=True)
    current_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'delivery_profiles'
        managed = True

    def __str__(self):
        return f"{self.user.username} - {self.vehicle_number} ({self.get_vehicle_type_display()})"


class Notification(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=150)
    message = models.TextField()
    notification_type = models.CharField(max_length=50, default='system')
    is_read = models.BooleanField(default=False)
    action_url = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        managed = True
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.recipient.username}: {self.title}"

