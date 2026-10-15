import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hashlib
import hmac
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.core import mail
import threading
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase
from rest_framework import status

from apps.accounts.models import User, ShelterProfile, DeliveryProfile, Notification
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication
from django.db import transaction
from apps.logistics.models import PetDelivery, DeliveryLocationPing, Payment, PaymentStatus
from apps.logistics.serializers import (
    DeliveryDriverTaskSerializer,
    DeliveryCustomerSerializer,
    ShelterDeliverySerializer
)
from apps.logistics.otp import issue_otp, check_otp
from apps.logistics.services import (
    assign_delivery_task,
    check_pickup_payment_guard,
    check_doorstep_payment_guard,
    is_payment_satisfied,
    verify_pickup_service,
    verify_doorstep_service,
    OTPVerificationOutcome,
    verify_pickup_otp,
    verify_doorstep_otp,
    dispatch_doorstep_otp,
    send_driver_assigned_notice,
    reissue_delivery_otp,
    ConflictError,
)


class LogisticsTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create Shelter User & Profile
        self.shelter_user = User.objects.create_user(
            username='shelter1',
            email='shelter1@example.com',
            password='password123',
            role='shelter',
            phone='1234567890',
            address='123 Shelter St'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Happy Paws Shelter',
            pickup_address='123 Shelter St',
            is_verified=True
        )

        # Create Driver User & Profile
        self.driver_user = User.objects.create_user(
            username='driver1',
            email='driver1@example.com',
            password='password123',
            role='delivery',
            phone='9876543210'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.shelter_profile,
            vehicle_type='two_wheeler',
            vehicle_number='KA-01-AB-1234',
            is_available=True
        )

        # Create Customer User
        self.customer_user = User.objects.create_user(
            username='customer1',
            email='customer1@example.com',
            password='password123',
            role='customer',
            phone='5551234567',
            address='456 Customer Ave'
        )

        # Create Pet
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Buddy',
            species='dog',
            breed='Golden Retriever',
            age_months=24,
            gender='male',
            status='available'
        )

        # Create Approved Adoption Application
        self.application = AdoptionApplication.objects.create(
            applicant=self.customer_user,
            pet=self.pet,
            status='approved',
            living_situation='House with yard',
            dropoff_address=self.customer_user.address,
            delivery_address='456 Customer Ave, Suite 101',
            delivery_city='Bangalore',
            delivery_pincode='560001',
            destination_latitude=Decimal('12.971600'),
            destination_longitude=Decimal('77.594600'),
            contact_phone=self.customer_user.phone,
            total_amount=Decimal('150.00'),
            payment_status='pending'
        )

    def test_driver_assignment_and_availability_toggle(self):
        """
        Tests driver assignment:
        - Delivery created in 'assigned' status.
        - Plaintext OTPs in database are empty (cryptographic hash stored).
        - Single-use pickup_otp is returned once in response data.
        - Driver availability toggled to False.
        """
        self.client.force_authenticate(user=self.shelter_user)
        url = '/api/v1/shelter/deliveries/assign/'
        payload = {
            'application_public_id': str(self.application.public_id),
            'driver_public_id': str(self.driver_profile.public_id),
            'payment_method': 'cod'
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify pickup_otp is provided once in response body
        self.assertIn('pickup_otp', response.data)
        self.assertEqual(len(response.data['pickup_otp']), 6)

        # Verify delivery created in DB without plaintext OTP storage
        delivery = PetDelivery.objects.get(application=self.application)
        self.assertEqual(delivery.status, 'assigned')
        self.assertEqual(delivery.pickup_otp, '')
        self.assertEqual(delivery.doorstep_otp, '')
        self.assertIsNotNone(delivery.pickup_otp_hash)
        self.assertIsNone(delivery.doorstep_otp_hash)

        # Verify driver availability updated to False
        self.driver_profile.refresh_from_db()
        self.assertFalse(self.driver_profile.is_available)

    def test_driver_single_tasking_rejection(self):
        # Assign first delivery task
        assign_delivery_task(self.application, self.driver_profile, payment_method='cod')

        # Create second approved application for same shelter
        pet2 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Max',
            species='dog',
            breed='Beagle',
            age_months=12,
            gender='male',
            status='available'
        )
        app2 = AdoptionApplication.objects.create(
            applicant=self.customer_user,
            pet=pet2,
            status='approved',
            living_situation='House with yard',
            dropoff_address=self.customer_user.address,
            contact_phone=self.customer_user.phone
        )

        self.client.force_authenticate(user=self.shelter_user)
        url = '/api/v1/shelter/deliveries/assign/'
        payload = {
            'application_public_id': str(app2.public_id),
            'driver_public_id': str(self.driver_profile.public_id),
            'payment_method': 'cod'
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pickup_otp_verification(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        pickup_code = delivery.raw_pickup_otp

        self.client.force_authenticate(user=self.driver_user)

        # Test active task GET endpoint
        active_res = self.client.get('/api/v1/delivery/tasks/active/')
        self.assertEqual(active_res.status_code, status.HTTP_200_OK)
        self.assertEqual(active_res.data['public_id'], str(delivery.public_id))
        self.assertEqual(active_res.data['delivery_address'], '456 Customer Ave, Suite 101')
        self.assertEqual(active_res.data['delivery_city'], 'Bangalore')
        self.assertEqual(active_res.data['delivery_pincode'], '560001')
        self.assertEqual(float(active_res.data['destination_latitude']), 12.9716)
        self.assertEqual(float(active_res.data['destination_longitude']), 77.5946)

        # Test pickup OTP verification
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        response = self.client.post(url, {'otp': pickup_code}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')
        self.assertIsNotNone(delivery.dispatched_at)
        self.assertIsNotNone(delivery.doorstep_otp_hash)

    def test_doorstep_otp_brute_force_protection(self):
        """
        Tests attempt counter and lockout:
        OTP_MAX_ATTEMPTS is 3:
        Attempts 1-3 fail with 400 Bad Request and decrement attempts_remaining.
        Attempt 4 returns HTTP 423 Locked.
        """
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'

        # Fail attempt 1
        res1 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res1.data['attempts_remaining'], 2)

        # Fail attempt 2
        res2 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res2.data['attempts_remaining'], 1)

        # Fail attempt 3
        res3 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res3.data['attempts_remaining'], 0)

        delivery.refresh_from_db()
        self.assertEqual(delivery.doorstep_otp_attempts, 3)

        # Attempt 4: Lockout -> HTTP 423 Locked
        res4 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res4.status_code, 423)
        self.assertIn('Maximum OTP verification attempts exceeded', res4.data['detail'])

    def test_successful_doorstep_otp_verification(self):
        with self.captureOnCommitCallbacks(execute=True):
            delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            verify_pickup_otp(delivery, delivery.raw_pickup_otp)

        # Obtain doorstep OTP from dispatched email
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[0].body)
        self.assertIsNotNone(match)
        doorstep_code = match.group(1)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'otp': doorstep_code}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'delivered')
        self.assertIsNotNone(delivery.delivered_at)

        # Check driver profile is free again
        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

        # Check pet and application status updated
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'adopted')

        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'completed')

    def test_gps_ping_ingestion_and_tracking(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)

        self.client.force_authenticate(user=self.driver_user)
        ping_url = f'/api/v1/delivery/tasks/{delivery.public_id}/ping/'
        payload = {
            'latitude': '12.971598',
            'longitude': '77.594566'
        }
        ping_res = self.client.post(ping_url, payload, format='json')
        self.assertEqual(ping_res.status_code, status.HTTP_201_CREATED)

        # Verify ping record created
        self.assertEqual(DeliveryLocationPing.objects.filter(delivery=delivery).count(), 1)

        # Check driver and delivery coordinates updated
        delivery.refresh_from_db()
        self.assertEqual(delivery.current_latitude, Decimal('12.971598'))
        self.assertEqual(delivery.current_longitude, Decimal('77.594566'))

        self.driver_profile.refresh_from_db()
        self.assertEqual(self.driver_profile.current_latitude, Decimal('12.971598'))
        self.assertEqual(self.driver_profile.current_longitude, Decimal('77.594566'))

        # Test tracking endpoint for Customer: must NOT expose doorstep_otp
        self.client.force_authenticate(user=self.customer_user)
        tracking_url = f'/api/v1/delivery/tasks/{delivery.public_id}/tracking/'
        tracking_res = self.client.get(tracking_url)
        self.assertEqual(tracking_res.status_code, status.HTTP_200_OK)
        self.assertEqual(tracking_res.data['status'], 'out_for_delivery')
        self.assertNotIn('doorstep_otp', tracking_res.data['delivery'])
        self.assertIn('driver_details', tracking_res.data['delivery'])
        self.assertEqual(len(tracking_res.data['breadcrumbs']), 1)

    @patch('razorpay.Client')
    def test_razorpay_order_creation(self, mock_razorpay_client):
        mock_instance = MagicMock()
        mock_instance.order.create.return_value = {
            'id': 'order_test_12345',
            'amount': 15000,
            'currency': 'INR',
            'status': 'created'
        }
        mock_razorpay_client.return_value = mock_instance

        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='online')
        self.client.force_authenticate(user=self.customer_user)

        url = '/api/v1/payments/create-order/'
        payload = {
            'delivery_public_id': str(delivery.public_id),
            'amount_in_paise': 15000
        }
        res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['razorpay_order_id'], 'order_test_12345')
        self.assertEqual(res.data['amount_in_paise'], 15000)

        # Check Payment ledger entry
        payment = Payment.objects.get(razorpay_order_id='order_test_12345')
        self.assertEqual(payment.status, 'created')
        self.assertEqual(payment.delivery_id, delivery.id)

    def test_razorpay_payment_signature_verification_success(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='online')
        payment = Payment.objects.create(
            delivery=delivery,
            application=self.application,
            amount_in_paise=15000,
            currency='INR',
            status='created',
            razorpay_order_id='order_mock_123'
        )

        order_id = 'order_mock_123'
        payment_id = 'pay_mock_456'
        msg = f"{order_id}|{payment_id}".encode('utf-8')
        secret = settings.RAZORPAY_KEY_SECRET.encode('utf-8')
        valid_signature = hmac.new(secret, msg, hashlib.sha256).hexdigest()

        self.client.force_authenticate(user=self.customer_user)
        url = '/api/v1/payments/verify/'
        payload = {
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': valid_signature
        }
        res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'captured')
        self.assertEqual(payment.razorpay_payment_id, payment_id)

    def test_razorpay_payment_signature_verification_failure(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='online')
        payment = Payment.objects.create(
            delivery=delivery,
            application=self.application,
            amount_in_paise=15000,
            currency='INR',
            status='created',
            razorpay_order_id='order_mock_invalid'
        )

        self.client.force_authenticate(user=self.customer_user)
        url = '/api/v1/payments/verify/'
        payload = {
            'razorpay_order_id': 'order_mock_invalid',
            'razorpay_payment_id': 'pay_mock_invalid',
            'razorpay_signature': 'invalid_signature_hex_1234567890'
        }
        res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'created')


class DoorstepOTPDispatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.shelter_user = User.objects.create_user(
            username='alert_shelter',
            email='shelter@alerts.org',
            password='password123',
            role='shelter',
            phone='1112223333',
            address='100 Shelter Blvd'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Alert Shelter',
            pickup_address='100 Shelter Blvd',
            is_verified=True
        )

        self.driver_user = User.objects.create_user(
            username='alert_driver',
            email='driver@alerts.org',
            password='password123',
            role='delivery',
            phone='9998887777'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.shelter_profile,
            vehicle_type='car',
            vehicle_number='KA-05-CD-5678',
            is_available=True
        )

        self.customer_user = User.objects.create_user(
            username='alert_customer',
            email='customer@alerts.org',
            password='password123',
            role='customer',
            phone='4445556666',
            address='200 Adopter Lane'
        )

        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='Luna',
            species='cat',
            breed='Siamese',
            age_months=12,
            gender='female',
            status='available'
        )

        self.application = AdoptionApplication.objects.create(
            applicant=self.customer_user,
            pet=self.pet,
            status='approved',
            living_situation='Apartment',
            dropoff_address=self.customer_user.address,
            contact_phone=self.customer_user.phone
        )

    def test_dispatch_doorstep_otp_service_success(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        mail.outbox.clear()

        # dispatch_doorstep_otp with raw_otp sends email and notification without code
        result = dispatch_doorstep_otp(delivery, raw_otp='654321')
        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)

        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.to, [self.customer_user.email])
        self.assertIn(self.pet.name, sent_email.subject)
        self.assertIn('654321', sent_email.body)
        self.assertIn(self.customer_user.username, sent_email.body)
        self.assertIn(self.driver_user.username, sent_email.body)
        self.assertIn(self.driver_user.phone, sent_email.body)

        # In-app notification must not contain OTP code
        notif = Notification.objects.filter(recipient=self.customer_user).first()
        self.assertIsNotNone(notif)
        self.assertNotIn('654321', notif.message)

    def test_driver_assigned_notice_triggered_on_assignment_view(self):
        """
        On assignment, adopter receives driver details notice with NO OTP.
        """
        mail.outbox.clear()
        self.client.force_authenticate(user=self.shelter_user)
        url = '/api/v1/shelter/deliveries/assign/'
        payload = {
            'application_public_id': str(self.application.public_id),
            'driver_public_id': str(self.driver_profile.public_id),
            'payment_method': 'cod'
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        delivery = PetDelivery.objects.get(application=self.application)
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.to, [self.customer_user.email])
        self.assertIn('Driver Assigned', sent_email.subject)
        self.assertIn(self.driver_user.username, sent_email.body)
        # MUST NOT contain pickup OTP
        self.assertNotIn(response.data['pickup_otp'], sent_email.body)

    def test_dispatch_doorstep_otp_triggered_on_pickup(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        pickup_code = delivery.raw_pickup_otp
        mail.outbox.clear()

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {'otp': pickup_code}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.to, [self.customer_user.email])
        self.assertIn('Doorstep Verification OTP', sent_email.subject)
        match = re.search(r"OTP is:\s*(\d{6})", sent_email.body)
        self.assertIsNotNone(match)
        doorstep_code = match.group(1)
        self.assertEqual(len(doorstep_code), 6)

    def test_update_status_blocks_in_transit_and_allows_failed(self):
        """
        update_status strictly blocks status='in_transit' or status='out_for_delivery' with 400.
        Only status='failed' is allowed, which frees the driver.
        """
        delivery = assign_delivery_task(self.application, self.driver_profile)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/status/'

        # Blocked: in_transit -> 400
        res_blocked = self.client.post(url, {'status': 'in_transit'}, format='json')
        self.assertEqual(res_blocked.status_code, status.HTTP_400_BAD_REQUEST)

        # Allowed: failed -> 200, frees driver
        res_fail = self.client.post(url, {'status': 'failed'}, format='json')
        self.assertEqual(res_fail.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'failed')
        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

    def test_dispatch_doorstep_otp_graceful_error_handling(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        mail.outbox.clear()

        # 1. Customer with no email address returns False gracefully
        self.customer_user.email = ''
        self.customer_user.save()
        result = dispatch_doorstep_otp(delivery, raw_otp='123456')
        self.assertFalse(result)
        self.assertEqual(len(mail.outbox), 0)

        # 2. send_mail exception is caught and logged, returning False without raising
        self.customer_user.email = 'customer@alerts.org'
        self.customer_user.save()
        with patch('apps.logistics.services.send_mail', side_effect=RuntimeError("SMTP Server down")):
            result_ex = dispatch_doorstep_otp(delivery, raw_otp='123456')
            self.assertFalse(result_ex)


class DeliveryOTPSecurityHardeningTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.shelter_user = User.objects.create_user(
            username='sec_shelter', email='shelter@sec.org', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Security Shelter', pickup_address='100 Sec St', is_verified=True
        )

        self.other_shelter_user = User.objects.create_user(
            username='other_sec_shelter', email='other_shelter@sec.org', password='password123', role='shelter'
        )
        self.other_shelter_profile = ShelterProfile.objects.create(
            user=self.other_shelter_user, organization_name='Other Shelter Org', pickup_address='999 Other St', is_verified=True
        )

        self.driver_user = User.objects.create_user(
            username='sec_driver', email='driver@sec.org', password='password123', role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter_profile, vehicle_type='two_wheeler', vehicle_number='KA-01-SEC-01', is_available=True
        )

        self.other_driver_user = User.objects.create_user(
            username='other_sec_driver', email='other@sec.org', password='password123', role='delivery'
        )
        self.other_driver_profile = DeliveryProfile.objects.create(
            user=self.other_driver_user, shelter=self.shelter_profile, vehicle_type='two_wheeler', vehicle_number='KA-01-SEC-02', is_available=True
        )

        self.customer_user = User.objects.create_user(
            username='sec_customer', email='customer@sec.org', password='password123', role='customer'
        )

        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='SecPet', species='dog', breed='Boxer', age_months=18, gender='male', status='available'
        )

        self.application = AdoptionApplication.objects.create(
            applicant=self.customer_user,
            pet=self.pet,
            status='approved',
            total_amount=Decimal('200.00'),
            payment_status='pending',
            payment_method='cod'
        )

    def test_regex_validation_does_not_consume_attempts(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'

        for invalid in ['123', '12345', '1234567', 'abcdef', '12a456', '']:
            res = self.client.post(url, {'otp': invalid}, format='json')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, 0)

    def test_unauthorized_driver_returns_403(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.other_driver_user)

        res_pickup = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/', {'otp': '123456'}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_403_FORBIDDEN)

        res_doorstep = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/', {'otp': '123456'}, format='json')
        self.assertEqual(res_doorstep.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_state_transitions_return_409(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)

        # Doorstep verify while still in 'assigned' -> 409
        res_doorstep = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/', {'otp': '123456'}, format='json')
        self.assertEqual(res_doorstep.status_code, status.HTTP_409_CONFLICT)

        # Successful pickup
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)

        # Pickup verify while in 'out_for_delivery' -> 409
        res_pickup = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/', {'otp': '123456'}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_409_CONFLICT)

    def test_expired_otp_returns_410_gone(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        delivery.expires_at = timezone.now() - timedelta(minutes=10)
        delivery.save(update_fields=['expires_at'])

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_410_GONE)

    def test_idempotent_doorstep_verification(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        with self.captureOnCommitCallbacks(execute=True):
            verify_pickup_otp(delivery, delivery.raw_pickup_otp)
        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[-1].body)
        doorstep_otp = match.group(1) if match else ''

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        res1 = self.client.post(url, {'otp': doorstep_otp}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Second call returns 200 OK idempotently
        res2 = self.client.post(url, {'otp': doorstep_otp}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['message'], "Delivery already completed.")

        # Legacy alias /verify-otp/ also returns 200 OK idempotently
        res3 = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/verify-otp/', {'otp': doorstep_otp}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_200_OK)

    def test_shelter_reissue_endpoint_permissions_and_invariants(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        reissue_url = f'/api/v1/shelter/deliveries/{delivery.public_id}/reissue-otp/'

        # Unauthorized user (customer) -> 403 Forbidden
        self.client.force_authenticate(user=self.customer_user)
        res_unauth = self.client.post(reissue_url, {'purpose': 'pickup'}, format='json')
        self.assertEqual(res_unauth.status_code, status.HTTP_403_FORBIDDEN)

        # Shelter that does NOT own the delivery -> 403 Forbidden
        self.client.force_authenticate(user=self.other_shelter_user)
        res_other_shelter = self.client.post(reissue_url, {'purpose': 'pickup'}, format='json')
        self.assertEqual(res_other_shelter.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("different shelter", res_other_shelter.data.get('detail', ''))

        # Authorized shelter: invalid purpose -> 400
        self.client.force_authenticate(user=self.shelter_user)
        res_bad = self.client.post(reissue_url, {'purpose': 'unknown'}, format='json')
        self.assertEqual(res_bad.status_code, status.HTTP_400_BAD_REQUEST)

        # Invariant: doorstep reissue in 'assigned' -> 409 Conflict
        res_conflict = self.client.post(reissue_url, {'purpose': 'doorstep'}, format='json')
        self.assertEqual(res_conflict.status_code, status.HTTP_409_CONFLICT)

        # Successful pickup reissue
        res_pickup = self.client.post(reissue_url, {'purpose': 'pickup'}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_200_OK)
        self.assertIn('pickup_otp', res_pickup.data)

        # Pickup delivery
        delivery.refresh_from_db()
        verify_pickup_otp(delivery, res_pickup.data['pickup_otp'])

        # Invariant: pickup reissue in 'out_for_delivery' -> 409 Conflict
        res_late_pickup = self.client.post(reissue_url, {'purpose': 'pickup'}, format='json')
        self.assertEqual(res_late_pickup.status_code, status.HTTP_409_CONFLICT)

        # Successful doorstep reissue
        mail.outbox.clear()
        res_doorstep = self.client.post(reissue_url, {'purpose': 'doorstep'}, format='json')
        self.assertEqual(res_doorstep.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

    def _pickup_and_get_doorstep_otp(self, delivery):
        with self.captureOnCommitCallbacks(execute=True):
            verify_pickup_otp(delivery, delivery.raw_pickup_otp)
        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[-1].body)
        return match.group(1) if match else ''

    def test_payment_status_evaluation(self):
        """
        Tests payment evaluation rules on doorstep delivery completion:
        1. COD paid: cash_collected >= total_amount -> payment_status='paid'
        2. COD underpaid: cash_collected < total_amount -> payment_status='pending'
        3. Online paid: verified Payment ('paid'/'captured') exists -> payment_status='paid'
        4. Online unpaid: no verified Payment ('created'/'failed') -> payment_status='pending'
        """
        # Case 1: COD underpaid -> remains pending
        del1 = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        del1.cash_collected = Decimal('50.00')  # less than total_amount of 200.00
        del1.save(update_fields=['cash_collected'])
        otp1 = self._pickup_and_get_doorstep_otp(del1)
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(f'/api/v1/delivery/tasks/{del1.public_id}/verify-doorstep/', {'otp': otp1}, format='json')
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'pending')

        # Case 2: COD fully paid -> payment_status='paid'
        pet_cod = Pet.objects.create(shelter=self.shelter_profile, name='CodPet', species='dog', breed='Lab', age_months=10, gender='male', status='available')
        app_cod = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_cod, status='approved', payment_method='cod', payment_status='pending', total_amount=Decimal('150.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_cod = assign_delivery_task(app_cod, self.driver_profile, payment_method='cod')
        del_cod.cash_collected = Decimal('150.00')  # equals total_amount
        del_cod.save(update_fields=['cash_collected'])
        otp_cod = self._pickup_and_get_doorstep_otp(del_cod)
        self.client.post(f'/api/v1/delivery/tasks/{del_cod.public_id}/verify-doorstep/', {'otp': otp_cod}, format='json')
        app_cod.refresh_from_db()
        self.assertEqual(app_cod.payment_status, 'paid')

        # Case 3: Online paid (captured) -> payment_status='paid'
        pet_onl = Pet.objects.create(shelter=self.shelter_profile, name='OnlPet', species='dog', breed='Pug', age_months=12, gender='female', status='available')
        app_onl = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_onl, status='approved', payment_method='online', payment_status='pending', total_amount=Decimal('200.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_onl = assign_delivery_task(app_onl, self.driver_profile, payment_method='online')
        Payment.objects.create(delivery=del_onl, application=app_onl, amount=Decimal('200.00'), amount_in_paise=20000, status='captured', razorpay_order_id='order_sec_paid')
        otp_onl = self._pickup_and_get_doorstep_otp(del_onl)
        self.client.post(f'/api/v1/delivery/tasks/{del_onl.public_id}/verify-doorstep/', {'otp': otp_onl}, format='json')
        app_onl.refresh_from_db()
        self.assertEqual(app_onl.payment_status, 'paid')

        # Case 4: Online unpaid (payment created or failed) -> remains pending
        pet_fail = Pet.objects.create(shelter=self.shelter_profile, name='FailPet', species='cat', breed='Persian', age_months=8, gender='female', status='available')
        app_fail = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_fail, status='approved', payment_method='online', payment_status='pending', total_amount=Decimal('100.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_fail = assign_delivery_task(app_fail, self.driver_profile, payment_method='online')
        Payment.objects.create(delivery=del_fail, application=app_fail, amount=Decimal('100.00'), amount_in_paise=10000, status='failed', razorpay_order_id='order_sec_fail')
        del_fail.status = 'out_for_delivery'
        del_fail.save(update_fields=['status'])
        otp_fail = issue_otp(del_fail, 'doorstep')
        self.client.post(f'/api/v1/delivery/tasks/{del_fail.public_id}/verify-doorstep/', {'otp': otp_fail}, format='json')
        app_fail.refresh_from_db()
        self.assertEqual(app_fail.payment_status, 'pending')

        # Case 5: Online underpaid (captured payment amount < application total_amount) -> remains pending
        pet_under = Pet.objects.create(shelter=self.shelter_profile, name='UnderPet', species='dog', breed='Husky', age_months=14, gender='male', status='available')
        app_under = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_under, status='approved', payment_method='online', payment_status='pending', total_amount=Decimal('250.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_under = assign_delivery_task(app_under, self.driver_profile, payment_method='online')
        Payment.objects.create(delivery=del_under, application=app_under, amount=Decimal('100.00'), amount_in_paise=10000, status='captured', razorpay_order_id='order_sec_underpaid')
        del_under.status = 'out_for_delivery'
        del_under.save(update_fields=['status'])
        otp_under = issue_otp(del_under, 'doorstep')
        self.client.post(f'/api/v1/delivery/tasks/{del_under.public_id}/verify-doorstep/', {'otp': otp_under}, format='json')
        app_under.refresh_from_db()
        self.assertEqual(app_under.payment_status, 'pending')

    def test_serializer_stripping_audit(self):
        self.assertNotIn('pickup_otp', DeliveryDriverTaskSerializer().fields)
        self.assertNotIn('doorstep_otp', DeliveryDriverTaskSerializer().fields)
        self.assertNotIn('pickup_otp', DeliveryCustomerSerializer().fields)
        self.assertNotIn('doorstep_otp', DeliveryCustomerSerializer().fields)
        self.assertNotIn('pickup_otp', ShelterDeliverySerializer().fields)
        self.assertNotIn('doorstep_otp', ShelterDeliverySerializer().fields)

    def test_wrong_otp_attempts_persisted_in_db_after_400(self):
        """
        Proves that a wrong OTP attempt increment is persisted to the database
        immediately upon returning HTTP 400 (verified by reading back via a fresh query).
        Tested for both pickup and doorstep verification.
        """
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)

        # 1. Pickup verification wrong OTP
        pickup_url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res_pickup = self.client.post(pickup_url, {'otp': '999999'}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_pickup.data['attempts_remaining'], 2)

        # Fresh query from database (verifies persistence)
        fresh_pickup_del = PetDelivery.objects.get(id=delivery.id)
        self.assertEqual(fresh_pickup_del.pickup_otp_attempts, 1)

        # Complete pickup via API to advance state to 'out_for_delivery'
        res_pickup_ok = self.client.post(pickup_url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res_pickup_ok.status_code, status.HTTP_200_OK)

        # 2. Doorstep verification wrong OTP
        doorstep_url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        res_doorstep = self.client.post(doorstep_url, {'otp': '999999'}, format='json')
        self.assertEqual(res_doorstep.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_doorstep.data['attempts_remaining'], 2)

        # Fresh query from database (verifies persistence)
        fresh_doorstep_del = PetDelivery.objects.get(id=delivery.id)
        self.assertEqual(fresh_doorstep_del.doorstep_otp_attempts, 1)

    def test_pickup_verification_lockout_and_rules(self):
        """
        Tests pickup verification rules: 400 with attempts_remaining on wrong codes,
        and 423 Locked on the 4th attempt after 3 failures.
        """
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'

        # Attempt 1: wrong code -> 400, 2 remaining
        res1 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res1.data['attempts_remaining'], 2)

        # Attempt 2: wrong code -> 400, 1 remaining
        res2 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res2.data['attempts_remaining'], 1)

        # Attempt 3: wrong code -> 400, 0 remaining
        res3 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res3.data['attempts_remaining'], 0)

        # Attempt 4: lockout -> 423 Locked
        res4 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res4.status_code, 423)
        self.assertIn('Maximum OTP verification attempts exceeded', res4.data['detail'])

    def test_no_notification_message_contains_any_otp_sequence(self):
        """
        Validates that across the entire delivery lifecycle (assign, pickup, doorstep, reissue),
        no Notification message contains any issued 6-digit OTP sequence or matches a 6-digit number.
        """
        Notification.objects.all().delete()
        mail.outbox.clear()

        # 1. Assignment
        self.client.force_authenticate(user=self.shelter_user)
        with self.captureOnCommitCallbacks(execute=True):
            res_assign = self.client.post('/api/v1/shelter/deliveries/assign/', {
                'application_public_id': str(self.application.public_id),
                'driver_public_id': str(self.driver_profile.public_id),
                'payment_method': 'cod'
            }, format='json')
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        pickup_code = res_assign.data['pickup_otp']

        # 2. Pickup verification -> issues doorstep OTP
        delivery = PetDelivery.objects.get(application=self.application)
        self.client.force_authenticate(user=self.driver_user)
        with self.captureOnCommitCallbacks(execute=True):
            res_pickup = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/', {'otp': pickup_code}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_200_OK)

        # Doorstep OTP from email
        match = re.search(r'\b\d{6}\b', mail.outbox[-1].body)
        doorstep_code = match.group(0) if match else ''
        self.assertTrue(bool(doorstep_code))

        # 3. Shelter reissue of doorstep OTP
        self.client.force_authenticate(user=self.shelter_user)
        with self.captureOnCommitCallbacks(execute=True):
            res_reissue = self.client.post(f'/api/v1/shelter/deliveries/{delivery.public_id}/reissue-otp/', {'purpose': 'doorstep'}, format='json')
        self.assertEqual(res_reissue.status_code, status.HTTP_200_OK)
        match_reissue = re.search(r'\b\d{6}\b', mail.outbox[-1].body)
        reissued_doorstep_code = match_reissue.group(0) if match_reissue else ''

        # 4. Doorstep verification
        self.client.force_authenticate(user=self.driver_user)
        with self.captureOnCommitCallbacks(execute=True):
            res_doorstep = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/', {'otp': reissued_doorstep_code}, format='json')
        self.assertEqual(res_doorstep.status_code, status.HTTP_200_OK)

        # Inspect all created Notifications
        all_notifs = Notification.objects.all()
        self.assertGreater(all_notifs.count(), 0)
        issued_codes = {pickup_code, doorstep_code, reissued_doorstep_code}

        for notif in all_notifs:
            for code in issued_codes:
                self.assertNotIn(code, notif.message, f"Notification #{notif.id} leaked issued OTP {code}!")
            six_digit_match = re.search(r'\b\d{6}\b', notif.message)
            self.assertIsNone(six_digit_match, f"Notification #{notif.id} contains a 6-digit sequence: {notif.message}")

    def test_otp_verify_throttle_rate_limit_429(self):
        """
        Confirms that the 'otp_verify' throttle scope is enforced for BOTH
        /api/v1/ and legacy /api/deliveries/ routes:
        Rate is 10/min. Requests 1-10 are processed,
        and the 11th request in that same minute returns HTTP 429 Too Many Requests.
        """
        from django.core.cache import cache
        cache.clear()

        # 1. Test /api/v1/ route
        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)
        url_v1 = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'

        for i in range(1, 11):
            res = self.client.post(url_v1, {'otp': '000000'}, format='json')
            self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, 423], f"Request {i} unexpectedly failed with status {res.status_code}")

        res_throttled_v1 = self.client.post(url_v1, {'otp': '000000'}, format='json')
        self.assertEqual(res_throttled_v1.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn('Request was throttled', str(res_throttled_v1.data))

        # 2. Test legacy /api/deliveries/ route
        cache.clear()
        delivery.pickup_otp_attempts = 0
        delivery.save(update_fields=['pickup_otp_attempts'])
        url_legacy = f'/api/deliveries/{delivery.public_id}/pickup/'

        for i in range(1, 11):
            res = self.client.post(url_legacy, {'otp': '000000'}, format='json')
            self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, 423], f"Legacy request {i} failed with {res.status_code}")

        res_throttled_legacy = self.client.post(url_legacy, {'otp': '000000'}, format='json')
        self.assertEqual(res_throttled_legacy.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn('Request was throttled', str(res_throttled_legacy.data))

    def test_pet_delivery_admin_security_configuration(self):
        """
        Validates admin.py security configuration for PetDeliveryAdmin:
        - secret_otp removed from list_display and search_fields
        - attempts, expiry, and sent_at present in list_display
        - hashes and attempts are in readonly_fields
        - no hash fields displayed in list_display
        """
        from django.contrib.admin.sites import site
        from apps.logistics.admin import PetDeliveryAdmin
        from apps.logistics.models import PetDelivery

        admin_instance = PetDeliveryAdmin(PetDelivery, site)
        self.assertNotIn('secret_otp', admin_instance.list_display)
        self.assertNotIn('secret_otp', admin_instance.search_fields)
        self.assertNotIn('pickup_otp_hash', admin_instance.list_display)
        self.assertNotIn('doorstep_otp_hash', admin_instance.list_display)

        # Show attempts, expiry, sent_at
        self.assertIn('pickup_otp_attempts', admin_instance.list_display)
        self.assertIn('doorstep_otp_attempts', admin_instance.list_display)
        self.assertIn('expires_at', admin_instance.list_display)
        self.assertIn('doorstep_otp_expires_at', admin_instance.list_display)
        self.assertIn('doorstep_otp_sent_at', admin_instance.list_display)

        # Hash and attempt fields read-only
        self.assertIn('pickup_otp_hash', admin_instance.readonly_fields)
        self.assertIn('doorstep_otp_hash', admin_instance.readonly_fields)
        self.assertIn('pickup_otp_attempts', admin_instance.readonly_fields)
        self.assertIn('doorstep_otp_attempts', admin_instance.readonly_fields)


class DeliveryOnCommitEmailTests(TestCase):
    """
    Tests proving all delivery notices and OTP emails dispatch strictly via transaction.on_commit:
    1. Exactly one email sent after commit.
    2. doorstep_otp_sent_at set only after successful send.
    3. Failed send leaves sent_at NULL.
    4. No plaintext OTP kept as attributes on the model instance (beyond raw_pickup_otp).
    5. If transaction fails after issue_otp, no email is sent and mail.outbox is empty.
    """
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(username='commit_shelter', email='shelter@commit.test', password='password123', role='shelter')
        self.shelter_profile = ShelterProfile.objects.create(user=self.shelter_user, organization_name='Commit Shelter', pickup_address='123 Test St', city='Bangalore', is_verified=True)
        self.customer_user = User.objects.create_user(username='commit_customer', email='cust@commit.test', password='password123', role='customer')
        self.driver_user = User.objects.create_user(username='commit_driver', email='driver@commit.test', password='password123', role='delivery')
        self.driver_profile = DeliveryProfile.objects.create(user=self.driver_user, shelter=self.shelter_profile, is_available=True, vehicle_number='KA-01-C-1234')
        self.pet = Pet.objects.create(shelter=self.shelter_profile, name='CommitPet', species='dog', breed='Indie', age_months=12, gender='male', status='available')
        self.application = AdoptionApplication.objects.create(applicant=self.customer_user, pet=self.pet, status='approved', payment_method='cod', total_amount=Decimal('100.00'))

    def test_driver_assigned_notice_sent_exactly_once_after_commit(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            delivery = assign_delivery_task(self.application, self.driver_profile)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.customer_user.email])
        self.assertIn("Driver Assigned", mail.outbox[0].subject)

    def test_doorstep_otp_email_sent_exactly_once_after_commit_and_sets_sent_at(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        mail.outbox.clear()
        self.assertIsNone(delivery.doorstep_otp_sent_at)

        with self.captureOnCommitCallbacks(execute=True):
            res = verify_pickup_service(delivery.id, delivery.raw_pickup_otp)

        self.assertEqual(res.outcome, OTPVerificationOutcome.SUCCESS)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Doorstep Verification OTP", mail.outbox[0].subject)

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.doorstep_otp_sent_at)
        # Ensure no raw_doorstep_otp attribute exists on delivery model
        self.assertFalse(hasattr(delivery, 'raw_doorstep_otp'))

    def test_completion_notice_sent_exactly_once_after_commit(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        with self.captureOnCommitCallbacks(execute=True):
            verify_pickup_service(delivery.id, delivery.raw_pickup_otp)

        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[-1].body)
        doorstep_otp = match.group(1)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            res = verify_doorstep_service(delivery.id, doorstep_otp)

        self.assertEqual(res.outcome, OTPVerificationOutcome.SUCCESS)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Delivery Completed", mail.outbox[0].subject)

    def test_no_email_sent_if_transaction_fails_after_issue_otp(self):
        mail.outbox.clear()
        delivery = assign_delivery_task(self.application, self.driver_profile)
        mail.outbox.clear()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                raw_doorstep_otp = issue_otp(delivery, 'doorstep')
                def _send():
                    dispatch_doorstep_otp(delivery, raw_otp=raw_doorstep_otp)
                transaction.on_commit(_send)
                raise RuntimeError("Simulated failure after issue_otp")

        # Rollback prevents on_commit callback execution
        self.assertEqual(len(mail.outbox), 0)

    def test_email_failure_leaves_sent_at_null(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        mail.outbox.clear()

        from unittest.mock import patch
        with patch('apps.logistics.services.send_mail', side_effect=RuntimeError("SMTP down")):
            with self.captureOnCommitCallbacks(execute=True):
                verify_pickup_service(delivery.id, delivery.raw_pickup_otp)

        delivery.refresh_from_db()
        self.assertIsNone(delivery.doorstep_otp_sent_at)


class DeliveryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.shelter_user = User.objects.create_user(
            username='conc_shelter', email='shelter@conc.org', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Conc Shelter', pickup_address='Concurrency St', is_verified=True
        )
        self.driver_user = User.objects.create_user(
            username='conc_driver', email='driver@conc.org', password='password123', role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter_profile, vehicle_type='two_wheeler', vehicle_number='KA-01-CONC-01', is_available=True
        )
        self.customer_user = User.objects.create_user(
            username='conc_customer', email='customer@conc.org', password='password123', role='customer'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='ConcPet', species='dog', breed='Boxer', age_months=18, gender='male', status='available'
        )
        self.application = AdoptionApplication.objects.create(
            applicant=self.customer_user,
            pet=self.pet,
            status='approved',
            total_amount=Decimal('200.00'),
            payment_status='pending',
            payment_method='cod'
        )

    def test_simultaneous_wrong_guesses_at_max_minus_one_attempts(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        max_attempts = getattr(settings, 'OTP_MAX_ATTEMPTS', 3)
        delivery.pickup_otp_attempts = max_attempts - 1
        delivery.save(update_fields=['pickup_otp_attempts'])

        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def worker():
            from django.db import connection
            try:
                client = APIClient()
                client.force_authenticate(user=self.driver_user)
                barrier.wait(timeout=5)
                res = client.post(url, {'otp': '000000'}, format='json')
                with lock:
                    results.append(res)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        status_codes = sorted([r.status_code for r in results])
        self.assertEqual(status_codes, [400, 423])

        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, max_attempts)

    def test_simultaneous_doorstep_submissions_with_correct_code(self):
        delivery = assign_delivery_task(self.application, self.driver_profile)
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)
        delivery.refresh_from_db()

        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[-1].body)
        doorstep_otp = match.group(1) if match else ''
        self.assertTrue(bool(doorstep_otp))

        mail.outbox.clear()

        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def worker():
            from django.db import connection
            try:
                client = APIClient()
                client.force_authenticate(user=self.driver_user)
                barrier.wait(timeout=5)
                res = client.post(url, {'otp': doorstep_otp}, format='json')
                with lock:
                    results.append(res)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.status_code, 200)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'delivered')
        self.assertIsNotNone(delivery.delivered_at)

        completion_emails = [m for m in mail.outbox if "Delivery Completed" in m.subject]
        self.assertEqual(len(completion_emails), 1)


