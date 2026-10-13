import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from apps.accounts.models import User, ShelterProfile, DeliveryProfile
from apps.pets.models import Pet, PetImage, PetMedicalRecord
from apps.adoptions.models import AdoptionApplication, AdoptionReview
from apps.logistics.models import PetDelivery, DeliveryLocationPing


class EndToEndContractFlowTestCase(TestCase):
    """
    Validates all five apps according to user specification:
    1. /api/auth/register/, login/, drivers/create/, me/
    2. /api/pets/, /api/pets/<id>/, /api/pets/<id>/images/, /api/pets/<id>/medical/
    3. /api/adoptions/apply/, my-applications/, shelter-applications/, <id>/status/
    4. /api/deliveries/assign/, driver-active/, <id>/ping/, <id>/track/, <id>/verify-otp/
    5. /api/reviews/
    """
    def setUp(self):
        self.client = APIClient()

    def test_full_platform_lifecycle(self):
        # ---------------------------------------------------------
        # 1. ACCOUNTS APP
        # ---------------------------------------------------------
        # 1.1 Register Customer
        cust_payload = {
            'username': 'e2e_customer',
            'email': 'e2e_customer@example.com',
            'password': 'StrongPassword123!',
            'role': 'customer',
            'phone': '9876543210',
            'address': '100 Koramangala 4th Block, Bangalore'
        }
        res_cust_reg = self.client.post('/api/auth/register/', data=cust_payload, format='json')
        self.assertEqual(res_cust_reg.status_code, status.HTTP_201_CREATED)
        customer_user = User.objects.get(username='e2e_customer')

        # 1.2 Register Shelter
        shelter_payload = {
            'username': 'e2e_shelter',
            'email': 'e2e_shelter@example.com',
            'password': 'StrongPassword123!',
            'role': 'shelter',
            'phone': '9876543211',
            'organization_name': 'KindHeart Hope Shelter',
            'license_no': 'SHELTER-BLR-001',
            'pickup_address': '50 Indiranagar 100ft Rd, Bangalore',
            'city': 'Bangalore',
            'latitude': 12.9784,
            'longitude': 77.6408
        }
        res_shelter_reg = self.client.post('/api/auth/register/', data=shelter_payload, format='json')
        self.assertEqual(res_shelter_reg.status_code, status.HTTP_201_CREATED)
        shelter_user = User.objects.get(username='e2e_shelter')
        shelter_profile = ShelterProfile.objects.get(user=shelter_user)

        # 1.3 Shelter Login (JWT token pair check)
        res_login = self.client.post('/api/auth/login/', data={
            'username': 'e2e_shelter',
            'password': 'StrongPassword123!'
        }, format='json')
        self.assertEqual(res_login.status_code, status.HTTP_200_OK)
        self.assertIn('access', res_login.data)
        self.assertIn('refresh', res_login.data)
        shelter_access_token = res_login.data['access']
        self.assertEqual(res_login.data['role'], 'shelter')
        self.assertEqual(res_login.data['shelter_id'], shelter_profile.id)

        # 1.4 Get Profile (/api/auth/me/)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {shelter_access_token}')
        res_me = self.client.get('/api/auth/me/')
        self.assertEqual(res_me.status_code, status.HTTP_200_OK)
        self.assertEqual(res_me.data['username'], 'e2e_shelter')

        # 1.5 Shelter creates Driver (/api/auth/drivers/create/)
        driver_create_payload = {
            'username': 'e2e_driver_1',
            'email': 'driver1@example.com',
            'password': 'StrongPassword123!',
            'phone': '9876543212',
            'vehicle_type': 'two_wheeler',
            'vehicle_number': 'KA-01-AB-1234'
        }
        res_driver_create = self.client.post('/api/auth/drivers/create/', data=driver_create_payload, format='json')
        self.assertEqual(res_driver_create.status_code, status.HTTP_201_CREATED)
        driver_user = User.objects.get(username='e2e_driver_1')
        driver_profile = DeliveryProfile.objects.get(user=driver_user)
        self.assertEqual(driver_profile.shelter_id, shelter_profile.id)

        # ---------------------------------------------------------
        # 2. PETS APP
        # ---------------------------------------------------------
        # 2.1 Shelter creates Pet (/api/pets/)
        pet_payload = {
            'name': 'Rocky',
            'species': 'dog',
            'breed': 'Labrador',
            'age_months': 14,
            'gender': 'male',
            'size': 'large',
            'description': 'Friendly, playful, vaccinated retriever mix.',
            'adoption_fee': '750.00',
            'is_vaccinated': True
        }
        res_pet_create = self.client.post('/api/pets/', data=pet_payload, format='json')
        self.assertEqual(res_pet_create.status_code, status.HTTP_201_CREATED)
        pet_id = res_pet_create.data['id']
        pet_public_id = res_pet_create.data['public_id']

        # 2.2 Upload Pet Image (/api/pets/<id>/images/)
        from django.core.files.uploadedfile import SimpleUploadedFile
        test_img = SimpleUploadedFile("rocky.jpg", b"fake_image_bytes", content_type="image/jpeg")
        res_img = self.client.post(
            f'/api/pets/{pet_public_id}/images/',
            data={'images': [test_img], 'is_primary': 'true'},
            format='multipart'
        )
        self.assertEqual(res_img.status_code, status.HTTP_201_CREATED)

        # 2.3 Add Medical Record (/api/pets/<id>/medical/)
        medical_payload = {
            'record_type': 'vaccination',
            'title': 'Rabies & DHPP Vaccine',
            'description': 'Core multivalent vaccination administered.',
            'administered_date': timezone.now().date().isoformat()
        }
        res_med = self.client.post(f'/api/pets/{pet_public_id}/medical/', data=medical_payload, format='json')
        self.assertEqual(res_med.status_code, status.HTTP_201_CREATED)

        # 2.4 Public Browse (/api/pets/ with filters)
        self.client.credentials() # Unauthenticated
        res_browse = self.client.get('/api/pets/?species=dog&breed=Labrador&vaccinated=true')
        self.assertEqual(res_browse.status_code, status.HTTP_200_OK)
        self.assertTrue(len(res_browse.data['results']) >= 1)

        # 2.5 Pet Details (/api/pets/<id>/)
        res_detail = self.client.get(f'/api/pets/{pet_public_id}/')
        self.assertEqual(res_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(res_detail.data['name'], 'Rocky')
        self.assertEqual(len(res_detail.data['images']), 1)
        self.assertEqual(len(res_detail.data['medical_records']), 1)

        # ---------------------------------------------------------
        # 3. ADOPTIONS APP
        # ---------------------------------------------------------
        # Customer Login
        res_cust_login = self.client.post('/api/auth/login/', data={
            'username': 'e2e_customer',
            'password': 'StrongPassword123!'
        }, format='json')
        cust_token = res_cust_login.data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {cust_token}')

        # 3.1 Customer Submits Application (/api/adoptions/apply/)
        apply_payload = {
            'pet_id': pet_public_id,
            'living_situation': 'Own 3BHK with fenced yard',
            'has_other_pets': False,
            'contact_phone': '9876543210',
            'delivery_address': 'Flat 202, Green Glen Layout, Bellandur',
            'delivery_city': 'Bangalore',
            'delivery_pincode': '560103',
            'destination_latitude': 12.9304,
            'destination_longitude': 77.6784,
            'payment_method': 'cod'
        }
        res_apply = self.client.post('/api/adoptions/apply/', data=apply_payload, format='json')
        self.assertEqual(res_apply.status_code, status.HTTP_201_CREATED)
        app_data = res_apply.data
        app_id = app_data['id']
        app_public_id = app_data['public_id']
        self.assertGreater(Decimal(str(app_data['distance_km'])), 0)
        self.assertGreater(Decimal(str(app_data['total_amount'])), 0)

        # 3.2 Duplicate Application Check (prevent duplicate active application for same pet)
        res_dup = self.client.post('/api/adoptions/apply/', data=apply_payload, format='json')
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)

        # 3.3 Customer My Applications (/api/adoptions/my-applications/)
        res_my_apps = self.client.get('/api/adoptions/my-applications/')
        self.assertEqual(res_my_apps.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_my_apps.data), 1)

        # 3.4 Shelter Vetting List (/api/adoptions/shelter-applications/)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {shelter_access_token}')
        res_shelter_apps = self.client.get('/api/adoptions/shelter-applications/')
        self.assertEqual(res_shelter_apps.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_shelter_apps.data), 1)

        # 3.5 Shelter Approves Application (/api/adoptions/<id>/status/)
        res_approve = self.client.patch(f'/api/adoptions/{app_public_id}/status/', data={
            'status': 'approved'
        }, format='json')
        self.assertEqual(res_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(res_approve.data['application']['status'], 'approved')
        # Check that pet status transitioned to pending
        pet_after = Pet.objects.get(id=pet_id)
        self.assertEqual(pet_after.status, 'pending')

        # ---------------------------------------------------------
        # 4. LOGISTICS APP
        # ---------------------------------------------------------
        # 4.1 Shelter Assigns Driver (/api/deliveries/assign/)
        assign_payload = {
            'application_id': str(app_public_id),
            'driver_id': str(driver_profile.public_id),
            'payment_method': 'cod'
        }
        res_assign = self.client.post('/api/deliveries/assign/', data=assign_payload, format='json')
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        delivery_public_id = res_assign.data['public_id']
        delivery_obj = PetDelivery.objects.get(public_id=delivery_public_id)

        # 4.2 Driver Logs In and Views Active Task (/api/deliveries/driver-active/)
        res_drv_login = self.client.post('/api/auth/login/', data={
            'username': 'e2e_driver_1',
            'password': 'StrongPassword123!'
        }, format='json')
        driver_token = res_drv_login.data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {driver_token}')

        res_drv_active = self.client.get('/api/deliveries/driver-active/')
        self.assertEqual(res_drv_active.status_code, status.HTTP_200_OK)
        self.assertEqual(res_drv_active.data['public_id'], str(delivery_public_id))

        # 4.3 Driver verifies Pickup OTP at shelter
        pickup_code = delivery_obj.pickup_otp
        res_pickup = self.client.post(f'/api/deliveries/{delivery_public_id}/pickup/', data={
            'otp': pickup_code
        }, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_200_OK)

        # 4.4 Driver Sends Location Breadcrumbs (/api/deliveries/<id>/ping/)
        ping_payload = {
            'latitude': '12.950000',
            'longitude': '77.660000'
        }
        res_ping = self.client.post(f'/api/deliveries/{delivery_public_id}/ping/', data=ping_payload, format='json')
        self.assertEqual(res_ping.status_code, status.HTTP_201_CREATED)

        # 4.5 Customer Tracks Live Delivery Coordinates (/api/deliveries/<id>/track/)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {cust_token}')
        res_track = self.client.get(f'/api/deliveries/{delivery_public_id}/track/')
        self.assertEqual(res_track.status_code, status.HTTP_200_OK)
        self.assertEqual(res_track.data['current_coordinates']['latitude'], '12.950000')
        self.assertEqual(res_track.data['current_coordinates']['longitude'], '77.660000')
        customer_doorstep_otp = res_track.data['delivery']['doorstep_otp']
        self.assertTrue(bool(customer_doorstep_otp))

        # 4.6 Driver Verifies Doorstep OTP (/api/deliveries/<id>/verify-otp/)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {driver_token}')
        res_verify_otp = self.client.post(f'/api/deliveries/{delivery_public_id}/verify-otp/', data={
            'otp': customer_doorstep_otp
        }, format='json')
        self.assertEqual(res_verify_otp.status_code, status.HTTP_200_OK)

        # Verify delivery is delivered and application is completed
        delivery_obj.refresh_from_db()
        self.assertEqual(delivery_obj.status, 'delivered')
        app_after = AdoptionApplication.objects.get(id=app_id)
        self.assertEqual(app_after.status, 'completed')

        # ---------------------------------------------------------
        # 5. REVIEWS APP
        # ---------------------------------------------------------
        # 5.1 Customer Submits Dual Ratings Review (/api/reviews/)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {cust_token}')
        review_payload = {
            'application_id': str(app_public_id),
            'shelter_rating': 5,
            'shelter_review': 'KindHeart Shelter took exceptional medical and emotional care of Rocky!',
            'driver_rating': 5,
            'driver_review': 'Driver handled the transit with utter care and arrived on time.'
        }
        res_review = self.client.post('/api/reviews/', data=review_payload, format='json')
        self.assertEqual(res_review.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_review.data['review']['shelter_rating'], 5)
        self.assertEqual(res_review.data['review']['driver_rating'], 5)
        self.assertEqual(AdoptionReview.objects.filter(application_id=app_id).count(), 1)

    def test_cors_preflight_and_allowed_origins(self):
        # Preflight OPTIONS request from Next.js origin
        response = self.client.options(
            '/api/pets/',
            HTTP_ORIGIN='http://localhost:3000',
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), 'http://localhost:3000')
        self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')

        # Request from 127.0.0.1:3000
        res2 = self.client.get('/api/pets/', HTTP_ORIGIN='http://127.0.0.1:3000')
        self.assertEqual(res2.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:3000')

        # Request from disallowed origin should not include Access-Control-Allow-Origin
        res_disallowed = self.client.get('/api/pets/', HTTP_ORIGIN='http://malicious-site.com')
        self.assertNotIn('Access-Control-Allow-Origin', res_disallowed.headers)

