import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hashlib
import hmac
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User, ShelterProfile, DeliveryProfile, Notification
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication
from apps.logistics.models import PetDelivery, DeliveryLocationPing, Payment
from apps.logistics.serializers import (
    DeliveryDriverTaskSerializer,
    DeliveryCustomerSerializer,
    ShelterDeliverySerializer
)
from apps.logistics.services import (
    assign_delivery_task,
    verify_pickup_otp,
    verify_doorstep_otp,
    dispatch_doorstep_otp,
    send_driver_assigned_notice,
    reissue_delivery_otp,
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
            pickup_address='123 Shelter St'
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
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        mail.outbox.clear()
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)

        # Obtain doorstep OTP from dispatched email
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"OTP is:\s*(\d{6})", mail.outbox[0].body)
        self.assertIsNotNone(match)
        doorstep_code = match.group(1)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
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
        self.assertEqual(payment.status, 'failed')


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
            pickup_address='100 Shelter Blvd'
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
            user=self.shelter_user, organization_name='Security Shelter', pickup_address='100 Sec St'
        )

        self.other_shelter_user = User.objects.create_user(
            username='other_sec_shelter', email='other_shelter@sec.org', password='password123', role='shelter'
        )
        self.other_shelter_profile = ShelterProfile.objects.create(
            user=self.other_shelter_user, organization_name='Other Shelter Org', pickup_address='999 Other St'
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
        verify_pickup_otp(delivery, delivery.raw_pickup_otp)
        doorstep_otp = delivery.raw_doorstep_otp

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
        verify_pickup_otp(del1, del1.raw_pickup_otp)
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(f'/api/v1/delivery/tasks/{del1.public_id}/verify-doorstep/', {'otp': del1.raw_doorstep_otp}, format='json')
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
        verify_pickup_otp(del_cod, del_cod.raw_pickup_otp)
        self.client.post(f'/api/v1/delivery/tasks/{del_cod.public_id}/verify-doorstep/', {'otp': del_cod.raw_doorstep_otp}, format='json')
        app_cod.refresh_from_db()
        self.assertEqual(app_cod.payment_status, 'paid')

        # Case 3: Online paid (captured) -> payment_status='paid'
        pet_onl = Pet.objects.create(shelter=self.shelter_profile, name='OnlPet', species='dog', breed='Pug', age_months=12, gender='female', status='available')
        app_onl = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_onl, status='approved', payment_method='online', payment_status='pending', total_amount=Decimal('200.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_onl = assign_delivery_task(app_onl, self.driver_profile, payment_method='online')
        Payment.objects.create(delivery=del_onl, application=app_onl, amount=Decimal('200.00'), amount_in_paise=20000, status='captured', razorpay_order_id='order_sec_paid')
        verify_pickup_otp(del_onl, del_onl.raw_pickup_otp)
        self.client.post(f'/api/v1/delivery/tasks/{del_onl.public_id}/verify-doorstep/', {'otp': del_onl.raw_doorstep_otp}, format='json')
        app_onl.refresh_from_db()
        self.assertEqual(app_onl.payment_status, 'paid')

        # Case 4: Online unpaid (payment created or failed) -> remains pending
        pet_fail = Pet.objects.create(shelter=self.shelter_profile, name='FailPet', species='cat', breed='Persian', age_months=8, gender='female', status='available')
        app_fail = AdoptionApplication.objects.create(applicant=self.customer_user, pet=pet_fail, status='approved', payment_method='online', payment_status='pending', total_amount=Decimal('100.00'))
        self.driver_profile.is_available = True
        self.driver_profile.save()
        del_fail = assign_delivery_task(app_fail, self.driver_profile, payment_method='online')
        Payment.objects.create(delivery=del_fail, application=app_fail, amount=Decimal('100.00'), amount_in_paise=10000, status='failed', razorpay_order_id='order_sec_fail')
        verify_pickup_otp(del_fail, del_fail.raw_pickup_otp)
        self.client.post(f'/api/v1/delivery/tasks/{del_fail.public_id}/verify-doorstep/', {'otp': del_fail.raw_doorstep_otp}, format='json')
        app_fail.refresh_from_db()
        self.assertEqual(app_fail.payment_status, 'pending')

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

        # Complete pickup to advance state to 'out_for_delivery'
        verify_pickup_otp(fresh_pickup_del, delivery.raw_pickup_otp)

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
        res_pickup = self.client.post(f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/', {'otp': pickup_code}, format='json')
        self.assertEqual(res_pickup.status_code, status.HTTP_200_OK)

        # Doorstep OTP from email
        match = re.search(r'\b\d{6}\b', mail.outbox[-1].body)
        doorstep_code = match.group(0) if match else ''
        self.assertTrue(bool(doorstep_code))

        # 3. Shelter reissue of doorstep OTP
        self.client.force_authenticate(user=self.shelter_user)
        res_reissue = self.client.post(f'/api/v1/shelter/deliveries/{delivery.public_id}/reissue-otp/', {'purpose': 'doorstep'}, format='json')
        self.assertEqual(res_reissue.status_code, status.HTTP_200_OK)
        match_reissue = re.search(r'\b\d{6}\b', mail.outbox[-1].body)
        reissued_doorstep_code = match_reissue.group(0) if match_reissue else ''

        # 4. Doorstep verification
        self.client.force_authenticate(user=self.driver_user)
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
        Confirms that the 'otp_verify' throttle scope is enforced:
        Rate is 10/min. Requests 1-10 are processed (returning 400 for bad input),
        and the 11th request in that same minute returns HTTP 429 Too Many Requests.
        """
        from django.core.cache import cache
        cache.clear()

        delivery = assign_delivery_task(self.application, self.driver_profile)
        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/pickup/'

        for i in range(1, 11):
            res = self.client.post(url, {'otp': '000000'}, format='json')
            self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, 423], f"Request {i} unexpectedly failed with status {res.status_code}")

        # 11th request must be throttled -> 429 Too Many Requests
        res_throttled = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn('Request was throttled', str(res_throttled.data))



