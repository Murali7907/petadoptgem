import sys
from pathlib import Path

# Add backend directory to sys.path so IDE linters resolve apps module correctly
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import jwt
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from apps.accounts.models import User, ShelterProfile, DeliveryProfile

class JWTAuthenticationTests(APITestCase):
    def setUp(self):
        self.register_url = reverse('auth_register')
        self.login_url = reverse('auth_login')
        self.refresh_url = reverse('auth_token_refresh')
        self.verify_url = reverse('auth_token_verify')
        self.me_url = reverse('auth_me')

        self.user_data = {
            'username': 'john_customer',
            'email': 'john@example.com',
            'password': 'password123',
            'first_name': 'John',
            'last_name': 'Doe',
            'role': 'customer',
            'phone': '1234567890',
            'address': '123 Main St'
        }

    def test_user_registration(self):
        response = self.client.post(self.register_url, self.user_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('user', response.data)
        self.assertEqual(response.data['user']['username'], 'john_customer')
        self.assertEqual(response.data['user']['role'], 'customer')

        user = User.objects.get(username='john_customer')
        self.assertTrue(user.check_password('password123'))

    def test_login_and_jwt_claims(self):
        # Register user
        self.client.post(self.register_url, self.user_data, format='json')

        # Login
        login_data = {
            'username': 'john_customer',
            'password': 'password123'
        }
        response = self.client.post(self.login_url, login_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

        # Decode JWT access token and verify custom claims
        access_token = response.data['access']
        decoded_payload = jwt.decode(access_token, settings.SECRET_KEY, algorithms=["HS256"])

        self.assertEqual(decoded_payload['email'], 'john@example.com')
        self.assertEqual(decoded_payload['role'], 'customer')
        self.assertIn('user_id', decoded_payload)
        self.assertIn('public_id', decoded_payload)

    def test_token_refresh(self):
        self.client.post(self.register_url, self.user_data, format='json')
        login_response = self.client.post(self.login_url, {'username': 'john_customer', 'password': 'password123'}, format='json')
        refresh_token = login_response.data['refresh']

        refresh_response = self.client.post(self.refresh_url, {'refresh': refresh_token}, format='json')
        self.assertEqual(refresh_response.status_code, status.HTTP_200_OK)
        self.assertIn('access', refresh_response.data)

    def test_token_verify(self):
        self.client.post(self.register_url, self.user_data, format='json')
        login_response = self.client.post(self.login_url, {'username': 'john_customer', 'password': 'password123'}, format='json')
        access_token = login_response.data['access']

        verify_response = self.client.post(self.verify_url, {'token': access_token}, format='json')
        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)

    def test_invalid_token_verify(self):
        verify_response = self.client.post(self.verify_url, {'token': 'invalid.jwt.token'}, format='json')
        self.assertEqual(verify_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_endpoint_authenticated(self):
        self.client.post(self.register_url, self.user_data, format='json')
        login_response = self.client.post(self.login_url, {'username': 'john_customer', 'password': 'password123'}, format='json')
        access_token = login_response.data['access']

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')
        me_response = self.client.get(self.me_url)
        self.assertEqual(me_response.status_code, status.HTTP_200_OK)
        self.assertEqual(me_response.data['username'], 'john_customer')
        self.assertEqual(me_response.data['role'], 'customer')

    def test_me_endpoint_unauthenticated(self):
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AdminPlatformOversightAPITests(APITestCase):
    def setUp(self):
        # 1. Admin User
        self.admin_user = User.objects.create_user(
            username='super_admin',
            email='admin@platform.org',
            password='adminpass123',
            role='admin'
        )

        # 2. Shelter User & Profile
        self.shelter_user = User.objects.create_user(
            username='care_shelter',
            email='care@shelter.org',
            password='shelterpass123',
            phone='+919800000001',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='KindCare Animal Shelter',
            license_no='SHELTER-TEST-001',
            pickup_address='789 Shelter Way, Bangalore',
            city='Bangalore',
            latitude=12.9716,
            longitude=77.5946,
            is_active=True,
            is_verified=False
        )

        # 3. Delivery Partner
        self.delivery_user = User.objects.create_user(
            username='speedy_driver',
            email='speedy@delivery.org',
            password='driverpass123',
            phone='+919800000002',
            role='delivery'
        )
        self.delivery_profile = DeliveryProfile.objects.create(
            user=self.delivery_user,
            shelter=self.shelter_profile,
            vehicle_type='car',
            vehicle_number='KA-01-AB-1234'
        )

        # 4. Customers
        self.customer_user1 = User.objects.create_user(
            username='alice_cust',
            email='alice@example.com',
            password='customerpass123',
            phone='+919800000003',
            role='customer'
        )
        self.customer_user2 = User.objects.create_user(
            username='bob_cust',
            email='bob@example.com',
            password='customerpass123',
            phone='+919800000004',
            role='customer'
        )

        self.shelters_admin_list_url = reverse('admin-shelters-list')
        self.dashboard_stats_url = reverse('admin-dashboard-stats')

    def test_admin_shelter_verification_and_suspension_toggle(self):
        """Verify admin can inspect, verify, and suspend shelter profiles."""
        self.client.force_authenticate(user=self.admin_user)

        detail_url = reverse('admin-shelters-detail', kwargs={'public_id': self.shelter_profile.public_id})
        verify_url = reverse('admin-shelters-verify', kwargs={'public_id': self.shelter_profile.public_id})
        suspend_url = reverse('admin-shelters-suspend', kwargs={'public_id': self.shelter_profile.public_id})

        # Initial GET detail
        get_resp = self.client.get(detail_url)
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.data['organization_name'], 'KindCare Animal Shelter')
        self.assertFalse(get_resp.data['is_verified'])
        self.assertEqual(get_resp.data['verification_status'], 'pending')
        self.assertNotIn('password', get_resp.data)  # Security: No raw password in serializer

        # Verify action
        verify_resp = self.client.post(verify_url)
        self.assertEqual(verify_resp.status_code, status.HTTP_200_OK)
        self.shelter_profile.refresh_from_db()
        self.assertTrue(self.shelter_profile.is_verified)
        self.assertTrue(self.shelter_profile.is_active)
        self.assertEqual(verify_resp.data['shelter']['verification_status'], 'verified')

        # Suspend action
        suspend_resp = self.client.post(suspend_url)
        self.assertEqual(suspend_resp.status_code, status.HTTP_200_OK)
        self.shelter_profile.refresh_from_db()
        self.assertFalse(self.shelter_profile.is_verified)
        self.assertFalse(self.shelter_profile.is_active)
        self.assertEqual(suspend_resp.data['shelter']['verification_status'], 'suspended')

    def test_unauthorized_role_cannot_access_admin_endpoints(self):
        """Verify non-admin roles (customer, shelter, unauthenticated) receive 401/403 on admin routes."""
        verify_url = reverse('admin-shelters-verify', kwargs={'public_id': self.shelter_profile.public_id})

        # 1. Unauthenticated -> 401
        unauth_resp = self.client.get(self.dashboard_stats_url)
        self.assertEqual(unauth_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # 2. Customer user -> 403
        self.client.force_authenticate(user=self.customer_user1)
        cust_stats_resp = self.client.get(self.dashboard_stats_url)
        self.assertEqual(cust_stats_resp.status_code, status.HTTP_403_FORBIDDEN)

        cust_shelters_resp = self.client.get(self.shelters_admin_list_url)
        self.assertEqual(cust_shelters_resp.status_code, status.HTTP_403_FORBIDDEN)

        # 3. Shelter user -> 403
        self.client.force_authenticate(user=self.shelter_user)
        shelter_stats_resp = self.client.get(self.dashboard_stats_url)
        self.assertEqual(shelter_stats_resp.status_code, status.HTTP_403_FORBIDDEN)

        shelter_verify_resp = self.client.post(verify_url)
        self.assertEqual(shelter_verify_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_dashboard_stats_endpoint_calculates_accurate_totals_and_revenue(self):
        """Verify dashboard stats aggregate accurate user, pet, delivery, and revenue metrics."""
        from apps.pets.models import Pet
        from apps.adoptions.models import AdoptionApplication, AdoptionReview
        from apps.logistics.models import PetDelivery, Payment
        from apps.logistics.otp import issue_otp

        # Create pets with various statuses
        pet1 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Spike',
            species='dog',
            age_months=12,
            gender='male',
            status='available'
        )
        pet2 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Mittens',
            species='cat',
            age_months=10,
            gender='female',
            status='available'
        )
        pet3 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Coco',
            species='dog',
            age_months=24,
            gender='male',
            status='adopted'
        )
        pet4 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Ginger',
            species='cat',
            age_months=8,
            gender='female',
            status='pending'
        )

        # Create an active delivery task
        app_active = AdoptionApplication.objects.create(
            applicant=self.customer_user1,
            pet=pet1,
            status='approved',
            dropoff_address='Customer Address 1',
            living_situation='Apartment',
            contact_phone='+919800000003'
        )
        delivery_active = PetDelivery.objects.create(
            application=app_active,
            driver=self.delivery_profile,
            status='out_for_delivery'
        )
        issue_otp(delivery_active, 'pickup')
        issue_otp(delivery_active, 'doorstep')

        # Paid payment (₹250.00 / 25000 paise)
        Payment.objects.create(
            application=app_active,
            delivery=delivery_active,
            amount_in_paise=25000,
            status='paid'
        )

        # Unpaid payment (should not count towards revenue)
        Payment.objects.create(
            application=app_active,
            delivery=delivery_active,
            amount_in_paise=10000,
            status='pending'
        )

        # Completed past delivery and review
        app_completed = AdoptionApplication.objects.create(
            applicant=self.customer_user2,
            pet=pet3,
            status='completed',
            dropoff_address='Customer Address 2',
            living_situation='House',
            contact_phone='+919800000004'
        )
        delivery_completed = PetDelivery.objects.create(
            application=app_completed,
            driver=self.delivery_profile,
            status='delivered',
            delivered_at=timezone.now()
        )
        issue_otp(delivery_completed, 'pickup')
        issue_otp(delivery_completed, 'doorstep')
        AdoptionReview.objects.create(
            application=app_completed,
            reviewer=self.customer_user2,
            shelter_rating=4,
            driver_rating=5,
            shelter_review='Great shelter service.',
            driver_review='Very friendly driver.'
        )

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.dashboard_stats_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        # Users total breakdown
        self.assertEqual(data['total_users']['total'], 5)
        self.assertEqual(data['total_users']['admin'], 1)
        self.assertEqual(data['total_users']['shelter'], 1)
        self.assertEqual(data['total_users']['delivery'], 1)
        self.assertEqual(data['total_users']['customer'], 2)

        # Pets breakdown
        self.assertEqual(data['total_pets']['total'], 4)
        self.assertEqual(data['total_pets']['available'], 2)
        self.assertEqual(data['total_pets']['adopted'], 1)
        self.assertEqual(data['total_pets']['pending'], 1)

        # Active deliveries (assigned/out_for_delivery)
        self.assertEqual(data['active_deliveries'], 1)

        # Revenue INR conversion
        self.assertEqual(data['total_revenue_inr'], 250.00)

        # Satisfaction ratings
        self.assertEqual(data['average_shelter_rating'], 4.0)
        self.assertEqual(data['average_driver_rating'], 5.0)


class NotificationAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='notif_user',
            email='notif_user@example.com',
            password='password123',
            role='customer'
        )
        self.other_user = User.objects.create_user(
            username='other_notif_user',
            email='other_user@example.com',
            password='password123',
            role='customer'
        )
        from apps.accounts.models import Notification
        self.notif1 = Notification.objects.create(
            recipient=self.user,
            title='Application Approved',
            message='Your application for Rocky has been approved.',
            notification_type='application',
            is_read=False
        )
        self.notif2 = Notification.objects.create(
            recipient=self.user,
            title='Out for Delivery',
            message='Rocky is on the way!',
            notification_type='delivery',
            is_read=False
        )
        self.other_notif = Notification.objects.create(
            recipient=self.other_user,
            title='Private message',
            message='Not for notif_user',
            is_read=False
        )
        self.notifications_url = '/api/v1/notifications/'

    def test_notification_list_and_read_actions(self):
        # 1. Unauthenticated -> 401
        res = self.client.get(self.notifications_url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        # 2. Authenticate
        self.client.force_authenticate(user=self.user)

        # List all (should only show 2 for self.user)
        list_res = self.client.get(self.notifications_url)
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_res.data), 2)

        # Filter unread
        unread_res = self.client.get(f'{self.notifications_url}?unread=true')
        self.assertEqual(unread_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(unread_res.data), 2)

        # 3. Mark single as read
        mark_url = f'{self.notifications_url}{self.notif1.public_id}/read/'
        mark_res = self.client.post(mark_url)
        self.assertEqual(mark_res.status_code, status.HTTP_200_OK)
        self.assertTrue(mark_res.data['is_read'])

        self.notif1.refresh_from_db()
        self.assertTrue(self.notif1.is_read)

        # Check filter unread now returns 1
        unread_res2 = self.client.get(f'{self.notifications_url}?unread=true')
        self.assertEqual(len(unread_res2.data), 1)

        # 4. Mark all read
        mark_all_url = f'{self.notifications_url}read-all/'
        mark_all_res = self.client.post(mark_all_url)
        self.assertEqual(mark_all_res.status_code, status.HTTP_200_OK)

        unread_res3 = self.client.get(f'{self.notifications_url}?unread=true')
        self.assertEqual(len(unread_res3.data), 0)


