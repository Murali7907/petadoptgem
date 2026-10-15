import uuid
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User, ShelterProfile, DeliveryProfile, Notification
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication
from apps.logistics.models import PetDelivery
from apps.logistics.otp import issue_otp


class AuthorizationMatrixComprehensiveTests(TestCase):
    """
    Comprehensive Authorization Matrix Tests covering:
    1. Anonymous -> 401 Unauthorized
    2. Wrong Role -> 403 Forbidden
    3. Cross-Tenant / Non-Owner -> 403 Forbidden or 404 Not Found
    4. Legitimate Owner / Participant -> 200/201/204 Success
    Across all 12 key platform operations on canonical (/api/v1/) and legacy (/api/) paths.
    """

    def setUp(self):
        self.client = APIClient()

        # Users
        self.customer_1 = User.objects.create_user(
            username='auth_customer_1', email='cust1@auth.org', password='password123', role='customer'
        )
        self.customer_2 = User.objects.create_user(
            username='auth_customer_2', email='cust2@auth.org', password='password123', role='customer'
        )

        # Shelter 1 (Verified)
        self.shelter_user_1 = User.objects.create_user(
            username='auth_shelter_1', email='shelter1@auth.org', password='password123', role='shelter'
        )
        self.shelter_profile_1 = ShelterProfile.objects.create(
            user=self.shelter_user_1,
            organization_name='Auth Shelter Alpha',
            pickup_address='100 Alpha St',
            city='Bangalore',
            is_verified=True,
            is_active=True
        )

        # Shelter 2 (Verified, stranger tenant)
        self.shelter_user_2 = User.objects.create_user(
            username='auth_shelter_2', email='shelter2@auth.org', password='password123', role='shelter'
        )
        self.shelter_profile_2 = ShelterProfile.objects.create(
            user=self.shelter_user_2,
            organization_name='Auth Shelter Beta',
            pickup_address='200 Beta St',
            city='Bangalore',
            is_verified=True,
            is_active=True
        )

        # Unverified Shelter
        self.shelter_user_unv = User.objects.create_user(
            username='auth_shelter_unv', email='shelter_unv@auth.org', password='password123', role='shelter'
        )
        self.shelter_profile_unv = ShelterProfile.objects.create(
            user=self.shelter_user_unv,
            organization_name='Unverified Shelter',
            pickup_address='300 Ghost St',
            city='Bangalore',
            is_verified=False,
            is_active=True
        )

        # Drivers
        self.driver_user_1 = User.objects.create_user(
            username='auth_driver_1', email='driver1@auth.org', password='password123', role='delivery'
        )
        self.driver_profile_1 = DeliveryProfile.objects.create(
            user=self.driver_user_1,
            shelter=self.shelter_profile_1,
            vehicle_number='KA-01-AUTH-01',
            is_available=True
        )

        self.driver_user_2 = User.objects.create_user(
            username='auth_driver_2', email='driver2@auth.org', password='password123', role='delivery'
        )
        self.driver_profile_2 = DeliveryProfile.objects.create(
            user=self.driver_user_2,
            shelter=self.shelter_profile_2,
            vehicle_number='KA-01-AUTH-02',
            is_available=True
        )

        # Pets
        self.pet_1 = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='AlphaPet',
            species='dog',
            breed='Labrador',
            age_months=24,
            gender='male',
            status='available',
            adoption_fee=Decimal('100.00')
        )
        self.pet_2 = Pet.objects.create(
            shelter=self.shelter_profile_2,
            name='BetaPet',
            species='cat',
            breed='Persian',
            age_months=12,
            gender='female',
            status='available',
            adoption_fee=Decimal('50.00')
        )

        # Applications
        self.app_1 = AdoptionApplication.objects.create(
            applicant=self.customer_1,
            pet=self.pet_1,
            status='approved',
            payment_method='cod',
            total_amount=Decimal('100.00'),
            contact_phone='9876543210',
            living_situation='house',
            has_fenced_yard=True
        )

        self.app_2 = AdoptionApplication.objects.create(
            applicant=self.customer_2,
            pet=self.pet_2,
            status='submitted',
            payment_method='cod',
            total_amount=Decimal('50.00'),
            contact_phone='9876543211',
            living_situation='apartment',
            has_fenced_yard=False
        )

        # Completed application for review testing
        self.pet_completed = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='DeliveredPet',
            species='dog',
            age_months=36,
            gender='male',
            status='adopted',
            adoption_fee=Decimal('150.00')
        )
        self.app_completed = AdoptionApplication.objects.create(
            applicant=self.customer_1,
            pet=self.pet_completed,
            status='completed',
            payment_method='cod',
            total_amount=Decimal('150.00'),
            contact_phone='9876543210'
        )

        # Deliveries
        self.delivery_1 = PetDelivery.objects.create(
            application=self.app_1,
            driver=self.driver_profile_1,
            status='assigned'
        )
        issue_otp(self.delivery_1, 'pickup')

        self.delivery_completed = PetDelivery.objects.create(
            application=self.app_completed,
            driver=self.driver_profile_1,
            status='delivered',
            delivered_at=timezone.now()
        )
        issue_otp(self.delivery_completed, 'pickup')
        issue_otp(self.delivery_completed, 'doorstep')

        # Notifications
        self.notif_1 = Notification.objects.create(
            recipient=self.customer_1,
            title='Adoption Update',
            message='Your application has been approved!',
            notification_type='application'
        )
        self.notif_2 = Notification.objects.create(
            recipient=self.customer_2,
            title='Welcome',
            message='Welcome to KindHeart!',
            notification_type='system'
        )

    # =========================================================================
    # 1. APPLY (/api/v1/adoptions/apply/ & /api/adoptions/apply/)
    # =========================================================================
    def test_apply_authorization(self):
        payload = {
            'pet_id': str(self.pet_1.public_id),
            'living_situation': 'own_house',
            'contact_phone': '9876543210',
            'has_fenced_yard': True,
            'delivery_address': '123 Customer Ave, Bangalore'
        }

        # Anonymous -> 401
        res = self.client.post('/api/v1/adoptions/apply/', payload)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        res_leg = self.client.post('/api/adoptions/apply/', payload)
        self.assertEqual(res_leg.status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Shelter) -> 403
        self.client.force_authenticate(user=self.shelter_user_1)
        res_shelter = self.client.post('/api/v1/adoptions/apply/', payload)
        self.assertEqual(res_shelter.status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        res_driver = self.client.post('/api/v1/adoptions/apply/', payload)
        self.assertEqual(res_driver.status_code, status.HTTP_403_FORBIDDEN)

        # Customer -> 201
        self.client.force_authenticate(user=self.customer_2)
        res_cust = self.client.post('/api/v1/adoptions/apply/', payload)
        self.assertEqual(res_cust.status_code, status.HTTP_201_CREATED)

    # =========================================================================
    # 2. MY-APPLICATIONS (/api/v1/adoptions/my-applications/ & /api/adoptions/my-applications/)
    # =========================================================================
    def test_my_applications_authorization(self):
        # Anonymous -> 401
        res = self.client.get('/api/v1/adoptions/my-applications/')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        res_leg = self.client.get('/api/adoptions/my-applications/')
        self.assertEqual(res_leg.status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Shelter) -> 403
        self.client.force_authenticate(user=self.shelter_user_1)
        self.assertEqual(self.client.get('/api/v1/adoptions/my-applications/').status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.get('/api/v1/adoptions/my-applications/').status_code, status.HTTP_403_FORBIDDEN)

        # Tenant isolation between customers
        self.client.force_authenticate(user=self.customer_1)
        res1 = self.client.get('/api/v1/adoptions/my-applications/')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        ids1 = [item['public_id'] for item in res1.data]
        self.assertIn(str(self.app_1.public_id), ids1)
        self.assertNotIn(str(self.app_2.public_id), ids1)

        self.client.force_authenticate(user=self.customer_2)
        res2 = self.client.get('/api/v1/adoptions/my-applications/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        ids2 = [item['public_id'] for item in res2.data]
        self.assertIn(str(self.app_2.public_id), ids2)
        self.assertNotIn(str(self.app_1.public_id), ids2)

    # =========================================================================
    # 3. SHELTER-APPLICATIONS (/api/v1/shelter/applications/ & legacy)
    # =========================================================================
    def test_shelter_applications_authorization(self):
        # Anonymous -> 401
        res = self.client.get('/api/v1/shelter/applications/')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        res_leg = self.client.get('/api/adoptions/shelter-applications/')
        self.assertEqual(res_leg.status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.get('/api/v1/shelter/applications/').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get('/api/adoptions/shelter-applications/').status_code, status.HTTP_403_FORBIDDEN)

        # Cross-tenant shelter isolation
        self.client.force_authenticate(user=self.shelter_user_1)
        res_sh1 = self.client.get('/api/v1/shelter/applications/')
        self.assertEqual(res_sh1.status_code, status.HTTP_200_OK)
        results1 = res_sh1.data.get('results', res_sh1.data)
        sh1_ids = [item['public_id'] for item in results1]
        self.assertIn(str(self.app_1.public_id), sh1_ids)
        self.assertNotIn(str(self.app_2.public_id), sh1_ids)

        self.client.force_authenticate(user=self.shelter_user_2)
        res_sh2 = self.client.get('/api/v1/shelter/applications/')
        self.assertEqual(res_sh2.status_code, status.HTTP_200_OK)
        results2 = res_sh2.data.get('results', res_sh2.data)
        sh2_ids = [item['public_id'] for item in results2]
        self.assertIn(str(self.app_2.public_id), sh2_ids)
        self.assertNotIn(str(self.app_1.public_id), sh2_ids)

    # =========================================================================
    # 4. APPLICATION DETAIL GET (/api/v1/adoptions/applications/<id>/)
    # =========================================================================
    def test_application_detail_authorization(self):
        url = f'/api/v1/adoptions/applications/{self.app_1.public_id}/'

        # Anonymous -> 401
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        # Cross-tenant Customer (Customer 2 on Customer 1's app) -> 404
        self.client.force_authenticate(user=self.customer_2)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

        # Owning Customer -> 200
        self.client.force_authenticate(user=self.customer_1)
        res_owner = self.client.get(url)
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        self.assertEqual(res_owner.data['public_id'], str(self.app_1.public_id))

    # =========================================================================
    # 5. STATUS UPDATE PATCH (/api/v1/adoptions/<id>/status/ & legacy)
    # =========================================================================
    def test_status_update_authorization(self):
        url = f'/api/v1/adoptions/{self.app_2.public_id}/status/'
        payload = {'status': 'reviewing', 'notes': 'Processing application'}

        # Anonymous -> 401
        self.assertEqual(self.client.patch(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.patch(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.patch(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Unverified shelter -> 403
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_unv = self.client.patch(url, payload)
        self.assertEqual(res_unv.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unv.data.get('code'), 'shelter_not_verified')

        # Cross-tenant shelter (Shelter 1 trying to update Shelter 2's app) -> 403
        self.client.force_authenticate(user=self.shelter_user_1)
        res_cross = self.client.patch(url, payload)
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)

        # Owning verified shelter -> 200
        self.client.force_authenticate(user=self.shelter_user_2)
        res_owner = self.client.patch(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        self.app_2.refresh_from_db()
        self.assertEqual(self.app_2.status, 'reviewing')

    # =========================================================================
    # 6. PET IMAGE CREATE VIEW (/api/v1/pets/<id>/images/ & /api/pets/<id>/images/)
    # =========================================================================
    def test_pet_image_create_authorization(self):
        url = f'/api/v1/pets/{self.pet_1.public_id}/images/'
        legacy_url = f'/api/pets/{self.pet_1.public_id}/images/'
        payload = {'image_url': 'https://example.com/pet1.jpg', 'caption': 'Cute pose'}

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Unverified shelter -> 403
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_unv = self.client.post(url, payload)
        self.assertEqual(res_unv.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unv.data.get('code'), 'shelter_not_verified')

        # Cross-tenant shelter (Shelter 2 on Pet 1) -> 403
        self.client.force_authenticate(user=self.shelter_user_2)
        res_cross = self.client.post(url, payload)
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)

        # Owning verified shelter -> 201
        self.client.force_authenticate(user=self.shelter_user_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_201_CREATED)

    # =========================================================================
    # 7. SHELTER DRIVER CREATE VIEW (/api/v1/shelter/drivers/create/ & /api/auth/drivers/create/)
    # =========================================================================
    def test_shelter_driver_create_authorization(self):
        url = '/api/v1/shelter/drivers/create/'
        legacy_url = '/api/auth/drivers/create/'
        payload = {
            'username': 'new_fleet_driver',
            'email': 'fleet_driver@auth.org',
            'password': 'password123',
            'vehicle_number': 'KA-05-NEW-99',
            'vehicle_type': 'van'
        }

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Unverified shelter -> 403
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_unv = self.client.post(url, payload)
        self.assertEqual(res_unv.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unv.data.get('code'), 'shelter_not_verified')

        # Owning verified shelter -> 201
        self.client.force_authenticate(user=self.shelter_user_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_201_CREATED)
        self.assertIn('driver', res_owner.data)

    # =========================================================================
    # 8. NOTIFICATIONS (/api/v1/notifications/)
    # =========================================================================
    def test_notifications_authorization_and_isolation(self):
        # Anonymous -> 401
        self.assertEqual(self.client.get('/api/v1/notifications/').status_code, status.HTTP_401_UNAUTHORIZED)

        # User 1 only receives User 1 notifications
        self.client.force_authenticate(user=self.customer_1)
        res1 = self.client.get('/api/v1/notifications/')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        notif_ids_1 = [n['public_id'] for n in res1.data]
        self.assertIn(str(self.notif_1.public_id), notif_ids_1)
        self.assertNotIn(str(self.notif_2.public_id), notif_ids_1)

        # User 2 only receives User 2 notifications
        self.client.force_authenticate(user=self.customer_2)
        res2 = self.client.get('/api/v1/notifications/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        notif_ids_2 = [n['public_id'] for n in res2.data]
        self.assertIn(str(self.notif_2.public_id), notif_ids_2)
        self.assertNotIn(str(self.notif_1.public_id), notif_ids_2)

    # =========================================================================
    # 9. REVIEW CREATE VIEW (/api/v1/reviews/create/ & /api/reviews/)
    # =========================================================================
    def test_review_create_authorization(self):
        url = '/api/v1/reviews/create/'
        payload = {
            'application_id': str(self.app_completed.public_id),
            'shelter_rating': 5,
            'shelter_review': 'Outstanding shelter care!',
            'driver_rating': 5,
            'driver_review': 'Courteous and gentle handling.'
        }

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Non-adopter / cross-tenant customer -> 400 (unauthorized applicant)
        self.client.force_authenticate(user=self.customer_2)
        res_non_owner = self.client.post(url, payload)
        self.assertIn(res_non_owner.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

        # Adopter of delivered application -> 201
        self.client.force_authenticate(user=self.customer_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_201_CREATED)

    # =========================================================================
    # 10. TRACKING (/api/v1/delivery/tasks/<id>/tracking/ & /api/tracking/<id>/)
    # =========================================================================
    def test_tracking_authorization(self):
        url = f'/api/v1/delivery/tasks/{self.delivery_1.public_id}/tracking/'
        direct_url = f'/api/tracking/{self.delivery_1.public_id}/'

        # Anonymous -> 401
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get(direct_url).status_code, status.HTTP_401_UNAUTHORIZED)

        # Stranger Customer (Customer 2) -> 403
        self.client.force_authenticate(user=self.customer_2)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(direct_url).status_code, status.HTTP_403_FORBIDDEN)

        # Stranger Driver (Driver 2) -> 403
        self.client.force_authenticate(user=self.driver_user_2)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        # Stranger Shelter (Shelter 2) -> 403
        self.client.force_authenticate(user=self.shelter_user_2)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        # Participants:
        # Customer 1 (Adopter) -> 200
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(direct_url).status_code, status.HTTP_200_OK)

        # Driver 1 (Assigned driver) -> 200
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

        # Shelter 1 (Origin shelter) -> 200
        self.client.force_authenticate(user=self.shelter_user_1)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    # =========================================================================
    # 11. REISSUE-OTP (/api/v1/shelter/deliveries/<id>/reissue-otp/ & legacy)
    # =========================================================================
    def test_reissue_otp_authorization(self):
        url = f'/api/v1/shelter/deliveries/{self.delivery_1.public_id}/reissue-otp/'
        legacy_url = f'/api/deliveries/{self.delivery_1.public_id}/reissue-otp/'
        payload = {'purpose': 'pickup'}

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Unverified shelter -> 403
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_unv = self.client.post(url, payload)
        self.assertEqual(res_unv.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unv.data.get('code'), 'shelter_not_verified')

        # Cross-tenant shelter (Shelter 2 for Delivery 1) -> 403
        self.client.force_authenticate(user=self.shelter_user_2)
        res_cross = self.client.post(url, payload)
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)

        # Owning verified shelter -> 200
        self.client.force_authenticate(user=self.shelter_user_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        self.assertTrue('pickup_otp' in res_owner.data or 'otp' in res_owner.data)

    # =========================================================================
    # 12. ASSIGN (/api/v1/shelter/deliveries/assign/ & /api/deliveries/assign/)
    # =========================================================================
    def test_assign_authorization(self):
        pet_for_assign = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='AssignPet',
            species='dog',
            age_months=12,
            gender='male',
            status='available'
        )
        app_for_assign = AdoptionApplication.objects.create(
            applicant=self.customer_1,
            pet=pet_for_assign,
            status='approved',
            payment_method='cod',
            total_amount=Decimal('50.00'),
            delivery_address='789 Assign Rd'
        )
        free_driver_user = User.objects.create_user(
            username='auth_driver_free', email='free_driver@auth.org', password='password123', role='delivery'
        )
        free_driver = DeliveryProfile.objects.create(
            user=free_driver_user,
            shelter=self.shelter_profile_1,
            vehicle_number='KA-01-FREE-99',
            is_available=True
        )

        url = '/api/v1/shelter/deliveries/assign/'
        legacy_url = '/api/deliveries/assign/'
        payload = {
            'application_ref': str(app_for_assign.public_id),
            'driver_ref': str(free_driver.public_id)
        }

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Wrong role (Customer) -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Wrong role (Driver) -> 403
        self.client.force_authenticate(user=self.driver_user_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Unverified shelter -> 403
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_unv = self.client.post(url, payload)
        self.assertEqual(res_unv.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unv.data.get('code'), 'shelter_not_verified')

        # Cross-tenant shelter (Shelter 2 assigning Shelter 1's app) -> 403
        self.client.force_authenticate(user=self.shelter_user_2)
        res_cross = self.client.post(url, payload)
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)

        # Owning verified shelter -> 201
        self.client.force_authenticate(user=self.shelter_user_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_201_CREATED)
        self.assertIn('pickup_otp', res_owner.data)
