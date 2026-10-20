import math
import uuid
from decimal import Decimal
from django.db import models

class AdoptionApplication(models.Model):
    STATUS_CHOICES = (
        ('submitted', 'Submitted'),
        ('reviewing', 'Reviewing'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
    )

    PAYMENT_METHOD_CHOICES = (
        ('unset', 'Unset'),
        ('online', 'Pay Online (Razorpay)'),
        ('cod', 'Cash on Delivery'),
    )

    PAYMENT_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
    )

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    pet = models.ForeignKey('pets.Pet', on_delete=models.CASCADE, db_column='pet_id', related_name='applications')
    applicant = models.ForeignKey('accounts.User', on_delete=models.CASCADE, db_column='applicant_id', related_name='applications')
    living_situation = models.CharField(max_length=255)
    has_other_pets = models.BooleanField(default=False)
    other_pets_description = models.TextField(blank=True, default='')
    id_proof_url = models.URLField(max_length=500, blank=True, default='')
    residence_proof_url = models.URLField(max_length=500, blank=True, default='')
    residence_type = models.CharField(max_length=50, blank=True, default='own_house')
    has_fenced_yard = models.BooleanField(default=False)
    dropoff_address = models.TextField(blank=True, default='')
    delivery_address = models.TextField(blank=True, default='')
    delivery_city = models.CharField(max_length=100, blank=True, default='')
    delivery_pincode = models.CharField(max_length=10, blank=True, default='')
    destination_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    destination_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    customer_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    customer_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    contact_phone = models.CharField(max_length=20)
    application_notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='submitted')

    # Fee calculation
    pet_care_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    base_delivery_fee = models.DecimalField(max_digits=8, decimal_places=2, default=50.00)
    distance_km = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    distance_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)

    # Payment fields
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='unset')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    razorpay_order_id = models.CharField(max_length=100, blank=True, default='')
    razorpay_payment_id = models.CharField(max_length=100, blank=True, default='')

    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'adoption_applications'
        managed = True
        ordering = ['-submitted_at', '-id']

    def save(self, *args, **kwargs):
        if not self.dropoff_address and self.delivery_address:
            self.dropoff_address = self.delivery_address
        elif not self.delivery_address and self.dropoff_address:
            self.delivery_address = self.dropoff_address

        if self.destination_latitude is not None and self.customer_latitude is None:
            self.customer_latitude = self.destination_latitude
        elif self.customer_latitude is not None and self.destination_latitude is None:
            self.destination_latitude = self.customer_latitude

        if self.destination_longitude is not None and self.customer_longitude is None:
            self.customer_longitude = self.destination_longitude
        elif self.customer_longitude is not None and self.destination_longitude is None:
            self.destination_longitude = self.customer_longitude

        prev_status = None
        if self.pk:
            orig = AdoptionApplication.objects.filter(pk=self.pk).values('status').first()
            if orig:
                prev_status = orig['status']

        super().save(*args, **kwargs)

        # Application code status convergence (independent of DB triggers)
        if (prev_status != 'approved') and self.status == 'approved':
            from apps.pets.models import Pet
            Pet.objects.filter(id=self.pet_id, status='available').update(status='pending')
        elif prev_status == 'approved' and self.status in ['rejected', 'cancelled']:
            has_other = AdoptionApplication.objects.filter(
                pet_id=self.pet_id,
                status__in=['approved', 'completed']
            ).exclude(id=self.id).exists()
            if not has_other:
                from apps.pets.models import Pet
                Pet.objects.filter(id=self.pet_id, status='pending').update(status='available')

    def calculate_total(self, per_km_rate=10):
        shelter = self.pet.shelter
        lat1 = float(shelter.latitude) if shelter.latitude else None
        lon1 = float(shelter.longitude) if shelter.longitude else None
        target_lat = self.destination_latitude if self.destination_latitude is not None else self.customer_latitude
        target_lon = self.destination_longitude if self.destination_longitude is not None else self.customer_longitude
        lat2 = float(target_lat) if target_lat is not None else None
        lon2 = float(target_lon) if target_lon is not None else None

        if None in (lat1, lon1, lat2, lon2):
            dist = 5.0
        else:
            R = 6371.0 # Earth radius in km
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = round(R * c, 2)

        self.distance_km = Decimal(str(dist))
        self.distance_fee = Decimal(str(round(dist * float(per_km_rate), 2)))
        self.pet_care_fee = self.pet.adoption_fee
        self.base_delivery_fee = Decimal('50.00')
        self.total_amount = self.pet_care_fee + self.base_delivery_fee + self.distance_fee
        self.save()

    @property
    def customer_id(self):
        return self.applicant_id

    @property
    def customer(self):
        return self.applicant

    def __str__(self):
        return f"App #{self.id} by {self.applicant.username} for {self.pet.name} ({self.get_status_display()})"


class AdoptionReview(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    application = models.OneToOneField(AdoptionApplication, on_delete=models.CASCADE, db_column='application_id', related_name='review')
    reviewer = models.ForeignKey('accounts.User', on_delete=models.CASCADE, db_column='reviewer_id', related_name='adoption_reviews')
    shelter_rating = models.IntegerField(default=5)
    shelter_review = models.TextField(blank=True, default='')
    driver_rating = models.IntegerField(default=5)
    driver_review = models.TextField(blank=True, default='')
    rating = models.IntegerField(default=5)
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'adoption_reviews'
        managed = True
        ordering = ['-created_at', '-id']

    def save(self, *args, **kwargs):
        if not self.rating:
            self.rating = self.shelter_rating
        if not self.comment:
            self.comment = self.shelter_review
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Review for App #{self.application_id} (Shelter: {self.shelter_rating}★, Driver: {self.driver_rating}★)"
