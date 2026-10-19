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

        # Admin user
        self.admin_user = User.objects.create_superuser(
            username='auth_admin', email='admin@auth.org', password='password123', role='admin'
        )

        # Unverified shelter pet, application, delivery
        self.pet_unv = Pet.objects.create(
            shelter=self.shelter_profile_unv,
            name='GhostPet',
            species='cat',
            breed='Domestic',
            age_months=6,
            gender='male',
            status='available',
            adoption_fee=Decimal('50.00')
        )
        self.app_unv = AdoptionApplication.objects.create(
            applicant=self.customer_1,
            pet=self.pet_unv,
            status='submitted',
            payment_method='cod',
            total_amount=Decimal('50.00'),
            contact_phone='9876543210'
        )
        self.delivery_unv = PetDelivery.objects.create(
            application=self.app_unv,
            driver=self.driver_profile_1,
            status='assigned'
        )
        issue_otp(self.delivery_unv, 'pickup')


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
    # 7. SHELTER DRIVER CREATE VIEW (/api/v1/auth/drivers/create/ & /api/auth/drivers/create/)
    # =========================================================================
    def test_shelter_driver_create_authorization(self):
        url = '/api/v1/auth/drivers/create/'
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
    # 9. REVIEW CREATE VIEW (/api/v1/reviews/ & /api/reviews/)
    # =========================================================================
    def test_review_create_authorization(self):
        url = '/api/v1/reviews/'
        legacy_url = '/api/reviews/'
        payload = {
            'application_id': str(self.app_completed.public_id),
            'shelter_rating': 5,
            'shelter_review': 'Outstanding shelter care!',
            'driver_rating': 5,
            'driver_review': 'Courteous and gentle handling.'
        }

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Non-adopter / cross-tenant customer -> 403 (unauthorized applicant)
        self.client.force_authenticate(user=self.customer_2)
        res_non_owner = self.client.post(url, payload)
        self.assertEqual(res_non_owner.status_code, status.HTTP_403_FORBIDDEN)

        # Adopter of delivered application -> 201
        self.client.force_authenticate(user=self.customer_1)
        res_owner = self.client.post(url, payload)
        self.assertEqual(res_owner.status_code, status.HTTP_201_CREATED)

    # =========================================================================
    # 10. TRACKING (/api/v1/delivery/tasks/<id>/tracking/ & /api/deliveries/<id>/track/)
    # =========================================================================
    def test_tracking_authorization(self):
        url = f'/api/v1/delivery/tasks/{self.delivery_1.public_id}/tracking/'
        legacy_url = f'/api/deliveries/{self.delivery_1.public_id}/track/'

        # Anonymous -> 401
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get(legacy_url).status_code, status.HTTP_401_UNAUTHORIZED)

        # Stranger Customer (Customer 2) -> 403
        self.client.force_authenticate(user=self.customer_2)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(legacy_url).status_code, status.HTTP_403_FORBIDDEN)

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
        self.assertEqual(self.client.get(legacy_url).status_code, status.HTTP_200_OK)

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

    # =========================================================================
    # 13. NOTIFICATIONS CROSS-TENANT READ & READ-ALL
    # =========================================================================
    def test_notifications_read_and_read_all_cross_tenant(self):
        # Customer 2 attempts to mark Customer 1's notification as read -> 404
        self.client.force_authenticate(user=self.customer_2)
        res_read = self.client.post(f'/api/v1/notifications/{self.notif_1.public_id}/read/')
        self.assertEqual(res_read.status_code, status.HTTP_404_NOT_FOUND)
        self.notif_1.refresh_from_db()
        self.assertFalse(self.notif_1.is_read)

        # Customer 2 calls read-all -> marks Customer 2's notifications only
        res_all = self.client.post('/api/v1/notifications/read-all/')
        self.assertEqual(res_all.status_code, status.HTTP_200_OK)
        self.notif_1.refresh_from_db()
        self.assertFalse(self.notif_1.is_read, "Customer 1 notification must NOT be marked read by Customer 2")
        self.notif_2.refresh_from_db()
        self.assertTrue(self.notif_2.is_read)

        # Customer 1 (owner) marks own notification as read -> 200
        self.client.force_authenticate(user=self.customer_1)
        res_owner_read = self.client.post(f'/api/v1/notifications/{self.notif_1.public_id}/read/')
        self.assertEqual(res_owner_read.status_code, status.HTTP_200_OK)
        self.notif_1.refresh_from_db()
        self.assertTrue(self.notif_1.is_read)

    # =========================================================================
    # 14. APPLICATION DETAIL GET & PATCH BY DIFFERENT CUSTOMER
    # =========================================================================
    def test_application_detail_cross_tenant_get_and_patch(self):
        url = f'/api/v1/adoptions/applications/{self.app_1.public_id}/'

        # Anonymous -> 401
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.patch(url, {'contact_phone': '1112223333'}).status_code, status.HTTP_401_UNAUTHORIZED)

        # Different Customer (Customer 2) on Customer 1's app -> 404
        self.client.force_authenticate(user=self.customer_2)
        res_get = self.client.get(url)
        self.assertEqual(res_get.status_code, status.HTTP_404_NOT_FOUND)
        res_patch = self.client.patch(url, {'contact_phone': '1112223333'})
        self.assertEqual(res_patch.status_code, status.HTTP_404_NOT_FOUND)
        self.app_1.refresh_from_db()
        self.assertEqual(self.app_1.contact_phone, '9876543210')

        # Customer 2 attempting status update PATCH -> 403 Forbidden
        status_url = f'/api/v1/adoptions/{self.app_1.public_id}/status/'
        res_status = self.client.patch(status_url, {'status': 'approved'})
        self.assertEqual(res_status.status_code, status.HTTP_403_FORBIDDEN)

        # Owning Customer (Customer 1) GET & PATCH own app -> 200
        self.client.force_authenticate(user=self.customer_1)
        res_owner_get = self.client.get(url)
        self.assertEqual(res_owner_get.status_code, status.HTTP_200_OK)
        res_owner_patch = self.client.patch(url, {'contact_phone': '9998887777'})
        self.assertEqual(res_owner_patch.status_code, status.HTTP_200_OK)
        self.app_1.refresh_from_db()
        self.assertEqual(self.app_1.contact_phone, '9998887777')

    # =========================================================================
    # 15. REVIEW CREATION ON SOMEONE ELSE'S APPLICATION
    # =========================================================================
    def test_review_creation_on_someone_elses_application_both_routes(self):
        # Customer 2 attempts review on Customer 1's completed delivery via canonical & legacy
        self.client.force_authenticate(user=self.customer_2)

        # Route 1: POST /api/v1/reviews/ -> 403
        payload_1 = {
            'application_id': str(self.app_completed.public_id),
            'shelter_rating': 5,
            'driver_rating': 5
        }
        res_v1 = self.client.post('/api/v1/reviews/', payload_1)
        self.assertEqual(res_v1.status_code, status.HTTP_403_FORBIDDEN)

        # Route 2: POST /api/reviews/ -> 403
        res_legacy = self.client.post('/api/reviews/', payload_1)
        self.assertEqual(res_legacy.status_code, status.HTTP_403_FORBIDDEN)

        # Route 3: POST /api/v1/adoptions/applications/<uuid>/review/ -> 403
        payload_2 = {
            'shelter_rating': 5,
            'shelter_review': 'Great shelter',
            'driver_rating': 5,
            'driver_review': 'Great driver'
        }
        res_app_review = self.client.post(
            f'/api/v1/adoptions/applications/{self.app_completed.public_id}/review/',
            payload_2
        )
        self.assertEqual(res_app_review.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # 16. DELIVERY LOCATION PING BY NON-ASSIGNED DRIVER
    # =========================================================================
    def test_delivery_ping_non_assigned_driver(self):
        url = f'/api/v1/delivery/tasks/{self.delivery_1.public_id}/ping/'
        legacy_url = f'/api/deliveries/{self.delivery_1.public_id}/ping/'
        payload = {'latitude': '12.971598', 'longitude': '77.594562'}

        # Anonymous -> 401
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(legacy_url, payload).status_code, status.HTTP_401_UNAUTHORIZED)

        # Customer -> 403
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.post(url, payload).status_code, status.HTTP_403_FORBIDDEN)

        # Non-assigned driver (Driver 2) -> 403 Forbidden
        self.client.force_authenticate(user=self.driver_user_2)
        res_driver2 = self.client.post(url, payload)
        self.assertEqual(res_driver2.status_code, status.HTTP_403_FORBIDDEN)
        res_driver2_legacy = self.client.post(legacy_url, payload)
        self.assertEqual(res_driver2_legacy.status_code, status.HTTP_403_FORBIDDEN)

        # Assigned driver (Driver 1) -> 201 Created
        self.client.force_authenticate(user=self.driver_user_1)
        res_assigned = self.client.post(url, payload)
        self.assertEqual(res_assigned.status_code, status.HTTP_201_CREATED)

    # =========================================================================
    # 17. PUBLIC PET DETAIL FOR UNVERIFIED SHELTER (ISOLATION)
    # =========================================================================
    def test_public_pet_detail_unverified_shelter_isolation(self):
        uuid_url = f'/api/v1/pets/{self.pet_unv.public_id}/'
        id_url = f'/api/v1/pets/{self.pet_unv.id}/'
        legacy_uuid_url = f'/api/pets/{self.pet_unv.public_id}/'
        legacy_id_url = f'/api/pets/{self.pet_unv.id}/'

        # Anonymous -> 404 Not Found (on both UUID and integer id)
        self.assertEqual(self.client.get(uuid_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(id_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(legacy_uuid_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(legacy_id_url).status_code, status.HTTP_404_NOT_FOUND)

        # Customer 1 -> 404 Not Found
        self.client.force_authenticate(user=self.customer_1)
        self.assertEqual(self.client.get(uuid_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(id_url).status_code, status.HTTP_404_NOT_FOUND)

        # Stranger Verified Shelter (Shelter 1) -> 404 Not Found
        self.client.force_authenticate(user=self.shelter_user_1)
        self.assertEqual(self.client.get(uuid_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(id_url).status_code, status.HTTP_404_NOT_FOUND)

        # Owning unverified shelter -> 200 OK
        self.client.force_authenticate(user=self.shelter_user_unv)
        res_owner_uuid = self.client.get(uuid_url)
        self.assertEqual(res_owner_uuid.status_code, status.HTTP_200_OK)
        self.assertEqual(res_owner_uuid.data['name'], 'GhostPet')
        res_owner_id = self.client.get(id_url)
        self.assertEqual(res_owner_id.status_code, status.HTTP_200_OK)

        # Admin -> 200 OK
        self.client.force_authenticate(user=self.admin_user)
        self.assertEqual(self.client.get(uuid_url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(id_url).status_code, status.HTTP_200_OK)

    # =========================================================================
    # 18. ALL SHELTER WRITES REJECT UNVERIFIED SHELTER (CANONICAL & LEGACY)
    # =========================================================================
    def test_all_shelter_writes_reject_unverified_shelter_on_canonical_and_legacy(self):
        self.client.force_authenticate(user=self.shelter_user_unv)

        # 1. AdoptionStatusUpdateView / AdoptionApplicationDetailView PATCH
        for path in [f'/api/v1/adoptions/{self.app_unv.public_id}/status/', f'/api/adoptions/{self.app_unv.public_id}/status/']:
            res = self.client.patch(path, {'status': 'reviewing'})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 2. ShelterApplicationViewSet review/approve/reject
        for action_name in ['review', 'approve', 'reject']:
            canonical_path = f'/api/v1/shelter/applications/{self.app_unv.public_id}/{action_name}/'
            legacy_path = f'/api/adoptions/shelter/applications/{self.app_unv.public_id}/{action_name}/'
            data = {'notes': 'Test note'} if action_name in ['approve', 'reject'] else {}
            res_c = self.client.post(canonical_path, data)
            self.assertEqual(res_c.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {canonical_path}")
            self.assertEqual(res_c.data.get('code'), 'shelter_not_verified')
            res_l = self.client.post(legacy_path, data)
            self.assertEqual(res_l.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {legacy_path}")
            self.assertEqual(res_l.data.get('code'), 'shelter_not_verified')

        # 3. PetImageCreateView POST
        for img_path in [f'/api/v1/pets/{self.pet_unv.public_id}/images/', f'/api/pets/{self.pet_unv.public_id}/images/']:
            res = self.client.post(img_path, {'image_url': 'https://example.com/unv.jpg'})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {img_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 4. Medical record POST (PetMedicalRecordListCreateView)
        for med_path in [f'/api/v1/pets/{self.pet_unv.public_id}/medical-records/', f'/api/pets/{self.pet_unv.public_id}/medical/']:
            res = self.client.post(med_path, {'title': 'Checkup', 'record_date': '2026-10-15'})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {med_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 5. Pet writes (POST, PATCH, DELETE)
        # Pet create
        for create_path in ['/api/v1/shelter/pets/', '/api/pets/']:
            res = self.client.post(create_path, {
                'name': 'UnvPetNew', 'species': 'dog', 'age_months': 10, 'gender': 'male', 'adoption_fee': '50.00'
            })
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {create_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # Pet update & delete
        for upd_path in [f'/api/v1/shelter/pets/{self.pet_unv.public_id}/', f'/api/pets/{self.pet_unv.public_id}/']:
            res = self.client.patch(upd_path, {'name': 'RenamedPet'})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {upd_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        for del_path in [f'/api/v1/shelter/pets/{self.pet_unv.public_id}/', f'/api/pets/{self.pet_unv.public_id}/']:
            res = self.client.delete(del_path)
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {del_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 6. Assign (ShelterDeliveryViewSet.assign)
        assign_payload = {
            'application_ref': str(self.app_unv.public_id),
            'driver_ref': str(self.driver_profile_1.public_id)
        }
        for assign_path in ['/api/v1/shelter/deliveries/assign/', '/api/deliveries/assign/']:
            res = self.client.post(assign_path, assign_payload)
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {assign_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 7. Reissue OTP (ShelterDeliveryViewSet.reissue_otp)
        for reissue_path in [
            f'/api/v1/shelter/deliveries/{self.delivery_unv.public_id}/reissue-otp/',
            f'/api/deliveries/{self.delivery_unv.public_id}/reissue-otp/'
        ]:
            res = self.client.post(reissue_path, {'purpose': 'pickup'})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {reissue_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

        # 8. ShelterDriverCreateView
        for driver_create_path in ['/api/v1/auth/drivers/create/', '/api/auth/drivers/create/']:
            res = self.client.post(driver_create_path, {
                'username': 'unv_driver_attempt',
                'email': 'unv_driver@auth.org',
                'password': 'password123',
                'vehicle_number': 'KA-01-UNV-01',
                'vehicle_type': 'bike'
            })
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, f"Failed on {driver_create_path}")
            self.assertEqual(res.data.get('code'), 'shelter_not_verified')

