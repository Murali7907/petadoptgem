import uuid
from django.db import models

class Pet(models.Model):
    SPECIES_CHOICES = (
        ('dog', 'Dog'),
        ('cat', 'Cat'),
        ('other', 'Other'),
    )

    GENDER_CHOICES = (
        ('male', 'Male'),
        ('female', 'Female'),
    )

    STATUS_CHOICES = (
        ('available', 'Available'),
        ('pending', 'Pending Adoption'),
        ('adopted', 'Adopted'),
    )

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    shelter = models.ForeignKey('accounts.ShelterProfile', on_delete=models.CASCADE, db_column='shelter_id', related_name='pets')
    name = models.CharField(max_length=100)
    species = models.CharField(max_length=20, choices=SPECIES_CHOICES)
    breed = models.CharField(max_length=100, blank=True, default='')
    age_months = models.PositiveIntegerField()
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    size = models.CharField(max_length=20, default='medium', blank=True)
    description = models.TextField(blank=True, default='')
    adoption_fee = models.DecimalField(
        max_digits=8, 
        decimal_places=2, 
        default=100.00, 
        help_text="Medical care fee (e.g. ₹100 dog, ₹200 cat)"
    )
    image = models.ImageField(upload_to='pets/%Y/%m/', blank=True, null=True)
    is_vaccinated = models.BooleanField(default=False)
    is_neutered = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pets'
        managed = True

    def __str__(self):
        return f"{self.name} ({self.get_species_display()} - {self.shelter.organization_name})"

    @property
    def age_display(self) -> str:
        if self.age_months < 12:
            return f"{self.age_months} month{'s' if self.age_months > 1 else ''}"
        years = self.age_months // 12
        rem_months = self.age_months % 12
        if rem_months > 0:
            return f"{years} yr{'s' if years > 1 else ''} {rem_months} mo"
        return f"{years} year{'s' if years > 1 else ''}"


class PetImage(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, db_column='pet_id', related_name='images')
    image_url = models.CharField(max_length=255)
    caption = models.CharField(max_length=255, blank=True, default='')
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pet_images'
        managed = True

    def __str__(self):
        return f"Image for {self.pet.name}"

    @property
    def image(self):
        """Image URL or local storage path alias."""
        return self.image_url

    @image.setter
    def image(self, value):
        self.image_url = str(value)


class PetMedicalRecord(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, db_column='pet_id', related_name='medical_records')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    vet_name = models.CharField(max_length=150, blank=True, default='')
    record_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pet_medical_records'
        managed = True

    def __str__(self):
        return f"{self.title} - {self.pet.name}"

    @property
    def document(self):
        """Document URL or path attached in record description if present."""
        if not self.description:
            return None
        for line in self.description.splitlines():
            if line.strip().startswith("Document: "):
                return line.strip()[len("Document: "):].strip()
        return None

    @document.setter
    def document(self, url):
        clean = self.clinical_description
        if url:
            self.description = f"{clean}\nDocument: {str(url).strip()}".strip() if clean else f"Document: {str(url).strip()}"
        else:
            self.description = clean

    @property
    def clinical_description(self):
        """Clinical medical notes excluding the appended Document: reference tag."""
        if not self.description:
            return ""
        lines = [line for line in self.description.splitlines() if not line.strip().startswith("Document: ")]
        return "\n".join(lines).strip()


class FavoritePet(models.Model):
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='favorites')
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, related_name='favorited_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'favorite_pets'
        managed = True
        constraints = [
            models.UniqueConstraint(fields=['user', 'pet'], name='uq_user_favorite_pet')
        ]

    def __str__(self):
        return f"{self.user.username} favorited {self.pet.name}"

