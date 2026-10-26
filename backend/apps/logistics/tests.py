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
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase
from rest_framework import status

from apps.accounts.models import User, ShelterProfile, DeliveryProfile, Notification
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication
from apps.logistics.models import (
    PetDelivery, DeliveryLocationPing, Payment, PaymentStatus,
    Refund, RefundStatus, RefundSource
)
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
        delivery.cash_collected = Decimal('150.00')
        delivery.save(update_fields=['cash_collected'])
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
        delivery.cash_collected = Decimal('150.00')
        delivery.save(update_fields=['cash_collected'])
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
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        delivery.cash_collected = self.application.total_amount
        delivery.save(update_fields=['cash_collected'])
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
        # Case 1: COD underpaid -> doorstep verification blocked with 409 cash_not_collected
        del1 = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        del1.cash_collected = Decimal('50.00')  # less than total_amount of 150.00
        del1.save(update_fields=['cash_collected'])
        otp1 = self._pickup_and_get_doorstep_otp(del1)
        self.client.force_authenticate(user=self.driver_user)
        res_blocked = self.client.post(f'/api/v1/delivery/tasks/{del1.public_id}/verify-doorstep/', {'otp': otp1}, format='json')
        self.assertEqual(res_blocked.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res_blocked.data.get('code'), 'cash_not_collected')
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'pending')

        # Pay remaining balance to complete del1 and free the driver
        del1.cash_collected = self.application.total_amount
        del1.save(update_fields=['cash_collected'])
        res_ok = self.client.post(f'/api/v1/delivery/tasks/{del1.public_id}/verify-doorstep/', {'otp': otp1}, format='json')
        self.assertEqual(res_ok.status_code, status.HTTP_200_OK)
        del1.refresh_from_db()
        self.assertEqual(del1.status, 'delivered')
        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

        # Case 2: COD fully paid -> payment_status='paid'
        pet_cod = Pet.objects.create(shelter=self.shelter_profile, name='CodPet', species='dog', breed='Lab', age_months=10, gender='male', status='available')
        app_cod = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_cod, status='approved', payment_method='cod', payment_status='pending', total_amount=Decimal('150.00'))
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
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        delivery.cash_collected = self.application.total_amount
        delivery.save(update_fields=['cash_collected'])
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

        # Satisfy cash collection before doorstep verification
        delivery.refresh_from_db()
        delivery.cash_collected = self.application.total_amount
        delivery.save(update_fields=['cash_collected'])

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

        delivery.refresh_from_db()
        delivery.cash_collected = Decimal('150.00')
        delivery.save(update_fields=['cash_collected'])

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

        delivery.refresh_from_db()
        delivery.cash_collected = self.application.total_amount
        delivery.save(update_fields=['cash_collected'])

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
        delivery.refresh_from_db()
        delivery.cash_collected = Decimal('150.00')
        delivery.save(update_fields=['cash_collected'])
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


