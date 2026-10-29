import sys
from pathlib import Path

# Add backend directory to sys.path so IDE linters resolve apps module correctly
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import re
from decimal import Decimal
import jwt
from django.conf import settings
from django.core import mail
from django.test import override_settings
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
            'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
            'EMAIL_HOST': 'smtp.sendgrid.net',
            'DEFAULT_FROM_EMAIL': 'KindHeart Pet Adoption <no-reply@kindheart.org>',
            'FRONTEND_BASE_URL': 'https://petadopt.kindheart.org',
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
            'EMAIL_BACKEND': 'django.core.mail.backends.console.EmailBackend',
            'EMAIL_HOST': 'mock_host',
            'DEFAULT_FROM_EMAIL': 'mock_email@test.com',
            'FRONTEND_BASE_URL': 'http://mock-frontend.local',
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

    def test_production_email_backend_insecure_fails(self):
        """When DEBUG is False, console, locmem, dummy, or filebased EMAIL_BACKEND raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        invalid_backends = [
            'django.core.mail.backends.console.EmailBackend',
            'django.core.mail.backends.locmem.EmailBackend',
            'django.core.mail.backends.dummy.EmailBackend',
            'django.core.mail.backends.filebased.EmailBackend',
        ]
        for backend in invalid_backends:
            bad_env = dict(self.valid_prod_env, EMAIL_BACKEND=backend)
            with mock.patch.dict('os.environ', bad_env):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self.validator()
                self.assertIn("EMAIL_BACKEND", str(ctx.exception))

    def test_production_email_host_empty_or_mock_fails(self):
        """When DEBUG is False, empty or mock EMAIL_HOST raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        for bad_host in ['', 'mock_smtp_host']:
            bad_env = dict(self.valid_prod_env, EMAIL_HOST=bad_host)
            with mock.patch.dict('os.environ', bad_env):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self.validator()
                self.assertIn("EMAIL_HOST", str(ctx.exception))

    def test_production_default_from_email_empty_or_mock_fails(self):
        """When DEBUG is False, empty or mock DEFAULT_FROM_EMAIL raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        for bad_email in ['', 'mock@kindheart.org']:
            bad_env = dict(self.valid_prod_env, DEFAULT_FROM_EMAIL=bad_email)
            with mock.patch.dict('os.environ', bad_env):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self.validator()
                self.assertIn("DEFAULT_FROM_EMAIL", str(ctx.exception))

    def test_production_frontend_base_url_empty_or_mock_fails(self):
        """When DEBUG is False, empty or mock FRONTEND_BASE_URL raises ImproperlyConfigured."""
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        for bad_url in ['', 'http://mock-frontend.local', 'https://mock.example.com']:
            bad_env = dict(self.valid_prod_env, FRONTEND_BASE_URL=bad_url)
            with mock.patch.dict('os.environ', bad_env):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self.validator()
                self.assertIn("FRONTEND_BASE_URL", str(ctx.exception))


class SeedPlatformCommandSafetyTests(APITestCase):
    """
    Tests ensuring seed_platform refuses to run when settings.DEBUG is False,
    raising CommandError before any database operations and producing zero DB mutations.
    """
    @override_settings(DEBUG=False)
    def test_seed_platform_refuses_when_debug_false(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from apps.accounts.models import User
        from apps.pets.models import Pet

        initial_user_count = User.objects.count()
        initial_pet_count = Pet.objects.count()

        with self.assertRaises(CommandError) as ctx:
            call_command('seed_platform')

        self.assertIn("strictly forbidden when DEBUG is False", str(ctx.exception))
        # Ensure zero rows were created
        self.assertEqual(User.objects.count(), initial_user_count)
        self.assertEqual(Pet.objects.count(), initial_pet_count)


class PasswordResetAndChangeTests(APITestCase):
    """
    Comprehensive tests for password reset request, confirmation, and authenticated change.
    Enforces enumeration protection, token single-use, expiration, strength validation,
    session revocation via refresh token blacklisting, and throttle enforcement.
    """
    def setUp(self):
        from django.core.cache import cache
        from django.core import mail
        cache.clear()
        mail.outbox = []
        self.user = User.objects.create_user(
            username='reset_tester',
            email='reset_tester@example.com',
            password='InitialPassword123!',
            role='customer',
            is_active=True,
        )

    def test_password_reset_request_happy_path(self):
        url = '/api/v1/auth/password-reset/'
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'email': 'reset_tester@example.com'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data.get('message'),
            "If an account with that email exists, a password reset link has been sent."
        )
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertIn("reset_tester@example.com", sent_email.to)
        self.assertIn("Reset Your Password", sent_email.subject)
        # Check link format
        body = sent_email.body
        self.assertIn("http://localhost:3000/reset-password?uid=", body)
        self.assertIn("&token=", body)

    @override_settings(AUTH_PASSWORD_RESET_ASYNC_EMAIL=True)
    def test_password_reset_request_async_threading_does_not_call_send_mail_synchronously(self):
        """
        Milestone 10d Task 3: When AUTH_PASSWORD_RESET_ASYNC_EMAIL is True,
        send_mail is dispatched in a daemon thread and does not block the request thread synchronously.
        """
        from unittest.mock import patch

        url = '/api/v1/auth/password-reset/'

        with patch('apps.accounts.views.send_mail') as mock_send_mail:
            with patch('apps.accounts.views.threading.Thread') as mock_thread_cls:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(url, {'email': 'reset_tester@example.com'}, format='json')

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                # Verify send_mail was NOT called synchronously on request thread
                mock_send_mail.assert_not_called()

                # Verify daemon thread was instantiated and started
                mock_thread_cls.assert_called_once()
                _, kwargs = mock_thread_cls.call_args
                self.assertTrue(kwargs.get('daemon'))
                self.assertEqual(kwargs.get('name'), f"pwd-reset-email-{self.user.pk}")
                mock_thread_cls.return_value.start.assert_called_once()

    def test_password_reset_request_unknown_email_returns_identical_response_and_sends_nothing(self):
        url = '/api/v1/auth/password-reset/'
        response = self.client.post(url, {'email': 'nonexistent_user@example.com'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data.get('message'),
            "If an account with that email exists, a password reset link has been sent."
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_request_inactive_user_returns_identical_response_and_sends_nothing(self):
        self.user.is_active = False
        self.user.save()
        url = '/api/v1/auth/password-reset/'
        response = self.client.post(url, {'email': self.user.email}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data.get('message'),
            "If an account with that email exists, a password reset link has been sent."
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_confirm_happy_path_and_blacklists_sessions(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes
        from rest_framework_simplejwt.tokens import RefreshToken

        # Issue a refresh token prior to reset
        old_refresh = RefreshToken.for_user(self.user)
        old_refresh_str = str(old_refresh)

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        confirm_url = '/api/v1/auth/password-reset/confirm/'
        payload = {
            'uid': uid,
            'token': token,
            'new_password': 'BrandNewPassword987!',
        }
        res = self.client.post(confirm_url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("Password has been reset successfully", res.data.get('message', ''))

        # Can log in with new password
        login_url = '/api/v1/auth/login/'
        res_login = self.client.post(login_url, {'username': self.user.username, 'password': 'BrandNewPassword987!'}, format='json')
        self.assertEqual(res_login.status_code, status.HTTP_200_OK)

        # Old password rejected
        res_old = self.client.post(login_url, {'username': self.user.username, 'password': 'InitialPassword123!'}, format='json')
        self.assertEqual(res_old.status_code, status.HTTP_401_UNAUTHORIZED)

        # Refresh token issued before reset is blacklisted
        refresh_url = '/api/v1/auth/token/refresh/'
        res_refresh = self.client.post(refresh_url, {'refresh': old_refresh_str}, format='json')
        self.assertEqual(res_refresh.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_reset_token_is_single_use(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        confirm_url = '/api/v1/auth/password-reset/confirm/'
        payload = {
            'uid': uid,
            'token': token,
            'new_password': 'FirstResetPassword123!',
        }
        res1 = self.client.post(confirm_url, payload, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Second attempt with same token must fail
        payload2 = {
            'uid': uid,
            'token': token,
            'new_password': 'SecondResetPassword123!',
        }
        res2 = self.client.post(confirm_url, payload2, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res2.data.get('code'), 'invalid_token')

    @override_settings(PASSWORD_RESET_TIMEOUT=-10)
    def test_password_reset_expired_token(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        confirm_url = '/api/v1/auth/password-reset/confirm/'
        payload = {
            'uid': uid,
            'token': token,
            'new_password': 'NewPassword123!',
        }
        res = self.client.post(confirm_url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_token')

    def test_password_reset_tampered_or_bad_uid(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        confirm_url = '/api/v1/auth/password-reset/confirm/'
        # Bad UID
        res_bad_uid = self.client.post(confirm_url, {
            'uid': 'invalid-base64',
            'token': token,
            'new_password': 'ValidPassword123!',
        }, format='json')
        self.assertEqual(res_bad_uid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_uid.data.get('code'), 'invalid_token')

        # Tampered Token
        res_bad_token = self.client.post(confirm_url, {
            'uid': uid,
            'token': 'tampered-invalid-token',
            'new_password': 'ValidPassword123!',
        }, format='json')
        self.assertEqual(res_bad_token.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_token.data.get('code'), 'invalid_token')

    def test_password_reset_weak_password_rejected(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        confirm_url = '/api/v1/auth/password-reset/confirm/'
        payload = {
            'uid': uid,
            'token': token,
            'new_password': '123',  # Too short / common
        }
        res = self.client.post(confirm_url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'weak_password')
        self.assertIn('messages', res.data)

    def test_password_change_requires_authentication(self):
        url = '/api/v1/auth/password/change/'
        res = self.client.post(url, {'old_password': 'foo', 'new_password': 'bar'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_change_wrong_old_password(self):
        self.client.force_authenticate(user=self.user)
        url = '/api/v1/auth/password/change/'
        res = self.client.post(url, {
            'old_password': 'WrongPassword123!',
            'new_password': 'NewStrongPassword123!',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_password')

    def test_password_change_weak_new_password(self):
        self.client.force_authenticate(user=self.user)
        url = '/api/v1/auth/password/change/'
        res = self.client.post(url, {
            'old_password': 'InitialPassword123!',
            'new_password': '123',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'weak_password')
        self.assertIn('messages', res.data)

    def test_password_change_happy_path_and_blacklists_sessions(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        old_refresh = RefreshToken.for_user(self.user)
        old_refresh_str = str(old_refresh)

        self.client.force_authenticate(user=self.user)
        url = '/api/v1/auth/password/change/'
        res = self.client.post(url, {
            'old_password': 'InitialPassword123!',
            'new_password': 'UpdatedPassword123!',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("Password changed successfully", res.data.get('message', ''))

        # Old refresh token is blacklisted
        refresh_url = '/api/v1/auth/token/refresh/'
        res_refresh = self.client.post(refresh_url, {'refresh': old_refresh_str}, format='json')
        self.assertEqual(res_refresh.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_reset_throttling(self):
        from django.core.cache import cache
        cache.clear()
        url = '/api/v1/auth/password-reset/'
        for _ in range(5):
            res = self.client.post(url, {'email': 'someone@example.com'}, format='json')
            self.assertEqual(res.status_code, status.HTTP_200_OK)

        # 6th request must be 429
        res_throttled = self.client.post(url, {'email': 'someone@example.com'}, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_password_reset_confirm_throttling(self):
        from django.core.cache import cache
        cache.clear()
        url = '/api/v1/auth/password-reset/confirm/'
        for _ in range(5):
            res = self.client.post(url, {'uid': 'abc', 'token': 'def', 'new_password': '123'}, format='json')
            self.assertIn(res.status_code, (status.HTTP_400_BAD_REQUEST, status.HTTP_200_OK))

        # 6th request must be 429
        res_throttled = self.client.post(url, {'uid': 'abc', 'token': 'def', 'new_password': '123'}, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class EmailVerificationTests(APITestCase):
    """
    Tests for email verification workflow:
    - Verification email sent on registration after commit
    - Verification success and idempotency
    - Rejection of mismatched, expired, or tampered tokens
    - Email modification resetting verification status
    - Resend verification behavior and throttling
    - Inclusion of email_verified in /me/ endpoint
    """
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        mail.outbox = []
        self.user = User.objects.create_user(
            username='verify_user',
            email='verify_user@example.com',
            password='InitialPassword123!',
            role='customer',
            is_active=True,
            email_verified=False,
        )

    def test_registration_sends_exactly_one_verification_email(self):
        url = '/api/v1/auth/register/'
        payload = {
            'username': 'brand_new_adopter',
            'email': 'brand_new@example.com',
            'password': 'StrongPassword123!',
            'role': 'customer'
        }
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("brand_new@example.com", email.to)
        self.assertIn("Verify Your Email Address", email.subject)
        self.assertIn("http://localhost:3000/verify-email?token=", email.body)

        created_user = User.objects.get(username='brand_new_adopter')
        self.assertFalse(created_user.email_verified)
        self.assertIsNone(created_user.email_verified_at)

    def test_verify_email_success_and_idempotent(self):
        from apps.accounts.email_verification import generate_email_verification_token
        token = generate_email_verification_token(self.user)

        url = '/api/v1/auth/verify-email/'
        res = self.client.post(url, {'token': token}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('message'), "Email address verified successfully.")

        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertIsNotNone(self.user.email_verified_at)
        original_verified_at = self.user.email_verified_at

        # Idempotent: second call succeeds without changing timestamp
        res2 = self.client.post(url, {'token': token}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email_verified_at, original_verified_at)

    def test_verify_email_wrong_user_or_email_mismatch_rejected(self):
        from apps.accounts.email_verification import generate_email_verification_token
        token = generate_email_verification_token(self.user)

        # Alter user's email directly without triggering email verification reset
        User.objects.filter(pk=self.user.pk).update(email='different@example.com')

        url = '/api/v1/auth/verify-email/'
        res = self.client.post(url, {'token': token}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_token')

    @override_settings(EMAIL_VERIFY_MAX_AGE=-10)
    def test_verify_email_expired_token_rejected(self):
        from apps.accounts.email_verification import generate_email_verification_token
        token = generate_email_verification_token(self.user)

        url = '/api/v1/auth/verify-email/'
        res = self.client.post(url, {'token': token}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_token')

    def test_verify_email_tampered_token_rejected(self):
        url = '/api/v1/auth/verify-email/'
        res = self.client.post(url, {'token': 'completely.tampered.token'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_token')

    def test_email_change_resets_verification_and_sends_new_link(self):
        self.user.email_verified = True
        self.user.email_verified_at = timezone.now()
        self.user.save()

        # Changing email address resets verification
        with self.captureOnCommitCallbacks(execute=True):
            self.user.email = 'new_email_address@example.com'
            self.user.save()

        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertIsNone(self.user.email_verified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new_email_address@example.com", mail.outbox[0].to)

    def test_resend_verification_sends_only_when_unverified(self):
        self.client.force_authenticate(user=self.user)
        url = '/api/v1/auth/resend-verification/'

        # 1. When unverified: returns 200 and sends email
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(url, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(res.data.get('message'), "Verification email dispatched.")

        mail.outbox.clear()
        # 2. When verified: returns 200 but sends no email
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])

        with self.captureOnCommitCallbacks(execute=True):
            res_verified = self.client.post(url, format='json')
        self.assertEqual(res_verified.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(res_verified.data.get('message'), "Email is already verified.")

    def test_resend_verification_throttled(self):
        from django.core.cache import cache
        cache.clear()
        self.client.force_authenticate(user=self.user)
        url = '/api/v1/auth/resend-verification/'

        for _ in range(5):
            res = self.client.post(url, format='json')
            self.assertEqual(res.status_code, status.HTTP_200_OK)

        # 6th request must be 429
        res_throttled = self.client.post(url, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_me_profile_includes_email_verified(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('email_verified', res.data)
        self.assertFalse(res.data['email_verified'])

        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])

        res2 = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertTrue(res2.data['email_verified'])


class LogHygieneTests(APITestCase):
    """
    Milestone 10c Task 3: Verifies that OTP flows, password reset, and email verification
    never log customer email addresses, uids, tokens, or plaintext 6-digit OTP strings.
    """
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        mail.outbox.clear()
        self.user = User.objects.create_user(
            username='log_hygiene_user',
            email='sensitive_adopter@example.com',
            password='Password123!',
            role='customer',
            email_verified=True
        )

    def test_log_hygiene_otp_flow(self):
        from apps.accounts.models import ShelterProfile, DeliveryProfile
        from apps.pets.models import Pet
        from apps.adoptions.models import AdoptionApplication
        from apps.logistics.services import assign_delivery_task, verify_pickup_service, verify_doorstep_service, OTPVerificationOutcome

        shelter_user = User.objects.create_user(
            username='log_shelter_owner',
            email='shelter_sensitive@example.com',
            password='password123',
            role='shelter'
        )
        shelter = ShelterProfile.objects.create(
            user=shelter_user,
            organization_name='Log Shelter',
            pickup_address='123 Log St',
            is_verified=True
        )
        driver_user = User.objects.create_user(
            username='log_driver_user',
            email='driver_sensitive@example.com',
            password='password123',
            role='delivery'
        )
        driver = DeliveryProfile.objects.create(
            user=driver_user,
            shelter=shelter,
            is_available=True
        )
        pet = Pet.objects.create(
            shelter=shelter,
            name='LoggiePet',
            species='dog',
            breed='Beagle',
            age_months=12,
            gender='male',
            status='available',
            adoption_fee=Decimal('100.00')
        )
        app = AdoptionApplication.objects.create(
            applicant=self.user,
            pet=pet,
            status='approved',
            payment_method='cod',
            total_amount=Decimal('100.00'),
            contact_phone='9876543210'
        )

        with self.assertLogs('apps.logistics.services', level='INFO') as cm:
            with self.captureOnCommitCallbacks(execute=True):
                delivery = assign_delivery_task(app, driver)

            with self.captureOnCommitCallbacks(execute=True):
                pickup_res = verify_pickup_service(delivery.id, delivery.raw_pickup_otp)
            self.assertEqual(pickup_res.outcome, OTPVerificationOutcome.SUCCESS)

            # Get doorstep OTP from mail
            doorstep_mail = [m for m in mail.outbox if "doorstep" in m.subject.lower() or "otp" in m.subject.lower()][-1]
            doorstep_otp_match = re.search(r'\b\d{6}\b', doorstep_mail.body)
            self.assertIsNotNone(doorstep_otp_match)
            doorstep_otp = doorstep_otp_match.group(0)

            # Satisfy COD payment guard
            delivery.cash_collected = Decimal('100.00')
            delivery.save(update_fields=['cash_collected'])

            with self.captureOnCommitCallbacks(execute=True):
                doorstep_res = verify_doorstep_service(delivery.id, doorstep_otp)
            self.assertEqual(doorstep_res.outcome, OTPVerificationOutcome.SUCCESS)

        full_logs = "\n".join(cm.output)
        # Verify no email address, customer username, or 6-digit OTP string in logs
        self.assertNotIn('sensitive_adopter@example.com', full_logs)
        self.assertNotIn('shelter_sensitive@example.com', full_logs)
        self.assertNotIn('driver_sensitive@example.com', full_logs)
        self.assertNotIn(doorstep_otp, full_logs)
        self.assertNotIn(delivery.raw_pickup_otp, full_logs)
        # Ensure regex 6-digit number is not present
        six_digit_matches = re.findall(r'\b\d{6}\b', full_logs)
        self.assertEqual(six_digit_matches, [], f"Found 6-digit OTP in logs: {six_digit_matches}")

    def test_log_hygiene_password_reset_and_email_verification(self):
        import logging
        test_logger = logging.getLogger('apps.accounts.views')

        # 1. Password reset request
        with self.assertLogs('apps.accounts.views', level='INFO') as cm_reset:
            test_logger.info("Executing password reset for user ID %s", self.user.id)
            with self.captureOnCommitCallbacks(execute=True):
                res_reset = self.client.post(
                    '/api/v1/auth/password-reset/',
                    {'email': 'sensitive_adopter@example.com'},
                    format='json'
                )
            self.assertEqual(res_reset.status_code, status.HTTP_200_OK)

        reset_logs = "\n".join(cm_reset.output)
        self.assertNotIn('sensitive_adopter@example.com', reset_logs)

        # Extract token from mail
        self.assertEqual(len(mail.outbox), 1)
        reset_mail = mail.outbox[0].body
        token_match = re.search(r'token=([A-Za-z0-9_-]+)', reset_mail)
        self.assertIsNotNone(token_match)
        raw_token = token_match.group(1)

        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))

        # 2. Confirm password reset
        with self.assertLogs('apps.accounts.views', level='INFO') as cm_confirm:
            test_logger.info("Confirming password reset for user ID %s", self.user.id)
            res_confirm = self.client.post('/api/v1/auth/password-reset/confirm/', {
                'uid': uid,
                'token': raw_token,
                'new_password': 'NewPassword123!Secure'
            }, format='json')
            self.assertEqual(res_confirm.status_code, status.HTTP_200_OK)

        confirm_logs = "\n".join(cm_confirm.output)
        self.assertNotIn('sensitive_adopter@example.com', confirm_logs)
        self.assertNotIn(raw_token, confirm_logs)
        self.assertNotIn(uid, confirm_logs)

        # 3. Registration and email verification
        mail.outbox.clear()
        with self.assertLogs('apps.accounts.views', level='INFO') as cm_reg:
            test_logger.info("Registering user account")
            with self.captureOnCommitCallbacks(execute=True):
                res_reg = self.client.post('/api/v1/auth/register/', {
                    'username': 'new_hygiene_user',
                    'email': 'new_hygiene@example.com',
                    'password': 'Password123!'
                }, format='json')
            self.assertEqual(res_reg.status_code, status.HTTP_201_CREATED)

        reg_logs = "\n".join(cm_reg.output)
        self.assertNotIn('new_hygiene@example.com', reg_logs)

        self.assertEqual(len(mail.outbox), 1)
        v_token_match = re.search(r'token=([A-Za-z0-9_:-]+)', mail.outbox[0].body)
        self.assertIsNotNone(v_token_match)
        v_token = v_token_match.group(1)

        with self.assertLogs('apps.accounts.views', level='INFO') as cm_verify:
            test_logger.info("Verifying email address")
            res_verify = self.client.post('/api/v1/auth/verify-email/', {'token': v_token}, format='json')
            self.assertEqual(res_verify.status_code, status.HTTP_200_OK)

        verify_logs = "\n".join(cm_verify.output)
        self.assertNotIn('new_hygiene@example.com', verify_logs)
        self.assertNotIn(v_token, verify_logs)


class EmailChangeHardeningTests(APITestCase):
    def setUp(self):
        mail.outbox.clear()
        self.user = User.objects.create_user(
            username='email_change_user',
            email='old_address@example.com',
            password='OriginalPassword123!',
            role='customer',
            email_verified=True,
            first_name='OriginalFirst',
            last_name='OriginalLast',
            phone='1234567890'
        )
        self.client.force_authenticate(user=self.user)
        self.endpoint = '/api/v1/auth/me/'

    def test_missing_password_blocks_email_change_returns_400_invalid_password(self):
        res = self.client.patch(self.endpoint, {'email': 'new_address@example.com'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_password')

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old_address@example.com')
        self.assertTrue(self.user.email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_password_blocks_email_change_returns_400_invalid_password(self):
        res = self.client.patch(self.endpoint, {
            'email': 'new_address@example.com',
            'current_password': 'WrongPassword999!'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'invalid_password')

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old_address@example.com')
        self.assertTrue(self.user.email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_email_change_sends_notice_to_old_and_link_to_new_and_resets_verified(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.patch(self.endpoint, {
                'email': 'new_address@example.com',
                'current_password': 'OriginalPassword123!'
            }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new_address@example.com')
        self.assertFalse(self.user.email_verified)
        self.assertIsNone(self.user.email_verified_at)

        # 2 emails must be dispatched: notice to OLD address, verification to NEW address
        self.assertEqual(len(mail.outbox), 2)

        # Check old address notice
        old_notices = [m for m in mail.outbox if 'old_address@example.com' in m.to]
        self.assertEqual(len(old_notices), 1)
        old_mail = old_notices[0]
        self.assertIn("Security Alert", old_mail.subject)
        # Verify NO links in notice
        self.assertNotIn("http://", old_mail.body)
        self.assertNotIn("https://", old_mail.body)
        self.assertNotIn("token=", old_mail.body)

        # Check new address verification email
        new_notices = [m for m in mail.outbox if 'new_address@example.com' in m.to]
        self.assertEqual(len(new_notices), 1)
        new_mail = new_notices[0]
        self.assertIn("Verify Your Email Address", new_mail.subject)
        self.assertIn("token=", new_mail.body)

    def test_profile_update_without_email_change_needs_no_password(self):
        res = self.client.patch(self.endpoint, {
            'first_name': 'NewFirst',
            'phone': '9876543210'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'NewFirst')
        self.assertEqual(self.user.phone, '9876543210')
        self.assertEqual(self.user.email, 'old_address@example.com')
        self.assertTrue(self.user.email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_unchanged_email_needs_no_password(self):
        res = self.client.put(self.endpoint, {
            'email': 'old_address@example.com',
            'first_name': 'PutFirst',
            'phone': '1112223333'
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old_address@example.com')
        self.assertEqual(self.user.first_name, 'PutFirst')
        self.assertTrue(self.user.email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_legacy_aliases_email_change_hardening(self):
        # Test /api/auth/me/ alias
        res_auth_me = self.client.patch('/api/auth/me/', {'email': 'alias@example.com'}, format='json')
        self.assertEqual(res_auth_me.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_auth_me.data.get('code'), 'invalid_password')

        # Test /api/v1/me/ alias
        res_v1_me = self.client.patch('/api/v1/me/', {'email': 'alias2@example.com'}, format='json')
        self.assertEqual(res_v1_me.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_v1_me.data.get('code'), 'invalid_password')





