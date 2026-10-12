import sys
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from apps.accounts.models import User, ShelterProfile, DeliveryProfile
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication, AdoptionReview
from apps.logistics.models import PetDelivery

class AdoptionApplicationWorkflowTests(APITestCase):
    def setUp(self):
        # Create Shelter 1 User & Profile
        self.shelter_user_1 = User.objects.create_user(
            username='happy_paws',
            email='shelter1@happypaws.org',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile_1 = ShelterProfile.objects.create(
            user=self.shelter_user_1,
            organization_name='Happy Paws Rescue',
            pickup_address='123 Shelter St',
            latitude=12.9716,
            longitude=77.5946
        )

        # Create Shelter 2 User & Profile
        self.shelter_user_2 = User.objects.create_user(
            username='paws_claws',
            email='shelter2@pawsclaws.org',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile_2 = ShelterProfile.objects.create(
            user=self.shelter_user_2,
            organization_name='Paws & Claws',
            pickup_address='456 Haven Ave',
            latitude=12.9800,
            longitude=77.6000
        )

        # Create Customer 1
        self.customer_1 = User.objects.create_user(
            username='alice_customer',
            email='alice@example.com',
            password='customerpass123',
            role='customer'
        )

        # Create Customer 2
        self.customer_2 = User.objects.create_user(
            username='bob_customer',
            email='bob@example.com',
            password='customerpass123',
            role='customer'
        )

        # Create Pet under Shelter 1
        self.pet_1 = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='Buddy',
            species='dog',
            breed='Labrador',
            age_months=24,
            gender='male',
            description='Friendly lab',
            adoption_fee=150.00,
            status='available'
        )

        self.customer_apps_url = reverse('customer-applications-list')
        self.shelter_apps_url = reverse('shelter-applications-list')

    def test_customer_submit_application_success(self):
        self.client.force_authenticate(user=self.customer_1)
        payload = {
            'pet_public_id': str(self.pet_1.public_id),
            'living_situation': 'Own house with fenced yard',
            'has_other_pets': True,
            'dropoff_address': '789 Park Ave, City',
            'contact_phone': '9876543210',
            'customer_latitude': 12.9600,
            'customer_longitude': 77.5800,
            'application_notes': 'Excited to adopt Buddy!'
        }
        response = self.client.post(self.customer_apps_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'submitted')
        self.assertEqual(response.data['pet']['name'], 'Buddy')

    def test_duplicate_application_prevention(self):
        self.client.force_authenticate(user=self.customer_1)
        payload = {
            'pet_public_id': str(self.pet_1.public_id),
            'living_situation': 'Apartment',
            'dropoff_address': '789 Park Ave, City',
            'contact_phone': '9876543210'
        }
        # First application -> Success
        resp1 = self.client.post(self.customer_apps_url, payload, format='json')
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        # Second application -> Blocked with 400 Bad Request
        resp2 = self.client.post(self.customer_apps_url, payload, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('pet_public_id', resp2.data)

    def test_customer_cancel_application(self):
        self.client.force_authenticate(user=self.customer_1)
        app = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address',
            contact_phone='123'
        )
        cancel_url = reverse('customer-applications-cancel', kwargs={'public_id': app.public_id})
        response = self.client.post(cancel_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        app.refresh_from_db()
        self.assertEqual(app.status, 'cancelled')

    def test_shelter_review_application(self):
        app = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address',
            contact_phone='123',
            status='submitted'
        )
        self.client.force_authenticate(user=self.shelter_user_1)
        review_url = reverse('shelter-applications-review', kwargs={'public_id': app.public_id})
        response = self.client.post(review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        app.refresh_from_db()
        self.assertEqual(app.status, 'reviewing')

    def test_shelter_approval_and_competing_rejection(self):
        # Alice applies for Buddy
        app_alice = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address 1',
            contact_phone='111',
            status='submitted'
        )
        # Bob applies for Buddy
        app_bob = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_2,
            living_situation='Apartment',
            dropoff_address='Address 2',
            contact_phone='222',
            status='reviewing'
        )

        # Shelter 1 approves Alice
        self.client.force_authenticate(user=self.shelter_user_1)
        approve_url = reverse('shelter-applications-approve', kwargs={'public_id': app_alice.public_id})
        response = self.client.post(approve_url, {'notes': 'Great home environment'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check Alice app approved
        app_alice.refresh_from_db()
        self.assertEqual(app_alice.status, 'approved')

        # Check Pet 1 status updated to 'pending'
        self.pet_1.refresh_from_db()
        self.assertEqual(self.pet_1.status, 'pending')

        # Check Bob app auto-rejected
        app_bob.refresh_from_db()
        self.assertEqual(app_bob.status, 'rejected')
        self.assertIn('Pet has been adopted by another applicant', app_bob.application_notes)

    def test_unauthorized_access_shelter_isolation(self):
        app = AdoptionApplication.objects.create(
            pet=self.pet_1, # Owned by Shelter 1
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address',
            contact_phone='123'
        )
        # Shelter 2 attempts to view / approve Shelter 1's application
        self.client.force_authenticate(user=self.shelter_user_2)
        shelter_detail_url = reverse('shelter-applications-detail', kwargs={'public_id': app.public_id})
        response = self.client.get(shelter_detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_review_submission_rejection_when_not_delivered(self):
        driver_user = User.objects.create_user(username='driver_rev', email='driver_rev@example.com', password='pass', role='delivery')
        driver_profile = DeliveryProfile.objects.create(user=driver_user, shelter=self.shelter_profile_1, vehicle_number='REV-01')
        app = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address',
            contact_phone='123',
            status='approved'
        )
        delivery = PetDelivery.objects.create(
            application=app,
            driver=driver_profile,
            status='assigned'
        )

        self.client.force_authenticate(user=self.customer_1)
        url = f'/api/v1/adoptions/applications/{app.public_id}/review/'
        payload = {
            'shelter_rating': 5,
            'shelter_review': 'Super shelter!',
            'driver_rating': 4,
            'driver_review': 'Friendly driver'
        }
        res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Reviews can only be submitted for completed deliveries.", str(res.data))

    def test_review_submission_success_and_duplicate_prevention(self):
        driver_user = User.objects.create_user(username='driver_rev2', email='driver_rev2@example.com', password='pass', role='delivery')
        driver_profile = DeliveryProfile.objects.create(user=driver_user, shelter=self.shelter_profile_1, vehicle_number='REV-02')
        app = AdoptionApplication.objects.create(
            pet=self.pet_1,
            applicant=self.customer_1,
            living_situation='House',
            dropoff_address='Address',
            contact_phone='123',
            status='completed'
        )
        delivery = PetDelivery.objects.create(
            application=app,
            driver=driver_profile,
            status='delivered'
        )

        self.client.force_authenticate(user=self.customer_1)
        url = f'/api/v1/adoptions/applications/{app.public_id}/review/'
        payload = {
            'shelter_rating': 5,
            'shelter_review': 'Outstanding service!',
            'driver_rating': 5,
            'driver_review': 'Fast and safe pet transport!'
        }
        res = self.client.post(url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['shelter_rating'], 5)
        self.assertEqual(res.data['driver_rating'], 5)

        # Duplicate review attempt should fail
        res_dup = self.client.post(url, payload, format='json')
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("A review for this application has already been submitted", str(res_dup.data))

        # Shelter review listing
        self.client.force_authenticate(user=self.shelter_user_1)
        shelter_reviews_res = self.client.get('/api/v1/shelter/reviews/')
        self.assertEqual(shelter_reviews_res.status_code, status.HTTP_200_OK)
        self.assertEqual(shelter_reviews_res.data['total_reviews'], 1)
        self.assertEqual(shelter_reviews_res.data['average_rating'], 5.0)

        # Driver review listing
        self.client.force_authenticate(user=driver_user)
        driver_reviews_res = self.client.get('/api/v1/delivery/reviews/')
        self.assertEqual(driver_reviews_res.status_code, status.HTTP_200_OK)
        self.assertEqual(driver_reviews_res.data['total_reviews'], 1)
        self.assertEqual(driver_reviews_res.data['average_rating'], 5.0)

    def test_customer_submit_application_with_dropoff_delivery_address_and_coordinates(self):
        """Verify submitting application with delivery address, city, pincode, and coordinates."""
        self.client.force_authenticate(user=self.customer_2)
        payload = {
            'pet_public_id': str(self.pet_1.public_id),
            'living_situation': 'Own house with fenced garden',
            'has_other_pets': False,
            'delivery_address': '402 Green Glen Layout, Outer Ring Road',
            'delivery_city': 'Bangalore',
            'delivery_pincode': '560103',
            'destination_latitude': 12.935200,
            'destination_longitude': 77.624500,
            'contact_phone': '+919876599999',
            'application_notes': 'Please deliver on weekend afternoon.'
        }
        res = self.client.post(self.customer_apps_url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['delivery_address'], '402 Green Glen Layout, Outer Ring Road')
        self.assertEqual(res.data['delivery_city'], 'Bangalore')
        self.assertEqual(res.data['delivery_pincode'], '560103')
        self.assertEqual(float(res.data['destination_latitude']), 12.9352)
        self.assertEqual(float(res.data['destination_longitude']), 77.6245)

        # Retrieve as shelter
        self.client.force_authenticate(user=self.shelter_user_1)
        shelter_app_res = self.client.get(f"{self.shelter_apps_url}{res.data['public_id']}/")
        self.assertEqual(shelter_app_res.status_code, status.HTTP_200_OK)
        self.assertEqual(shelter_app_res.data['delivery_address'], '402 Green Glen Layout, Outer Ring Road')
        self.assertEqual(shelter_app_res.data['delivery_city'], 'Bangalore')
        self.assertEqual(shelter_app_res.data['delivery_pincode'], '560103')
        self.assertEqual(float(shelter_app_res.data['destination_latitude']), 12.9352)
        self.assertEqual(float(shelter_app_res.data['destination_longitude']), 77.6245)

    def test_customer_submit_application_with_kyc_fields(self):
        """Verify submitting adoption application with full KYC verification data."""
        # Create another available pet so customer_1 does not hit duplicate application check
        pet_kyc = Pet.objects.create(
            shelter=self.shelter_profile_1,
            name='Cooper',
            species='dog',
            breed='Beagle',
            age_months=12,
            gender='male',
            status='available'
        )
        self.client.force_authenticate(user=self.customer_1)
        payload = {
            'pet_public_id': str(pet_kyc.public_id),
            'living_situation': 'Own Villa',
            'delivery_address': 'Villa 14, Palm Meadows, Bangalore',
            'contact_phone': '+919876543210',
            'id_proof_url': 'https://s3.amazonaws.com/kyc-docs/passport_customer1.pdf',
            'residence_proof_url': 'https://s3.amazonaws.com/kyc-docs/electricity_bill_customer1.pdf',
            'residence_type': 'independent_house',
            'has_fenced_yard': True,
            'has_other_pets': True,
            'other_pets_description': 'One 5-year-old neutered cat (indoor only).'
        }
        res = self.client.post(self.customer_apps_url, payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['id_proof_url'], 'https://s3.amazonaws.com/kyc-docs/passport_customer1.pdf')
        self.assertEqual(res.data['residence_proof_url'], 'https://s3.amazonaws.com/kyc-docs/electricity_bill_customer1.pdf')
        self.assertEqual(res.data['residence_type'], 'independent_house')
        self.assertTrue(res.data['has_fenced_yard'])
        self.assertTrue(res.data['has_other_pets'])
        self.assertEqual(res.data['other_pets_description'], 'One 5-year-old neutered cat (indoor only).')

        # Retrieve as shelter reviewing KYC
        self.client.force_authenticate(user=self.shelter_user_1)
        shelter_app_res = self.client.get(f"{self.shelter_apps_url}{res.data['public_id']}/")
        self.assertEqual(shelter_app_res.status_code, status.HTTP_200_OK)
        self.assertEqual(shelter_app_res.data['id_proof_url'], 'https://s3.amazonaws.com/kyc-docs/passport_customer1.pdf')
        self.assertEqual(shelter_app_res.data['residence_proof_url'], 'https://s3.amazonaws.com/kyc-docs/electricity_bill_customer1.pdf')
        self.assertEqual(shelter_app_res.data['residence_type'], 'independent_house')
        self.assertTrue(shelter_app_res.data['has_fenced_yard'])
        self.assertTrue(shelter_app_res.data['has_other_pets'])
        self.assertEqual(shelter_app_res.data['other_pets_description'], 'One 5-year-old neutered cat (indoor only).')



