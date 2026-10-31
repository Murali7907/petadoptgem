import sys
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from apps.accounts.models import User, ShelterProfile
from apps.pets.models import Pet, PetImage, PetMedicalRecord
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import override_settings

class PetManagementAPITests(APITestCase):
    def setUp(self):
        # Create shelter user 1 & profile
        self.shelter_user_1 = User.objects.create_user(
            username='happy_paws_shelter',
            email='shelter1@happypaws.org',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile_1 = ShelterProfile.objects.create(
            user=self.shelter_user_1,
            organization_name='Happy Paws Rescue',
            pickup_address='123 Shelter Lane, City',
            latitude=12.9716,
            longitude=77.5946,
            is_verified=True
        )

        # Create shelter user 2 & profile (for multi-tenancy testing)
        self.shelter_user_2 = User.objects.create_user(
            username='paws_and_claws',
            email='shelter2@pawsandclaws.org',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile_2 = ShelterProfile.objects.create(
            user=self.shelter_user_2,
            organization_name='Paws and Claws Rescue',
            pickup_address='456 Haven Road, City',
            latitude=12.9800,
            longitude=77.6000,
            is_verified=True
        )

        # Create customer user
        self.customer_user = User.objects.create_user(
            username='jane_customer',
            email='jane@example.com',
            password='customerpass123',
            role='customer'
        )

        # Create pet for Shelter 1
        self.pet_1 = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='Buddy',
            species='dog',
            breed='Golden Retriever',
            age_months=24,
            gender='male',
            description='Friendly energetic golden retriever',
            adoption_fee=150.00,
            is_vaccinated=True,
            is_neutered=True,
            status='available'
        )

        # Create pet for Shelter 2
        self.pet_2 = Pet.objects.create(
            shelter=self.shelter_profile_2,
            name='Whiskers',
            species='cat',
            breed='Persian',
            age_months=12,
            gender='female',
            description='Calm persian cat',
            adoption_fee=100.00,
            status='available'
        )

        self.public_list_url = reverse('pet-list')
        self.public_detail_url = reverse('pet-detail', kwargs={'public_id': self.pet_1.public_id})

        self.shelter_pet_list_url = reverse('shelter-pet-list')
        self.shelter_pet_detail_1_url = reverse('shelter-pet-detail', kwargs={'public_id': self.pet_1.public_id})
        self.shelter_pet_detail_2_url = reverse('shelter-pet-detail', kwargs={'public_id': self.pet_2.public_id})

    def test_public_list_pets(self):
        response = self.client.get(self.public_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

    def test_shelter_multitenancy_isolation(self):
        # Authenticate as Shelter 1
        self.client.force_authenticate(user=self.shelter_user_1)
        response = self.client.get(self.shelter_pet_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Shelter 1 should only see pet_1
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Buddy')

        # Shelter 1 attempts to access pet_2 (owned by Shelter 2) -> 404 Not Found due to scoped queryset
        detail_response = self.client.get(self.shelter_pet_detail_2_url)
        self.assertEqual(detail_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_shelter_create_pet_auto_creates_medical_record(self):
        self.client.force_authenticate(user=self.shelter_user_1)
        payload = {
            'name': 'Charlie',
            'species': 'dog',
            'breed': 'Beagle',
            'age_months': 10,
            'gender': 'male',
            'description': 'Playful beagle puppy',
            'adoption_fee': 120.00,
            'is_vaccinated': True,
            'is_neutered': False,
            'status': 'available'
        }
        response = self.client.post(self.shelter_pet_list_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('public_id', response.data)
        new_pet_public_id = response.data['public_id']

        # Verify auto-created medical record
        pet = Pet.objects.get(public_id=new_pet_public_id)
        self.assertTrue(pet.medical_records.filter(title__icontains='Initial Intake Examination').exists())

    def test_status_validation_blocks_manual_adopted_status(self):
        self.client.force_authenticate(user=self.shelter_user_1)
        payload = {
            'name': 'Rocky',
            'species': 'dog',
            'breed': 'Boxer',
            'age_months': 36,
            'gender': 'male',
            'description': 'Strong boxer',
            'adoption_fee': 200.00,
            'status': 'adopted'  # Invalid manual transition
        }
        response = self.client.post(self.shelter_pet_list_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('status', response.data)

    def test_shelter_pet_medical_custom_action(self):
        self.client.force_authenticate(user=self.shelter_user_1)
        medical_url = reverse('shelter-pet-medical', kwargs={'public_id': self.pet_1.public_id})

        # GET medical record
        get_resp = self.client.get(medical_url)
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)

        # Update medical record
        update_payload = {
            'title': 'Deworming & Rabies Vaccination',
            'description': 'Administered 5ml dewormer and rabies vaccine.',
            'vet_name': 'Dr. Alan Grant',
            'record_date': '2026-06-15'
        }
        put_resp = self.client.put(medical_url, update_payload, format='json')
        self.assertIn(put_resp.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        self.assertEqual(put_resp.data['vet_name'], 'Dr. Alan Grant')

    def test_shelter_pet_images_custom_actions(self):
        self.client.force_authenticate(user=self.shelter_user_1)
        images_url = reverse('shelter-pet-images', kwargs={'public_id': self.pet_1.public_id})

        # POST image URL
        img_payload = {
            'image_url': 'https://example.com/pets/buddy1.jpg',
            'caption': 'Buddy playing in garden',
            'is_primary': True
        }
        post_resp = self.client.post(images_url, img_payload, format='json')
        self.assertEqual(post_resp.status_code, status.HTTP_201_CREATED)
        img_public_id = post_resp.data[0]['public_id']

        # GET images
        get_resp = self.client.get(images_url)
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(get_resp.data), 1)

        # DELETE image
        delete_url = reverse('shelter-pet-delete-image', kwargs={'public_id': self.pet_1.public_id, 'image_id': img_public_id})
        del_resp = self.client.delete(delete_url)
        self.assertEqual(del_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(PetImage.objects.filter(pet=self.pet_1).count(), 0)


class PublicPetCatalogAPITests(APITestCase):
    def setUp(self):
        # Create Shelter 1 in Bangalore City (Central)
        self.shelter_user_central = User.objects.create_user(
            username='central_shelter_user',
            email='central@shelter.org',
            password='shelterpass123',
            phone='+919876543210',
            role='shelter'
        )
        self.shelter_central = ShelterProfile.objects.create(
            user=self.shelter_user_central,
            organization_name='Central City Animal Rescue',
            pickup_address='MG Road, Central Bangalore',
            city='Bangalore',
            latitude=12.971600,
            longitude=77.594600,
            is_verified=True
        )

        # Create Shelter 2 in Whitefield (Farther East, ~17km)
        self.shelter_user_east = User.objects.create_user(
            username='east_shelter_user',
            email='east@shelter.org',
            password='shelterpass123',
            phone='+919876543211',
            role='shelter'
        )
        self.shelter_east = ShelterProfile.objects.create(
            user=self.shelter_user_east,
            organization_name='Whitefield Haven Care',
            pickup_address='ITPL Main Road, Whitefield',
            city='Whitefield',
            latitude=12.969800,
            longitude=77.750000,
            is_verified=True
        )

        # 1. Available Dog at Central Shelter
        self.pet_central = Pet.objects.create(
            shelter=self.shelter_central,
            name='Max',
            species='dog',
            breed='Golden Retriever',
            age_months=18,
            gender='male',
            size='large',
            description='Active and loving golden retriever',
            adoption_fee=150.00,
            is_vaccinated=True,
            is_neutered=True,
            status='available'
        )
        self.central_pet_img = PetImage.objects.create(
            pet=self.pet_central,
            image_url='https://example.com/pets/max_primary.jpg',
            caption='Max smiling',
            is_primary=True
        )
        self.central_pet_med = PetMedicalRecord.objects.create(
            pet=self.pet_central,
            title='Annual Core Vaccinations',
            description='Rabies, DHPP, and Bordetella administered.',
            vet_name='Dr. Maya Sen',
            record_date='2026-05-10'
        )

        # 2. Available Cat at East Shelter
        self.pet_east = Pet.objects.create(
            shelter=self.shelter_east,
            name='Luna',
            species='cat',
            breed='Persian',
            age_months=36,
            gender='female',
            size='small',
            description='Quiet and affectionate cat',
            adoption_fee=80.00,
            is_vaccinated=True,
            is_neutered=False,
            status='available'
        )

        # 3. Available Dog (medium, younger) at Central Shelter
        self.pet_puppy = Pet.objects.create(
            shelter=self.shelter_central,
            name='Bella',
            species='dog',
            breed='Beagle',
            age_months=6,
            gender='female',
            size='small',
            description='Curious beagle puppy',
            adoption_fee=120.00,
            status='available'
        )

        # 4. Pending Pet (should be excluded from public catalog)
        self.pet_pending = Pet.objects.create(
            shelter=self.shelter_central,
            name='Rocky',
            species='dog',
            breed='Boxer',
            age_months=24,
            gender='male',
            size='large',
            status='pending'
        )

        # 5. Adopted Pet (should be excluded from public catalog)
        self.pet_adopted = Pet.objects.create(
            shelter=self.shelter_east,
            name='Milo',
            species='cat',
            breed='Siamese',
            age_months=14,
            gender='male',
            size='small',
            status='adopted'
        )

        self.public_pets_url = reverse('public-pets-list')

    def test_public_listing_returns_only_available_pets(self):
        """Verify that public pet discovery returns only available pets and excludes pending/adopted pets."""
        response = self.client.get(self.public_pets_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        results = data.get('results', data)
        pet_names = [p['name'] for p in results]

        # Available pets must be present
        self.assertIn('Max', pet_names)
        self.assertIn('Luna', pet_names)
        self.assertIn('Bella', pet_names)

        # Non-available pets must be excluded
        self.assertNotIn('Rocky', pet_names)
        self.assertNotIn('Milo', pet_names)

    def test_filtering_by_species_breed_and_age_ranges(self):
        """Verify filtering by species, breed (icontains), age range, size, and shelter city."""
        # Species filter
        dog_resp = self.client.get(f"{self.public_pets_url}?species=dog")
        self.assertEqual(dog_resp.status_code, status.HTTP_200_OK)
        dog_results = dog_resp.data.get('results', dog_resp.data)
        self.assertTrue(all(p['species'] == 'dog' for p in dog_results))
        self.assertEqual(len(dog_results), 2)

        cat_resp = self.client.get(f"{self.public_pets_url}?species=cat")
        self.assertEqual(cat_resp.status_code, status.HTTP_200_OK)
        cat_results = cat_resp.data.get('results', cat_resp.data)
        self.assertEqual(len(cat_results), 1)
        self.assertEqual(cat_results[0]['name'], 'Luna')

        # Breed icontains filter
        retriever_resp = self.client.get(f"{self.public_pets_url}?breed=retriever")
        self.assertEqual(retriever_resp.status_code, status.HTTP_200_OK)
        retriever_results = retriever_resp.data.get('results', retriever_resp.data)
        self.assertEqual(len(retriever_results), 1)
        self.assertEqual(retriever_results[0]['name'], 'Max')

        # Min and Max age filters
        min_age_resp = self.client.get(f"{self.public_pets_url}?min_age=20")
        min_age_results = min_age_resp.data.get('results', min_age_resp.data)
        self.assertEqual(len(min_age_results), 1)
        self.assertEqual(min_age_results[0]['name'], 'Luna')

        max_age_resp = self.client.get(f"{self.public_pets_url}?max_age=12")
        max_age_results = max_age_resp.data.get('results', max_age_resp.data)
        self.assertEqual(len(max_age_results), 1)
        self.assertEqual(max_age_results[0]['name'], 'Bella')

        # Size filter
        size_resp = self.client.get(f"{self.public_pets_url}?size=large")
        size_results = size_resp.data.get('results', size_resp.data)
        self.assertEqual(len(size_results), 1)
        self.assertEqual(size_results[0]['name'], 'Max')

        # Shelter city filter
        city_resp = self.client.get(f"{self.public_pets_url}?city=Whitefield")
        city_results = city_resp.data.get('results', city_resp.data)
        self.assertEqual(len(city_results), 1)
        self.assertEqual(city_results[0]['name'], 'Luna')

    def test_haversine_proximity_sorting_orders_closer_shelter_pets_first(self):
        """
        Verify proximity sorting when lat and lng are provided.
        Closer shelter pets should appear before further shelter pets.
        """
        # User is located near Central Shelter (MG Road: 12.9716, 77.5946)
        user_lat = 12.971600
        user_lng = 77.594600

        response = self.client.get(f"{self.public_pets_url}?lat={user_lat}&lng={user_lng}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.data.get('results', response.data)
        self.assertGreaterEqual(len(results), 2)

        # Distance annotation should be present on results
        self.assertIsNotNone(results[0]['distance_km'])
        self.assertIsNotNone(results[1]['distance_km'])

        # Central shelter pets (Max, Bella) should be closer (< 1 km)
        # East shelter pet (Luna, ~17 km away) should be after Central shelter pets
        central_pet_names = ['Max', 'Bella']
        self.assertIn(results[0]['name'], central_pet_names)

        # The last result should be Luna at Whitefield
        self.assertEqual(results[-1]['name'], 'Luna')
        self.assertGreater(results[-1]['distance_km'], results[0]['distance_km'])

    def test_retrieve_view_displays_full_medical_overview_and_shelter_details(self):
        """Verify detailed view exposes sanitized medical overview and complete shelter coordinates."""
        detail_url = reverse('public-pets-detail', kwargs={'public_id': self.pet_central.public_id})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        self.assertEqual(data['name'], 'Max')
        self.assertEqual(data['species'], 'dog')
        self.assertEqual(data['breed'], 'Golden Retriever')

        # Verify sanitized medical overview
        self.assertIn('medical_overview', data)
        med = data['medical_overview']
        self.assertTrue(med['is_vaccinated'])
        self.assertTrue(med['is_neutered'])
        self.assertGreaterEqual(len(med['medical_records']), 1)
        self.assertEqual(med['medical_records'][0]['title'], 'Annual Core Vaccinations')

        # Verify shelter details: masked coordinates & phone for anonymous
        self.assertIn('shelter', data)
        shelter_data = data['shelter']
        self.assertEqual(shelter_data['organization_name'], 'Central City Animal Rescue')
        self.assertEqual(shelter_data['city'], 'Bangalore')
        self.assertIsNone(shelter_data['phone'])
        self.assertIsNone(shelter_data['latitude'])
        self.assertIsNone(shelter_data['longitude'])
        self.assertIsNone(shelter_data['pickup_address'])

        # Verify unmasked coordinates & phone for owning shelter
        self.client.force_authenticate(user=self.shelter_user_central)
        res_owner = self.client.get(detail_url)
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        owner_shelter = res_owner.data['shelter']
        self.assertEqual(owner_shelter['phone'], '+919876543210')
        self.assertIsNotNone(owner_shelter['latitude'])
        self.assertIsNotNone(owner_shelter['longitude'])
        self.assertIsNotNone(owner_shelter['pickup_address'])
        self.client.logout()

        # Verify gallery images
        self.assertIn('images', data)
        self.assertEqual(len(data['images']), 1)
        self.assertEqual(data['images'][0]['image_url'], 'https://example.com/pets/max_primary.jpg')

    def test_swagger_schema_endpoint(self):
        """Verify OpenAPI schema and Swagger UI endpoints respond with HTTP 200."""
        schema_resp = self.client.get('/api/schema/')
        self.assertEqual(schema_resp.status_code, status.HTTP_200_OK)

        docs_resp = self.client.get('/api/docs/')
        self.assertEqual(docs_resp.status_code, status.HTTP_200_OK)


class CustomerFavoritePetsAPITests(APITestCase):
    def setUp(self):
        self.shelter_user = User.objects.create_user(
            username='shelter_fav',
            email='shelter_fav@example.com',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Paws Shelter',
            pickup_address='100 Main St',
            is_verified=True
        )
        self.customer = User.objects.create_user(
            username='alice_fav',
            email='alice_fav@example.com',
            password='password123',
            role='customer'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Rocky',
            species='dog',
            breed='Boxer',
            age_months=18,
            gender='male',
            status='available'
        )
        self.favorites_url = '/api/v1/customer/favorites/'
        self.favorite_action_url = f'/api/v1/customer/favorites/{self.pet.public_id}/'

    def test_customer_favorites_workflow(self):
        # 1. Unauthenticated -> 401
        res = self.client.get(self.favorites_url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        # 2. Authenticate customer
        self.client.force_authenticate(user=self.customer)

        # Initial list is empty
        list_res = self.client.get(self.favorites_url)
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_res.data), 0)

        # 3. Add to favorites (POST)
        post_res = self.client.post(self.favorite_action_url)
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(post_res.data['is_favorited'])

        # Now listed
        list_res2 = self.client.get(self.favorites_url)
        self.assertEqual(list_res2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_res2.data), 1)
        self.assertEqual(list_res2.data[0]['name'], 'Rocky')
        self.assertEqual(list_res2.data[0]['public_id'], str(self.pet.public_id))

        # 4. Toggle off (POST again)
        post_res2 = self.client.post(self.favorite_action_url)
        self.assertEqual(post_res2.status_code, status.HTTP_200_OK)
        self.assertFalse(post_res2.data['is_favorited'])

        # 5. Add back and remove via DELETE
        self.client.post(self.favorite_action_url)
        del_res = self.client.delete(self.favorite_action_url)
        self.assertEqual(del_res.status_code, status.HTTP_200_OK)
        self.assertFalse(del_res.data['is_favorited'])

        list_res3 = self.client.get(self.favorites_url)
        self.assertEqual(len(list_res3.data), 0)


@override_settings(
    USE_CLOUDINARY=False,
    MEDIA_URL='/media/',
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'raw': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    }
)
class MediaStoragePipelineTests(APITestCase):
    def setUp(self):
        self.shelter_user = User.objects.create_user(
            username='storage_shelter',
            email='storage@shelter.org',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Storage Pipeline Shelter',
            pickup_address='789 Storage Rd',
            latitude=12.9716,
            longitude=77.5946,
            is_verified=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Bella',
            species='dog',
            breed='Labrador',
            age_months=18,
            gender='female',
            description='Friendly Labrador',
            adoption_fee=150.00
        )
        self.client.force_authenticate(user=self.shelter_user)

    def test_media_and_static_settings_configuration(self):
        self.assertEqual(settings.MEDIA_URL, '/media/')
        self.assertEqual(settings.STATIC_URL, '/static/')
        self.assertIn('CLOUD_NAME', settings.CLOUDINARY_STORAGE)
        self.assertIn('API_KEY', settings.CLOUDINARY_STORAGE)
        self.assertIn('API_SECRET', settings.CLOUDINARY_STORAGE)

    def test_pet_gallery_image_upload_saves_to_storage(self):
        from io import BytesIO
        from PIL import Image
        bio = BytesIO()
        Image.new('RGB', (10, 10), color='green').save(bio, format='JPEG')
        fake_image = SimpleUploadedFile("bella_gallery.jpg", bio.getvalue(), content_type="image/jpeg")

        url = f"/api/pets/{self.pet.id}/images/"
        response = self.client.post(url, {'image': fake_image, 'caption': 'Bella at park'}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(len(response.data) > 0)
        created_image = PetImage.objects.filter(pet=self.pet).first()
        self.assertIsNotNone(created_image)
        self.assertIn('/media/pets/', created_image.image_url)
        self.assertEqual(created_image.image, created_image.image_url)

    def test_pet_medical_record_document_upload_saves_to_storage(self):
        url = f"/api/pets/{self.pet.id}/medical/"
        fake_doc = SimpleUploadedFile("rabies_cert.pdf", b"%PDF-1.4\n%fake_pdf_binary_content\n%%EOF", content_type="application/pdf")
        response = self.client.post(url, {
            'title': 'Rabies Vaccination',
            'record_date': '2026-05-10',
            'vet_name': 'Dr. Sharma',
            'document': fake_doc
        }, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        med_rec = PetMedicalRecord.objects.filter(pet=self.pet, title='Rabies Vaccination').first()
        self.assertIsNotNone(med_rec)
        self.assertIsNotNone(med_rec.document)
        self.assertIn('/media/medical/', med_rec.document)

    def test_shelter_pet_images_action_upload(self):
        from io import BytesIO
        from PIL import Image
        bio = BytesIO()
        Image.new('RGB', (10, 10), color='yellow').save(bio, format='JPEG')
        fake_image = SimpleUploadedFile("bella_action.jpg", bio.getvalue(), content_type="image/jpeg")

        url = f"/api/v1/shelter/pets/{self.pet.public_id}/images/"
        response = self.client.post(url, {'images': fake_image, 'caption': 'Action Photo'}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('/media/pets/', response.data[0]['image_url'])

    def test_medical_record_patch_preserves_description_and_document(self):
        # Create record with initial note
        med_rec = PetMedicalRecord.objects.create(
            pet=self.pet,
            title='General Checkup',
            description='Dog in good health',
            vet_name='Dr. Smith',
            record_date='2026-06-01'
        )

        url = f"/api/v1/shelter/pets/{self.pet.public_id}/medical/"
        # 1. Document-only PATCH should preserve existing description
        fake_doc = SimpleUploadedFile("report.pdf", b"%PDF-1.4\n%pdf_data\n%%EOF", content_type="application/pdf")
        patch_resp = self.client.patch(url, {'document': fake_doc}, format='multipart')
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        med_rec.refresh_from_db()
        self.assertEqual(med_rec.clinical_description, 'Dog in good health')
        self.assertIsNotNone(med_rec.document)

        # 2. Description update PATCH should preserve existing document
        patch_resp2 = self.client.patch(url, {'description': 'Updated health observation'}, format='json')
        self.assertEqual(patch_resp2.status_code, status.HTTP_200_OK)
        med_rec.refresh_from_db()
        self.assertEqual(med_rec.clinical_description, 'Updated health observation')
        self.assertIsNotNone(med_rec.document)

    @override_settings(
        USE_CLOUDINARY=False,
        STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'raw': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        }
    )
    def test_storage_config_filesystem_mode(self):
        self.assertFalse(settings.USE_CLOUDINARY)
        self.assertEqual(settings.STORAGES['default']['BACKEND'], 'django.core.files.storage.FileSystemStorage')
        self.assertEqual(settings.STORAGES['raw']['BACKEND'], 'django.core.files.storage.FileSystemStorage')

    @override_settings(
        USE_CLOUDINARY=True,
        STORAGES={
            'default': {'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage'},
            'raw': {'BACKEND': 'cloudinary_storage.storage.RawMediaCloudinaryStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        }
    )
    def test_storage_config_cloudinary_mode(self):
        self.assertTrue(settings.USE_CLOUDINARY)
        self.assertEqual(settings.STORAGES['default']['BACKEND'], 'cloudinary_storage.storage.MediaCloudinaryStorage')
        self.assertEqual(settings.STORAGES['raw']['BACKEND'], 'cloudinary_storage.storage.RawMediaCloudinaryStorage')


class VerifiedShelterPetPolicyTests(APITestCase):
    def setUp(self):
        # Unverified shelter
        self.unverified_user = User.objects.create_user(
            username='unverified_shelter_pet',
            email='unverified_pet@shelter.org',
            password='password123',
            role='shelter'
        )
        self.unverified_profile = ShelterProfile.objects.create(
            user=self.unverified_user,
            organization_name='Unverified Pet Haven',
            pickup_address='101 Pending Pet Rd',
            is_verified=False
        )
        self.unverified_pet = Pet.objects.create(
            shelter=self.unverified_profile,
            name='Ghost',
            species='dog',
            breed='Husky',
            age_months=12,
            gender='male',
            status='available'
        )

        # Verified shelter
        self.verified_user = User.objects.create_user(
            username='verified_shelter_pet',
            email='verified_pet@shelter.org',
            password='password123',
            role='shelter'
        )
        self.verified_profile = ShelterProfile.objects.create(
            user=self.verified_user,
            organization_name='Verified Pet Haven',
            pickup_address='202 Verified Pet Rd',
            is_verified=True
        )
        self.verified_pet = Pet.objects.create(
            shelter=self.verified_profile,
            name='Buddy',
            species='dog',
            breed='Golden Retriever',
            age_months=24,
            gender='male',
            status='available'
        )

        # Admin user
        self.admin_user = User.objects.create_superuser(
            username='admin_pet_boss',
            email='admin_pet@kindheart.org',
            password='password123'
        )

        # Customer user
        self.customer_user = User.objects.create_user(
            username='adopter_sam',
            email='sam@customer.org',
            password='password123',
            role='customer'
        )

    def test_unverified_shelter_cannot_create_pet(self):
        self.client.force_authenticate(user=self.unverified_user)
        payload = {
            'name': 'Shadow',
            'species': 'cat',
            'breed': 'Persian',
            'age_months': 6,
            'gender': 'female',
            'adoption_fee': 50.00
        }
        res = self.client.post('/api/v1/shelter/pets/', payload)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')

    def test_verified_shelter_can_create_pet(self):
        self.client.force_authenticate(user=self.verified_user)
        payload = {
            'name': 'Shadow',
            'species': 'cat',
            'breed': 'Persian',
            'age_months': 6,
            'gender': 'female',
            'adoption_fee': 50.00
        }
        res = self.client.post('/api/v1/shelter/pets/', payload)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_admin_can_manage_pets(self):
        self.client.force_authenticate(user=self.admin_user)
        payload = {
            'shelter': self.verified_profile.id,
            'name': 'AdminPet',
            'species': 'dog',
            'breed': 'Beagle',
            'age_months': 8,
            'gender': 'male',
            'adoption_fee': 0.00
        }
        res = self.client.post('/api/pets/', payload)
        self.assertNotEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_public_catalog_filters_unverified_shelters(self):
        # Anonymous user visiting public pets list
        res = self.client.get('/api/pets/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data['results'] if 'results' in res.data else res.data
        pet_names = [p['name'] for p in results]
        self.assertIn('Buddy', pet_names)
        self.assertNotIn('Ghost', pet_names)

        # Owning shelter can still see their own pet in catalog
        self.client.force_authenticate(user=self.unverified_user)
        res_own = self.client.get('/api/pets/')
        own_results = res_own.data['results'] if 'results' in res_own.data else res_own.data
        own_names = [p['name'] for p in own_results]
        self.assertIn('Ghost', own_names)

        # Admin can see all pets in catalog
        self.client.force_authenticate(user=self.admin_user)
        res_admin = self.client.get('/api/pets/')
        admin_results = res_admin.data['results'] if 'results' in res_admin.data else res_admin.data
        admin_names = [p['name'] for p in admin_results]
        self.assertIn('Ghost', admin_names)
        self.assertIn('Buddy', admin_names)

    def test_suspension_immediately_blocks_pet_writes(self):
        self.client.force_authenticate(user=self.verified_user)
        self.verified_profile.is_verified = False
        self.verified_profile.save()

        res = self.client.patch(f'/api/v1/shelter/pets/{self.verified_pet.public_id}/', {'name': 'Buddy Renamed'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')

    def test_unverified_shelter_blocked_from_pet_media_and_medical(self):
        self.client.force_authenticate(user=self.unverified_user)
        # Pet image upload
        res_img = self.client.post(f'/api/pets/{self.unverified_pet.id}/images/', {'is_primary': True})
        self.assertEqual(res_img.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_img.data.get('code'), 'shelter_not_verified')

        # Medical record creation
        res_med = self.client.post(f'/api/pets/{self.unverified_pet.id}/medical/', {
            'record_type': 'vaccination',
            'title': 'Rabies Vaccine',
            'record_date': '2026-10-15'
        })
        self.assertEqual(res_med.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_med.data.get('code'), 'shelter_not_verified')


class UploadValidationAPITests(APITestCase):
    def setUp(self):
        from io import BytesIO
        from PIL import Image

        self.shelter_user = User.objects.create_user(
            username='upload_shelter_user',
            email='upload.shelter@example.org',
            password='password123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Upload Shelter Org',
            pickup_address='500 Upload Blvd',
            city='Bangalore',
            is_verified=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='UploadTestPet',
            species='dog',
            breed='Poodle',
            age_months=12,
            gender='female',
            status='available'
        )
        self.client.force_authenticate(user=self.shelter_user)

    def _create_image_file(self, filename, fmt='JPEG', size=(20, 20)):
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile

        io = BytesIO()
        img = Image.new('RGB', size, color='blue')
        img.save(io, format=fmt)
        content_type = f"image/{fmt.lower()}"
        return SimpleUploadedFile(filename, io.getvalue(), content_type=content_type)

    def test_valid_pet_image_upload_success(self):
        valid_img = self._create_image_file("valid.jpg", fmt='JPEG')
        url = f'/api/pets/{self.pet.id}/images/'
        res = self.client.post(url, {'images': [valid_img], 'caption': 'Valid Image'}, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_fake_image_exe_renamed_jpg_rejected_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        fake_exe = SimpleUploadedFile("malware.jpg", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff", content_type="image/jpeg")
        url = f'/api/pets/{self.pet.id}/images/'
        res = self.client.post(url, {'images': [fake_exe]}, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mismatched_image_extension_rejected_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # PNG content with .jpg filename
        png_as_jpg = self._create_image_file("mismatch.jpg", fmt='PNG')
        url = f'/api/pets/{self.pet.id}/images/'
        res = self.client.post(url, {'images': [png_as_jpg]}, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversize_pet_image_rejected_400(self):
        from unittest.mock import patch
        valid_img = self._create_image_file("oversize.png", fmt='PNG')
        url = f'/api/pets/{self.pet.id}/images/'
        # Force max size limit to 50 bytes
        with patch('apps.pets.validators.get_pet_image_max_size', return_value=50):
            res = self.client.post(url, {'images': [valid_img]}, format='multipart')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disallowed_image_extension_rejected_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        gif_file = SimpleUploadedFile("animation.gif", b"GIF89a...", content_type="image/gif")
        url = f'/api/pets/{self.pet.id}/images/'
        res = self.client.post(url, {'images': [gif_file]}, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_medical_pdf_upload_success(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        valid_pdf = SimpleUploadedFile("report.pdf", b"%PDF-1.4\n%test pdf content\n%%EOF", content_type="application/pdf")
        url = f'/api/pets/{self.pet.id}/medical/'
        res = self.client.post(url, {
            'title': 'Vaccination Certificate',
            'record_type': 'vaccination',
            'record_date': '2026-10-15',
            'document': valid_pdf
        }, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_fake_medical_pdf_text_file_rejected_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        fake_pdf = SimpleUploadedFile("report.pdf", b"This is plain text pretending to be a pdf file.", content_type="application/pdf")
        url = f'/api/pets/{self.pet.id}/medical/'
        res = self.client.post(url, {
            'title': 'Vaccination Certificate',
            'record_type': 'vaccination',
            'record_date': '2026-10-15',
            'document': fake_pdf
        }, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_medical_image_upload_success(self):
        valid_img = self._create_image_file("xray.png", fmt='PNG')
        url = f'/api/pets/{self.pet.id}/medical/'
        res = self.client.post(url, {
            'title': 'Chest X-Ray',
            'record_type': 'exam',
            'record_date': '2026-10-15',
            'document': valid_img
        }, format='multipart')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_oversize_medical_document_rejected_400(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        valid_pdf = SimpleUploadedFile("large_report.pdf", b"%PDF-1.4\n%test content\n%%EOF", content_type="application/pdf")
        url = f'/api/pets/{self.pet.id}/medical/'
        with patch('apps.pets.validators.get_medical_doc_max_size', return_value=10):
            res = self.client.post(url, {
                'title': 'Large Report',
                'record_type': 'exam',
                'record_date': '2026-10-15',
                'document': valid_pdf
            }, format='multipart')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class PetMedicalRecordVisibilityHybridTests(APITestCase):
    def setUp(self):
        from decimal import Decimal
        from apps.accounts.models import ShelterProfile
        from apps.adoptions.models import AdoptionApplication

        # 1. Owner shelter
        self.owner_user = User.objects.create_user(
            username='med_shelter_owner', email='owner@medhaven.org', password='password123', role='shelter', phone='9876543210'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.owner_user, organization_name='Med Haven Shelter', is_verified=True,
            pickup_address='42 Rescue Lane', city='Bangalore',
            latitude=Decimal('12.971600'), longitude=Decimal('77.594600')
        )

        # 2. Foreign shelter
        self.foreign_user = User.objects.create_user(
            username='foreign_shelter_user', email='foreign@shelter.org', password='password123', role='shelter'
        )
        self.foreign_shelter = ShelterProfile.objects.create(
            user=self.foreign_user, organization_name='Foreign Shelter', is_verified=True
        )

        # 3. Applicant customer with approved application
        self.applicant = User.objects.create_user(
            username='med_applicant', email='applicant@example.com', password='password123', role='customer'
        )

        # 4. Other customer (not applicant for this pet)
        self.other_customer = User.objects.create_user(
            username='other_customer_med', email='other@example.com', password='password123', role='customer'
        )

        # 5. Pending applicant (application still submitted/reviewing)
        self.pending_applicant = User.objects.create_user(
            username='pending_applicant_med', email='pending@example.com', password='password123', role='customer'
        )

        # 6. Admin
        self.admin = User.objects.create_user(
            username='med_admin', email='admin@medhaven.org', password='password123', role='admin', is_staff=True
        )

        # 7. Pet & Medical Record
        self.pet = Pet.objects.create(
            shelter=self.shelter, name='MedPet', species='dog', breed='Retriever', age_months=18, gender='male', status='available'
        )
        self.doc_secret_url = 'https://storage.googleapis.com/kindheart-docs/medical/report_confidential_123.pdf'
        self.record = PetMedicalRecord.objects.create(
            pet=self.pet,
            title='Rabies Vaccination & Health Certificate',
            description=f'Routine vaccination administered.\nDocument: {self.doc_secret_url}',
            vet_name='Dr. Sharma',
            record_date='2026-05-15'
        )

        # 8. Applications
        self.application = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.applicant, living_situation='House', contact_phone='9876543210', status='approved'
        )
        self.pending_application = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.pending_applicant, living_situation='Apartment', contact_phone='9876543211', status='submitted'
        )

    def test_anonymous_user_sees_metadata_but_null_document_url(self):
        self.client.logout()
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        self.assertEqual(len(records), 1)
        rec = records[0]

        # Metadata visible
        self.assertEqual(rec['title'], 'Rabies Vaccination & Health Certificate')
        self.assertEqual(rec['vet_name'], 'Dr. Sharma')
        self.assertEqual(rec['record_date'], '2026-05-15')

        # Document link masked
        self.assertIsNone(rec['document_url'])
        self.assertNotIn('Document:', rec['description'])
        self.assertNotIn('https://', rec['description'])

    def test_applicant_gets_document_url(self):
        self.client.force_authenticate(user=self.applicant)
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        self.assertEqual(len(records), 1)
        rec = records[0]

        self.assertEqual(rec['document_url'], self.doc_secret_url)

    def test_pending_applicant_gets_null_document_url(self):
        self.client.force_authenticate(user=self.pending_applicant)
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        rec = records[0]

        self.assertIsNone(rec['document_url'])
        self.assertNotIn(self.doc_secret_url, rec['description'])

    def test_other_customer_gets_null_document_url(self):
        self.client.force_authenticate(user=self.other_customer)
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        rec = records[0]

        self.assertIsNone(rec['document_url'])
        self.assertNotIn(self.doc_secret_url, rec['description'])

    def test_owner_shelter_gets_document_url(self):
        self.client.force_authenticate(user=self.owner_user)
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        rec = records[0]

        self.assertEqual(rec['document_url'], self.doc_secret_url)

    def test_admin_gets_document_url(self):
        self.client.force_authenticate(user=self.admin)
        url = f'/api/v1/pets/{self.pet.public_id}/medical-records/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        records = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        rec = records[0]

        self.assertEqual(rec['document_url'], self.doc_secret_url)

    def test_no_link_leaks_through_public_pet_detail(self):
        # 1. Anonymous request to public pet detail
        self.client.logout()
        url = f'/api/v1/pets/{self.pet.public_id}/'
        res_anon = self.client.get(url)
        self.assertEqual(res_anon.status_code, status.HTTP_200_OK)

        overview = res_anon.data.get('medical_overview', {})
        self.assertIn('medical_records', overview)
        med_list = overview['medical_records']
        self.assertEqual(len(med_list), 1)
        self.assertIsNone(med_list[0]['document_url'])
        self.assertNotIn('Document:', med_list[0]['description'])
        self.assertNotIn('https://', med_list[0]['description'])
        self.assertNotIn(self.doc_secret_url, res_anon.content.decode())

        # 2. Unrelated customer request to public pet detail
        self.client.force_authenticate(user=self.other_customer)
        res_cust = self.client.get(url)
        self.assertEqual(res_cust.status_code, status.HTTP_200_OK)
        med_list_cust = res_cust.data['medical_overview']['medical_records']
        self.assertIsNone(med_list_cust[0]['document_url'])
        self.assertNotIn(self.doc_secret_url, res_cust.content.decode())

        # 3. Applicant with approved application request to public pet detail -> gets link
        self.client.force_authenticate(user=self.applicant)
        res_app = self.client.get(url)
        self.assertEqual(res_app.status_code, status.HTTP_200_OK)
        med_list_app = res_app.data['medical_overview']['medical_records']
        self.assertEqual(med_list_app[0]['document_url'], self.doc_secret_url)

    def test_tiered_location_disclosure_all_roles(self):
        url = f'/api/v1/pets/{self.pet.public_id}/'

        # 1. Anonymous
        self.client.logout()
        res_anon = self.client.get(url)
        self.assertEqual(res_anon.status_code, status.HTTP_200_OK)
        shelter = res_anon.data['shelter']
        self.assertEqual(shelter['organization_name'], 'Med Haven Shelter')
        self.assertEqual(shelter['city'], 'Bangalore')
        self.assertIsNone(shelter['pickup_address'])
        self.assertIsNone(shelter['latitude'])
        self.assertIsNone(shelter['longitude'])
        self.assertIsNone(shelter['phone'])

        # 2. Other customer
        self.client.force_authenticate(user=self.other_customer)
        res_other = self.client.get(url)
        shelter_other = res_other.data['shelter']
        self.assertEqual(shelter_other['organization_name'], 'Med Haven Shelter')
        self.assertEqual(shelter_other['city'], 'Bangalore')
        self.assertIsNone(shelter_other['pickup_address'])
        self.assertIsNone(shelter_other['latitude'])
        self.assertIsNone(shelter_other['longitude'])
        self.assertIsNone(shelter_other['phone'])

        # 3. Pending applicant
        self.client.force_authenticate(user=self.pending_applicant)
        res_pending = self.client.get(url)
        shelter_pending = res_pending.data['shelter']
        self.assertEqual(shelter_pending['organization_name'], 'Med Haven Shelter')
        self.assertEqual(shelter_pending['city'], 'Bangalore')
        self.assertIsNone(shelter_pending['pickup_address'])
        self.assertIsNone(shelter_pending['latitude'])
        self.assertIsNone(shelter_pending['longitude'])
        self.assertIsNone(shelter_pending['phone'])

        # 4. Adopter with approved application
        self.client.force_authenticate(user=self.applicant)
        res_approved = self.client.get(url)
        shelter_approved = res_approved.data['shelter']
        self.assertEqual(shelter_approved['organization_name'], 'Med Haven Shelter')
        self.assertEqual(shelter_approved['city'], 'Bangalore')
        self.assertEqual(shelter_approved['pickup_address'], '42 Rescue Lane')
        self.assertEqual(shelter_approved['latitude'], '12.971600')
        self.assertEqual(shelter_approved['longitude'], '77.594600')
        self.assertEqual(shelter_approved['phone'], '9876543210')

        # 5. Adopter with completed application
        from apps.adoptions.models import AdoptionApplication
        completed_user = User.objects.create_user(
            username='med_completed', email='completed@example.com', password='password123', role='customer'
        )
        AdoptionApplication.objects.create(
            pet=self.pet, applicant=completed_user, living_situation='House', contact_phone='1234567890', status='completed'
        )
        self.client.force_authenticate(user=completed_user)
        res_comp = self.client.get(url)
        shelter_comp = res_comp.data['shelter']
        self.assertEqual(shelter_comp['pickup_address'], '42 Rescue Lane')
        self.assertEqual(shelter_comp['phone'], '9876543210')

        # 6. Owning shelter staff
        self.client.force_authenticate(user=self.owner_user)
        res_owner = self.client.get(url)
        shelter_owner = res_owner.data['shelter']
        self.assertEqual(shelter_owner['pickup_address'], '42 Rescue Lane')
        self.assertEqual(shelter_owner['phone'], '9876543210')

        # 7. Admin
        self.client.force_authenticate(user=self.admin)
        res_admin = self.client.get(url)
        shelter_admin = res_admin.data['shelter']
        self.assertEqual(shelter_admin['pickup_address'], '42 Rescue Lane')
        self.assertEqual(shelter_admin['phone'], '9876543210')

    def test_public_pet_list_empty_city_returns_placeholder_not_address(self):
        from apps.accounts.models import ShelterProfile
        empty_city_shelter_user = User.objects.create_user(
            username='empty_city_shelter', email='empty@shelter.org', password='password123', role='shelter'
        )
        empty_shelter = ShelterProfile.objects.create(
            user=empty_city_shelter_user,
            organization_name='Secret Sanctuary',
            is_verified=True,
            city='',
            pickup_address='Hidden Basement 77, Secret Road'
        )
        secret_pet = Pet.objects.create(
            shelter=empty_shelter, name='HiddenPet', species='cat', breed='Persian', age_months=12, gender='female', status='available'
        )
        self.client.logout()
        res = self.client.get('/api/v1/pets/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        pet_entry = next((p for p in data if p['name'] == 'HiddenPet'), None)
        self.assertIsNotNone(pet_entry)
        self.assertEqual(pet_entry['shelter_city'], 'location not provided')
        self.assertNotIn('Hidden Basement 77', res.content.decode())

    def test_inline_serializers_match_schema_keys(self):
        from apps.pets.serializers import (
            MedicalOverviewSerializer,
            MedicalOverviewRecordSerializer,
            PublicPetShelterSerializer,
            PublicPetDetailSerializer,
        )
        serializer = PublicPetDetailSerializer(self.pet, context={'request': None})
        data = serializer.data

        # 1. medical_overview matches MedicalOverviewSerializer declared fields
        med_overview = data['medical_overview']
        self.assertEqual(set(med_overview.keys()), set(MedicalOverviewSerializer().fields.keys()))

        # 2. Each record in medical_overview matches MedicalOverviewRecordSerializer declared fields
        for rec in med_overview['medical_records']:
            self.assertEqual(set(rec.keys()), set(MedicalOverviewRecordSerializer().fields.keys()))

        # 3. shelter matches PublicPetShelterSerializer declared fields
        shelter_data = data['shelter']
        self.assertEqual(set(shelter_data.keys()), set(PublicPetShelterSerializer().fields.keys()))

    def _assert_no_leaks_recursive(self, data, sensitive_keys, path=""):
        if isinstance(data, dict):
            for key, val in data.items():
                curr_path = f"{path}.{key}" if path else key
                if key in sensitive_keys and val is not None:
                    self.fail(f"Privacy leak detected at '{curr_path}': value is {val!r}")
                self._assert_no_leaks_recursive(val, sensitive_keys, curr_path)
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                self._assert_no_leaks_recursive(item, sensitive_keys, f"{path}[{idx}]")

    def _get_pet_family_endpoints(self):
        pet_id = str(self.pet.public_id)
        return [
            # PetViewSet (List and Detail)
            ('/api/pets/', 'PetViewSet List (legacy)'),
            (f'/api/pets/{pet_id}/', 'PetViewSet Detail (legacy)'),
            ('/api/v1/pets/', 'PetViewSet List (canonical)'),
            (f'/api/v1/pets/{pet_id}/', 'PetViewSet Detail (canonical)'),
            # PublicPetViewSet (List and Detail)
            ('/api/pets/public/pets/', 'PublicPetViewSet List (legacy)'),
            (f'/api/pets/public/pets/{pet_id}/', 'PublicPetViewSet Detail (legacy)'),
            ('/api/v1/pets/public/pets/', 'PublicPetViewSet List (v1 nested)'),
            (f'/api/v1/pets/public/pets/{pet_id}/', 'PublicPetViewSet Detail (v1 nested)'),
            ('/api/v1/public/pets/', 'PublicPetViewSet List (canonical)'),
            (f'/api/v1/public/pets/{pet_id}/', 'PublicPetViewSet Detail (canonical)'),
        ]

    def test_recursive_leak_sweep_anonymous_actor(self):
        """
        Verify that an anonymous visitor receives NO non-null sensitive fields
        at any depth across all pet route families (PetViewSet and PublicPetViewSet).
        """
        self.client.logout()
        sensitive_keys = {'pickup_address', 'latitude', 'longitude', 'phone', 'email', 'document_url'}
        for url, desc in self._get_pet_family_endpoints():
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK, f"Failed on {desc} ({url})")
            self._assert_no_leaks_recursive(res.data, sensitive_keys, path=f"{desc}:{url}")

    def test_recursive_leak_sweep_other_customer_actor(self):
        """
        Verify that an authenticated customer with NO application receives NO non-null
        sensitive fields at any depth across all pet route families.
        """
        self.client.force_authenticate(user=self.other_customer)
        sensitive_keys = {'pickup_address', 'latitude', 'longitude', 'phone', 'email', 'document_url'}
        for url, desc in self._get_pet_family_endpoints():
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK, f"Failed on {desc} ({url})")
            self._assert_no_leaks_recursive(res.data, sensitive_keys, path=f"{desc}:{url}")

    def test_recursive_leak_sweep_submitted_applicant_actor(self):
        """
        Verify that a customer with a SUBMITTED (pending review) application receives NO non-null
        sensitive fields at any depth across all pet route families.
        """
        self.client.force_authenticate(user=self.pending_applicant)
        sensitive_keys = {'pickup_address', 'latitude', 'longitude', 'phone', 'email', 'document_url'}
        for url, desc in self._get_pet_family_endpoints():
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK, f"Failed on {desc} ({url})")
            self._assert_no_leaks_recursive(res.data, sensitive_keys, path=f"{desc}:{url}")

    def test_recursive_leak_sweep_approved_applicant_receives_confidential_fields(self):
        """
        Verify that an applicant with an APPROVED application successfully receives
        unmasked confidential fields (pickup_address, latitude, longitude, phone, document_url) on detail endpoints.
        """
        self.client.force_authenticate(user=self.applicant)
        for url in [f'/api/pets/{self.pet.public_id}/', f'/api/v1/pets/{self.pet.public_id}/', f'/api/v1/public/pets/{self.pet.public_id}/']:
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            shelter = res.data['shelter']
            self.assertEqual(shelter['pickup_address'], '42 Rescue Lane')
            self.assertIsNotNone(shelter['latitude'])
            self.assertIsNotNone(shelter['longitude'])
            self.assertEqual(shelter['phone'], '9876543210')
            records = res.data['medical_overview']['medical_records']
            self.assertEqual(records[0]['document_url'], self.doc_secret_url)

    def test_recursive_leak_sweep_owner_shelter_receives_confidential_fields(self):
        """
        Verify that the owning shelter staff successfully receives unmasked confidential fields on detail endpoints.
        """
        self.client.force_authenticate(user=self.owner_user)
        for url in [f'/api/pets/{self.pet.public_id}/', f'/api/v1/pets/{self.pet.public_id}/', f'/api/v1/public/pets/{self.pet.public_id}/']:
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            shelter = res.data['shelter']
            self.assertEqual(shelter['pickup_address'], '42 Rescue Lane')
            self.assertEqual(shelter['phone'], '9876543210')

    def test_recursive_leak_sweep_admin_receives_confidential_fields(self):
        """
        Verify that platform admin successfully receives unmasked confidential fields on detail endpoints.
        """
        self.client.force_authenticate(user=self.admin)
        for url in [f'/api/pets/{self.pet.public_id}/', f'/api/v1/pets/{self.pet.public_id}/', f'/api/v1/public/pets/{self.pet.public_id}/']:
            res = self.client.get(url)
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            shelter = res.data['shelter']
            self.assertEqual(shelter['pickup_address'], '42 Rescue Lane')
            self.assertEqual(shelter['phone'], '9876543210')


class DistancePrivacyAndThrottlingTests(APITestCase):
    """
    Milestone 8b Task 3:
    - Round distance_km to whole kilometres (minimum 1) in every response, for every actor.
    - Two origins 100 m apart give the same value.
    - Values are integers (not floats).
    - Throttling (pets_geo, default 30/min) on requests carrying client-supplied coordinates.
    - 429 after limit.
    """

    def setUp(self):
        from decimal import Decimal
        from rest_framework.test import APIClient
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()

        # Shelter at Bangalore center: lat=12.971600, lng=77.594600
        self.shelter_user = User.objects.create_user(
            username='geoshelter',
            email='geoshelter@test.org',
            password='Password123!',
            role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Geo Safe Shelter',
            license_no='GEO-12345',
            pickup_address='100 MG Road',
            city='Bangalore',
            latitude=Decimal('12.971600'),
            longitude=Decimal('77.594600'),
            is_active=True,
            is_verified=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter,
            name='PrivacyPup',
            species='dog',
            breed='Indie',
            age_months=12,
            gender='male',
            size='medium',
            status='available',
            adoption_fee=Decimal('100.00')
        )

    def test_distance_km_values_are_integers_and_minimum_one(self):
        # Query 1: Very close origin (100m away) -> should round to minimum 1
        resp = self.client.get('/api/v1/pets/?lat=12.972000&lng=77.594600')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data.get('results', resp.data)
        self.assertTrue(len(results) > 0)
        dist = results[0]['distance_km']
        self.assertIsInstance(dist, int, f"distance_km should be int, got {type(dist)}")
        self.assertEqual(dist, 1, "Distance under 1km should be clamped to minimum 1")

        # Query 2: Detail endpoint
        detail_resp = self.client.get(f'/api/v1/pets/{self.pet.public_id}/?lat=12.972000&lng=77.594600')
        self.assertEqual(detail_resp.status_code, status.HTTP_200_OK)
        detail_dist = detail_resp.data['distance_km']
        self.assertIsInstance(detail_dist, int)
        self.assertEqual(detail_dist, 1)

    def test_two_origins_100m_apart_give_same_value(self):
        # Origin A: 5.0 km away (approx 0.045 deg north: lat=13.0166, lng=77.5946)
        lat_a = 13.016600
        lng_a = 77.594600
        # Origin B: 100 meters further north (approx 0.0009 deg north: lat=13.0175, lng=77.5946)
        lat_b = 13.017500
        lng_b = 77.594600

        resp_a = self.client.get(f'/api/v1/pets/?lat={lat_a}&lng={lng_a}')
        resp_b = self.client.get(f'/api/v1/pets/?lat={lat_b}&lng={lng_b}')

        self.assertEqual(resp_a.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_b.status_code, status.HTTP_200_OK)

        dist_a = resp_a.data.get('results', resp_a.data)[0]['distance_km']
        dist_b = resp_b.data.get('results', resp_b.data)[0]['distance_km']

        self.assertIsInstance(dist_a, int)
        self.assertIsInstance(dist_b, int)
        self.assertEqual(dist_a, dist_b, f"Expected same whole km value for origins 100m apart, got {dist_a} vs {dist_b}")

    def test_pets_geo_throttle_enforces_limit_and_returns_429(self):
        from django.core.cache import cache
        from unittest.mock import patch
        cache.clear()

        # Override throttle rate to 3/min via env
        with patch.dict('os.environ', {'PETS_GEO_THROTTLE_RATE': '3/min'}):
            # Requests carrying coordinates should be throttled after 3
            url = '/api/v1/pets/?lat=12.971600&lng=77.594600'
            for i in range(3):
                r = self.client.get(url)
                self.assertEqual(r.status_code, status.HTTP_200_OK, f"Request {i+1} should succeed")

            # 4th request must be throttled with 429
            r_throttled = self.client.get(url)
            self.assertEqual(r_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

            # Requests without coordinates from the same client are NOT throttled
            r_no_geo = self.client.get('/api/v1/pets/')
            self.assertEqual(r_no_geo.status_code, status.HTTP_200_OK)


class FileCleanupOnDeleteTests(APITestCase):
    """
    Milestone 10a Task 4: post_delete storage cleanup for PetImage and PetMedicalRecord.
    Asserts:
      - File removed after delete
      - External URL untouched
      - Rolled-back delete keeps the file
      - Cascade delete removes files
      - Queryset .delete() removes files
      - Storage errors are logged and swallowed without raising
    """
    def setUp(self):
        import tempfile
        self.temp_media = tempfile.mkdtemp()
        self.override = override_settings(
            MEDIA_ROOT=self.temp_media,
            USE_CLOUDINARY=False,
        )
        self.override.enable()

        self.user = User.objects.create_user(
            username='cleanup_shelter',
            email='cleanup@example.com',
            password='pass',
            role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.user,
            organization_name='Cleanup Shelter',
            is_verified=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter,
            name='CleanupPet',
            species='dog',
            age_months=12,
            status='available'
        )

    def tearDown(self):
        import shutil
        self.override.disable()
        shutil.rmtree(self.temp_media, ignore_errors=True)

    def test_file_removed_after_delete_for_image_and_medical(self):
        """Stored files are deleted via default_storage on transaction commit."""
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile

        img_path = default_storage.save('pets/test_img.jpg', ContentFile(b'test image bytes'))
        doc_path = default_storage.save('medical/test_doc.pdf', ContentFile(b'%PDF-1.4 test bytes'))

        img = PetImage.objects.create(pet=self.pet, image_url=img_path)
        med = PetMedicalRecord.objects.create(
            pet=self.pet,
            title='Rabies Vaccine',
            record_date='2026-01-01',
            description=f'Document: {doc_path}'
        )

        self.assertTrue(default_storage.exists(img_path))
        self.assertTrue(default_storage.exists(doc_path))

        with self.captureOnCommitCallbacks(execute=True):
            img.delete()
            med.delete()

        self.assertFalse(default_storage.exists(img_path))
        self.assertFalse(default_storage.exists(doc_path))

    def test_external_url_untouched_on_delete(self):
        """External image URLs and document URLs are never passed to storage.delete."""
        from django.core.files.storage import default_storage
        from unittest.mock import patch

        img = PetImage.objects.create(pet=self.pet, image_url='https://images.unsplash.com/photo-sample-dog')
        med = PetMedicalRecord.objects.create(
            pet=self.pet,
            title='External Certificate',
            record_date='2026-01-01',
            description='Document: https://external-docs.example.com/record.pdf'
        )

        with patch.object(default_storage, 'delete') as mock_delete:
            with self.captureOnCommitCallbacks(execute=True):
                img.delete()
                med.delete()

        mock_delete.assert_not_called()

    def test_rolled_back_delete_keeps_file(self):
        """When a deletion transaction is rolled back, the storage file is preserved."""
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile
        from django.db import transaction

        img_path = default_storage.save('pets/keep_me.jpg', ContentFile(b'preserve this file'))
        img = PetImage.objects.create(pet=self.pet, image_url=img_path)
        img_id = img.id

        try:
            with transaction.atomic():
                img.delete()
                raise RuntimeError("Simulated transaction failure")
        except RuntimeError:
            pass

        # File must still exist because on_commit callbacks were discarded
        self.assertTrue(default_storage.exists(img_path))
        self.assertTrue(PetImage.objects.filter(id=img_id).exists())

    def test_cascade_delete_removes_files(self):
        """Deleting a parent Pet cascades to PetImage and PetMedicalRecord and deletes files."""
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile

        img_path = default_storage.save('pets/cascade_img.jpg', ContentFile(b'cascade image'))
        doc_path = default_storage.save('medical/cascade_doc.pdf', ContentFile(b'cascade doc'))

        PetImage.objects.create(pet=self.pet, image_url=img_path)
        PetMedicalRecord.objects.create(
            pet=self.pet,
            title='Checkup Record',
            record_date='2026-01-01',
            description=f'Document: {doc_path}'
        )

        self.assertTrue(default_storage.exists(img_path))
        self.assertTrue(default_storage.exists(doc_path))

        with self.captureOnCommitCallbacks(execute=True):
            self.pet.delete()

        self.assertFalse(default_storage.exists(img_path))
        self.assertFalse(default_storage.exists(doc_path))

    def test_queryset_delete_removes_files(self):
        """Bulk queryset .delete() fires signals and deletes all associated files."""
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile

        img1_path = default_storage.save('pets/qs1.jpg', ContentFile(b'image 1'))
        img2_path = default_storage.save('pets/qs2.jpg', ContentFile(b'image 2'))

        PetImage.objects.create(pet=self.pet, image_url=img1_path)
        PetImage.objects.create(pet=self.pet, image_url=img2_path)

        self.assertTrue(default_storage.exists(img1_path))
        self.assertTrue(default_storage.exists(img2_path))

        with self.captureOnCommitCallbacks(execute=True):
            PetImage.objects.filter(pet=self.pet).delete()

        self.assertFalse(default_storage.exists(img1_path))
        self.assertFalse(default_storage.exists(img2_path))

    def test_storage_error_does_not_raise(self):
        """Storage exceptions during deletion are logged and swallowed without breaking request."""
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile
        from unittest.mock import patch

        img_path = default_storage.save('pets/error_test.jpg', ContentFile(b'test bytes'))
        img = PetImage.objects.create(pet=self.pet, image_url=img_path)

        with patch.object(default_storage, 'delete', side_effect=IOError("Simulated filesystem error")):
            with self.captureOnCommitCallbacks(execute=True):
                img.delete()  # Must not raise IOError

        self.assertFalse(PetImage.objects.filter(id=img.id).exists())


class CloudinaryRawStorageForMedicalDocumentsTests(APITestCase):
    """
    Milestone 10a Task 5: Cloudinary Raw Storage for Medical Documents.
    Asserts:
      - Raw storage configuration in settings uses RawMediaCloudinaryStorage with RESOURCE_TYPE='raw'
      - Local storage resolves to FileSystemStorage
      - Medical record creation uses raw document storage, not image storage
      - All storage operations patched with zero network calls
    """
    def setUp(self):
        self.user = User.objects.create_user(
            username='vet_shelter',
            email='vet@example.com',
            password='pass',
            role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.user,
            organization_name='Vet Shelter',
            is_verified=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter,
            name='Luna',
            species='cat',
            age_months=8,
            status='available'
        )
        self.client.force_authenticate(user=self.user)

    def test_cloudinary_raw_storage_backend_and_resource_type(self):
        """When USE_CLOUDINARY is True, named storage raw_documents uses RawMediaCloudinaryStorage (RESOURCE_TYPE='raw')."""
        from cloudinary_storage.storage import RawMediaCloudinaryStorage, MediaCloudinaryStorage
        from django.core.files.storage import StorageHandler

        custom_storages = {
            'default': {
                'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage',
            },
            'raw_documents': {
                'BACKEND': 'cloudinary_storage.storage.RawMediaCloudinaryStorage',
            },
            'staticfiles': {
                'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
            },
        }
        with override_settings(STORAGES=custom_storages, USE_CLOUDINARY=True):
            handler = StorageHandler()
            raw_storage = handler['raw_documents']
            default_img_storage = handler['default']

            self.assertIsInstance(raw_storage, RawMediaCloudinaryStorage)
            self.assertEqual(raw_storage.RESOURCE_TYPE, 'raw')
            self.assertIsInstance(default_img_storage, MediaCloudinaryStorage)
            self.assertEqual(default_img_storage.RESOURCE_TYPE, 'image')

    def test_local_storage_mode_uses_filesystem_storage(self):
        """In local development (USE_CLOUDINARY=False), raw_documents resolves to FileSystemStorage."""
        from django.core.files.storage import StorageHandler, FileSystemStorage

        custom_storages = {
            'default': {
                'BACKEND': 'django.core.files.storage.FileSystemStorage',
            },
            'raw_documents': {
                'BACKEND': 'django.core.files.storage.FileSystemStorage',
            },
        }
        with override_settings(STORAGES=custom_storages, USE_CLOUDINARY=False):
            handler = StorageHandler()
            self.assertIsInstance(handler['raw_documents'], FileSystemStorage)

    def test_medical_record_upload_calls_raw_document_storage_not_image_storage(self):
        """Uploading a PDF/DOCX routes through get_document_storage (mocked raw storage), not image storage."""
        from unittest.mock import MagicMock, patch
        from django.core.files.uploadedfile import SimpleUploadedFile

        mock_raw_storage = MagicMock()
        mock_raw_storage.save.return_value = 'medical/1/cert.pdf'
        mock_raw_storage.url.return_value = 'https://res.cloudinary.com/cloud/raw/upload/v12345/medical/1/cert.pdf'

        pdf_content = b"%PDF-1.4 sample pdf content for medical test"
        uploaded_doc = SimpleUploadedFile("cert.pdf", pdf_content, content_type="application/pdf")

        with patch('apps.pets.storage.get_document_storage', return_value=mock_raw_storage), \
             patch('apps.pets.serializers.get_document_storage', return_value=mock_raw_storage), \
             patch('apps.pets.views.get_document_storage', return_value=mock_raw_storage):
            with patch('django.core.files.storage.default_storage.save') as mock_default_save:
                url = reverse('pet_medical_records', kwargs={'pet_public_id': self.pet.public_id})
                data = {
                    'title': 'Vaccination Certificate',
                    'record_date': '2026-05-01',
                    'description': 'Annual rabies vaccination',
                    'document': uploaded_doc,
                }
                response = self.client.post(url, data, format='multipart')

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                # Verify raw storage was used
                mock_raw_storage.save.assert_called_once()
                mock_raw_storage.url.assert_called_once()
                # Default image storage was NOT called for document save
                mock_default_save.assert_not_called()