class AdopterEmailLogisticsTests(APITestCase):
    def setUp(self):
        from apps.accounts.models import ShelterProfile, DeliveryProfile
        from apps.pets.models import Pet
        from apps.adoptions.models import AdoptionApplication

        self.shelter_user = User.objects.create_user(
            username='shelter_adopter_test',
            email='shelter@adoptertest.org',
            password='password123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Adopter Test Shelter',
            pickup_address='123 Test St',
            is_verified=True
        )
        self.driver_user = User.objects.create_user(
            username='driver_adopter_test',
            email='driver@adoptertest.org',
            password='password123',
            role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.shelter_profile,
            vehicle_type='car',
            vehicle_number='KA-05-EM-1111',
            is_available=True
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile,
            name='NoEmailPet',
            species='dog',
            breed='Terrier',
            age_months=12,
            size='medium',
            gender='male',
            status='available'
        )
        # Adopter user with empty email
        self.adopter_no_email = User.objects.create_user(
            username='adopter_no_email',
            email='',
            password='password123',
            role='customer'
        )
        self.application_no_email = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter_no_email,
            status='approved',
            total_amount=Decimal('0.00'),
            payment_status='paid'
        )

        # Adopter user with valid email
        self.adopter_with_email = User.objects.create_user(
            username='adopter_with_email',
            email='adopter.valid@example.com',
            password='password123',
            role='customer'
        )
        self.pet2 = Pet.objects.create(
            shelter=self.shelter_profile,
            name='EmailPet',
            species='cat',
            breed='Persian',
            age_months=8,
            size='small',
            gender='female',
            status='available'
        )
        self.application_with_email = AdoptionApplication.objects.create(
            pet=self.pet2,
            applicant=self.adopter_with_email,
            status='approved',
            total_amount=Decimal('0.00'),
            payment_status='paid'
        )

    def test_assign_delivery_refuses_when_adopter_has_no_email_service(self):
        with self.assertRaises(ConflictError) as ctx:
            assign_delivery_task(self.application_no_email, self.driver_profile)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail.get('code'), 'adopter_email_missing')

    def test_assign_delivery_refuses_when_adopter_has_no_email_api(self):
        self.client.force_authenticate(user=self.shelter_user)
        payload = {
            'application_id': str(self.application_no_email.public_id),
            'driver_id': str(self.driver_profile.public_id),
            'payment_method': 'cod'
        }
        res = self.client.post('/api/v1/shelter/deliveries/assign/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'adopter_email_missing')

    def test_reissue_response_message_matches_email_dispatch_outcome(self):
        # Assign delivery for application with email
        delivery = assign_delivery_task(self.application_with_email, self.driver_profile)
        delivery.status = 'out_for_delivery'
        delivery.save(update_fields=['status'])

        reissue_url = f'/api/v1/shelter/deliveries/{delivery.public_id}/reissue-otp/'
        self.client.force_authenticate(user=self.shelter_user)

        # 1. When email succeeds
        with patch('apps.logistics.services.dispatch_doorstep_otp', return_value=True):
            res_success = self.client.post(reissue_url, {'purpose': 'doorstep'}, format='json')
            self.assertEqual(res_success.status_code, status.HTTP_200_OK)
            self.assertTrue(res_success.data.get('email_dispatched'))
            self.assertIn("emailed to adopter", res_success.data.get('detail', ''))

        # 2. When email fails
        with patch('apps.logistics.services.dispatch_doorstep_otp', return_value=False):
            res_fail = self.client.post(reissue_url, {'purpose': 'doorstep'}, format='json')
            self.assertEqual(res_fail.status_code, status.HTTP_200_OK)
            self.assertFalse(res_fail.data.get('email_dispatched'))
            self.assertNotIn("emailed to adopter", res_fail.data.get('detail', ''))
            self.assertIn("failed", res_fail.data.get('detail', ''))


class ShelterCancelDeliveryAPITests(APITestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. Owner shelter (verified)
        self.owner_shelter_user = User.objects.create_user(
            username='owner_shelter_cancel',
            email='owner.cancel@shelter.org',
            password='password123',
            role='shelter'
        )
        self.owner_shelter = ShelterProfile.objects.create(
            user=self.owner_shelter_user,
            organization_name='Owner Shelter Org',
            pickup_address='100 Shelter Way',
            city='Bangalore',
            is_verified=True
        )

        # 2. Other shelter (verified)
        self.other_shelter_user = User.objects.create_user(
            username='other_shelter_cancel',
            email='other.cancel@shelter.org',
            password='password123',
            role='shelter'
        )
        self.other_shelter = ShelterProfile.objects.create(
            user=self.other_shelter_user,
            organization_name='Other Shelter Org',
            pickup_address='200 Foreign Rd',
            city='Bangalore',
            is_verified=True
        )

        # 3. Unverified shelter
        self.unverified_shelter_user = User.objects.create_user(
            username='unverified_shelter_cancel',
            email='unverified.cancel@shelter.org',
            password='password123',
            role='shelter'
        )
        self.unverified_shelter = ShelterProfile.objects.create(
            user=self.unverified_shelter_user,
            organization_name='Unverified Shelter Org',
            pickup_address='300 Pending Ave',
            city='Bangalore',
            is_verified=False
        )

        # 4. Admin user
        self.admin_user = User.objects.create_user(
            username='admin_cancel_user',
            email='admin.cancel@platform.org',
            password='password123',
            role='admin',
            is_staff=True
        )

        # 5. Driver
        self.driver_user = User.objects.create_user(
            username='driver_cancel_test',
            email='driver.cancel@delivery.org',
            password='password123',
            role='delivery'
        )
        self.driver_profile = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.owner_shelter,
            vehicle_type='bike',
            vehicle_number='KA-01-CN-1234',
            is_available=False
        )

        # 6. Adopter
        self.adopter = User.objects.create_user(
            username='adopter_cancel_test',
            email='adopter.cancel@example.com',
            password='password123',
            role='customer'
        )

        # 7. Pet and Application
        self.pet = Pet.objects.create(
            shelter=self.owner_shelter,
            name='CancelPet',
            species='dog',
            breed='Labrador',
            age_months=10,
            gender='male',
            status='pending'
        )
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            status='approved',
            total_amount=Decimal('500.00'),
            payment_method='cod',
            payment_status='pending'
        )

        # 8. Delivery
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            driver=self.driver_profile,
            status='assigned',
            cash_collected=Decimal('0.00')
        )

    def test_owner_shelter_can_cancel_assigned_delivery(self):
        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')

        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'approved')

    def test_owner_shelter_can_cancel_out_for_delivery(self):
        self.delivery.status = 'out_for_delivery'
        self.delivery.save(update_fields=['status'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')
        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

    def test_admin_can_cancel_delivery(self):
        self.client.force_authenticate(user=self.admin_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

    def test_other_shelter_cannot_cancel_returns_403(self):
        self.client.force_authenticate(user=self.other_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'assigned')

    def test_unverified_shelter_cannot_cancel_returns_403(self):
        self.client.force_authenticate(user=self.unverified_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')

    def test_cash_collected_blocks_cancellation_returns_409(self):
        self.delivery.cash_collected = Decimal('500.00')
        self.delivery.save(update_fields=['cash_collected'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'cash_collected')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'assigned')

    def test_cancellation_is_idempotent_for_already_failed(self):
        self.delivery.status = 'failed'
        self.delivery.save(update_fields=['status'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')
        self.assertTrue(res.data.get('idempotent'))

    def test_cancellation_blocked_for_completed_delivery(self):
        self.delivery.status = 'delivered'
        self.delivery.delivered_at = timezone.now()
        self.delivery.save(update_fields=['status', 'delivered_at'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_legacy_url_alias_cancel(self):
        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

    def test_captured_online_payment_blocks_cancellation_returns_409(self):
        Payment.objects.create(
            application=self.application,
            delivery=self.delivery,
            status=PaymentStatus.CAPTURED,
            amount=Decimal('500.00'),
            razorpay_payment_id='pay_captured_test_123'
        )
        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'refund_required')
        self.assertIn('refund', res.data.get('detail', '').lower())

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'assigned')

    def test_cod_with_no_cash_cancellation_allowed(self):
        self.delivery.cash_collected = Decimal('0.00')
        self.delivery.save(update_fields=['cash_collected'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

    def test_unpaid_online_order_cancellation_allowed(self):
        self.application.payment_method = 'online'
        self.application.payment_status = 'pending'
        self.application.save(update_fields=['payment_method', 'payment_status'])

        Payment.objects.create(
            application=self.application,
            delivery=self.delivery,
            status=PaymentStatus.CREATED,
            amount=Decimal('500.00'),
            razorpay_order_id='order_unpaid_123'
        )

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

    def test_cod_payment_row_with_zero_cash_allows_cancellation(self):
        # Even if a COD payment row exists, if cash_collected is 0, cancellation is allowed
        Payment.objects.create(
            application=self.application,
            delivery=self.delivery,
            status=PaymentStatus.CAPTURED,
            payment_method='cod',
            amount=Decimal('500.00'),
            transaction_id='cod_tx_123'
        )
        self.delivery.cash_collected = Decimal('0.00')
        self.delivery.save(update_fields=['cash_collected'])

        self.client.force_authenticate(user=self.owner_shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'failed')

    def test_inline_serializers_match_schema_keys(self):
        from apps.logistics.serializers import (
            DeliveryDriverTaskSerializer,
            DeliveryCustomerSerializer,
            DriverTaskPetSummarySerializer,
            CustomerTrackingPetSummarySerializer,
            DeliveryDriverDetailsSerializer,
        )
        # Restore delivery status so serializers operate on standard assigned delivery
        self.delivery.status = 'assigned'
        self.delivery.save(update_fields=['status'])

        task_serializer = DeliveryDriverTaskSerializer(self.delivery)
        task_data = task_serializer.data

        # 1. Driver task pet_summary matches DriverTaskPetSummarySerializer declared fields
        pet_summary = task_data['pet_summary']
        self.assertIsNotNone(pet_summary)
        self.assertEqual(set(pet_summary.keys()), set(DriverTaskPetSummarySerializer().fields.keys()))

        tracking_serializer = DeliveryCustomerSerializer(self.delivery)
        tracking_data = tracking_serializer.data

        # 2. Customer tracking driver_details matches DeliveryDriverDetailsSerializer declared fields
        driver_details = tracking_data['driver_details']
        self.assertIsNotNone(driver_details)
        self.assertEqual(set(driver_details.keys()), set(DeliveryDriverDetailsSerializer().fields.keys()))

        # 3. Customer tracking pet_summary matches CustomerTrackingPetSummarySerializer declared fields
        cust_pet_summary = tracking_data['pet_summary']
        self.assertIsNotNone(cust_pet_summary)
        self.assertEqual(set(cust_pet_summary.keys()), set(CustomerTrackingPetSummarySerializer().fields.keys()))


class StableErrorCodesTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(
            username='err_shelter', email='err_shelter@test.org', password='password123', role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Error Shelter', is_verified=True
        )
        self.driver_user = User.objects.create_user(
            username='err_driver', email='err_driver@test.org', password='password123', role='delivery'
        )
        self.driver = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter_profile, is_available=True, vehicle_number='KA-05-ERR-1111'
        )
        self.customer = User.objects.create_user(
            username='err_cust', email='err_cust@test.org', password='password123', role='customer'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter_profile, name='ErrPet', species='dog', breed='Lab', age_months=12, status='available'
        )
        self.app = AdoptionApplication.objects.create(
            applicant=self.customer,
            pet=self.pet,
            status='approved',
            total_amount=Decimal('200.00'),
            payment_method='cod',
            payment_status='pending',
            contact_phone='9999999999'
        )
        self.delivery = assign_delivery_task(self.app, self.driver, payment_method='cod')

    def test_pickup_stable_error_codes(self):
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{self.delivery.public_id}/pickup/'

        # 1. Missing OTP -> 400 validation_error
        res = self.client.post(url, {})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 2. Malformed OTP -> 400 validation_error
        res = self.client.post(url, {'otp': '123'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 3. Invalid OTP -> 400 otp_invalid with attempts_remaining
        res = self.client.post(url, {'otp': '000000'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'otp_invalid')
        self.assertEqual(res.data.get('attempts_remaining'), 2)

        # 4. Lockout -> 423 otp_locked
        self.client.post(url, {'otp': '000001'})
        self.client.post(url, {'otp': '000002'})
        res_lock = self.client.post(url, {'otp': '000003'})
        self.assertEqual(res_lock.status_code, 423)
        self.assertEqual(res_lock.data.get('code'), 'otp_locked')

        # 5. Expired OTP -> 410 otp_expired
        reissue_delivery_otp(self.delivery, 'pickup')
        self.delivery.refresh_from_db()
        self.delivery.expires_at = timezone.now() - timedelta(minutes=1)
        self.delivery.save(update_fields=['expires_at'])
        res_exp = self.client.post(url, {'otp': '123456'})
        self.assertEqual(res_exp.status_code, status.HTTP_410_GONE)
        self.assertEqual(res_exp.data.get('code'), 'otp_expired')

        # 6. Wrong state -> 409 invalid_state
        self.delivery.status = 'out_for_delivery'
        self.delivery.save(update_fields=['status'])
        res_state = self.client.post(url, {'otp': '123456'})
        self.assertEqual(res_state.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res_state.data.get('code'), 'invalid_state')

    def test_doorstep_and_verify_otp_stable_error_codes(self):
        for endpoint in ['verify-doorstep', 'verify-otp']:
            iter_driver_user = User.objects.create_user(
                username=f'drv_{endpoint}', email=f'drv_{endpoint}@test.org', password='password123', role='delivery'
            )
            iter_driver = DeliveryProfile.objects.create(
                user=iter_driver_user, shelter=self.shelter_profile, is_available=True, vehicle_number=f'KA-05-D-{endpoint[:4]}'
            )
            deliv = assign_delivery_task(
                AdoptionApplication.objects.create(
                    applicant=self.customer, pet=self.pet, status='approved', total_amount=Decimal('100.00'),
                    payment_method='cod', payment_status='paid', contact_phone='888'
                ),
                iter_driver,
                payment_method='cod'
            )
            deliv.status = 'out_for_delivery'
            deliv.cash_collected = Decimal('100.00')
            deliv.save(update_fields=['status', 'cash_collected'])
            issue_otp(deliv, 'doorstep')

            self.client.force_authenticate(user=iter_driver_user)
            url = f'/api/v1/delivery/tasks/{deliv.public_id}/{endpoint}/'

            # 1. Missing OTP -> 400 validation_error
            res = self.client.post(url, {})
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(res.data.get('code'), 'validation_error')

            # 2. Malformed OTP -> 400 validation_error
            res = self.client.post(url, {'otp': 'abc'})
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(res.data.get('code'), 'validation_error')

            # 3. Invalid OTP -> 400 otp_invalid with attempts_remaining
            res = self.client.post(url, {'otp': '000000'})
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(res.data.get('code'), 'otp_invalid')
            self.assertEqual(res.data.get('attempts_remaining'), 2)

            # 4. Lockout -> 423 otp_locked
            self.client.post(url, {'otp': '000001'})
            self.client.post(url, {'otp': '000002'})
            res_lock = self.client.post(url, {'otp': '000003'})
            self.assertEqual(res_lock.status_code, 423)
            self.assertEqual(res_lock.data.get('code'), 'otp_locked')

            # 5. Expired OTP -> 410 otp_expired
            reissue_delivery_otp(deliv, 'doorstep')
            deliv.refresh_from_db()
            deliv.doorstep_otp_expires_at = timezone.now() - timedelta(minutes=1)
            deliv.save(update_fields=['doorstep_otp_expires_at'])
            res_exp = self.client.post(url, {'otp': '123456'})
            self.assertEqual(res_exp.status_code, status.HTTP_410_GONE)
            self.assertEqual(res_exp.data.get('code'), 'otp_expired')

            # 6. Wrong state -> 409 invalid_state
            deliv.status = 'assigned'
            deliv.save(update_fields=['status'])
            res_state = self.client.post(url, {'otp': '123456'})
            self.assertEqual(res_state.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(res_state.data.get('code'), 'invalid_state')

    def test_collect_cash_stable_error_codes(self):
        self.delivery.status = 'out_for_delivery'
        self.delivery.save(update_fields=['status'])
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{self.delivery.public_id}/collect-cash/'

        # 1. Missing amount -> 400 validation_error
        res = self.client.post(url, {})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 2. Negative amount -> 400 validation_error
        res = self.client.post(url, {'amount': '-50.00'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 3. Non-numeric amount -> 400 validation_error
        res = self.client.post(url, {'amount': 'invalid'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 4. Wrong amount -> 400 validation_error
        res = self.client.post(url, {'amount': '150.00'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 5. Wrong state (assigned) -> 409 invalid_state
        self.delivery.status = 'assigned'
        self.delivery.save(update_fields=['status'])
        res_state = self.client.post(url, {'amount': '200.00'})
        self.assertEqual(res_state.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res_state.data.get('code'), 'invalid_state')

    def test_reissue_otp_stable_error_codes(self):
        self.client.force_authenticate(user=self.shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reissue-otp/'

        # 1. Malformed / missing purpose -> 400 validation_error
        res = self.client.post(url, {'purpose': 'invalid'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 2. Wrong state for doorstep reissue -> 409 invalid_state
        res = self.client.post(url, {'purpose': 'doorstep'})
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'invalid_state')

    def test_cancel_delivery_stable_error_codes(self):
        self.client.force_authenticate(user=self.shelter_user)
        url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel/'

        # 1. Invalid status (delivered) -> 400 validation_error
        self.delivery.status = 'delivered'
        self.delivery.delivered_at = timezone.now()
        self.delivery.save(update_fields=['status', 'delivered_at'])
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data.get('code'), 'validation_error')

        # 2. Cash collected -> 409 cash_collected
        self.delivery.status = 'out_for_delivery'
        self.delivery.cash_collected = Decimal('200.00')
        self.delivery.save(update_fields=['status', 'cash_collected'])
        res_cash = self.client.post(url)
        self.assertEqual(res_cash.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res_cash.data.get('code'), 'cash_collected')


class DocumentationRouteRegressionTests(TestCase):
    """
    Milestone 8b Regression: Asserts every endpoint path documented in
    docs/backend_handoff.md and docs/api_contract_logistics.md resolves
    strictly via django.urls.resolve().
    """

    def test_all_documented_routes_resolve(self):
        import re
        from django.urls import resolve, Resolver404
        from django.conf import settings

        repo_root = Path(settings.BASE_DIR).parent
        doc_files = [
            repo_root / "docs" / "backend_handoff.md",
            repo_root / "docs" / "api_contract_logistics.md",
        ]
        sample_uuid = "334bd0f8-92c1-4f50-8ba7-b561f67202c3"
        pattern = r"(/api/[a-zA-Z0-9_\-/<:>{}]*)"

        all_paths = set()
        for doc_file in doc_files:
            self.assertTrue(doc_file.exists(), f"Doc file missing: {doc_file}")
            text = doc_file.read_text(encoding="utf-8")
            matches = re.findall(pattern, text)
            for m in matches:
                p = m.rstrip(".,;:)\"'*`")
                if not p.endswith("/"):
                    p += "/"
                all_paths.add((p, doc_file.name))

        self.assertGreater(len(all_paths), 30, "Expected to find documented routes in markdown files")

        unresolved = []
        for path_template, doc_name in sorted(all_paths):
            concrete = re.sub(r"<[^>]+>", sample_uuid, path_template)
            concrete = re.sub(r"\{[^}]+\}", sample_uuid, concrete)
            try:
                resolve(concrete)
            except Resolver404:
                # Retry with integer ID if UUID didn't resolve
                concrete_int = re.sub(r"<[^>]+>", "1", path_template)
                concrete_int = re.sub(r"\{[^}]+\}", "1", concrete_int)
                try:
                    resolve(concrete_int)
                except Resolver404:
                    unresolved.append(f"{path_template} (from {doc_name}) -> tried {concrete} and {concrete_int}")

        self.assertEqual(
            unresolved,
            [],
            f"The following documented API paths failed to resolve via django.urls.resolve: {unresolved}"
        )

    def test_doc_regression_fails_on_bogus_route(self):
        from django.urls import resolve, Resolver404
        sample_uuid = "334bd0f8-92c1-4f50-8ba7-b561f67202c3"
        bogus_path = f"/api/v1/customer/deliveries/{sample_uuid}/track/"
        with self.assertRaises(Resolver404):
            resolve(bogus_path)

    def test_all_openapi_paths_resolve(self):
        """Assert that every path specified in docs/openapi.yaml resolves via django.urls.resolve."""
        import yaml
        import re
        from django.urls import resolve, Resolver404
        from django.conf import settings

        repo_root = Path(settings.BASE_DIR).parent
        openapi_file = repo_root / "docs" / "openapi.yaml"
        self.assertTrue(openapi_file.exists(), f"OpenAPI spec file missing: {openapi_file}")

        with open(openapi_file, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        paths = list(spec.get("paths", {}).keys())
        self.assertGreater(len(paths), 30, "Expected openapi.yaml to have routes")

        sample_uuid = "334bd0f8-92c1-4f50-8ba7-b561f67202c3"
        unresolved = []

        for path_template in paths:
            concrete = re.sub(r"\{[^}]+\}", sample_uuid, path_template)
            try:
                resolve(concrete)
            except Resolver404:
                concrete_int = re.sub(r"\{[^}]+\}", "1", path_template)
                try:
                    resolve(concrete_int)
                except Resolver404:
                    unresolved.append(f"{path_template} -> tried {concrete} and {concrete_int}")

        self.assertEqual(
            unresolved,
            [],
            f"The following openapi.yaml paths failed to resolve via django.urls.resolve: {unresolved}"
        )

    def test_bare_id_routes_under_v1_do_not_resolve(self):
        """Assert that ambiguous bare-id flat routes under /api/v1/ do not resolve."""
        from django.urls import resolve, Resolver404
        sample_uuid = "334bd0f8-92c1-4f50-8ba7-b561f67202c3"
        bare_paths = [
            f"/api/v1/{sample_uuid}/status/",
            f"/api/v1/{sample_uuid}/pickup/",
            f"/api/v1/{sample_uuid}/ping/",
            f"/api/v1/{sample_uuid}/verify-otp/",
            f"/api/v1/{sample_uuid}/verify-doorstep/",
            f"/api/v1/{sample_uuid}/collect-cash/",
            f"/api/v1/{sample_uuid}/reissue-otp/",
            f"/api/v1/{sample_uuid}/cancel/",
        ]
        for path in bare_paths:
            with self.assertRaises(Resolver404, msg=f"Bare route {path} should not resolve"):
                resolve(path)


class RefundLedgerAndServiceTests(APITestCase):
    """
    Milestone 9 Task 1: Refund Ledger & issue_refund Service Tests
    """
    def setUp(self):
        from apps.accounts.models import User, ShelterProfile, DeliveryProfile
        from apps.pets.models import Pet
        from apps.adoptions.models import AdoptionApplication
        from apps.logistics.models import PetDelivery, Payment, PaymentStatus, Refund, RefundStatus

        self.shelter_user = User.objects.create_user(
            username='refund_shelter_owner',
            email='owner@refundshelter.org',
            password='password123',
            role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Refund Shelter Paws',
            pickup_address='100 Shelter Way',
            city='Bangalore',
            is_verified=True
        )

        self.adopter = User.objects.create_user(
            username='refund_adopter',
            email='adopter@refundexample.org',
            password='password123',
            role='customer'
        )

        self.pet = Pet.objects.create(
            shelter=self.shelter,
            name='RefundPet',
            species='dog',
            breed='Beagle',
            age_months=10,
            status='available'
        )

        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            living_situation='Apartment',
            contact_phone='9998887776',
            status='approved',
            total_amount=Decimal('250.00'),
            payment_status='paid'
        )

        self.delivery = PetDelivery.objects.create(
            application=self.application,
            status='assigned',
            payment_status='paid'
        )

        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount=Decimal('250.00'),
            amount_in_paise=25000,
            status=PaymentStatus.CAPTURED,
            razorpay_order_id='order_refund_test_123',
            razorpay_payment_id='pay_refund_test_456'
        )

    def test_refund_model_partial_unique_constraint(self):
        """Assert only one active non-failed refund can exist per payment."""
        from django.db import IntegrityError
        from apps.logistics.models import Refund, RefundStatus

        # First refund created
        r1 = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            reason="Test refund 1"
        )
        self.assertIsNotNone(r1.id)

        # Second active refund raises IntegrityError (wrapped in atomic to handle savepoint in Postgres)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Refund.objects.create(
                    payment=self.payment,
                    application=self.application,
                    amount_in_paise=25000,
                    status=RefundStatus.CREATED,
                    reason="Test refund 2 duplicate"
                )

        # If r1 is failed, a new refund can be created
        r1.status = RefundStatus.FAILED
        r1.save()

        r2 = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            reason="Test refund 3 after failure"
        )
        self.assertIsNotNone(r2.id)

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_issue_refund_success_and_notifications(self, mock_get_client):
        """Test issue_refund end-to-end success transitions state and dispatches notifications."""
        from django.core import mail
        from apps.accounts.models import Notification
        from apps.logistics.models import Refund, RefundStatus, PaymentStatus
        from apps.logistics.payment_services import issue_refund

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_mock_success_123',
            'entity': 'refund',
            'amount': 25000,
            'status': 'processed',
            'payment_id': 'pay_refund_test_456'
        }

        with self.captureOnCommitCallbacks(execute=True):
            refund = issue_refund(
                payment=self.payment,
                reason="Application cancelled by shelter",
                initiated_by=self.shelter_user,
                source='manual'
            )

        self.assertEqual(refund.status, RefundStatus.PROCESSED)
        self.assertEqual(refund.razorpay_refund_id, 'rfnd_mock_success_123')
        self.assertEqual(refund.amount_in_paise, 25000)

        # Model states updated
        self.payment.refresh_from_db()
        self.application.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.application.payment_status, 'refunded')
        self.assertEqual(self.delivery.payment_status, 'refunded')

        # In-app notification created
        notif = Notification.objects.filter(recipient=self.adopter, notification_type='payment').first()
        self.assertIsNotNone(notif)
        self.assertIn("Refund Processed", notif.title)
        self.assertIn("250.00", notif.message)

        # Email dispatched
        self.assertTrue(len(mail.outbox) >= 1)
        refund_email = [m for m in mail.outbox if "Refund Processed" in m.subject][0]
        self.assertIn(self.adopter.email, refund_email.to)
        self.assertIn("250.00", refund_email.body)

        # Verify idempotency: second call returns existing refund without second gateway call
        mock_client.payment.refund.reset_mock()
        second_refund = issue_refund(payment=self.payment, reason="Retry")
        self.assertEqual(second_refund.id, refund.id)
        mock_client.payment.refund.assert_not_called()

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_issue_refund_gateway_error_allows_retry(self, mock_get_client):
        """Test issue_refund marks refund failed on API error and allows retry."""
        from apps.logistics.models import Refund, RefundStatus, PaymentStatus
        from apps.logistics.payment_services import issue_refund, RefundFailedError

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.side_effect = Exception("Razorpay 500 Internal Error")

        with self.assertRaises(RefundFailedError):
            issue_refund(payment=self.payment, reason="Failed test")

        failed_refund = Refund.objects.filter(payment=self.payment).first()
        self.assertIsNotNone(failed_refund)
        self.assertEqual(failed_refund.status, RefundStatus.FAILED)
        self.assertIn("Razorpay 500", failed_refund.failure_reason)

        # Payment remains captured
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CAPTURED)

        # Retry with success
        mock_client.payment.refund.side_effect = None
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_retry_success_999',
            'amount': 25000,
            'status': 'processed'
        }

        success_refund = issue_refund(payment=self.payment, reason="Retry succeeds")
        self.assertEqual(success_refund.status, RefundStatus.PROCESSED)
        self.assertEqual(success_refund.razorpay_refund_id, 'rfnd_retry_success_999')
        self.assertEqual(success_refund.id, failed_refund.id)

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_refund_call_succeeds_then_db_write_raises_retry_adopts_existing_razorpay_refund(self, mock_get_client):
        """
        Milestone 9b Task 2: When a refund exists at Razorpay gateway without our DB having
        the razorpay_refund_id, the retry adopts the existing Razorpay refund and does not call refund again.
        """
        mock_client = mock_get_client.return_value

        # Existing unlinked refund in 'created' or 'failed' (simulating DB write crash after gateway call)
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.FAILED,
            failure_reason="Simulated DB write failure after gateway success",
            reason="Original refund attempt",
            source=RefundSource.MANUAL
        )

        # Gateway returns the existing full refund in fetch_multiple_refund
        mock_client.payment.fetch_multiple_refund.return_value = {
            'entity': 'collection',
            'count': 1,
            'items': [
                {
                    'id': 'rfnd_existing_gateway_adopt_888',
                    'amount': 25000,
                    'status': 'processed',
                    'payment_id': self.payment.razorpay_payment_id
                }
            ]
        }

        # Issue refund on retry
        from apps.logistics.payment_services import issue_refund
        adopted_refund = issue_refund(payment=self.payment, reason="Retry adopting existing gateway refund")

        self.assertEqual(adopted_refund.id, refund.id)
        self.assertEqual(adopted_refund.status, RefundStatus.PROCESSED)
        self.assertEqual(adopted_refund.razorpay_refund_id, 'rfnd_existing_gateway_adopt_888')

        # Crucial assertion: client.payment.refund was NEVER called
        mock_client.payment.refund.assert_not_called()

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_refund_idempotency_header_and_deterministic_keys(self, mock_get_client):
        """
        Milestone 10a Task 2: Assert exact X-Refund-Idempotency header,
        that retries of one Refund row send the same key, and a new row sends a different key.
        """
        import re
        from apps.logistics.models import Refund, RefundStatus
        from apps.logistics.payment_services import issue_refund

        mock_client = mock_get_client.return_value
        mock_client.payment.fetch_multiple_refund.return_value = {'items': []}

        # Step 1: Initial refund call fails at gateway (so row remains FAILED and retryable)
        mock_client.payment.refund.side_effect = Exception("Gateway connection error")
        from apps.logistics.payment_services import RefundFailedError
        with self.assertRaises(RefundFailedError):
            issue_refund(payment=self.payment, reason="Initial attempt")

        first_call_args = mock_client.payment.refund.call_args
        first_headers = first_call_args.kwargs.get('headers', {})
        self.assertIn('X-Refund-Idempotency', first_headers)
        first_key = first_headers['X-Refund-Idempotency']

        refund_row_1 = Refund.objects.filter(payment=self.payment).order_by('-created_at').first()
        self.assertIsNotNone(refund_row_1)
        expected_key_1 = f"kh-refund-{refund_row_1.id}"
        self.assertEqual(first_key, expected_key_1)
        # Key must be at least 10 chars and contain only letters, digits, hyphens, and underscores
        self.assertGreaterEqual(len(first_key), 10)
        self.assertTrue(re.match(r'^[A-Za-z0-9_-]+$', first_key))

        # Step 2: Retry of the same Refund row reuses the same deterministic key
        mock_client.payment.refund.reset_mock()
        mock_client.payment.refund.side_effect = None
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_retry_row1',
            'amount': 25000,
            'status': 'processed'
        }
        retried_refund = issue_refund(payment=self.payment, reason="Retry of row 1", refund_obj=refund_row_1)
        self.assertEqual(retried_refund.id, refund_row_1.id)

        retry_call_args = mock_client.payment.refund.call_args
        retry_headers = retry_call_args.kwargs.get('headers', {})
        self.assertEqual(retry_headers.get('X-Refund-Idempotency'), expected_key_1)

        # Step 3: A new Refund row for another payment gets a distinct key
        payment_2 = Payment.objects.create(
            delivery=None,
            application=self.application,
            amount_in_paise=15000,
            status=PaymentStatus.CAPTURED,
            razorpay_order_id='order_distinct_pay_2',
            razorpay_payment_id='pay_distinct_pay_2',
            currency='INR'
        )
        mock_client.payment.refund.reset_mock()
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_new_row2',
            'amount': 15000,
            'status': 'processed'
        }
        new_refund = issue_refund(payment=payment_2, reason="New payment refund")
        self.assertNotEqual(new_refund.id, refund_row_1.id)

        new_call_args = mock_client.payment.refund.call_args
        new_headers = new_call_args.kwargs.get('headers', {})
        new_key = new_headers.get('X-Refund-Idempotency')
        expected_key_2 = f"kh-refund-{new_refund.id}"
        self.assertEqual(new_key, expected_key_2)
        self.assertNotEqual(new_key, first_key)
        self.assertGreaterEqual(len(new_key), 10)
        self.assertTrue(re.match(r'^[A-Za-z0-9_-]+$', new_key))

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_refund_409_response_handled_as_in_progress_without_crash_or_state_change(self, mock_get_client):
        """
        Milestone 10a Task 2: Razorpay 409 response for idempotency header is treated
        as 'refund_in_progress' (retryable): no state change, clear log line, no crash.
        """
        from apps.logistics.models import Refund, RefundStatus
        from apps.logistics.payment_services import issue_refund

        mock_client = mock_get_client.return_value
        mock_client.payment.fetch_multiple_refund.return_value = {'items': []}

        # Simulate Razorpay 409 exception
        conflict_err = Exception("409 Client Error: Conflict for url: https://api.razorpay.com/v1/payments/.../refund")
        conflict_err.status_code = 409
        mock_client.payment.refund.side_effect = conflict_err

        # Should not raise RefundFailedError or crash
        refund = issue_refund(payment=self.payment, reason="Testing 409 handling")

        # No state change: status must NOT be FAILED, remains CREATED
        self.assertEqual(refund.status, RefundStatus.CREATED)
        self.assertEqual(refund.failure_reason, '')

        # Payment remains captured
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CAPTURED)


class CancelAndRefundEndpointTests(TestCase):
    """Milestone 9 Task 2: Shelter Delivery Cancel and Refund Endpoint Tests."""

    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(
            username='owning_shelter_mgr',
            email='owning_shelter@test.com',
            role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Happy Paws Shelter',
            pickup_address='123 Shelter Way',
            is_verified=True
        )

        self.unverified_shelter_user = User.objects.create_user(
            username='unverified_shelter_mgr',
            email='unverified_shelter@test.com',
            role='shelter'
        )
        self.unverified_shelter = ShelterProfile.objects.create(
            user=self.unverified_shelter_user,
            organization_name='Unverified Paws Shelter',
            pickup_address='456 Unverified Way',
            is_verified=False
        )

        self.other_shelter_user = User.objects.create_user(
            username='other_shelter_mgr',
            email='other_shelter@test.com',
            role='shelter'
        )
        self.other_shelter = ShelterProfile.objects.create(
            user=self.other_shelter_user,
            organization_name='Other Paws Shelter',
            pickup_address='789 Other Way',
            is_verified=True
        )

        self.admin_user = User.objects.create_user(
            username='admin_boss',
            email='admin@test.com',
            role='admin',
            is_staff=True
        )

        self.adopter = User.objects.create_user(
            username='adopter_john',
            email='john@test.com',
            role='adopter'
        )

        self.driver_user = User.objects.create_user(
            username='speedy_driver',
            email='driver@test.com',
            role='delivery'
        )
        self.driver = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.shelter,
            is_available=False,
            vehicle_type='two_wheeler',
            vehicle_number='KA01AB1234'
        )

        self.pet = Pet.objects.create(
            shelter=self.shelter,
            name='Buddy',
            species='dog',
            breed='Golden Retriever',
            age_months=12,
            status='pending'
        )

        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            living_situation='House with yard',
            contact_phone='9998887776',
            status='approved',
            total_amount=Decimal('250.00'),
            payment_status='paid'
        )

        self.competing_app = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.other_shelter_user,
            living_situation='Apartment',
            contact_phone='9998887775',
            status='rejected',
            total_amount=Decimal('250.00'),
            payment_status='unpaid'
        )

        self.delivery = PetDelivery.objects.create(
            application=self.application,
            driver=self.driver,
            status='assigned',
            payment_status='paid'
        )

        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount=Decimal('250.00'),
            amount_in_paise=25000,
            status=PaymentStatus.CAPTURED,
            payment_method='razorpay',
            razorpay_order_id='order_cr_test_123',
            razorpay_payment_id='pay_cr_test_456'
        )
        self.url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel-and-refund/'

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_cancel_and_refund_success_flow(self, mock_get_client):
        """Owning verified shelter cancels and refunds active delivery, updating state and freeing resources."""
        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_cr_flow_123',
            'amount': 25000,
            'status': 'processed'
        }

        self.client.force_authenticate(user=self.shelter_user)
        from apps.accounts.models import Notification
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.url, {'reason': 'Adopter requested immediate cancellation'}, format='json')

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['code'], 'success')
        self.assertEqual(res.data['status'], 'refunded')
        self.assertEqual(res.data['amount_refunded_paise'], 25000)

        # Verify state changes
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

        self.driver.refresh_from_db()
        self.assertTrue(self.driver.is_available)

        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'cancelled')
        self.assertEqual(self.application.payment_status, 'refunded')

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'available')

        self.competing_app.refresh_from_db()
        self.assertEqual(self.competing_app.status, 'rejected')

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)

        from apps.logistics.models import Refund, RefundStatus
        refund = Refund.objects.filter(payment=self.payment).first()
        self.assertIsNotNone(refund)
        self.assertEqual(refund.status, RefundStatus.PROCESSED)
        self.assertEqual(refund.amount_in_paise, 25000)
        self.assertEqual(refund.razorpay_refund_id, 'rfnd_cr_flow_123')

        # Notifications: exactly one adopter notice per refund, one driver notice when active delivery cancelled
        driver_notices = Notification.objects.filter(recipient=self.driver_user, notification_type='delivery')
        self.assertEqual(driver_notices.count(), 1)
        self.assertIn("Delivery Cancelled", driver_notices.first().title)

        adopter_notices = Notification.objects.filter(recipient=self.adopter, notification_type='payment')
        self.assertEqual(adopter_notices.count(), 1)
        self.assertIn("Refund Processed", adopter_notices.first().title)

        # Idempotency: repeat call returns 200 with code already_refunded
        res_repeat = self.client.post(self.url, {'reason': 'Repeat call'}, format='json')
        self.assertEqual(res_repeat.status_code, status.HTTP_200_OK)
        self.assertEqual(res_repeat.data['code'], 'already_refunded')

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_cancel_and_refund_admin_allowed(self, mock_get_client):
        """Platform admin can cancel and refund deliveries across any shelter."""
        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_admin_flow_456',
            'amount': 25000,
            'status': 'processed'
        }

        self.client.force_authenticate(user=self.admin_user)
        res = self.client.post(self.url, {'reason': 'Admin policy cancellation'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['code'], 'success')

    def test_cancel_and_refund_permissions_enforcement(self):
        """Only owning verified shelter or admin can cancel-and-refund; others rejected with 403 or 401, 404 for missing."""
        # 1. Foreign shelter -> 403
        self.client.force_authenticate(user=self.other_shelter_user)
        res_foreign = self.client.post(self.url, {'reason': 'Wrong shelter'}, format='json')
        self.assertEqual(res_foreign.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Unverified shelter -> 403
        self.client.force_authenticate(user=self.unverified_shelter_user)
        res_unverified = self.client.post(self.url, {'reason': 'Unverified'}, format='json')
        self.assertEqual(res_unverified.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_unverified.data.get('code'), 'shelter_not_verified')

        # 3. Adopter -> 403
        self.client.force_authenticate(user=self.adopter)
        res_adopter = self.client.post(self.url, {'reason': 'Adopter attempt'}, format='json')
        self.assertEqual(res_adopter.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Driver -> 403
        self.client.force_authenticate(user=self.driver_user)
        res_driver = self.client.post(self.url, {'reason': 'Driver attempt'}, format='json')
        self.assertEqual(res_driver.status_code, status.HTTP_403_FORBIDDEN)

        # 5. Anonymous -> 401
        self.client.force_authenticate(user=None)
        res_anon = self.client.post(self.url, {'reason': 'Anon attempt'}, format='json')
        self.assertEqual(res_anon.status_code, status.HTTP_401_UNAUTHORIZED)

        # 6. Non-existent delivery -> 404
        self.client.force_authenticate(user=self.shelter_user)
        res_404 = self.client.post('/api/v1/shelter/deliveries/00000000-0000-0000-0000-000000000000/cancel-and-refund/', {'reason': 'Missing'}, format='json')
        self.assertEqual(res_404.status_code, status.HTTP_404_NOT_FOUND)

    def test_cancel_and_refund_validation_errors(self):
        """Missing or oversized reason returns 400 validation error."""
        self.client.force_authenticate(user=self.shelter_user)

        # Empty reason
        res_empty = self.client.post(self.url, {'reason': ''}, format='json')
        self.assertEqual(res_empty.status_code, status.HTTP_400_BAD_REQUEST)

        # Reason > 500 chars
        res_long = self.client.post(self.url, {'reason': 'A' * 501}, format='json')
        self.assertEqual(res_long.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_and_refund_blocks_delivered_status(self):
        """Cannot cancel completed (delivered) delivery."""
        self.delivery.status = 'delivered'
        self.delivery.delivered_at = timezone.now()
        self.delivery.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(self.url, {'reason': 'Delivery completed already'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data['code'], 'delivery_completed')

    def test_cancel_and_refund_blocks_cash_collected(self):
        """Cannot cancel delivery after cash has been collected."""
        self.delivery.cash_collected = Decimal('250.00')
        self.delivery.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(self.url, {'reason': 'Cash collected already'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data['code'], 'cash_collected')

    def test_cancel_and_refund_blocks_no_captured_payment(self):
        """Cannot refund when payment is not captured."""
        self.payment.status = 'created'
        self.payment.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(self.url, {'reason': 'Payment not captured'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data['code'], 'no_captured_payment')

    def test_cancel_and_refund_blocks_refund_in_progress(self):
        """Returns 409 refund_in_progress when an active non-failed refund already exists."""
        from apps.logistics.models import Refund, RefundStatus
        Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            reason="In progress refund"
        )

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(self.url, {'reason': 'Parallel request'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data['code'], 'refund_in_progress')

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_cancel_and_refund_gateway_failure_and_retry(self, mock_get_client):
        """Gateway failure sets delivery 'failed', frees driver, keeps app approved and pet pending; retry succeeds."""
        mock_client = mock_get_client.return_value
        mock_client.payment.refund.side_effect = Exception("Gateway connection error")

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(self.url, {'reason': 'Test gateway failure'}, format='json')

        self.assertEqual(res.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(res.data['code'], 'refund_failed')
        self.assertEqual(mock_client.payment.refund.call_count, 1)

        # Under Milestone 9b: Phase 1 commits delivery 'failed' and frees driver before Phase 2 gateway call
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')

        self.driver.refresh_from_db()
        self.assertTrue(self.driver.is_available)

        # Application and pet remain untouched (approved and pending)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'approved')

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')

        # Now mock gateway succeeding on retry from 'failed' status
        mock_client.payment.refund.side_effect = None
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_retry_after_fail_789',
            'amount': 25000,
            'status': 'processed'
        }

        res_retry = self.client.post(self.url, {'reason': 'Retry after gateway error'}, format='json')
        self.assertEqual(res_retry.status_code, status.HTTP_200_OK)
        self.assertEqual(res_retry.data['code'], 'success')
        self.assertEqual(mock_client.payment.refund.call_count, 2)

        # Verify mutated on successful retry
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, 'failed')
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.is_available)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'cancelled')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'available')


class CancelAndRefundConcurrencyTests(TransactionTestCase):
    """Milestone 9: Concurrency test ensuring simultaneous cancel-and-refund makes exactly 1 Razorpay call."""

    def setUp(self):
        super().setUp()
        self.shelter_user = User.objects.create_user(
            username='conc_refund_shelter', email='crs@test.com', role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='CRS Shelter', pickup_address='100 Conc St', is_verified=True
        )
        self.driver_user = User.objects.create_user(
            username='conc_refund_driver', email='crd@test.com', role='delivery'
        )
        self.driver = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter, is_available=False, vehicle_type='two_wheeler', vehicle_number='KA-02-C-1111'
        )
        self.adopter = User.objects.create_user(
            username='conc_refund_adopter', email='cra@test.com', role='adopter'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter, name='ConcPet', species='dog', breed='Pug', age_months=6, status='pending'
        )
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            living_situation='Apartment',
            contact_phone='9888877777',
            status='approved',
            total_amount=Decimal('150.00'),
            payment_status='paid'
        )
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            driver=self.driver,
            status='assigned',
            payment_status='paid'
        )
        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount=Decimal('150.00'),
            amount_in_paise=15000,
            status=PaymentStatus.CAPTURED,
            payment_method='razorpay',
            razorpay_order_id='order_conc_rfnd_1',
            razorpay_payment_id='pay_conc_rfnd_1'
        )
        self.url = f'/api/v1/shelter/deliveries/{self.delivery.public_id}/cancel-and-refund/'

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_two_simultaneous_cancel_and_refund_makes_exactly_one_gateway_call(self, mock_get_client):
        import threading
        from django.db import connection

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_conc_single_call',
            'amount': 15000,
            'status': 'processed'
        }

        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def worker():
            try:
                client = APIClient()
                client.force_authenticate(user=self.shelter_user)
                barrier.wait(timeout=5)
                res = client.post(self.url, {'reason': 'Simultaneous cancellation'}, format='json')
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

        # Assert exactly one call was made to Razorpay
        self.assertEqual(mock_client.payment.refund.call_count, 1)

        # Both returned valid responses
        self.assertEqual(len(results), 2)
        status_codes = [r.status_code for r in results]
        self.assertIn(status.HTTP_200_OK, status_codes)

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_doorstep_verify_racing_cancel_and_refund(self, mock_get_client):
        """
        Doorstep verify racing cancel-and-refund on an out_for_delivery delivery gives
        strictly one outcome:
        - either delivered with zero refund calls,
        - or cancelled and refunded with verify returning 409,
        never both.
        """
        import threading
        from django.db import connection
        from apps.logistics.otp import issue_otp

        # Set delivery to 'out_for_delivery'
        self.delivery.status = 'out_for_delivery'
        self.delivery.save(update_fields=['status'])

        raw_otp = issue_otp(self.delivery, 'doorstep')

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_race_doorstep_cancel',
            'amount': 15000,
            'status': 'processed'
        }

        verify_url = f'/api/v1/deliveries/{self.delivery.public_id}/verify-doorstep/'
        cancel_url = self.url

        barrier = threading.Barrier(2)
        results = {}
        lock = threading.Lock()

        def verify_worker():
            try:
                client = APIClient()
                client.force_authenticate(user=self.driver_user)
                barrier.wait(timeout=5)
                res = client.post(verify_url, {'otp': raw_otp}, format='json')
                with lock:
                    results['verify'] = res
            finally:
                connection.close()

        def cancel_worker():
            try:
                client = APIClient()
                client.force_authenticate(user=self.shelter_user)
                barrier.wait(timeout=5)
                res = client.post(cancel_url, {'reason': 'Racing cancellation'}, format='json')
                with lock:
                    results['cancel'] = res
            finally:
                connection.close()

        t1 = threading.Thread(target=verify_worker)
        t2 = threading.Thread(target=cancel_worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        verify_res = results['verify']
        cancel_res = results['cancel']

        # Exactly one outcome, never both
        if verify_res.status_code == status.HTTP_200_OK:
            # Doorstep verification won the race
            self.assertEqual(cancel_res.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(cancel_res.data.get('code'), 'delivery_completed')
            # ZERO refund calls must have been made
            self.assertEqual(mock_client.payment.refund.call_count, 0)
            self.delivery.refresh_from_db()
            self.assertEqual(self.delivery.status, 'delivered')
            self.application.refresh_from_db()
            self.assertEqual(self.application.status, 'completed')
            self.pet.refresh_from_db()
            self.assertEqual(self.pet.status, 'adopted')
        else:
            # Cancel-and-refund won the race
            self.assertEqual(cancel_res.status_code, status.HTTP_200_OK)
            self.assertEqual(verify_res.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(verify_res.data.get('code'), 'invalid_state')
            # EXACTLY ONE refund call must have been made
            self.assertEqual(mock_client.payment.refund.call_count, 1)
            self.delivery.refresh_from_db()
            self.assertEqual(self.delivery.status, 'failed')
            self.application.refresh_from_db()
            self.assertEqual(self.application.status, 'cancelled')
            self.pet.refresh_from_db()
            self.assertEqual(self.pet.status, 'available')



class LateCaptureAndRefundCommandTests(TestCase):
    """Milestone 9 Task 3: Late capture refund scheduling, retry command, and admin tests."""

    def setUp(self):
        self.shelter_user = User.objects.create_user(
            username='late_shelter_mgr', email='late_s@test.com', role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Late Shelter', pickup_address='1 Late Rd', is_verified=True
        )
        self.adopter = User.objects.create_user(
            username='late_adopter', email='late_a@test.com', role='adopter'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter, name='LatePet', species='dog', breed='Terrier', age_months=8, status='available'
        )
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            living_situation='Apartment',
            contact_phone='9991112222',
            status='cancelled',
            total_amount=Decimal('200.00'),
            payment_status='pending'
        )
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            status='failed',
            payment_status='pending'
        )
        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount=Decimal('200.00'),
            amount_in_paise=20000,
            status=PaymentStatus.CREATED,
            payment_method='razorpay',
            razorpay_order_id='order_late_cap_1',
            razorpay_payment_id='pay_late_cap_1'
        )

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_late_capture_on_cancelled_app_records_capture_and_processes_refund(self, mock_get_client):
        """Late capture on cancelled application records payment capture and processes refund."""
        from apps.logistics.payment_services import mark_payment_captured
        from apps.logistics.models import Refund, RefundStatus, RefundSource

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_late_auto_1',
            'amount': 20000,
            'status': 'processed'
        }

        with self.captureOnCommitCallbacks(execute=True):
            res = mark_payment_captured(self.payment, 'pay_late_cap_1')
            self.assertTrue(res)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.payment.razorpay_payment_id, 'pay_late_cap_1')

        # Check Refund row
        refund = Refund.objects.filter(payment=self.payment).first()
        self.assertIsNotNone(refund)
        self.assertEqual(refund.status, RefundStatus.PROCESSED)
        self.assertEqual(refund.source, RefundSource.LATE_CAPTURE)
        self.assertEqual(refund.amount_in_paise, 20000)
        self.assertEqual(refund.razorpay_refund_id, 'rfnd_late_auto_1')

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_retry_pending_refunds_command_is_idempotent(self, mock_get_client):
        """Management command retry_pending_refunds retries older failed/created refunds idempotently."""
        from datetime import timedelta
        from django.utils import timezone
        from django.core.management import call_command
        from io import StringIO
        from apps.logistics.models import Refund, RefundStatus

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_mgmt_retry_100',
            'amount': 20000,
            'status': 'processed'
        }

        self.payment.status = PaymentStatus.CAPTURED
        self.payment.save()

        # Create a refund in failed state, backdated by 20 minutes
        old_time = timezone.now() - timedelta(minutes=20)
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=20000,
            status=RefundStatus.FAILED,
            reason="Initial attempt failed",
            failure_reason="Gateway timeout"
        )
        Refund.objects.filter(id=refund.id).update(created_at=old_time)

        out = StringIO()
        call_command('retry_pending_refunds', minutes=15, stdout=out)
        output = out.getvalue()
        self.assertIn("Completed refund retries: 1 retried, 1 succeeded", output)

        # Check refund was processed
        processed_refund = Refund.objects.filter(payment=self.payment, status=RefundStatus.PROCESSED).first()
        self.assertIsNotNone(processed_refund)
        self.assertEqual(processed_refund.razorpay_refund_id, 'rfnd_mgmt_retry_100')

        # Re-run command - must be idempotent (0 pending found)
        out_second = StringIO()
        call_command('retry_pending_refunds', minutes=15, stdout=out_second)
        self.assertIn("No pending or failed refunds found", out_second.getvalue())

    def test_refund_admin_is_read_only(self):
        """RefundAdmin has all permissions disabled (no add, change, delete)."""
        from django.contrib.admin.sites import AdminSite
        from apps.logistics.admin import RefundAdmin
        from apps.logistics.models import Refund

        admin_instance = RefundAdmin(Refund, AdminSite())
        self.assertFalse(admin_instance.has_add_permission(None))
        self.assertFalse(admin_instance.has_delete_permission(None))
        self.assertFalse(admin_instance.has_change_permission(None))

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_retry_pending_refunds_uses_refund_retry_after_minutes_setting(self, mock_get_client):
        """Milestone 9b Task 3: retry_pending_refunds honors REFUND_RETRY_AFTER_MINUTES setting."""
        from datetime import timedelta
        from django.conf import settings
        from django.core.management import call_command
        from django.test import override_settings
        from django.utils import timezone
        from io import StringIO
        from apps.logistics.models import Refund, RefundStatus

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_env_test_999',
            'amount': 20000,
            'status': 'processed'
        }

        self.payment.status = PaymentStatus.CAPTURED
        self.payment.save()

        # Create refund 8 minutes ago (less than default 10 minutes)
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=20000,
            status=RefundStatus.FAILED,
            reason="Recent failure",
            failure_reason="Temporary network failure"
        )
        Refund.objects.filter(id=refund.id).update(created_at=timezone.now() - timedelta(minutes=8))

        # Default is 10 minutes: 8-minute-old refund should NOT be retried
        out = StringIO()
        call_command('retry_pending_refunds', stdout=out)
        self.assertIn("No pending or failed refunds found", out.getvalue())
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.FAILED)

        # Override setting to 5 minutes: now 8-minute-old refund SHOULD be retried
        with override_settings(REFUND_RETRY_AFTER_MINUTES=5):
            out2 = StringIO()
            call_command('retry_pending_refunds', stdout=out2)
            self.assertIn("Completed refund retries: 1 retried, 1 succeeded", out2.getvalue())
            refund.refresh_from_db()
            self.assertEqual(refund.status, RefundStatus.PROCESSED)
            self.assertEqual(refund.razorpay_refund_id, 'rfnd_env_test_999')

    def test_refund_admin_list_display_and_age(self):
        """Milestone 9b Task 3: RefundAdmin shows status, reason, source, failure_reason and age."""
        from django.contrib.admin.sites import AdminSite
        from apps.logistics.admin import RefundAdmin
        from apps.logistics.models import Refund, RefundStatus, RefundSource

        admin_instance = RefundAdmin(Refund, AdminSite())
        for field in ['status', 'reason', 'source', 'failure_reason', 'age']:
            self.assertIn(field, admin_instance.list_display)

        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=20000,
            status=RefundStatus.FAILED,
            reason="Test failure",
            source=RefundSource.MANUAL,
            failure_reason="Gateway rejected"
        )
        age_str = admin_instance.age(refund)
        self.assertIsInstance(age_str, str)
        self.assertTrue(len(age_str) > 0)

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_refund_admin_retry_selected_refunds_action_platform_admin(self, mock_get_client):
        """Milestone 9b Task 3: Platform admin can trigger 'Retry selected refunds' action."""
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from apps.logistics.admin import RefundAdmin
        from apps.logistics.models import Refund, RefundStatus

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_admin_action_success_1',
            'amount': 20000,
            'status': 'processed'
        }

        self.payment.status = PaymentStatus.CAPTURED
        self.payment.save()

        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=20000,
            status=RefundStatus.FAILED,
            reason="Test admin retry",
            failure_reason="Network error"
        )

        admin_user = User.objects.create_superuser(
            username='platform_admin_refund', email='admin_ref@test.com', password='password123'
        )

        rf = RequestFactory()
        request = rf.post('/admin/logistics/refund/')
        request.user = admin_user
        request.session = 'session'
        request._messages = FallbackStorage(request)

        admin_instance = RefundAdmin(Refund, AdminSite())
        actions = admin_instance.get_actions(request)
        self.assertIn('retry_selected_refunds', actions)

        # Execute action
        admin_instance.retry_selected_refunds(request, Refund.objects.filter(id=refund.id))

        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.PROCESSED)
        self.assertEqual(refund.razorpay_refund_id, 'rfnd_admin_action_success_1')

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)

    def test_refund_admin_retry_selected_refunds_denied_for_non_platform_admin(self):
        """Milestone 9b Task 3: Non-platform admin is denied retry action."""
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from apps.logistics.admin import RefundAdmin
        from apps.logistics.models import Refund, RefundStatus

        self.payment.status = PaymentStatus.CAPTURED
        self.payment.save()

        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=20000,
            status=RefundStatus.FAILED,
            reason="Cannot retry by shelter",
            failure_reason="Initial fail"
        )

        rf = RequestFactory()
        request = rf.post('/admin/logistics/refund/')
        request.user = self.shelter_user  # shelter role, not admin
        request.session = 'session'
        request._messages = FallbackStorage(request)

        admin_instance = RefundAdmin(Refund, AdminSite())
        actions = admin_instance.get_actions(request)
        self.assertNotIn('retry_selected_refunds', actions)

        # Direct invocation must be blocked
        admin_instance.retry_selected_refunds(request, Refund.objects.filter(id=refund.id))

        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.FAILED)