class VerifiedShelterAccountsTests(APITestCase):
    def setUp(self):
        self.create_driver_url = '/api/auth/drivers/create/'

        # Unverified shelter
        self.unverified_user = User.objects.create_user(
            username='unverified_shelter',
            email='unverified@shelter.org',
            password='password123',
            role='shelter'
        )
        self.unverified_profile = ShelterProfile.objects.create(
            user=self.unverified_user,
            organization_name='Unverified Haven',
            pickup_address='123 Pending St',
            is_verified=False
        )

        # Verified shelter
        self.verified_user = User.objects.create_user(
            username='verified_shelter',
            email='verified@shelter.org',
            password='password123',
            role='shelter'
        )
        self.verified_profile = ShelterProfile.objects.create(
            user=self.verified_user,
            organization_name='Verified Haven',
            pickup_address='456 Verified Ave',
            is_verified=True
        )

        # Admin user
        self.admin_user = User.objects.create_superuser(
            username='admin_boss',
            email='admin@shelter.org',
            password='password123'
        )

        self.driver_payload = {
            'username': 'new_fleet_driver',
            'email': 'driver@fleet.org',
            'password': 'driverpass123',
            'vehicle_type': 'two_wheeler',
            'vehicle_number': 'KA-01-VF-9999'
        }

    def test_unverified_shelter_cannot_create_driver(self):
        self.client.force_authenticate(user=self.unverified_user)
        res = self.client.post(self.create_driver_url, self.driver_payload)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')

    def test_verified_shelter_can_create_driver(self):
        self.client.force_authenticate(user=self.verified_user)
        res = self.client.post(self.create_driver_url, self.driver_payload)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['driver']['email'], 'driver@fleet.org')

    def test_admin_bypass_for_driver_creation(self):
        # Admin bypasses permission check; needs shelter in context or validated data if required
        self.client.force_authenticate(user=self.admin_user)
        payload = dict(self.driver_payload)
        payload['username'] = 'admin_created_driver'
        payload['email'] = 'admin_driver@fleet.org'
        payload['shelter_id'] = self.verified_profile.id
        res = self.client.post(self.create_driver_url, payload)
        # Should not be blocked by 403 shelter_not_verified
        self.assertNotEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_suspending_shelter_immediately_blocks_driver_creation(self):
        self.client.force_authenticate(user=self.verified_user)
        # Suspend shelter
        self.verified_profile.is_verified = False
        self.verified_profile.save()

        res = self.client.post(self.create_driver_url, self.driver_payload)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')


class AdopterEmailEnforcementTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.register_url = reverse('auth_register')
        self.valid_data = {
            'username': 'unique_user',
            'email': 'unique.user@example.com',
            'password': 'password123',
            'first_name': 'Unique',
            'last_name': 'User',
            'role': 'customer',
            'phone': '1234567890',
            'address': '123 Unique St'
        }

    def test_registration_requires_non_empty_email(self):
        # Empty string
        data_empty = dict(self.valid_data, username='user_empty', email='')
        res = self.client.post(self.register_url, data_empty, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res.data)

        # Whitespace only
        data_ws = dict(self.valid_data, username='user_ws', email='   ')
        res_ws = self.client.post(self.register_url, data_ws, format='json')
        self.assertEqual(res_ws.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res_ws.data)

        # Missing email key
        data_missing = dict(self.valid_data, username='user_missing')
        data_missing.pop('email')
        res_missing = self.client.post(self.register_url, data_missing, format='json')
        self.assertEqual(res_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res_missing.data)

    def test_registration_requires_valid_email_format(self):
        data_invalid = dict(self.valid_data, username='user_invalid', email='not-a-valid-email')
        res = self.client.post(self.register_url, data_invalid, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res.data)

    def test_registration_enforces_case_insensitive_uniqueness(self):
        # Register mixed-case
        data1 = dict(self.valid_data, username='mixed_user', email='John.Doe@Example.com')
        res1 = self.client.post(self.register_url, data1, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Register lowercase duplicate
        data2 = dict(self.valid_data, username='lower_user', email='john.doe@example.com')
        res2 = self.client.post(self.register_url, data2, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res2.data)

        # Register uppercase duplicate
        data3 = dict(self.valid_data, username='upper_user', email='JOHN.DOE@EXAMPLE.COM')
        res3 = self.client.post(self.register_url, data3, format='json')
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res3.data)

    def test_db_unique_constraint_on_lower_email_non_empty(self):
        from django.db import IntegrityError
        # Multiple users with empty string email are allowed (partial index where email != '')
        u1 = User.objects.create_user(username='empty1', email='', password='pass123password')
        u2 = User.objects.create_user(username='empty2', email='', password='pass123password')
        self.assertEqual(u1.email, '')
        self.assertEqual(u2.email, '')

        # Non-empty email duplicate (case-insensitive) at DB level raises IntegrityError
        User.objects.create_user(username='unique1', email='db_test@example.com', password='pass123password')
        with self.assertRaises(IntegrityError):
            User.objects.create_user(username='unique2', email='DB_TEST@EXAMPLE.COM', password='pass123password')


class AuthAbuseProtectionTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user(
            username='auth_abuse_user',
            email='auth_abuse@example.com',
            password='testpassword123',
            role='customer'
        )

    def tearDown(self):
        from django.core.cache import cache
        cache.clear()

    def test_login_throttled_after_limit(self):
        from django.core.cache import cache
        cache.clear()
        url = '/api/v1/auth/login/'
        payload = {'username': 'auth_abuse_user', 'password': 'wrongpassword'}

        for i in range(10):
            res = self.client.post(url, payload, format='json')
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        # 11th request must be throttled
        res_throttled = self.client.post(url, payload, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_register_throttled_after_limit(self):
        from django.core.cache import cache
        cache.clear()
        url = '/api/v1/auth/register/'

        for i in range(5):
            payload = {
                'username': f'reg_throttle_user_{i}',
                'email': f'reg_throttle_{i}@example.com',
                'password': 'password123',
                'role': 'customer'
            }
            res = self.client.post(url, payload, format='json')
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # 6th request must be throttled
        payload_6 = {
            'username': 'reg_throttle_user_6',
            'email': 'reg_throttle_6@example.com',
            'password': 'password123',
            'role': 'customer'
        }
        res_throttled = self.client.post(url, payload_6, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_logout_blacklists_refresh_token_and_rejected_by_refresh(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.user)
        refresh_str = str(refresh)

        # Logout by blacklisting refresh token
        logout_url = '/api/v1/auth/logout/'
        res_logout = self.client.post(logout_url, {'refresh': refresh_str}, format='json')
        self.assertEqual(res_logout.status_code, status.HTTP_200_OK)
        self.assertEqual(res_logout.data.get('message'), 'Successfully logged out.')

        # Attempt to use blacklisted refresh token on refresh endpoint -> rejected with 401
        refresh_url = '/api/v1/auth/token/refresh/'
        res_after = self.client.post(refresh_url, {'refresh': refresh_str}, format='json')
        self.assertEqual(res_after.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_twice_is_harmless_idempotent(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.user)
        refresh_str = str(refresh)
        logout_url = '/api/v1/auth/logout/'

        # First logout
        res1 = self.client.post(logout_url, {'refresh': refresh_str}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Second logout (harmless)
        res2 = self.client.post(logout_url, {'refresh': refresh_str}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

    def test_logout_with_invalid_token_returns_400(self):
        logout_url = '/api/v1/auth/logout/'

        # Invalid string
        res_invalid = self.client.post(logout_url, {'refresh': 'garbage_not_a_valid_token'}, format='json')
        self.assertEqual(res_invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_invalid.data.get('code'), 'invalid_token')

        # Empty payload
        res_empty = self.client.post(logout_url, {}, format='json')
        self.assertEqual(res_empty.status_code, status.HTTP_400_BAD_REQUEST)


class SettingsAndLoggingTests(APITestCase):
    def test_mask_sensitive_data_filter_redacts_otp(self):
        import logging
        from config.logging_filters import MaskSensitiveDataFilter, MaskSensitiveFormatter

        record = logging.LogRecord(
            name='test_logger',
            level=logging.INFO,
            pathname='test.py',
            lineno=10,
            msg='Dispatching doorstep OTP 987654 to customer@example.com with code: 123456 and password=secretpass',
            args=(),
            exc_info=None
        )
        filt = MaskSensitiveDataFilter()
        filt.filter(record)

        self.assertNotIn('987654', record.msg)
        self.assertNotIn('123456', record.msg)
        self.assertNotIn('secretpass', record.msg)
        self.assertIn('[REDACTED_OTP]', record.msg)

    def test_mask_sensitive_formatter_redacts_jwt(self):
        import logging
        from config.logging_filters import MaskSensitiveFormatter

        jwt_sample = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.do_not_leak_this_sig'
        record = logging.LogRecord(
            name='test_logger',
            level=logging.INFO,
            pathname='test.py',
            lineno=20,
            msg=f'Authorization header: Bearer {jwt_sample}',
            args=(),
            exc_info=None
        )
        formatter = MaskSensitiveFormatter('%(message)s')
        formatted = formatter.format(record)

        self.assertNotIn(jwt_sample, formatted)
        self.assertIn('[REDACTED_TOKEN]', formatted)


class ProductionCredentialFailClosedTests(APITestCase):
    """
    Unit tests ensuring production security fails closed against mock/insecure credentials
    when DEBUG is False, while leaving development (DEBUG=True) completely unaffected.
    """
    def setUp(self):
        from config.settings import validate_production_credentials
        self.validator = validate_production_credentials
        self.valid_prod_env = {
            'DEBUG': 'False',
            'SECRET_KEY': 'real-strong-production-random-secret-key-123456789',
            'RAZORPAY_KEY_ID': 'rzp_live_realKeyId123',
            'RAZORPAY_KEY_SECRET': 'realLiveSecretKey456',
            'RAZORPAY_WEBHOOK_SECRET': 'realLiveWebhookSecret789',
            'USE_CLOUDINARY': 'True',
            'CLOUDINARY_CLOUD_NAME': 'real_cloud_kindheart',
            'CLOUDINARY_API_KEY': '987654321098765',
            'CLOUDINARY_API_SECRET': 'realCloudinarySecretKeyXyZ',
        }

    def test_dev_mode_debug_true_unaffected(self):
        """When DEBUG is True, mock and insecure credentials must not raise ImproperlyConfigured."""
        from unittest import mock
        dev_env = {
            'DEBUG': 'True',
            'SECRET_KEY': 'django-insecure-pet-adoption-secret-key-dev-only',
            'RAZORPAY_KEY_ID': 'rzp_test_mock_key_id',
            'RAZORPAY_KEY_SECRET': 'mock_secret_key_12345',
            'RAZORPAY_WEBHOOK_SECRET': 'mock_webhook_secret_12345',
            'USE_CLOUDINARY': 'True',
            'CLOUDINARY_CLOUD_NAME': 'mock_cloud',
            'CLOUDINARY_API_KEY': 'mock_key',
            'CLOUDINARY_API_SECRET': 'mock_secret',
        }
        with mock.patch.dict('os.environ', dev_env):
            # Should succeed silently
            self.validator()

    def test_production_valid_real_credentials_pass(self):
        """When DEBUG is False with real production credentials, validation passes."""
        from unittest import mock
        with mock.patch.dict('os.environ', self.valid_prod_env):
            self.validator()

    def test_production_secret_key_insecure_fails(self):
        """When DEBUG is False, SECRET_KEY starting with 'django-insecure' raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        bad_env = dict(self.valid_prod_env, SECRET_KEY='django-insecure-dev-key')
        with mock.patch.dict('os.environ', bad_env):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                self.validator()
            self.assertIn("django-insecure", str(ctx.exception))

    def test_production_secret_key_empty_fails(self):
        """When DEBUG is False, empty SECRET_KEY raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        bad_env = dict(self.valid_prod_env, SECRET_KEY='')
        with mock.patch.dict('os.environ', bad_env):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                self.validator()
            self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_production_razorpay_mock_key_id_fails(self):
        """When DEBUG is False, RAZORPAY_KEY_ID containing 'mock' raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        bad_env = dict(self.valid_prod_env, RAZORPAY_KEY_ID='rzp_live_mock_123')
        with mock.patch.dict('os.environ', bad_env):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                self.validator()
            self.assertIn("RAZORPAY_KEY_ID", str(ctx.exception))

    def test_production_razorpay_mock_secret_fails(self):
        """When DEBUG is False, RAZORPAY_KEY_SECRET containing 'mock' raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        bad_env = dict(self.valid_prod_env, RAZORPAY_KEY_SECRET='super_mock_secret')
        with mock.patch.dict('os.environ', bad_env):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                self.validator()
            self.assertIn("RAZORPAY_KEY_SECRET", str(ctx.exception))

    def test_production_razorpay_mock_webhook_secret_fails(self):
        """When DEBUG is False, RAZORPAY_WEBHOOK_SECRET containing 'mock' raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        bad_env = dict(self.valid_prod_env, RAZORPAY_WEBHOOK_SECRET='mock_wh_secret')
        with mock.patch.dict('os.environ', bad_env):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                self.validator()
            self.assertIn("RAZORPAY_WEBHOOK_SECRET", str(ctx.exception))

    def test_production_cloudinary_mock_fails_when_use_cloudinary_true(self):
        """When DEBUG is False and USE_CLOUDINARY is True, CLOUDINARY_* starting with 'mock' raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        for field in ['CLOUDINARY_CLOUD_NAME', 'CLOUDINARY_API_KEY', 'CLOUDINARY_API_SECRET']:
            bad_env = dict(self.valid_prod_env, **{field: f'mock_{field.lower()}'})
            with mock.patch.dict('os.environ', bad_env):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self.validator()
                self.assertIn(field, str(ctx.exception))

    def test_production_cloudinary_mock_ignored_when_use_cloudinary_false(self):
        """When DEBUG is False and USE_CLOUDINARY is False, mock Cloudinary credentials are not checked."""
        from unittest import mock
        env = dict(
            self.valid_prod_env,
            USE_CLOUDINARY='False',
            CLOUDINARY_CLOUD_NAME='mock_cloud',
            CLOUDINARY_API_KEY='mock_key',
            CLOUDINARY_API_SECRET='mock_secret',
        )
        with mock.patch.dict('os.environ', env):
            # Should not raise because Cloudinary is disabled
            self.validator()






