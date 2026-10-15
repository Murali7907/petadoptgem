import secrets
import uuid
from django.db import models

class PetDelivery(models.Model):
    STATUS_CHOICES = (
        ('assigned', 'Assigned'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
    )

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    application = models.OneToOneField('adoptions.AdoptionApplication', on_delete=models.CASCADE, db_column='application_id', related_name='delivery')
    driver = models.ForeignKey('accounts.DeliveryProfile', on_delete=models.SET_NULL, null=True, blank=True, db_column='driver_id', related_name='deliveries')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='assigned')
    
    pickup_otp = models.CharField(max_length=6, blank=True, default='')
    doorstep_otp = models.CharField(max_length=6, blank=True, default='')
    secret_otp = models.CharField(max_length=6, blank=True, default='') # Fallback alias

    # Cryptographic HMAC-SHA256 OTP hashes & dispatch timestamps
    pickup_otp_hash = models.CharField(max_length=64, null=True, blank=True)
    doorstep_otp_hash = models.CharField(max_length=64, null=True, blank=True)
    doorstep_otp_sent_at = models.DateTimeField(null=True, blank=True)

    pickup_otp_attempts = models.IntegerField(default=0)
    doorstep_otp_attempts = models.IntegerField(default=0)

    expires_at = models.DateTimeField(null=True, blank=True)
    doorstep_otp_expires_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)

    # Live tracking
    current_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_location_update = models.DateTimeField(null=True, blank=True)

    # Doorstep COD collection
    cash_collected = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    is_cash_settled_with_shelter = models.BooleanField(default=False)
    payment_status = models.CharField(max_length=20, default='pending')

    scheduled_date = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pet_deliveries'
        managed = True
        indexes = [
            models.Index(fields=['status'], name='pet_delivery_status_idx'),
            models.Index(fields=['driver', 'status'], name='pet_delivery_drv_status_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(pickup_otp_attempts__gte=0),
                name='check_pickup_otp_attempts_gte_0'
            ),
            models.CheckConstraint(
                condition=models.Q(doorstep_otp_attempts__gte=0),
                name='check_doorstep_otp_attempts_gte_0'
            ),
            models.CheckConstraint(
                condition=models.Q(status='delivered', delivered_at__isnull=False) | ~models.Q(status='delivered'),
                name='check_delivered_has_delivered_at'
            ),
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    @property
    def delivery_partner(self):
        return self.driver

    @property
    def shelter_id(self):
        return self.application.pet.shelter_id if self.application and self.application.pet else None

    def __str__(self):
        driver_str = self.driver.user.username if self.driver else "Unassigned"
        return f"Delivery #{self.id} for App #{self.application_id} ({driver_str}) - Status: {self.get_status_display()}"


class PaymentStatus:
    CREATED = 'created'
    PENDING = 'pending'
    CAPTURED = 'captured'
    FAILED = 'failed'
    # Legacy alias
    PAID = 'paid'

    CHOICES = (
        (CREATED, 'Created'),
        (PENDING, 'Pending'),
        (CAPTURED, 'Captured'),
        (FAILED, 'Failed'),
    )


class Payment(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    delivery = models.ForeignKey(PetDelivery, on_delete=models.CASCADE, null=True, blank=True, db_column='delivery_id', related_name='payments')
    application = models.ForeignKey('adoptions.AdoptionApplication', on_delete=models.CASCADE, null=True, blank=True, db_column='application_id', related_name='payments')
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    amount_in_paise = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=10, default='INR')
    payment_method = models.CharField(max_length=20, default='razorpay')
    status = models.CharField(max_length=20, default='created')
    razorpay_order_id = models.CharField(max_length=100, blank=True, default='')
    razorpay_payment_id = models.CharField(max_length=100, blank=True, default='')
    razorpay_signature = models.CharField(max_length=255, blank=True, default='')
    transaction_id = models.CharField(max_length=100, blank=True, default='')
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payments'
        managed = True

    def save(self, *args, **kwargs):
        from decimal import Decimal
        if self.amount_in_paise and not self.amount:
            self.amount = Decimal(self.amount_in_paise) / Decimal(100)
        elif self.amount and not self.amount_in_paise:
            self.amount_in_paise = int(self.amount * 100)
        if self.razorpay_payment_id and not self.transaction_id:
            self.transaction_id = self.razorpay_payment_id
        if self.delivery and not self.application and self.delivery.application:
            self.application = self.delivery.application
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Payment #{self.id} (₹{self.amount} / {self.amount_in_paise} paise) - Status: {self.status}"


class DeliveryLocationPing(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    delivery = models.ForeignKey(PetDelivery, on_delete=models.CASCADE, db_column='delivery_id', related_name='location_pings')
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    pinged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'delivery_location_pings'
        managed = True

    def __str__(self):
        return f"Ping at ({self.latitude}, {self.longitude}) for Delivery #{self.delivery_id}"