class DeliveryReassignmentTests(TestCase):
    def setUp(self):
        super().setUp()
        self.shelter_user = User.objects.create_user(
            username='reassign_shelter', email='shelter@reassign.org', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Reassign Shelter', pickup_address='123 Reassign Ave', is_verified=True
        )
        self.driver_user1 = User.objects.create_user(
            username='driver1', email='driver1@reassign.org', password='password123', role='delivery'
        )
        self.driver_profile1 = DeliveryProfile.objects.create(
            user=self.driver_user1, shelter=self.shelter_profile, vehicle_type='two_wheeler', vehicle_number='KA-01-DRV-01', is_available=True
        )
        self.driver_user2 = User.objects.create_user(
            username='driver2', email='driver2@reassign.org', password='password123', role='delivery'
        )
        self.driver_profile2 = DeliveryProfile.objects.create(
            user=self.driver_user2, shelter=self.shelter_profile, vehicle_type='four_wheeler', vehicle_number='KA-01-DRV-02', is_available=True
        )
        self.customer = User.objects.create_user(
            username='reassign_customer', email='cust@reassign.org', password='password123', role='customer'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='ReassignPet', species='dog', breed='Labrador', age_months=12, gender='female', status='available'
        )
        self.application = AdoptionApplication.objects.create(
            applicant=self.customer, pet=self.pet, status='approved', total_amount=Decimal('150.00'), payment_status='pending', payment_method='cod'
        )
        self.client = APIClient()

    def test_reassign_in_assigned_state_frees_old_driver_and_resets_delivery(self):
        delivery = assign_delivery_task(self.application, self.driver_profile1)
        self.driver_profile1.refresh_from_db()
        self.assertFalse(self.driver_profile1.is_available)
        initial_delivery_id = delivery.id
        initial_otp = delivery.raw_pickup_otp

        self.client.force_authenticate(user=self.shelter_user)
        payload = {
            'application_id': str(self.application.public_id),
            'driver_id': str(self.driver_profile2.public_id),
            'payment_method': 'cod'
        }
        res = self.client.post('/api/v1/shelter/deliveries/assign/', data=payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Old driver is freed
        self.driver_profile1.refresh_from_db()
        self.assertTrue(self.driver_profile1.is_available)

        # New driver is marked unavailable
        self.driver_profile2.refresh_from_db()
        self.assertFalse(self.driver_profile2.is_available)

        # Existing delivery row updated (preserving OneToOne relationship and public_id)
        delivery.refresh_from_db()
        self.assertEqual(delivery.id, initial_delivery_id)
        self.assertEqual(delivery.driver, self.driver_profile2)
        self.assertEqual(delivery.status, 'assigned')
        self.assertIsNone(delivery.dispatched_at)
        self.assertEqual(delivery.pickup_otp_attempts, 0)
        self.assertNotEqual(res.data.get('pickup_otp'), initial_otp)

    def test_reassign_in_out_for_delivery_or_delivered_returns_409_conflict(self):
        delivery = assign_delivery_task(self.application, self.driver_profile1)
        # Advance to out_for_delivery
        with self.captureOnCommitCallbacks(execute=True):
            verify_pickup_otp(delivery, delivery.raw_pickup_otp)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')

        self.client.force_authenticate(user=self.shelter_user)
        payload = {
            'application_id': str(self.application.public_id),
            'driver_id': str(self.driver_profile2.public_id),
            'payment_method': 'cod'
        }
        res = self.client.post('/api/v1/shelter/deliveries/assign/', data=payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("out_for_delivery", res.data.get('detail', ''))

        with self.assertRaises(ConflictError):
            assign_delivery_task(self.application, self.driver_profile2)

        # Advance to delivered
        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[-1].body)
        doorstep_otp = match.group(1) if match else ''
        verify_doorstep_otp(delivery, doorstep_otp)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'delivered')

        res_delivered = self.client.post('/api/v1/shelter/deliveries/assign/', data=payload, format='json')
        self.assertEqual(res_delivered.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("delivered", res_delivered.data.get('detail', ''))

        with self.assertRaises(ConflictError):
            assign_delivery_task(self.application, self.driver_profile2)

    def test_reassign_in_failed_state_is_allowed_and_issues_fresh_otps(self):
        delivery = assign_delivery_task(self.application, self.driver_profile1)
        delivery.status = 'failed'
        delivery.save(update_fields=['status'])
        self.driver_profile1.is_available = True
        self.driver_profile1.save(update_fields=['is_available'])

        self.client.force_authenticate(user=self.shelter_user)
        payload = {
            'application_id': str(self.application.public_id),
            'driver_id': str(self.driver_profile2.public_id),
            'payment_method': 'cod'
        }
        res = self.client.post('/api/v1/shelter/deliveries/assign/', data=payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'assigned')
        self.assertEqual(delivery.driver, self.driver_profile2)
        self.driver_profile2.refresh_from_db()
        self.assertFalse(self.driver_profile2.is_available)
        fresh_otp = res.data.get('pickup_otp')
        self.assertTrue(bool(fresh_otp))


class PaymentHardeningTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Shelter
        self.shelter_user = User.objects.create_user(
            username='pay_shelter', email='pay_shelter@test.com', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Pay Shelter', pickup_address='123 Pay St', is_verified=True
        )

        # Driver
        self.driver_user = User.objects.create_user(
            username='pay_driver', email='pay_driver@test.com', password='password123', role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter_profile, is_available=True, vehicle_number='KA-01-P-1234'
        )

        # Adopter (applicant)
        self.applicant = User.objects.create_user(
            username='pay_applicant', email='pay_applicant@test.com', password='password123', role='customer'
        )

        # Other User (non-owner)
        self.other_user = User.objects.create_user(
            username='pay_other', email='pay_other@test.com', password='password123', role='customer'
        )

        # Pet
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='PayPet', species='dog', breed='Lab', age_months=12, status='available'
        )

        # Application
        self.application = AdoptionApplication.objects.create(
            applicant=self.applicant,
            pet=self.pet,
            status='approved',
            total_amount=Decimal('250.00'),
            payment_status='pending',
            payment_method='online'
        )

        # Delivery in 'assigned' status
        self.delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='online')

    @patch('razorpay.Client')
    def test_non_owner_cannot_create_or_verify_returns_404(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_instance.order.create.return_value = {'id': 'order_harden_nonowner', 'amount': 25000, 'currency': 'INR', 'status': 'created'}
        mock_client_cls.return_value = mock_instance

        # 1. Non-owner cannot create order -> 404
        self.client.force_authenticate(user=self.other_user)
        create_res = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(create_res.status_code, status.HTTP_404_NOT_FOUND)

        # 2. Owner creates order
        self.client.force_authenticate(user=self.applicant)
        owner_create = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(owner_create.status_code, status.HTTP_201_CREATED)
        order_id = owner_create.data['razorpay_order_id']

        # 3. Non-owner cannot verify -> 404
        self.client.force_authenticate(user=self.other_user)
        verify_payload = {
            'razorpay_order_id': order_id,
            'razorpay_payment_id': 'pay_dummy_123',
            'razorpay_signature': 'dummy_sig'
        }
        verify_res = self.client.post('/api/v1/payments/verify/', verify_payload, format='json')
        self.assertEqual(verify_res.status_code, status.HTTP_404_NOT_FOUND)

        # Unknown order -> 404
        self.client.force_authenticate(user=self.applicant)
        unknown_res = self.client.post('/api/v1/payments/verify/', {
            'razorpay_order_id': 'order_unknown_999999',
            'razorpay_payment_id': 'pay_dummy_123',
            'razorpay_signature': 'dummy_sig'
        }, format='json')
        self.assertEqual(unknown_res.status_code, status.HTTP_404_NOT_FOUND)

    @patch('razorpay.Client')
    def test_client_supplied_amount_is_ignored(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_instance.order.create.return_value = {'id': 'order_harden_ignore_amt', 'amount': 25000, 'currency': 'INR', 'status': 'created'}
        mock_client_cls.return_value = mock_instance

        self.client.force_authenticate(user=self.applicant)
        # Client tries to send 100 paise or 1 rupee
        res = self.client.post('/api/v1/payments/create-order/', {
            'delivery_public_id': str(self.delivery.public_id),
            'amount_in_paise': 100,
            'amount': 1.00
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['amount_in_paise'], 25000)

        # Check call to razorpay client used application total_amount paise (25000)
        mock_instance.order.create.assert_called_once()
        create_call_args = mock_instance.order.create.call_args[1]['data']
        self.assertEqual(create_call_args['amount'], 25000)

    def test_create_order_precondition_rejections(self):
        self.client.force_authenticate(user=self.applicant)

        # 1. total_amount <= 0 -> 400
        self.application.total_amount = Decimal('0.00')
        self.application.save(update_fields=['total_amount'])
        res_zero = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res_zero.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("greater than zero", str(res_zero.data))
        self.application.total_amount = Decimal('250.00')
        self.application.save(update_fields=['total_amount'])

        # 2. application.status != 'approved' -> 409
        self.application.status = 'under_review'
        self.application.save(update_fields=['status'])
        res_unapproved = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res_unapproved.status_code, status.HTTP_409_CONFLICT)
        self.application.status = 'approved'
        self.application.save(update_fields=['status'])

        # 3. payment_method == 'cod' -> 409
        self.application.payment_method = 'cod'
        self.application.save(update_fields=['payment_method'])
        res_cod = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res_cod.status_code, status.HTTP_409_CONFLICT)
        self.application.payment_method = 'online'
        self.application.save(update_fields=['payment_method'])

        # 4. payment_status already 'paid' -> 409
        self.application.payment_status = 'paid'
        self.application.save(update_fields=['payment_status'])
        res_paid = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res_paid.status_code, status.HTTP_409_CONFLICT)
        self.application.payment_status = 'pending'
        self.application.save(update_fields=['payment_status'])

        # 5. delivery.status not in ('assigned', 'out_for_delivery') -> 409
        self.delivery.status = 'failed'
        self.delivery.save(update_fields=['status'])
        res_del_status = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res_del_status.status_code, status.HTTP_409_CONFLICT)

    @patch('razorpay.Client')
    def test_same_order_is_reused(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_instance.order.create.return_value = {'id': 'order_harden_reuse_1', 'amount': 25000, 'currency': 'INR', 'status': 'created'}
        mock_client_cls.return_value = mock_instance

        self.client.force_authenticate(user=self.applicant)
        res1 = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res1.data['razorpay_order_id'], 'order_harden_reuse_1')
        self.assertEqual(mock_instance.order.create.call_count, 1)

        # Call again: returns same order without calling Razorpay or creating new row
        res2 = self.client.post('/api/v1/payments/create-order/', {'delivery_public_id': str(self.delivery.public_id)}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data['razorpay_order_id'], 'order_harden_reuse_1')
        self.assertEqual(mock_instance.order.create.call_count, 1)  # NOT called again
        self.assertEqual(Payment.objects.filter(delivery=self.delivery).count(), 1)

    def test_verify_success_and_repeat_verify_idempotent(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_verify_idem_1'
        )

        order_id = 'order_verify_idem_1'
        payment_id = 'pay_verify_idem_1'
        msg = f"{order_id}|{payment_id}".encode('utf-8')
        secret = settings.RAZORPAY_KEY_SECRET.encode('utf-8')
        sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()

        self.client.force_authenticate(user=self.applicant)
        verify_payload = {
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': sig
        }

        # 1. First verify
        res1 = self.client.post('/api/v1/payments/verify/', verify_payload, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data['status'], PaymentStatus.CAPTURED)

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'paid')
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.payment_status, 'paid')

        # 2. Repeat verify -> 200 idempotent
        res2 = self.client.post('/api/v1/payments/verify/', verify_payload, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['status'], PaymentStatus.CAPTURED)

    def test_verify_wrong_signature_returns_400_and_no_state_change(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_verify_bad_sig'
        )

        self.client.force_authenticate(user=self.applicant)
        verify_payload = {
            'razorpay_order_id': 'order_verify_bad_sig',
            'razorpay_payment_id': 'pay_dummy_bad',
            'razorpay_signature': 'invalidsignaturehex123'
        }

        res = self.client.post('/api/v1/payments/verify/', verify_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Ensure NO state change
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CREATED)
        self.assertFalse(bool(payment.razorpay_payment_id))
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'pending')
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.payment_status, 'pending')

    def test_verify_amount_mismatch_returns_400(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=10000,  # 100.00 in db but application total_amount is 250.00 (25000 paise)
            amount=Decimal('100.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_verify_amt_mismatch'
        )

        order_id = 'order_verify_amt_mismatch'
        payment_id = 'pay_verify_mismatch_1'
        msg = f"{order_id}|{payment_id}".encode('utf-8')
        secret = settings.RAZORPAY_KEY_SECRET.encode('utf-8')
        sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()

        self.client.force_authenticate(user=self.applicant)
        res = self.client.post('/api/v1/payments/verify/', {
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': sig
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not match application total amount", str(res.data))

    def test_webhook_without_auth_header_works_and_bad_signature_returns_400(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_webhook_auth'
        )

        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        payload_dict = {
            'event': 'payment.captured',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_webhook_123',
                        'order_id': 'order_webhook_auth',
                        'amount': 25000,
                        'currency': 'INR',
                        'status': 'captured'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        valid_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        # 1. Posting with bad signature returns 400
        unauth_client = APIClient()  # NO authorization header
        bad_res = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE='invalid_webhook_sig'
        )
        self.assertEqual(bad_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid webhook signature", str(bad_res.data))

        # 2. Posting with valid signature and NO auth header returns 200
        good_res = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=valid_sig
        )
        self.assertEqual(good_res.status_code, status.HTTP_200_OK)

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        self.assertEqual(payment.razorpay_payment_id, 'pay_webhook_123')
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'paid')
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.payment_status, 'paid')

    def test_webhook_valid_signature_no_auth_header_success(self):
        return self.test_webhook_without_auth_header_works_and_bad_signature_returns_400()

    def test_webhook_invalid_signature_rejected_400(self):
        unauth_client = APIClient()
        res = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=json.dumps({'event': 'payment.captured'}),
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE='invalid_signature_hex'
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid webhook signature", str(res.data))

    def test_webhook_amount_mismatch_ignored(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_webhook_mismatch'
        )

        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        # Amount in payload is 5000 instead of 25000
        payload_dict = {
            'event': 'payment.captured',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_webhook_mismatch',
                        'order_id': 'order_webhook_mismatch',
                        'amount': 5000,
                        'currency': 'INR',
                        'status': 'captured'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        valid_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        res = self.client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=valid_sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'ignored')

        # Payment remains untouched
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CREATED)
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'pending')

    def test_webhook_repeat_idempotent(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_webhook_repeat'
        )

        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        payload_dict = {
            'event': 'payment.captured',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_webhook_repeat',
                        'order_id': 'order_webhook_repeat',
                        'amount': 25000,
                        'currency': 'INR',
                        'status': 'captured'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        valid_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        # Send once
        res1 = self.client.post('/api/v1/payments/webhook/', data=raw_body, content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=valid_sig)
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Send again (repeat event)
        res2 = self.client.post('/api/v1/payments/webhook/', data=raw_body, content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=valid_sig)
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)

    def test_webhook_failed_does_not_overwrite_captured_payment(self):
        payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CAPTURED,
            razorpay_order_id='order_webhook_fail_test',
            razorpay_payment_id='pay_captured_already'
        )

        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        payload_dict = {
            'event': 'payment.failed',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_failed_attempt',
                        'order_id': 'order_webhook_fail_test',
                        'amount': 25000,
                        'currency': 'INR',
                        'status': 'failed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        valid_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        res = self.client.post('/api/v1/payments/webhook/', data=raw_body, content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=valid_sig)
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Must NOT be overwritten to 'failed'
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        self.assertEqual(payment.razorpay_payment_id, 'pay_captured_already')

    def test_webhook_unknown_order_returns_200_no_change(self):
        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        payload_dict = {
            'event': 'payment.captured',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_unknown',
                        'order_id': 'order_completely_unknown_9999',
                        'amount': 25000,
                        'currency': 'INR',
                        'status': 'captured'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        valid_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        res = self.client.post('/api/v1/payments/webhook/', data=raw_body, content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=valid_sig)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'ignored')


class PaymentConcurrencyHardeningTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.shelter_user = User.objects.create_user(
            username='conc_pay_shelter', email='cps@test.com', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='CPS Shelter', pickup_address='123 CPS St', is_verified=True
        )
        self.driver_user = User.objects.create_user(
            username='conc_pay_driver', email='cpd@test.com', password='password123', role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter_profile, is_available=True, vehicle_number='KA-01-P-9999'
        )
        self.applicant = User.objects.create_user(
            username='conc_pay_applicant', email='cpa@test.com', password='password123', role='customer'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='ConcPayPet', species='dog', breed='Lab', age_months=12, status='available'
        )
        self.application = AdoptionApplication.objects.create(
            applicant=self.applicant,
            pet=self.pet,
            status='approved',
            total_amount=Decimal('300.00'),
            payment_status='pending',
            payment_method='online'
        )
        self.delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='online')
        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=30000,
            amount=Decimal('300.00'),
            currency='INR',
            status=PaymentStatus.CREATED,
            razorpay_order_id='order_concurrent_test_1'
        )

    def test_concurrent_verify_and_webhook_captures_exactly_once(self):
        order_id = 'order_concurrent_test_1'
        payment_id = 'pay_concurrent_123'

        # Prepare verify signature
        msg = f"{order_id}|{payment_id}".encode('utf-8')
        secret = settings.RAZORPAY_KEY_SECRET.encode('utf-8')
        verify_sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()

        # Prepare webhook payload & signature
        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        payload_dict = {
            'event': 'payment.captured',
            'payload': {
                'payment': {
                    'entity': {
                        'id': payment_id,
                        'order_id': order_id,
                        'amount': 30000,
                        'currency': 'INR',
                        'status': 'captured'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        webhook_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body.encode('utf-8'), hashlib.sha256).hexdigest()

        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def verify_worker():
            from django.db import connection
            try:
                client = APIClient()
                client.force_authenticate(user=self.applicant)
                barrier.wait(timeout=5)
                res = client.post('/api/v1/payments/verify/', {
                    'razorpay_order_id': order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature': verify_sig
                }, format='json')
                with lock:
                    results.append(('verify', res.status_code))
            finally:
                connection.close()

        def webhook_worker():
            from django.db import connection
            try:
                client = APIClient()
                barrier.wait(timeout=5)
                res = client.post(
                    '/api/v1/payments/webhook/',
                    data=raw_body,
                    content_type='application/json',
                    HTTP_X_RAZORPAY_SIGNATURE=webhook_sig
                )
                with lock:
                    results.append(('webhook', res.status_code))
            finally:
                connection.close()

        t1 = threading.Thread(target=verify_worker)
        t2 = threading.Thread(target=webhook_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        for name, code in results:
            self.assertEqual(code, 200, f"{name} failed with status {code}")

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CAPTURED)
        self.assertEqual(self.payment.razorpay_payment_id, payment_id)

        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'paid')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.payment_status, 'paid')


class VerifiedShelterLogisticsPolicyTests(APITestCase):
    def setUp(self):
        # Unverified shelter
        self.unverified_user = User.objects.create_user(
            username='unverified_shelter_log', email='unverified_log@shelter.org', password='password123', role='shelter'
        )
        self.unverified_profile = ShelterProfile.objects.create(
            user=self.unverified_user, organization_name='Unverified Log Haven', pickup_address='100 Log Rd', is_verified=False
        )
        self.unverified_driver_user = User.objects.create_user(
            username='unverified_log_driver', email='driver_unv@log.org', password='password123', role='delivery'
        )
        self.unverified_driver = DeliveryProfile.objects.create(
            user=self.unverified_driver_user, shelter=self.unverified_profile, vehicle_number='KA-01-UNV-01', is_available=True
        )
        self.unverified_pet = Pet.objects.create(
            shelter=self.unverified_profile, name='GhostLog', species='dog', status='available', age_months=12, gender='male'
        )

        # Verified shelter
        self.verified_user = User.objects.create_user(
            username='verified_shelter_log', email='verified_log@shelter.org', password='password123', role='shelter'
        )
        self.verified_profile = ShelterProfile.objects.create(
            user=self.verified_user, organization_name='Verified Log Haven', pickup_address='200 Log Rd', is_verified=True
        )
        self.verified_driver_user = User.objects.create_user(
            username='verified_log_driver', email='driver_ver@log.org', password='password123', role='delivery'
        )
        self.verified_driver = DeliveryProfile.objects.create(
            user=self.verified_driver_user, shelter=self.verified_profile, vehicle_number='KA-01-VER-01', is_available=True
        )
        self.verified_pet = Pet.objects.create(
            shelter=self.verified_profile, name='HeroLog', species='dog', status='available', age_months=12, gender='male'
        )

        self.customer = User.objects.create_user(
            username='log_customer', email='cust@log.org', password='password123', role='customer'
        )

        self.app_unverified = AdoptionApplication.objects.create(
            applicant=self.customer, pet=self.unverified_pet, status='approved', payment_method='cod', total_amount=Decimal('50.00')
        )
        self.app_verified = AdoptionApplication.objects.create(
            applicant=self.customer, pet=self.verified_pet, status='approved', payment_method='cod', total_amount=Decimal('50.00')
        )

    def test_unverified_shelter_can_read_deliveries_and_drivers(self):
        self.client.force_authenticate(user=self.unverified_user)
        res_list = self.client.get('/api/v1/shelter/deliveries/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)

        res_drivers = self.client.get('/api/v1/shelter/deliveries/drivers/')
        self.assertEqual(res_drivers.status_code, status.HTTP_200_OK)

    def test_unverified_shelter_blocked_from_assign_and_reissue(self):
        self.client.force_authenticate(user=self.unverified_user)
        # Assign
        assign_payload = {
            'application_ref': str(self.app_unverified.public_id),
            'driver_ref': str(self.unverified_driver.public_id)
        }
        res_assign = self.client.post('/api/v1/shelter/deliveries/assign/', assign_payload)
        self.assertEqual(res_assign.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_assign.data.get('code'), 'shelter_not_verified')

        # Legacy assign
        res_assign_legacy = self.client.post('/api/deliveries/assign/', assign_payload)
        self.assertEqual(res_assign_legacy.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_assign_legacy.data.get('code'), 'shelter_not_verified')

    def test_verified_shelter_can_assign_delivery(self):
        self.client.force_authenticate(user=self.verified_user)
        assign_payload = {
            'application_ref': str(self.app_verified.public_id),
            'driver_ref': str(self.verified_driver.public_id)
        }
        res_assign = self.client.post('/api/v1/shelter/deliveries/assign/', assign_payload)
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        self.assertIn('pickup_otp', res_assign.data)

    def test_suspension_immediately_blocks_delivery_writes(self):
        self.client.force_authenticate(user=self.verified_user)
        self.verified_profile.is_verified = False
        self.verified_profile.save()

        assign_payload = {
            'application_ref': str(self.app_verified.public_id),
            'driver_ref': str(self.verified_driver.public_id)
        }
        res_assign = self.client.post('/api/v1/shelter/deliveries/assign/', assign_payload)
        self.assertEqual(res_assign.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_assign.data.get('code'), 'shelter_not_verified')


class PickupPaymentGuardTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(username='guard_shelter', email='guard_shelter@test.org', password='password123', role='shelter')
        self.shelter = ShelterProfile.objects.create(user=self.shelter_user, organization_name='Guard Shelter', is_verified=True)
        self.driver_user = User.objects.create_user(username='guard_driver', email='guard_driver@test.org', password='password123', role='delivery')
        self.driver = DeliveryProfile.objects.create(user=self.driver_user, shelter=self.shelter, is_available=True, vehicle_number='KA-05-PG-1111')
        self.customer = User.objects.create_user(username='guard_cust', email='guard_cust@test.org', password='password123', role='customer')

    def _create_setup(self, total_amount=Decimal('100.00'), payment_method='unset'):
        pet = Pet.objects.create(shelter=self.shelter, name='GuardPet', species='dog', breed='Lab', age_months=12, gender='female', status='available')
        app = AdoptionApplication.objects.create(
            applicant=self.customer,
            pet=pet,
            status='approved',
            total_amount=total_amount,
            payment_method=payment_method,
            payment_status='pending',
            contact_phone='9999999999'
        )
        delivery = assign_delivery_task(app, self.driver, payment_method=payment_method)
        return app, delivery

    def test_pickup_payment_guard_total_amount_zero_allowed(self):
        app, delivery = self._create_setup(total_amount=Decimal('0.00'), payment_method='unset')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')

    def test_pickup_payment_guard_cod_allowed(self):
        app, delivery = self._create_setup(total_amount=Decimal('150.00'), payment_method='cod')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')

    def test_pickup_payment_guard_online_without_captured_payment_returns_409(self):
        app, delivery = self._create_setup(total_amount=Decimal('200.00'), payment_method='online')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'payment_required')
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, 0)
        self.assertEqual(delivery.status, 'assigned')

    def test_pickup_payment_guard_online_underpaid_returns_409(self):
        app, delivery = self._create_setup(total_amount=Decimal('200.00'), payment_method='online')
        # Captured payment for 100.00 (half of 200.00)
        Payment.objects.create(
            application=app,
            delivery=delivery,
            amount=Decimal('100.00'),
            amount_in_paise=10000,
            status=PaymentStatus.CAPTURED
        )
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'payment_required')
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, 0)

    def test_pickup_payment_guard_unset_with_positive_total_returns_409(self):
        app, delivery = self._create_setup(total_amount=Decimal('100.00'), payment_method='unset')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'
        res = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'payment_method_required')
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, 0)
        self.assertEqual(delivery.status, 'assigned')

    def test_pickup_payment_guard_online_paid_after_409_succeeds(self):
        app, delivery = self._create_setup(total_amount=Decimal('300.00'), payment_method='online')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'

        # First attempt without payment -> 409 payment_required
        res_fail = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res_fail.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res_fail.data.get('code'), 'payment_required')
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup_otp_attempts, 0)
        self.assertEqual(delivery.status, 'assigned')

        # Customer pays: Payment captured
        Payment.objects.create(
            application=app,
            delivery=delivery,
            amount=Decimal('300.00'),
            amount_in_paise=30000,
            status=PaymentStatus.CAPTURED
        )

        # Second attempt with payment captured -> 200 OK, out_for_delivery
        res_ok = self.client.post(url, {'otp': delivery.raw_pickup_otp}, format='json')
        self.assertEqual(res_ok.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')

    def test_helper_direct_evaluation(self):
        app, delivery = self._create_setup(total_amount=Decimal('50.00'), payment_method='unset')
        allowed, code, _ = check_pickup_payment_guard(delivery)
        self.assertFalse(allowed)
        self.assertEqual(code, 'payment_method_required')

        app.payment_method = 'cod'
        app.save()
        allowed, code, _ = check_pickup_payment_guard(delivery)
        self.assertTrue(allowed)

        app.payment_method = 'online'
        app.save()
        allowed, code, _ = check_pickup_payment_guard(delivery)
        self.assertFalse(allowed)
        self.assertEqual(code, 'payment_required')

        Payment.objects.create(application=app, delivery=delivery, amount=Decimal('50.00'), amount_in_paise=5000, status=PaymentStatus.CAPTURED)
        allowed, code, _ = check_pickup_payment_guard(delivery)
        self.assertTrue(allowed)


class CODCashCollectionTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(username='cod_shelter', email='cod_shelter@test.org', password='password123', role='shelter')
        self.shelter = ShelterProfile.objects.create(user=self.shelter_user, organization_name='COD Shelter', is_verified=True)
        self.driver_user = User.objects.create_user(username='cod_driver', email='cod_driver@test.org', password='password123', role='delivery')
        self.driver = DeliveryProfile.objects.create(user=self.driver_user, shelter=self.shelter, is_available=True, vehicle_number='KA-03-CD-2222')
        self.other_driver_user = User.objects.create_user(username='cod_other_driver', email='cod_other@test.org', password='password123', role='delivery')
        self.other_driver = DeliveryProfile.objects.create(user=self.other_driver_user, shelter=self.shelter, is_available=True, vehicle_number='KA-03-CD-3333')
        self.customer = User.objects.create_user(username='cod_cust', email='cod_cust@test.org', password='password123', role='customer')

    def _setup_out_for_delivery(self, total_amount=Decimal('150.00'), payment_method='cod'):
        self.driver.is_available = True
        self.driver.save(update_fields=['is_available'])
        pet = Pet.objects.create(shelter=self.shelter, name='CODPet', species='dog', breed='Beagle', age_months=10, gender='male', status='available')
        app = AdoptionApplication.objects.create(
            applicant=self.customer,
            pet=pet,
            status='approved',
            total_amount=total_amount,
            payment_method=payment_method,
            payment_status='pending',
            contact_phone='8888888888'
        )
        delivery = assign_delivery_task(app, self.driver, payment_method=payment_method)
        # Advance to out_for_delivery
        delivery.status = 'out_for_delivery'
        delivery.save(update_fields=['status'])
        issue_otp(delivery, 'doorstep')
        return app, delivery

    def test_collect_cash_success_creates_single_ledger_row(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'), payment_method='cod')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('cash_collected'), '150.00')
        self.assertEqual(res.data.get('remaining_amount'), '0.00')
        self.assertEqual(res.data.get('payment_status'), 'paid')

        delivery.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(delivery.cash_collected, Decimal('150.00'))
        self.assertEqual(app.payment_status, 'paid')

        payments = Payment.objects.filter(delivery=delivery)
        self.assertEqual(payments.count(), 1)
        p = payments.first()
        self.assertEqual(p.payment_method, 'cod')
        self.assertEqual(p.status, PaymentStatus.CAPTURED)
        self.assertEqual(p.amount, Decimal('150.00'))
        self.assertEqual(p.amount_in_paise, 15000)

    def test_collect_cash_non_assigned_driver_returns_403(self):
        app, delivery = self._setup_out_for_delivery()
        self.client.force_authenticate(user=self.other_driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_collect_cash_wrong_status_assigned_returns_409(self):
        app, delivery = self._setup_out_for_delivery()
        delivery.status = 'assigned'
        delivery.save(update_fields=['status'])
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

    def test_collect_cash_wrong_status_delivered_and_failed_returns_409(self):
        app, delivery = self._setup_out_for_delivery()
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'

        delivery.status = 'delivered'
        delivery.delivered_at = timezone.now()
        delivery.save(update_fields=['status', 'delivered_at'])
        res1 = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_409_CONFLICT)

        delivery.status = 'failed'
        delivery.save(update_fields=['status'])
        res2 = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)

    def test_collect_cash_non_cod_method_returns_400(self):
        app, delivery = self._setup_out_for_delivery(payment_method='online')
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_collect_cash_zero_total_returns_400(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('0.00'))
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '10.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_collect_cash_wrong_amount_returns_400_with_remaining_amount(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'))
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'
        res = self.client.post(url, {'amount': '100.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('remaining_amount'), '150.00')

    def test_repeat_collect_cash_idempotent_creates_no_second_ledger_row(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'))
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/'

        res1 = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(Payment.objects.filter(delivery=delivery).count(), 1)

        # Second collect call (idempotent)
        res2 = self.client.post(url, {'amount': '150.00'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(Payment.objects.filter(delivery=delivery).count(), 1)

    def test_doorstep_otp_verify_without_cash_returns_409_consumes_no_attempts(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'))
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        res = self.client.post(url, {'otp': '123456'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'cash_not_collected')

        delivery.refresh_from_db()
        self.assertEqual(delivery.doorstep_otp_attempts, 0)

    def test_doorstep_otp_verify_after_cash_collected_succeeds(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'))
        self.client.force_authenticate(user=self.driver_user)

        # 1. Collect cash
        res_cash = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/', {'amount': '150.00'}, format='json')
        self.assertEqual(res_cash.status_code, status.HTTP_200_OK)

        # 2. Doorstep verify with valid OTP
        doorstep_raw_otp = issue_otp(delivery, 'doorstep')
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        res_otp = self.client.post(url, {'otp': doorstep_raw_otp}, format='json')
        self.assertEqual(res_otp.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(delivery.status, 'delivered')
        self.assertEqual(app.status, 'completed')
        self.assertEqual(app.payment_status, 'paid')

    def test_driver_cannot_mark_failed_after_cash_collected(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('150.00'))
        self.client.force_authenticate(user=self.driver_user)

        # Collect cash
        self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/', {'amount': '150.00'}, format='json')

        # Driver attempts to mark failed
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/status/'
        res = self.client.post(url, {'status': 'failed'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'cash_already_collected')

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')

    def test_legacy_alias_collect_cash(self):
        app, delivery = self._setup_out_for_delivery(total_amount=Decimal('120.00'))
        self.client.force_authenticate(user=self.driver_user)

        legacy_url = f'/api/deliveries/{delivery.public_id}/collect-cash/'
        res = self.client.post(legacy_url, {'amount': '120.00'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('cash_collected'), '120.00')


class CODCashCollectionConcurrencyTestCase(TransactionTestCase):
    def test_concurrent_collect_cash_records_once(self):
        shelter_user = User.objects.create_user(username='conc_shelter', email='conc_shelter@test.org', password='password123', role='shelter')
        shelter = ShelterProfile.objects.create(user=shelter_user, organization_name='Conc Shelter', is_verified=True)
        driver_user = User.objects.create_user(username='conc_driver', email='conc_driver@test.org', password='password123', role='delivery')
        driver = DeliveryProfile.objects.create(user=driver_user, shelter=shelter, is_available=True, vehicle_number='KA-03-CC-9999')
        customer = User.objects.create_user(username='conc_cust', email='conc_cust@test.org', password='password123', role='customer')

        pet = Pet.objects.create(shelter=shelter, name='ConcPet', species='dog', breed='Boxer', age_months=15, gender='female', status='available')
        app = AdoptionApplication.objects.create(
            applicant=customer,
            pet=pet,
            status='approved',
            total_amount=Decimal('100.00'),
            payment_method='cod',
            payment_status='pending',
            contact_phone='7777777777'
        )
        delivery = assign_delivery_task(app, driver, payment_method='cod')
        delivery.status = 'out_for_delivery'
        delivery.save(update_fields=['status'])

        results = []
        errors = []

        def _worker():
            from django.db import connections
            try:
                c = APIClient()
                c.force_authenticate(user=driver_user)
                res = c.post(f'/api/v1/delivery/tasks/{delivery.public_id}/collect-cash/', {'amount': '100.00'}, format='json')
                results.append(res.status_code)
            except Exception as e:
                errors.append(e)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), 3)
        for code in results:
            self.assertEqual(code, 200)

        # Assert exactly ONE Payment row exists in the ledger
        self.assertEqual(Payment.objects.filter(delivery=delivery).count(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.cash_collected, Decimal('100.00'))







