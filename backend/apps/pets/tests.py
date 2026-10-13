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
            longitude=77.5946
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
            longitude=77.6000
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
            longitude=77.594600
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
            longitude=77.750000
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

    def test_postgis_proximity_sorting_orders_closer_shelter_pets_first(self):
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

        # Verify shelter details and coordinates
        self.assertIn('shelter', data)
        shelter_data = data['shelter']
        self.assertEqual(shelter_data['organization_name'], 'Central City Animal Rescue')
        self.assertEqual(shelter_data['city'], 'Bangalore')
        self.assertEqual(shelter_data['phone'], '+919876543210')
        self.assertIsNotNone(shelter_data['latitude'])
        self.assertIsNotNone(shelter_data['longitude'])

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
            pickup_address='100 Main St'
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
            longitude=77.5946
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
        url = f"/api/pets/{self.pet.id}/images/"
        fake_image = SimpleUploadedFile("bella_gallery.jpg", b"fake_image_binary_data", content_type="image/jpeg")
        response = self.client.post(url, {'image': fake_image, 'caption': 'Bella at park'}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(len(response.data) > 0)
        created_image = PetImage.objects.filter(pet=self.pet).first()
        self.assertIsNotNone(created_image)
        self.assertIn('/media/pets/', created_image.image_url)
        self.assertEqual(created_image.image, created_image.image_url)

    def test_pet_medical_record_document_upload_saves_to_storage(self):
        url = f"/api/pets/{self.pet.id}/medical/"
        fake_doc = SimpleUploadedFile("rabies_cert.pdf", b"fake_pdf_binary_content", content_type="application/pdf")
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
        url = f"/api/v1/shelter/pets/{self.pet.public_id}/images/"
        fake_image = SimpleUploadedFile("bella_action.jpg", b"fake_binary_jpeg", content_type="image/jpeg")
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
        fake_doc = SimpleUploadedFile("report.pdf", b"pdf_data", content_type="application/pdf")
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


