import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.shortcuts import get_object_or_404
from django.db import transaction, models, DatabaseError, IntegrityError, InternalError
from rest_framework import viewsets, mixins, status, permissions
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from drf_spectacular.utils import extend_schema
from .models import AdoptionApplication, AdoptionReview
from .permissions import IsCustomer, IsApplicationOwner, IsShelterForApplication, IsDeliveredApplicationCustomer
from .serializers import (
    ApplicationCreateSerializer,
    CustomerApplicationSerializer,
    ShelterApplicationSerializer,
    ApplicationDecisionSerializer,
    AdoptionReviewCreateSerializer,
    AdoptionReviewDetailSerializer
)
from apps.pets.models import Pet
from apps.pets.permissions import IsShelterOwner
from apps.accounts.permissions import IsVerifiedShelter

class CustomerApplicationViewSet(mixins.ListModelMixin, 
                                mixins.RetrieveModelMixin, 
                                mixins.CreateModelMixin, 
                                mixins.UpdateModelMixin,
                                viewsets.GenericViewSet):
    """
    ViewSet for Customer users to view their application history, submit new applications,
    and cancel pending applications.
    """
    lookup_field = 'public_id'
    permission_classes = [permissions.IsAuthenticated, IsCustomer]
    ordering = ['-submitted_at', '-id']

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return AdoptionApplication.objects.none()
        return AdoptionApplication.objects.filter(applicant=user).select_related('pet', 'pet__shelter').order_by('-submitted_at', '-id')

    def get_serializer_class(self):
        if self.action == 'create':
            return ApplicationCreateSerializer
        return CustomerApplicationSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        application = serializer.save()
        response_serializer = CustomerApplicationSerializer(application, context={'request': request})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, public_id=None):
        application = self.get_object()

        if application.status not in ['submitted', 'reviewing']:
            raise ValidationError(f"Cannot cancel application with status '{application.get_status_display()}'.")

        application.status = 'cancelled'
        application.save()
        serializer = CustomerApplicationSerializer(application, context={'request': request})
        return Response({
            "message": "Application cancelled successfully.",
            "application": serializer.data
        }, status=status.HTTP_200_OK)


class ShelterApplicationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Shelter partners to review, approve, or reject adoption applications
    submitted for their listed pets.
    """
    lookup_field = 'public_id'
    permission_classes = [permissions.IsAuthenticated, IsShelterOwner]
    serializer_class = ShelterApplicationSerializer
    ordering = ['-submitted_at', '-id']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [permissions.IsAuthenticated(), IsShelterOwner()]
        return [permissions.IsAuthenticated(), IsVerifiedShelter(), IsShelterOwner()]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated or not hasattr(user, 'shelter_profile'):
            return AdoptionApplication.objects.none()

        qs = AdoptionApplication.objects.filter(pet__shelter=user.shelter_profile).select_related('applicant', 'pet', 'pet__shelter').order_by('-submitted_at', '-id')

        # Filter by status query param
        app_status = self.request.query_params.get('status')
        if app_status:
            qs = qs.filter(status=app_status)

        # Filter by pet_public_id query param
        pet_public_id = self.request.query_params.get('pet_public_id')
        if pet_public_id:
            qs = qs.filter(pet__public_id=pet_public_id)

        return qs

    @action(detail=True, methods=['post'], url_path='review')
    def review(self, request, public_id=None):
        application = self.get_object()

        if application.status != 'submitted':
            raise ValidationError(f"Only applications with status 'submitted' can be moved to reviewing (current status: '{application.get_status_display()}').")

        application.status = 'reviewing'
        application.save()
        serializer = self.get_serializer(application)
        return Response({
            "message": "Application is now under review.",
            "application": serializer.data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, public_id=None):
        application = self.get_object()
        decision_serializer = ApplicationDecisionSerializer(data=request.data)
        decision_serializer.is_valid(raise_exception=True)
        notes = decision_serializer.validated_data.get('notes', '')

        with transaction.atomic():
            # Lock target pet row to prevent race conditions
            pet = Pet.objects.select_for_update().get(id=application.pet_id)

            if pet.status != 'available':
                raise ValidationError(f"Pet '{pet.name}' is no longer available for adoption (status: '{pet.get_status_display()}').")

            # Approve this application
            application.status = 'approved'
            if notes:
                application.application_notes = f"{application.application_notes}\n[Shelter Note]: {notes}".strip()
            application.save()

            # Update pet status to pending
            pet.status = 'pending'
            pet.save()

            # Auto-reject competing active applications for this pet
            competing_apps = AdoptionApplication.objects.filter(
                pet=pet,
                status__in=['submitted', 'reviewing']
            ).exclude(id=application.id)

            rejection_reason = "Pet has been adopted by another applicant."
            for comp_app in competing_apps:
                comp_app.status = 'rejected'
                comp_app.application_notes = f"{comp_app.application_notes}\n[System Note]: {rejection_reason}".strip()
                comp_app.save()

        serializer = self.get_serializer(application)
        return Response({
            "message": f"Application approved for {pet.name}. Competing applications have been auto-rejected.",
            "application": serializer.data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, public_id=None):
        application = self.get_object()
        decision_serializer = ApplicationDecisionSerializer(data=request.data)
        decision_serializer.is_valid(raise_exception=True)
        notes = decision_serializer.validated_data.get('notes', '')

        if application.status in ['approved', 'completed', 'cancelled']:
            raise ValidationError(f"Cannot reject an application with status '{application.get_status_display()}'.")

        application.status = 'rejected'
        if notes:
            application.application_notes = f"{application.application_notes}\n[Rejection Reason]: {notes}".strip()
        application.save()

        serializer = self.get_serializer(application)
        return Response({
            "message": "Application rejected.",
            "application": serializer.data
        }, status=status.HTTP_200_OK)


class ApplicationReviewCreateView(APIView):
    """
    POST /api/v1/adoptions/applications/<uuid:public_id>/review/
    Allows customer applicant to submit a dual review once delivery is completed.
    """
    serializer_class = AdoptionReviewCreateSerializer
    permission_classes = [permissions.IsAuthenticated, IsDeliveredApplicationCustomer]

    def post(self, request, public_id):
        application = get_object_or_404(AdoptionApplication, public_id=public_id)
        self.check_object_permissions(request, application)

        if application.applicant_id != request.user.id:
            raise PermissionDenied("You are not authorized to review this application.")

        # Check delivery status
        delivery = getattr(application, 'delivery', None)
        if not delivery or delivery.status != 'delivered':
            return Response(
                {"detail": "Reviews can only be submitted for completed deliveries."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prevent duplicate reviews
        if AdoptionReview.objects.filter(application=application).exists():
            return Response(
                {"detail": "A review for this application has already been submitted."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = AdoptionReviewCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                review = AdoptionReview.objects.create(
                    application=application,
                    reviewer=request.user,
                    shelter_rating=serializer.validated_data['shelter_rating'],
                    shelter_review=serializer.validated_data.get('shelter_review', ''),
                    driver_rating=serializer.validated_data['driver_rating'],
                    driver_review=serializer.validated_data.get('driver_review', ''),
                    rating=serializer.validated_data['shelter_rating'],
                    comment=serializer.validated_data.get('shelter_review', '')
                )
        except (DatabaseError, IntegrityError, InternalError) as e:
            return Response(
                {"detail": "Reviews can only be submitted for completed deliveries."},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(AdoptionReviewDetailSerializer(review).data, status=status.HTTP_201_CREATED)


class ShelterReviewListView(APIView):
    """
    GET /api/v1/shelter/reviews/
    Lists all reviews for the authenticated shelter, including average rating.
    """
    serializer_class = AdoptionReviewDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'shelter_profile') or not request.user.shelter_profile:
            return Response({"detail": "User is not associated with a shelter profile."}, status=status.HTTP_403_FORBIDDEN)

        shelter = request.user.shelter_profile
        reviews = AdoptionReview.objects.filter(application__pet__shelter=shelter).order_by('-created_at', '-id')
        avg_score = reviews.aggregate(models.Avg('shelter_rating'))['shelter_rating__avg']

        return Response({
            "average_rating": round(float(avg_score), 2) if avg_score is not None else 0.0,
            "total_reviews": reviews.count(),
            "reviews": AdoptionReviewDetailSerializer(reviews, many=True).data
        }, status=status.HTTP_200_OK)


class DeliveryReviewListView(APIView):
    """
    GET /api/v1/delivery/reviews/
    Lists all reviews for the authenticated driver, including average rating.
    """
    serializer_class = AdoptionReviewDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'delivery_profile') or not request.user.delivery_profile:
            return Response({"detail": "User is not associated with a delivery profile."}, status=status.HTTP_403_FORBIDDEN)

        driver = request.user.delivery_profile
        reviews = AdoptionReview.objects.filter(application__delivery__driver=driver).order_by('-created_at', '-id')
        avg_score = reviews.aggregate(models.Avg('driver_rating'))['driver_rating__avg']

        return Response({
            "average_rating": round(float(avg_score), 2) if avg_score is not None else 0.0,
            "total_reviews": reviews.count(),
            "reviews": AdoptionReviewDetailSerializer(reviews, many=True).data
        }, status=status.HTTP_200_OK)


def resolve_application(identifier):
    app = None
    try:
        import uuid
        val = uuid.UUID(str(identifier))
        app = AdoptionApplication.objects.filter(public_id=val).first()
    except (ValueError, TypeError, AttributeError):
        pass
    if not app and str(identifier).isdigit():
        app = AdoptionApplication.objects.filter(id=int(identifier)).first()
    if not app:
        app = AdoptionApplication.objects.filter(public_id=identifier).first()
    if not app:
        raise NotFound("Adoption application not found.")
    return app


@extend_schema(
    request=ApplicationCreateSerializer,
    responses={201: CustomerApplicationSerializer}
)
class CustomerApplicationApplyView(APIView):
    """
    POST /api/adoptions/apply/
    Customer application submission with duplicate checks and distance calculation.
    """
    serializer_class = ApplicationCreateSerializer
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def post(self, request):
        serializer = ApplicationCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        application = serializer.save()
        return Response(CustomerApplicationSerializer(application, context={'request': request}).data, status=status.HTTP_201_CREATED)


@extend_schema(
    responses={200: CustomerApplicationSerializer(many=True)}
)
class CustomerMyApplicationsView(APIView):
    """
    GET /api/adoptions/my-applications/
    Returns authenticated customer's application history.
    """
    serializer_class = CustomerApplicationSerializer
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get(self, request):
        applications = AdoptionApplication.objects.filter(applicant=request.user).select_related('pet', 'pet__shelter').order_by('-submitted_at', '-id')
        return Response(CustomerApplicationSerializer(applications, many=True, context={'request': request}).data, status=status.HTTP_200_OK)


@extend_schema(
    responses={200: ShelterApplicationSerializer(many=True)}
)
class ShelterApplicationsListView(APIView):
    """
    GET /api/adoptions/shelter-applications/
    Returns applications for pets belonging to the shelter.
    """
    serializer_class = ShelterApplicationSerializer
    permission_classes = [permissions.IsAuthenticated, IsShelterOwner]

    def get(self, request):
        if not hasattr(request.user, 'shelter_profile') or not request.user.shelter_profile:
            return Response({"detail": "User is not associated with a shelter profile."}, status=status.HTTP_403_FORBIDDEN)

        qs = AdoptionApplication.objects.filter(pet__shelter=request.user.shelter_profile).select_related('applicant', 'pet', 'pet__shelter').order_by('-submitted_at', '-id')
        app_status = request.query_params.get('status')
        if app_status:
            qs = qs.filter(status=app_status)
        pet_id = request.query_params.get('pet_id') or request.query_params.get('pet_public_id')
        if pet_id:
            try:
                import uuid
                val = uuid.UUID(str(pet_id))
                qs = qs.filter(pet__public_id=val)
            except (ValueError, TypeError):
                if str(pet_id).isdigit():
                    qs = qs.filter(pet_id=int(pet_id))
                else:
                    qs = qs.filter(pet__public_id=pet_id)
        return Response(ShelterApplicationSerializer(qs, many=True).data, status=status.HTTP_200_OK)


@extend_schema(
    request=ApplicationDecisionSerializer,
    responses={200: ShelterApplicationSerializer}
)
class AdoptionStatusUpdateView(APIView):
    """
    PATCH /api/adoptions/<id>/status/
    Shelter approve/reject action.
    """
    serializer_class = ApplicationDecisionSerializer
    permission_classes = [permissions.IsAuthenticated, IsVerifiedShelter, IsShelterOwner]

    def patch(self, request, id):
        application = resolve_application(id)
        if not hasattr(request.user, 'shelter_profile') or application.pet.shelter_id != request.user.shelter_profile.id:
            raise PermissionDenied("You can only modify applications submitted to your shelter.")

        new_status = request.data.get('status')
        notes = request.data.get('notes') or request.data.get('application_notes') or ''

        if new_status not in ['approved', 'rejected', 'reviewing']:
            raise ValidationError({"status": "Status must be 'approved', 'rejected', or 'reviewing'."})

        if new_status == 'approved':
            with transaction.atomic():
                pet = Pet.objects.select_for_update().get(id=application.pet_id)
                if pet.status != 'available':
                    raise ValidationError(f"Pet '{pet.name}' is no longer available for adoption.")

                application.status = 'approved'
                if notes:
                    application.application_notes = f"{application.application_notes}\n[Shelter Note]: {notes}".strip()
                application.save()

                pet.status = 'pending'
                pet.save(update_fields=['status'])

                # Auto-reject competing active applications
                competing_apps = AdoptionApplication.objects.filter(
                    pet=pet,
                    status__in=['submitted', 'reviewing']
                ).exclude(id=application.id)
                for comp in competing_apps:
                    comp.status = 'rejected'
                    comp.application_notes = f"{comp.application_notes}\n[System Note]: Pet has been adopted by another applicant.".strip()
                    comp.save()

        elif new_status == 'rejected':
            application.status = 'rejected'
            if notes:
                application.application_notes = f"{application.application_notes}\n[Rejection Reason]: {notes}".strip()
            application.save()

        elif new_status == 'reviewing':
            application.status = 'reviewing'
            if notes:
                application.application_notes = f"{application.application_notes}\n[Shelter Note]: {notes}".strip()
            application.save()

        return Response({
            "message": f"Application status updated to '{new_status}'.",
            "application": ShelterApplicationSerializer(application).data
        }, status=status.HTTP_200_OK)


AdoptionApplicationDetailView = AdoptionStatusUpdateView