class LateCaptureConcurrencyTests(TransactionTestCase):
    """Milestone 9: Concurrent verify + webhook on cancelled application creates exactly one refund."""

    def setUp(self):
        super().setUp()
        self.shelter_user = User.objects.create_user(
            username='conc_late_shelter', email='cls@test.com', role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='CLS Shelter', pickup_address='102 Conc St', is_verified=True
        )
        self.adopter = User.objects.create_user(
            username='conc_late_adopter', email='cla@test.com', role='adopter'
        )
        self.pet = Pet.objects.create(
            shelter=self.shelter, name='ConcLatePet', species='cat', breed='Persian', age_months=7, status='available'
        )
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter,
            living_situation='Apartment',
            contact_phone='9777766666',
            status='cancelled',
            total_amount=Decimal('180.00'),
            payment_status='pending'
        )
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            status='failed',
            payment_status='pending'
        )
        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount=Decimal('180.00'),
            amount_in_paise=18000,
            status=PaymentStatus.CREATED,
            payment_method='razorpay',
            razorpay_order_id='order_late_conc_1',
            razorpay_payment_id='pay_late_conc_1'
        )

    @patch('apps.logistics.payment_services.get_razorpay_client')
    def test_concurrent_mark_payment_captured_creates_exactly_one_refund(self, mock_get_client):
        import threading
        from django.db import connection
        from apps.logistics.payment_services import mark_payment_captured
        from apps.logistics.models import Refund

        mock_client = mock_get_client.return_value
        mock_client.payment.refund.return_value = {
            'id': 'rfnd_late_conc_single',
            'amount': 18000,
            'status': 'processed'
        }

        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                mark_payment_captured(self.payment, 'pay_late_conc_1')
            except Exception as e:
                errors.append(e)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(errors, [])
        # Exactly one non-failed Refund created
        active_refunds = Refund.objects.filter(payment=self.payment).exclude(status='failed')
        self.assertEqual(active_refunds.count(), 1)


class RefundWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(
            email='shelter_wh@example.com',
            username='shelter_wh',
            password='TestPassword123!',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='WH Shelter',
            pickup_address='123 WH St',
            city='Test City',
            latitude=Decimal('12.9716'),
            longitude=Decimal('77.5946'),
            is_active=True,
            is_verified=True
        )
        self.adopter_user = User.objects.create_user(
            email='adopter_wh@example.com',
            username='adopter_wh',
            password='TestPassword123!',
            role='adopter'
        )
        self.pet = Pet.objects.create(
            name='WH Pet',
            species='dog',
            breed='Labrador',
            age_months=24,
            shelter=self.shelter_profile,
            status='adopted'
        )
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter_user,
            living_situation='House',
            contact_phone='9990001111',
            status='approved',
            total_amount=Decimal('250.00'),
            payment_status='paid'
        )
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            status='out_for_delivery',
            payment_status='paid'
        )
        self.payment = Payment.objects.create(
            delivery=self.delivery,
            application=self.application,
            amount_in_paise=25000,
            amount=Decimal('250.00'),
            currency='INR',
            status=PaymentStatus.CAPTURED,
            razorpay_order_id='order_wh_123',
            razorpay_payment_id='pay_wh_123'
        )
        self.webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    def _make_signature(self, raw_body: str) -> str:
        return hmac.new(
            self.webhook_secret.encode('utf-8'),
            raw_body.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def test_refund_processed_webhook_success_and_idempotent(self):
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            razorpay_refund_id='rfnd_wh_proc_123',
            reason='Cancelled by shelter',
            source=RefundSource.MANUAL
        )

        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_wh_proc_123',
                        'amount': 25000,
                        'status': 'processed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        # Public auth, no authorization header
        unauth_client = APIClient()
        res1 = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data.get('status'), 'success')

        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.PROCESSED)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.application.refresh_from_db()
        self.assertEqual(self.application.payment_status, 'refunded')
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.payment_status, 'refunded')

        # Repeat webhook is idempotent
        res2 = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertTrue(res2.data.get('idempotent', False) or res2.data.get('status') == 'success')
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.PROCESSED)

    def test_refund_failed_webhook_flags_for_retry(self):
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            razorpay_refund_id='rfnd_wh_fail_123',
            reason='Cancelled by shelter',
            source=RefundSource.MANUAL
        )

        payload_dict = {
            'event': 'refund.failed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_wh_fail_123',
                        'amount': 25000,
                        'status': 'failed',
                        'error_description': 'Customer bank account closed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        unauth_client = APIClient()
        res = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.FAILED)
        self.assertIn('Customer bank account closed', refund.failure_reason)

    def test_refund_webhook_amount_mismatch_ignored(self):
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            razorpay_refund_id='rfnd_wh_mismatch_123',
            reason='Cancelled by shelter',
            source=RefundSource.MANUAL
        )

        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_wh_mismatch_123',
                        'amount': 10000,
                        'status': 'processed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        res = self.client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'ignored')
        self.assertEqual(res.data.get('detail'), 'Amount mismatch.')

        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundStatus.CREATED)

    def test_refund_webhook_unknown_id_returns_200(self):
        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_unknown_999999',
                        'amount': 25000,
                        'status': 'processed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        res = self.client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'ignored')
        self.assertEqual(res.data.get('detail'), 'Refund not found.')

    def test_refund_webhook_bad_signature_returns_400(self):
        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_wh_bad_sig',
                        'amount': 25000,
                        'status': 'processed'
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)

        unauth_client = APIClient()
        res = unauth_client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE='bad_signature_value'
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid webhook signature", str(res.data))

    def test_unknown_refund_id_matched_by_payment_id(self):
        """
        Milestone 9b Task 2: When refund id is unknown, match by payment id in payload entity to our Payment.
        If exactly one non-failed Refund row has no razorpay_refund_id, attach id and apply event.
        """
        # Exactly one unlinked non-failed Refund row
        refund = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            razorpay_refund_id=None,
            reason='Cancelled by shelter',
            source=RefundSource.MANUAL
        )

        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_unknown_but_matched_by_pay_id_777',
                        'amount': 25000,
                        'status': 'processed',
                        'payment_id': self.payment.razorpay_payment_id
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        res = self.client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'success')

        refund.refresh_from_db()
        self.assertEqual(refund.razorpay_refund_id, 'rfnd_unknown_but_matched_by_pay_id_777')
        self.assertEqual(refund.status, RefundStatus.PROCESSED)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)

    def test_unknown_refund_id_with_multiple_candidates_returns_200_no_change(self):
        """
        Milestone 9b Task 2: If refund id is unknown and matching by payment id finds != 1 unlinked candidate,
        log and return 200 with no change.
        """
        # Two unlinked non-failed refund rows
        r1 = Refund.objects.create(
            payment=self.payment,
            application=self.application,
            amount_in_paise=25000,
            status=RefundStatus.CREATED,
            razorpay_refund_id=None,
            reason='R1'
        )
        # Note: partial unique constraint allows only 1 non-failed per payment in DB, so candidate count is 0 if no unlinked rows
        # Let's test candidate count == 0 when payment has no unlinked refund
        r1.razorpay_refund_id = 'rfnd_already_linked'
        r1.save()

        payload_dict = {
            'event': 'refund.processed',
            'payload': {
                'refund': {
                    'entity': {
                        'id': 'rfnd_unknown_unmatched_000',
                        'amount': 25000,
                        'status': 'processed',
                        'payment_id': self.payment.razorpay_payment_id
                    }
                }
            }
        }
        raw_body = json.dumps(payload_dict)
        sig = self._make_signature(raw_body)

        res = self.client.post(
            '/api/v1/payments/webhook/',
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'ignored')
        self.assertEqual(res.data.get('detail'), 'Multiple or zero candidate refunds found for payment.')


class CODCashReconciliationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.shelter_user = User.objects.create_user(
            email='shelter_recon@example.com',
            username='shelter_recon',
            password='TestPassword123!',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Recon Shelter',
            pickup_address='123 Recon Way',
            city='Bangalore',
            latitude=Decimal('12.9716'),
            longitude=Decimal('77.5946'),
            is_active=True,
            is_verified=True
        )

        self.other_shelter_user = User.objects.create_user(
            email='other_recon@example.com',
            username='other_recon',
            password='TestPassword123!',
            role='shelter'
        )
        self.other_shelter_profile = ShelterProfile.objects.create(
            user=self.other_shelter_user,
            organization_name='Other Shelter',
            pickup_address='456 Other Way',
            city='Bangalore',
            latitude=Decimal('12.9716'),
            longitude=Decimal('77.5946'),
            is_active=True,
            is_verified=True
        )

        self.admin_user = User.objects.create_user(
            email='admin_recon@example.com',
            username='admin_recon',
            password='TestPassword123!',
            role='admin'
        )

        self.adopter_user = User.objects.create_user(
            email='adopter_recon@example.com',
            username='adopter_recon',
            password='TestPassword123!',
            role='adopter'
        )

        self.driver_user = User.objects.create_user(
            email='driver_recon@example.com',
            username='driver_recon',
            password='TestPassword123!',
            role='delivery'
        )

        self.pet = Pet.objects.create(
            name='Recon Pet',
            species='dog',
            breed='Beagle',
            age_months=12,
            shelter=self.shelter_profile,
            status='adopted'
        )

        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.adopter_user,
            living_situation='Apartment',
            contact_phone='9992223333',
            status='approved',
            total_amount=Decimal('150.00'),
            payment_status='paid',
            payment_method='cod'
        )

        self.delivery = PetDelivery.objects.create(
            application=self.application,
            status='delivered',
            delivered_at=timezone.now(),
            cash_collected=Decimal('150.00'),
            is_cash_settled_with_shelter=False,
            payment_status='paid'
        )

    def test_reconcile_cash_success_owner_shelter(self):
        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data.get('status'), 'reconciled')
        self.assertEqual(res.data.get('code'), 'success')
        self.assertTrue(res.data.get('is_cash_settled_with_shelter'))

        self.delivery.refresh_from_db()
        self.assertTrue(self.delivery.is_cash_settled_with_shelter)
        self.assertIsNotNone(self.delivery.cash_settled_at)
        self.assertEqual(self.delivery.cash_settled_by, self.shelter_user)

    def test_reconcile_cash_success_admin(self):
        self.client.force_authenticate(user=self.admin_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertTrue(self.delivery.is_cash_settled_with_shelter)
        self.assertEqual(self.delivery.cash_settled_by, self.admin_user)

    def test_reconcile_cash_forbidden_other_shelter(self):
        self.client.force_authenticate(user=self.other_shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.delivery.refresh_from_db()
        self.assertFalse(self.delivery.is_cash_settled_with_shelter)

    def test_reconcile_cash_forbidden_unverified_shelter(self):
        self.shelter_profile.is_verified = False
        self.shelter_profile.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get('code'), 'shelter_not_verified')

    def test_reconcile_cash_forbidden_adopter_and_anon(self):
        # Anonymous -> 401
        res_anon = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res_anon.status_code, status.HTTP_401_UNAUTHORIZED)

        # Adopter -> 403
        self.client.force_authenticate(user=self.adopter_user)
        res_adopter = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res_adopter.status_code, status.HTTP_403_FORBIDDEN)

        # Driver -> 403
        self.client.force_authenticate(user=self.driver_user)
        res_driver = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res_driver.status_code, status.HTTP_403_FORBIDDEN)

        # Non-existent delivery -> 404
        self.client.force_authenticate(user=self.shelter_user)
        res_404 = self.client.post('/api/v1/shelter/deliveries/00000000-0000-0000-0000-000000000000/reconcile-cash/')
        self.assertEqual(res_404.status_code, status.HTTP_404_NOT_FOUND)

    def test_reconcile_cash_blocked_if_not_cod(self):
        self.application.payment_method = 'online'
        self.application.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'invalid_state')

    def test_reconcile_cash_blocked_if_not_delivered(self):
        self.delivery.status = 'out_for_delivery'
        self.delivery.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'invalid_state')

    def test_reconcile_cash_blocked_if_cash_not_collected(self):
        self.delivery.cash_collected = Decimal('0.00')
        self.delivery.save()

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'cash_not_collected')

    def test_reconcile_cash_repeat_call_idempotent(self):
        self.client.force_authenticate(user=self.shelter_user)
        res1 = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        self.delivery.refresh_from_db()
        first_settled_at = self.delivery.cash_settled_at

        # Repeat call
        res2 = self.client.post(f'/api/v1/shelter/deliveries/{self.delivery.public_id}/reconcile-cash/')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data.get('code'), 'already_settled')
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.cash_settled_at, first_settled_at)

    def test_shelter_delivery_list_cash_unsettled_filter(self):
        # Create second delivery for same shelter, settled
        pet2 = Pet.objects.create(
            name='Pet 2', species='dog', breed='Pug', age_months=6,
            shelter=self.shelter_profile, status='adopted'
        )
        app2 = AdoptionApplication.objects.create(
            pet=pet2, applicant=self.adopter_user, living_situation='House',
            contact_phone='9994445555', status='approved',
            payment_method='cod', payment_status='paid'
        )
        deliv2 = PetDelivery.objects.create(
            application=app2, status='delivered', delivered_at=timezone.now(),
            cash_collected=Decimal('100.00'), is_cash_settled_with_shelter=True,
            cash_settled_at=timezone.now(), cash_settled_by=self.shelter_user
        )

        self.client.force_authenticate(user=self.shelter_user)

        # 1. cash_unsettled=true -> only delivery 1
        res_unsettled = self.client.get('/api/v1/shelter/deliveries/?cash_unsettled=true')
        self.assertEqual(res_unsettled.status_code, status.HTTP_200_OK)
        ids_unsettled = [d['public_id'] for d in res_unsettled.data]
        self.assertIn(str(self.delivery.public_id), ids_unsettled)
        self.assertNotIn(str(deliv2.public_id), ids_unsettled)

        # 2. cash_unsettled=false -> only delivery 2
        res_settled = self.client.get('/api/v1/shelter/deliveries/?cash_unsettled=false')
        self.assertEqual(res_settled.status_code, status.HTTP_200_OK)
        ids_settled = [d['public_id'] for d in res_settled.data]
        self.assertNotIn(str(self.delivery.public_id), ids_settled)
        self.assertIn(str(deliv2.public_id), ids_settled)

        # 3. no filter -> both
        res_all = self.client.get('/api/v1/shelter/deliveries/')
        self.assertEqual(res_all.status_code, status.HTTP_200_OK)
        ids_all = [d['public_id'] for d in res_all.data]
        self.assertIn(str(self.delivery.public_id), ids_all)
        self.assertIn(str(deliv2.public_id), ids_all)




















