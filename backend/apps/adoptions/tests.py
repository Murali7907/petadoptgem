import sys
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from decimal import Decimal
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase, APIClient
from django.utils import timezone
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
            longitude=77.5946,
            is_verified=True
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
            longitude=77.6000,
            is_verified=True
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
            status='delivered',
            delivered_at=timezone.now()
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


class VerifiedShelterAdoptionPolicyTests(APITestCase):
    def setUp(self):
        # Unverified shelter
        self.unverified_user = User.objects.create_user(
            username='unverified_shelter_adopt',
            email='unverified_adopt@shelter.org',
            password='password123',
            role='shelter'
        )
        self.unverified_profile = ShelterProfile.objects.create(
            user=self.unverified_user,
            organization_name='Unverified Adoption Haven',
            pickup_address='100 Unverified Rd',
            is_verified=False
        )
        self.unverified_pet = Pet.objects.create(
            shelter=self.unverified_profile,
            name='GhostPet',
            species='dog',
            breed='Labrador',
            age_months=12,
            gender='male',
            status='available'
        )

        # Verified shelter
        self.verified_user = User.objects.create_user(
            username='verified_shelter_adopt',
            email='verified_adopt@shelter.org',
            password='password123',
            role='shelter'
        )
        self.verified_profile = ShelterProfile.objects.create(
            user=self.verified_user,
            organization_name='Verified Adoption Haven',
            pickup_address='200 Verified Rd',
            is_verified=True
        )
        self.verified_pet = Pet.objects.create(
            shelter=self.verified_profile,
            name='HeroPet',
            species='dog',
            breed='Retriever',
            age_months=18,
            gender='male',
            status='available'
        )

        # Customer
        self.customer = User.objects.create_user(
            username='applicant_adopt',
            email='applicant@adopt.org',
            password='password123',
            role='customer'
        )

        # Admin
        self.admin_user = User.objects.create_superuser(
            username='admin_adopt',
            email='admin@adopt.org',
            password='password123'
        )

        # Applications
        self.app_unverified = AdoptionApplication.objects.create(
            applicant=self.customer,
            pet=self.unverified_pet,
            status='submitted',
            living_situation='House',
            contact_phone='+919876543210'
        )
        self.app_verified = AdoptionApplication.objects.create(
            applicant=self.customer,
            pet=self.verified_pet,
            status='submitted',
            living_situation='Apartment',
            contact_phone='+919876543211'
        )

    def test_unverified_shelter_can_read_own_applications(self):
        self.client.force_authenticate(user=self.unverified_user)
        # List
        res_list = self.client.get('/api/v1/shelter/applications/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        # Retrieve
        res_get = self.client.get(f'/api/v1/shelter/applications/{self.app_unverified.public_id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)

    def test_unverified_shelter_blocked_from_review_approve_reject(self):
        self.client.force_authenticate(user=self.unverified_user)
        # Review
        res_rev = self.client.post(f'/api/v1/shelter/applications/{self.app_unverified.public_id}/review/')
        self.assertEqual(res_rev.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_rev.data.get('code'), 'shelter_not_verified')

        # Approve
        res_app = self.client.post(f'/api/v1/shelter/applications/{self.app_unverified.public_id}/approve/', {'notes': 'approved'})
        self.assertEqual(res_app.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_app.data.get('code'), 'shelter_not_verified')

        # Reject
        res_rej = self.client.post(f'/api/v1/shelter/applications/{self.app_unverified.public_id}/reject/', {'notes': 'rejected'})
        self.assertEqual(res_rej.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_rej.data.get('code'), 'shelter_not_verified')

        # Legacy status PATCH
        res_patch = self.client.patch(f'/api/adoptions/{self.app_unverified.public_id}/status/', {'status': 'reviewing'})
        self.assertEqual(res_patch.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_patch.data.get('code'), 'shelter_not_verified')

    def test_verified_shelter_can_review_and_approve(self):
        self.client.force_authenticate(user=self.verified_user)
        res_rev = self.client.post(f'/api/v1/shelter/applications/{self.app_verified.public_id}/review/')
        self.assertEqual(res_rev.status_code, status.HTTP_200_OK)

        res_app = self.client.post(f'/api/v1/shelter/applications/{self.app_verified.public_id}/approve/', {'notes': 'Great profile'})
        self.assertEqual(res_app.status_code, status.HTTP_200_OK)

    def test_suspension_immediately_blocks_application_writes(self):
        self.client.force_authenticate(user=self.verified_user)
        self.verified_profile.is_verified = False
        self.verified_profile.save()

        res_rev = self.client.post(f'/api/v1/shelter/applications/{self.app_verified.public_id}/review/')
        self.assertEqual(res_rev.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_rev.data.get('code'), 'shelter_not_verified')


class AdoptionPaginationStabilityTests(APITestCase):
    """
    Task 10: Verify deterministic ordering and stable pagination
    without duplicates, gaps, or UnorderedObjectListWarning.
    """
    def setUp(self):
        import warnings
        from django.core.paginator import UnorderedObjectListWarning

        self.shelter_user = User.objects.create_user(
            username='pag_shelter',
            email='pag_shelter@example.com',
            password='shelterpass123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Pagination Rescue Org',
            pickup_address='789 Stability Way',
            is_verified=True
        )
        self.customer = User.objects.create_user(
            username='pag_customer',
            email='pag_customer@example.com',
            password='customerpass123',
            role='customer'
        )

        from datetime import timedelta
        base_time = timezone.now()
        self.applications = []

        # Create 18 pets and applications to span 2 pages (PAGE_SIZE = 12)
        for i in range(18):
            pet = Pet.objects.create(
                shelter=self.shelter_profile,
                name=f"Pet {i:02d}",
                species='dog',
                breed='Labrador',
                age_months=12 + i,
                adoption_fee=100.00,
                status='available'
            )
            # Alternate timestamps; give some identical timestamps to test tie-breaking on -id
            submitted_time = base_time - timedelta(minutes=(i // 2) * 10)
            app = AdoptionApplication.objects.create(
                pet=pet,
                applicant=self.customer,
                living_situation='Apartment',
                contact_phone='9876543210',
                delivery_address='100 Pagination Rd',
                delivery_city='Bengaluru',
                status='submitted'
            )
            # Update auto_now_add submitted_at directly to guarantee exact timestamps
            AdoptionApplication.objects.filter(id=app.id).update(submitted_at=submitted_time)
            app.refresh_from_db()
            self.applications.append(app)

    def test_pagination_two_pages_have_no_duplicates_no_gaps_and_no_warning(self):
        import warnings
        from django.core.paginator import UnorderedObjectListWarning

        self.client.force_authenticate(user=self.shelter_user)

        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")

            # Page 1
            res_p1 = self.client.get('/api/v1/shelter/applications/?page=1')
            self.assertEqual(res_p1.status_code, status.HTTP_200_OK)
            data_p1 = res_p1.data

            # Page 2
            res_p2 = self.client.get('/api/v1/shelter/applications/?page=2')
            self.assertEqual(res_p2.status_code, status.HTTP_200_OK)
            data_p2 = res_p2.data

            # Ensure no UnorderedObjectListWarning was triggered
            unordered_warnings = [
                w for w in caught_warnings if issubclass(w.category, UnorderedObjectListWarning)
            ]
            self.assertEqual(
                len(unordered_warnings), 0,
                f"UnorderedObjectListWarning detected during pagination: {unordered_warnings}"
            )

        self.assertEqual(data_p1['count'], 18)
        self.assertEqual(len(data_p1['results']), 12)
        self.assertEqual(len(data_p2['results']), 6)

        page1_ids = [item['public_id'] for item in data_p1['results']]
        page2_ids = [item['public_id'] for item in data_p2['results']]

        # 1. No duplicates between page 1 and page 2
        overlap = set(page1_ids).intersection(set(page2_ids))
        self.assertEqual(len(overlap), 0, f"Duplicate items across pages: {overlap}")

        # 2. No gaps: union of both pages covers all 18 applications
        all_paginated_ids = set(page1_ids).union(set(page2_ids))
        self.assertEqual(len(all_paginated_ids), 18)

        # 3. Deterministic order matches database (-submitted_at, -id)
        expected_ids = [
            str(pid) for pid in AdoptionApplication.objects.filter(
                pet__shelter=self.shelter_profile
            ).order_by('-submitted_at', '-id').values_list('public_id', flat=True)
        ]
        self.assertEqual(page1_ids + page2_ids, expected_ids)


class ApplicationPetStatusConvergenceTests(APITestCase):
    def setUp(self):
        self.client = APIClient()

        # Shelter
        self.shelter_user = User.objects.create_user(
            username='conv_shelter', email='conv@shelter.org', password='password123', role='shelter'
        )
        self.shelter = ShelterProfile.objects.create(
            user=self.shelter_user, organization_name='Convergence Shelter', is_verified=True
        )

        # Adopters
        self.adopter_1 = User.objects.create_user(
            username='conv_adopter1', email='adopter1@conv.org', password='password123', role='customer'
        )
        self.adopter_2 = User.objects.create_user(
            username='conv_adopter2', email='adopter2@conv.org', password='password123', role='customer'
        )

        # Driver
        self.driver_user = User.objects.create_user(
            username='conv_driver', email='driver@conv.org', password='password123', role='delivery'
        )
        self.driver = DeliveryProfile.objects.create(
            user=self.driver_user, shelter=self.shelter, vehicle_number='KA-01-CV-9999', is_available=True
        )

        # Pet
        self.pet = Pet.objects.create(
            shelter=self.shelter, name='ConvergencePet', species='dog', breed='Indie', age_months=12, gender='male', status='available'
        )

    def test_approve_application_transitions_pet_to_pending(self):
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='1234567890', status='submitted'
        )
        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/applications/{app.public_id}/approve/', {'notes': 'Approved'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.pet.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(app.status, 'approved')
        self.assertEqual(self.pet.status, 'pending')

    def test_competing_applications_auto_rejected_on_approval(self):
        app1 = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='1111111111', status='submitted'
        )
        app2 = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_2, living_situation='Apartment', contact_phone='2222222222', status='reviewing'
        )

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/applications/{app1.public_id}/approve/', {'notes': 'Approve 1'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        app1.refresh_from_db()
        app2.refresh_from_db()
        self.assertEqual(app1.status, 'approved')
        self.assertEqual(app2.status, 'rejected')
        self.assertIn('adopted by another applicant', app2.application_notes)

    def test_reject_approved_application_restores_pet_to_available(self):
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='1234567890', status='submitted'
        )
        self.client.force_authenticate(user=self.shelter_user)
        self.client.post(f'/api/v1/shelter/applications/{app.public_id}/approve/', {'notes': 'Approved'})

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')

        # Now shelter rejects the approved application
        res = self.client.post(f'/api/v1/shelter/applications/{app.public_id}/reject/', {'notes': 'Adopter backed out'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        app.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(app.status, 'rejected')
        self.assertEqual(self.pet.status, 'available')

    def test_customer_cancel_approved_application_restores_pet_to_available(self):
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='1234567890', status='submitted'
        )
        self.client.force_authenticate(user=self.shelter_user)
        self.client.post(f'/api/v1/shelter/applications/{app.public_id}/approve/', {'notes': 'Approved'})

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')

        # Adopter cancels approved application
        self.client.force_authenticate(user=self.adopter_1)
        cancel_url = reverse('customer-applications-cancel', kwargs={'public_id': app.public_id})
        res = self.client.post(cancel_url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        app.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(app.status, 'cancelled')
        self.assertEqual(self.pet.status, 'available')

    def test_reject_does_not_make_available_if_another_approved_exists(self):
        app1 = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved'
        )
        app2 = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_2, living_situation='Apt', contact_phone='222', status='approved'
        )
        self.pet.status = 'pending'
        self.pet.save(update_fields=['status'])

        # Reject app1
        app1.status = 'rejected'
        app1.save()

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')

        # Reject app2
        app2.status = 'rejected'
        app2.save()

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'available')

    def test_delivery_success_transitions_pet_to_adopted(self):
        from apps.logistics.models import PetDelivery
        from apps.logistics.services import issue_otp, verify_doorstep_service
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved',
            total_amount=Decimal('0.00'), payment_method='unset'
        )
        self.pet.status = 'pending'
        self.pet.save(update_fields=['status'])

        delivery = PetDelivery.objects.create(
            application=app, driver=self.driver, status='out_for_delivery'
        )
        raw_otp = issue_otp(delivery, 'doorstep')

        res = verify_doorstep_service(delivery.id, raw_otp)
        self.assertEqual(res.outcome.value, 'success')

        self.pet.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(self.pet.status, 'adopted')
        self.assertEqual(app.status, 'completed')

    def test_driver_failed_delivery_leaves_pet_pending_and_app_approved(self):
        from apps.logistics.models import PetDelivery
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved'
        )
        self.pet.status = 'pending'
        self.pet.save(update_fields=['status'])

        delivery = PetDelivery.objects.create(
            application=app, driver=self.driver, status='assigned'
        )

        self.client.force_authenticate(user=self.driver_user)
        res = self.client.patch(f'/api/v1/delivery/tasks/{delivery.public_id}/status/', {'status': 'failed'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.pet.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')
        self.assertEqual(app.status, 'approved')

    def test_shelter_cancelled_delivery_leaves_pet_pending_and_app_approved(self):
        from apps.logistics.models import PetDelivery
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved',
            payment_method='cod', total_amount=Decimal('100.00')
        )
        self.pet.status = 'pending'
        self.pet.save(update_fields=['status'])

        delivery = PetDelivery.objects.create(
            application=app, driver=self.driver, status='assigned', cash_collected=Decimal('0.00')
        )

        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/deliveries/{delivery.public_id}/cancel/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.pet.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending')
        self.assertEqual(app.status, 'approved')

    def test_adopter_cannot_cancel_approved_app_with_captured_online_payment(self):
        from apps.logistics.models import Payment, PaymentStatus
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved',
            payment_method='online', total_amount=Decimal('100.00')
        )
        Payment.objects.create(
            application=app, status=PaymentStatus.CAPTURED, payment_method='razorpay',
            amount=Decimal('100.00'), razorpay_payment_id='pay_online_captured_app'
        )
        self.client.force_authenticate(user=self.adopter_1)
        res = self.client.post(f'/api/v1/applications/{app.public_id}/cancel/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'refund_required')
        app.refresh_from_db()
        self.assertEqual(app.status, 'approved')

    def test_shelter_cannot_reject_approved_app_with_captured_online_payment(self):
        from apps.logistics.models import Payment, PaymentStatus
        app = AdoptionApplication.objects.create(
            pet=self.pet, applicant=self.adopter_1, living_situation='House', contact_phone='111', status='approved',
            payment_method='online', total_amount=Decimal('100.00')
        )
        Payment.objects.create(
            application=app, status=PaymentStatus.CAPTURED, payment_method='razorpay',
            amount=Decimal('100.00'), razorpay_payment_id='pay_online_captured_app2'
        )
        self.client.force_authenticate(user=self.shelter_user)
        res = self.client.post(f'/api/v1/shelter/applications/{app.public_id}/reject/', {'notes': 'No longer eligible'})
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data.get('code'), 'refund_required')
        app.refresh_from_db()
        self.assertEqual(app.status, 'approved')





