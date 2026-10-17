from rest_framework import status, generics, viewsets, views
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from django.db.models import Sum, Avg
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes, OpenApiResponse
from django.db import transaction
from .models import User, ShelterProfile, DeliveryProfile, Notification
from .permissions import IsAdminRole, IsShelterRole, IsVerifiedShelter
from apps.pets.models import Pet
from apps.logistics.models import PetDelivery, Payment
from apps.adoptions.models import AdoptionReview
from .serializers import (
    CustomTokenObtainPairSerializer, 
    UserRegistrationSerializer, 
    UserProfileSerializer,
    AdminShelterDetailSerializer,
    AdminPlatformStatsSerializer,
    NotificationSerializer,
    DriverCreateSerializer,
    LogoutSerializer,
)

class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Login endpoint returning access and refresh JWT tokens with custom embedded role claims.
    Throttled per IP via auth_login scope.
    """
    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'


class CustomTokenRefreshView(TokenRefreshView):
    """
    Token refresh endpoint generating new access token from refresh token.
    Throttled per IP via auth_refresh scope.
    """
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_refresh'


class LogoutView(views.APIView):
    """
    Logout endpoint blacklisting the provided refresh token.
    Logging out twice is harmless (idempotent).
    """
    permission_classes = [AllowAny]
    serializer_class = LogoutSerializer

    @extend_schema(
        summary="Logout and blacklist refresh token",
        description="Blacklists the provided refresh token. Logging out twice is harmless.",
        request=LogoutSerializer,
        responses={
            200: OpenApiResponse(description="Successfully logged out"),
            400: OpenApiResponse(description="Missing or invalid token")
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = LogoutSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        refresh_token = serializer.validated_data['refresh']
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError as e:
            if 'blacklisted' in str(e).lower():
                return Response({
                    "message": "Successfully logged out (token already blacklisted)."
                }, status=status.HTTP_200_OK)
            return Response({
                "error": str(e),
                "code": "invalid_token"
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({
                "error": "Invalid token.",
                "code": "invalid_token"
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message": "Successfully logged out."
        }, status=status.HTTP_200_OK)


class RegisterView(generics.CreateAPIView):
    """
    User registration endpoint open to all applicants.
    Throttled per IP via auth_register scope.
    """
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_register'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        response_serializer = UserProfileSerializer(user)
        return Response(
            {
                "message": "User registered successfully",
                "user": response_serializer.data
            },
            status=status.HTTP_201_CREATED
        )


class UserProfileView(generics.RetrieveAPIView):
    """
    Endpoint returning authenticated user profile and identity.
    """
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class ShelterDriverCreateView(views.APIView):
    """
    Dedicated Fleets: Delivery partners belong strictly to a single shelter.
    Drivers cannot self-register; they are created by their shelter.
    POST /api/auth/drivers/create/
    """
    permission_classes = [IsAuthenticated, IsVerifiedShelter]

    @extend_schema(
        summary="Create a new delivery driver for shelter fleet",
        request=DriverCreateSerializer,
        responses={201: UserProfileSerializer}
    )
    def post(self, request):
        shelter = getattr(request.user, 'shelter_profile', None)
        if not shelter:
            if request.user.role == 'admin' or request.user.is_superuser or request.user.is_staff:
                shelter_id = request.data.get('shelter_id')
                if shelter_id:
                    shelter = ShelterProfile.objects.filter(id=shelter_id).first()
            if not shelter:
                return Response(
                    {"detail": "Authenticated user is not associated with a shelter profile."},
                    status=status.HTTP_403_FORBIDDEN
                )

        serializer = DriverCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        with transaction.atomic():
            driver_user = User.objects.create(
                username=data['username'],
                email=data['email'],
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
                phone=data.get('phone', ''),
                address=data.get('address', ''),
                role='delivery'
            )
            driver_user.set_password(data['password'])
            driver_user.save()

            delivery_profile = DeliveryProfile.objects.create(
                user=driver_user,
                shelter=shelter,
                vehicle_type=data.get('vehicle_type', 'car'),
                vehicle_number=data['vehicle_number'],
                is_available=True
            )

        response_serializer = UserProfileSerializer(driver_user)
        return Response({
            "message": "Delivery driver created successfully and assigned to your shelter fleet.",
            "driver": response_serializer.data
        }, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(summary="List all shelters for admin oversight"),
    retrieve=extend_schema(summary="Retrieve full shelter detail including verification and inventory"),
    verify=extend_schema(summary="Approve and verify a shelter"),
    suspend=extend_schema(summary="Suspend a shelter profile"),
)
class AdminShelterVerificationViewSet(viewsets.ModelViewSet):
    """
    Admin ViewSet for oversight, verification, and suspension of shelter profiles.
    """
    queryset = ShelterProfile.objects.select_related('user').all().order_by('-created_at')
    serializer_class = AdminShelterDetailSerializer
    permission_classes = [IsAuthenticated, IsAdminRole]
    lookup_field = 'public_id'

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs[lookup_url_kwarg]

        obj = None
        try:
            obj = queryset.filter(public_id=lookup_value).first()
        except (ValueError, Exception):
            pass
        if not obj:
            try:
                obj = queryset.filter(user__public_id=lookup_value).first()
            except (ValueError, Exception):
                pass
        if not obj and str(lookup_value).isdigit():
            obj = queryset.filter(id=int(lookup_value)).first()

        if not obj:
            raise NotFound("Shelter profile not found.")

        self.check_object_permissions(self.request, obj)
        return obj

    @action(detail=True, methods=['post'], url_path='verify')
    def verify(self, request, *args, **kwargs):
        shelter = self.get_object()
        shelter.is_verified = True
        shelter.is_active = True
        shelter.save(update_fields=['is_verified', 'is_active'])
        serializer = self.get_serializer(shelter)
        return Response({
            "message": f"Shelter '{shelter.organization_name}' verified successfully.",
            "shelter": serializer.data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='suspend')
    def suspend(self, request, *args, **kwargs):
        shelter = self.get_object()
        shelter.is_verified = False
        shelter.is_active = False
        shelter.save(update_fields=['is_verified', 'is_active'])
        serializer = self.get_serializer(shelter)
        return Response({
            "message": f"Shelter '{shelter.organization_name}' has been suspended.",
            "shelter": serializer.data
        }, status=status.HTTP_200_OK)


@extend_schema(
    summary="Platform Admin Dashboard Statistics",
    description="Returns aggregate metrics across users, pet catalog, active delivery fleet, revenue, and satisfaction ratings.",
    responses={200: AdminPlatformStatsSerializer}
)
class AdminPlatformDashboardView(views.APIView):
    """
    GET /api/v1/admin/dashboard/stats/
    Provides platform-wide aggregated analytics for platform administrators.
    """
    permission_classes = [IsAuthenticated, IsAdminRole]
    serializer_class = AdminPlatformStatsSerializer

    def get(self, request):
        # 1. Total users breakdown
        total_users_count = User.objects.count()
        admin_count = User.objects.filter(role='admin').count()
        shelter_count = User.objects.filter(role='shelter').count()
        delivery_count = User.objects.filter(role='delivery').count()
        customer_count = User.objects.filter(role='customer').count()

        # 2. Total pets breakdown
        total_pets_count = Pet.objects.count()
        available_pets = Pet.objects.filter(status='available').count()
        adopted_pets = Pet.objects.filter(status='adopted').count()
        pending_pets = Pet.objects.filter(status='pending').count()
        in_transit_pets = Pet.objects.filter(status='in_transit').count()
        if in_transit_pets == 0:
            in_transit_pets = PetDelivery.objects.filter(status__in=['out_for_delivery', 'in_transit']).count()

        # 3. Active deliveries
        active_deliveries_count = PetDelivery.objects.filter(
            status__in=['assigned', 'out_for_delivery', 'in_transit']
        ).count()

        # 4. Total revenue INR (sum of captured payments amount_in_paise / 100.0)
        captured_payments = Payment.objects.filter(status='paid')
        total_paise = captured_payments.aggregate(total=Sum('amount_in_paise'))['total'] or 0
        total_revenue_inr = round(float(total_paise) / 100.0, 2)

        # 5. Average ratings
        avg_shelter = AdoptionReview.objects.aggregate(avg=Avg('shelter_rating'))['avg']
        avg_shelter_rating = round(float(avg_shelter), 2) if avg_shelter is not None else 0.0

        avg_driver = AdoptionReview.objects.aggregate(avg=Avg('driver_rating'))['avg']
        avg_driver_rating = round(float(avg_driver), 2) if avg_driver is not None else 0.0

        stats_data = {
            "total_users": {
                "total": total_users_count,
                "admin": admin_count,
                "shelter": shelter_count,
                "delivery": delivery_count,
                "customer": customer_count,
            },
            "total_pets": {
                "total": total_pets_count,
                "available": available_pets,
                "adopted": adopted_pets,
                "in_transit": in_transit_pets,
                "pending": pending_pets,
            },
            "active_deliveries": active_deliveries_count,
            "total_revenue_inr": total_revenue_inr,
            "average_shelter_rating": avg_shelter_rating,
            "average_driver_rating": avg_driver_rating,
        }

        serializer = AdminPlatformStatsSerializer(stats_data)
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationListView(views.APIView):
    """
    List notifications for the authenticated user, optionally filtered by ?unread=true.
    GET /api/v1/notifications/
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List user notifications",
        description="List all notifications for the authenticated user, optionally filtered by ?unread=true.",
        parameters=[
            OpenApiParameter(name='unread', type=OpenApiTypes.BOOL, location=OpenApiParameter.QUERY, description='Filter unread notifications only')
        ],
        responses={200: NotificationSerializer(many=True)}
    )
    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user)
        unread = request.query_params.get('unread')
        if unread is not None and unread.lower() in ('true', '1', 'yes'):
            qs = qs.filter(is_read=False)
        serializer = NotificationSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationMarkReadView(views.APIView):
    """
    Mark a single notification as read.
    POST /api/v1/notifications/<uuid:public_id>/read/
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Mark notification as read",
        description="Mark a specific notification as read by public_id.",
        request=None,
        responses={200: NotificationSerializer}
    )
    def post(self, request, public_id):
        notification = Notification.objects.filter(recipient=request.user, public_id=public_id).first()
        if not notification:
            raise NotFound("Notification not found.")
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        serializer = NotificationSerializer(notification)
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationMarkAllReadView(views.APIView):
    """
    Mark all notifications for the authenticated user as read.
    POST /api/v1/notifications/read-all/
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Mark all notifications as read",
        description="Mark all notifications for the authenticated user as read.",
        request=None,
        responses={200: OpenApiTypes.OBJECT}
    )
    def post(self, request):
        updated_count = Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({
            'message': f'{updated_count} notification(s) marked as read.',
            'updated_count': updated_count
        }, status=status.HTTP_200_OK)


