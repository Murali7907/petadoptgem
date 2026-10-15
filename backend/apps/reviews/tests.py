from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from apps.accounts.models import User, ShelterProfile, DeliveryProfile
from apps.pets.models import Pet
from apps.adoptions.models import AdoptionApplication, AdoptionReview
from apps.logistics.models import PetDelivery
from apps.logistics.otp import issue_otp


class AdoptionReviewAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Users
        self.customer = User.objects.create_user(
            username='review_customer',
            email='review_customer@example.com',
            password='testpassword123',
            role='customer'
        )
        self.other_customer = User.objects.create_user(
            username='other_customer',
            email='other_customer@example.com',
            password='testpassword123',
            role='customer'
        )
        self.shelter_user = User.objects.create_user(
            username='review_shelter',
            email='review_shelter@example.com',
            password='testpassword123',
            role='shelter'
        )
        self.shelter_profile = ShelterProfile.objects.create(
            user=self.shelter_user,
            organization_name='Review Shelter Org',
            pickup_address='123 Shelter St, Bangalore',
            city='Bangalore',
            is_verified=True
        )

        self.driver_user = User.objects.create_user(
            username='review_driver',
            email='review_driver@example.com',
            password='testpassword123',
            role='delivery'
        )
        self.delivery_profile = DeliveryProfile.objects.create(
            user=self.driver_user,
            shelter=self.shelter_profile,
            vehicle_number='KA-01-REV-1234'
        )

        # Pet
        self.pet = Pet.objects.create(
            name='Buddy',
            species='dog',
            breed='Golden Retriever',
            age_months=18,
            shelter=self.shelter_profile,
            adoption_fee=500.00,
            status='adopted'
        )

        # Application
        self.application = AdoptionApplication.objects.create(
            pet=self.pet,
            applicant=self.customer,
            status='completed',
            contact_phone='9876543210',
            living_situation='apartment',
            has_fenced_yard=False
        )

        # Delivery
        self.delivery = PetDelivery.objects.create(
            application=self.application,
            driver=self.delivery_profile,
            status='delivered',
            delivered_at=timezone.now()
        )
        issue_otp(self.delivery, 'pickup')
        issue_otp(self.delivery, 'doorstep')

    def test_submit_review_success(self):
        self.client.force_authenticate(user=self.customer)
        payload = {
            'application_id': str(self.application.public_id),
            'shelter_rating': 5,
            'shelter_review': 'Amazing shelter care and clean facilities!',
            'driver_rating': 4,
            'driver_review': 'Timely and careful pet handling.'
        }
        response = self.client.post('/api/reviews/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(AdoptionReview.objects.filter(application=self.application).count(), 1)
        review = AdoptionReview.objects.get(application=self.application)
        self.assertEqual(review.shelter_rating, 5)
        self.assertEqual(review.driver_rating, 4)
        self.assertEqual(review.reviewer, self.customer)

    def test_submit_review_duplicate_rejected(self):
        self.client.force_authenticate(user=self.customer)
        AdoptionReview.objects.create(
            application=self.application,
            reviewer=self.customer,
            shelter_rating=5,
            shelter_review='Great',
            driver_rating=5,
            driver_review='Fast',
            rating=5
        )
        payload = {
            'application_id': str(self.application.public_id),
            'shelter_rating': 4,
            'driver_rating': 4
        }
        response = self.client.post('/api/reviews/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_review_unauthorized_user(self):
        self.client.force_authenticate(user=self.other_customer)
        payload = {
            'application_id': str(self.application.public_id),
            'shelter_rating': 5,
            'driver_rating': 5
        }
        response = self.client.post('/api/reviews/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_submit_review_delivery_not_completed(self):
        self.delivery.status = 'out_for_delivery'
        self.delivery.save()

        self.client.force_authenticate(user=self.customer)
        payload = {
            'application_id': str(self.application.public_id),
            'shelter_rating': 5,
            'driver_rating': 5
        }
        response = self.client.post('/api/reviews/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
