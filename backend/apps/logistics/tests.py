import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hashlib
import hmac
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User, ShelterProfile, DeliveryProfile
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication
from apps.logistics.models import PetDelivery, DeliveryLocationPing, Payment
from apps.logistics.services import assign_delivery_task, verify_pickup_otp


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
            contact_phone=self.customer_user.phone
        )

    def test_driver_assignment_and_availability_toggle(self):
        self.client.force_authenticate(user=self.shelter_user)
        url = '/api/v1/shelter/deliveries/assign/'
        payload = {
            'application_public_id': str(self.application.public_id),
            'driver_public_id': str(self.driver_profile.public_id),
            'payment_method': 'cod'
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify delivery created
        delivery = PetDelivery.objects.get(application=self.application)
        self.assertEqual(delivery.status, 'assigned')
        self.assertEqual(len(delivery.pickup_otp), 6)
        self.assertEqual(len(delivery.doorstep_otp), 6)

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
        response = self.client.post(url, {'otp': delivery.pickup_otp}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'out_for_delivery')
        self.assertIsNotNone(delivery.dispatched_at)

    def test_doorstep_otp_brute_force_protection(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        verify_pickup_otp(delivery, delivery.pickup_otp)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'

        # Fail 4 times with invalid OTP
        for _ in range(4):
            res = self.client.post(url, {'otp': '000000'}, format='json')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        delivery.refresh_from_db()
        self.assertEqual(delivery.doorstep_otp_attempts, 4)

        # 5th failed attempt should lock out
        res5 = self.client.post(url, {'otp': '000000'}, format='json')
        self.assertEqual(res5.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Maximum OTP attempts exceeded', str(res5.data))

        # 6th attempt when locked out
        res6 = self.client.post(url, {'otp': delivery.doorstep_otp}, format='json')
        self.assertEqual(res6.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Maximum OTP attempts exceeded', str(res6.data))

    def test_successful_doorstep_otp_verification(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        verify_pickup_otp(delivery, delivery.pickup_otp)

        self.client.force_authenticate(user=self.driver_user)
        url = f'/api/v1/delivery/tasks/{delivery.public_id}/verify-doorstep/'
        response = self.client.post(url, {'otp': delivery.doorstep_otp}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'delivered')
        self.assertIsNotNone(delivery.delivered_at)

        # Check driver profile is free again
        self.driver_profile.refresh_from_db()
        self.assertTrue(self.driver_profile.is_available)

        # Check application status updated to completed
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, 'completed')

    def test_gps_ping_ingestion_and_tracking(self):
        delivery = assign_delivery_task(self.application, self.driver_profile, payment_method='cod')
        verify_pickup_otp(delivery, delivery.pickup_otp)

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

        # Test tracking endpoint for Customer
        self.client.force_authenticate(user=self.customer_user)
        tracking_url = f'/api/v1/delivery/tasks/{delivery.public_id}/tracking/'
        tracking_res = self.client.get(tracking_url)
        self.assertEqual(tracking_res.status_code, status.HTTP_200_OK)
        self.assertEqual(tracking_res.data['status'], 'out_for_delivery')
        self.assertIn('doorstep_otp', tracking_res.data['delivery'])
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

        delivery.refresh_from_db()
        self.assertEqual(delivery.payment_status, 'paid')

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

