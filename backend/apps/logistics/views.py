import hmac
import logging
import re
import sys
from decimal import Decimal
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError, NotFound
from rest_framework.throttling import ScopedRateThrottle

from drf_spectacular.utils import extend_schema

from .models import PetDelivery, DeliveryLocationPing, Payment
from .permissions import IsShelterForDelivery, IsAssignedDeliveryDriver, IsDeliveryParticipant
from .serializers import (
    DeliveryAssignSerializer,
    DeliveryAssignResponseSerializer,
    DeliveryReissueOTPRequestSerializer,
    DeliveryReissueOTPResponseSerializer,
    DeliveryStatusUpdateRequestSerializer,
    DeliveryPickupResponseSerializer,
    DeliveryDoorstepResponseSerializer,
    DeliveryPingResponseSerializer,
    DeliveryTrackingResponseSerializer,
    DeliveryDriverTaskSerializer,
    DeliveryCustomerSerializer,
    ShelterDeliverySerializer,
    DriverListSerializer,
    LocationPingSerializer,
    OTPVerificationSerializer,
    PaymentOrderCreateSerializer,
    PaymentVerifySerializer,
    PaymentLedgerSerializer
)
from .otp import check_otp, issue_otp
from .services import (
    assign_delivery_task,
    verify_pickup_service,
    verify_doorstep_service,
    OTPVerificationOutcome,
    verify_pickup_otp,
    verify_doorstep_otp,
    dispatch_doorstep_otp,
    reissue_delivery_otp,
    send_delivery_completed_notice,
    resolve_delivery_payment_status,
)
from .payment_services import (
    create_razorpay_order,
    verify_razorpay_payment,
    process_razorpay_webhook
)
from apps.accounts.models import DeliveryProfile, ShelterProfile
from apps.adoptions.models import AdoptionApplication

logger = logging.getLogger(__name__)







class ShelterDeliveryViewSet(viewsets.GenericViewSet):
    """
    Endpoints for shelter admins to assign and manage deliveries.
    - POST /api/v1/shelter/deliveries/assign/
    - POST /api/v1/shelter/deliveries/<id>/reissue-otp/
    - GET  /api/v1/shelter/deliveries/drivers/
    - GET  /api/v1/shelter/deliveries/
    """
    serializer_class = ShelterDeliverySerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'public_id'

    def get_shelter_profile(self):
        user = self.request.user
        if not hasattr(user, 'shelter_profile') or not user.shelter_profile:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("User is not associated with a shelter profile.")
        return user.shelter_profile

    def get_object(self):
        lookup_val = self.kwargs.get(self.lookup_field) or self.kwargs.get('id') or self.kwargs.get('pk')
        import uuid
        obj = None
        if lookup_val:
            try:
                val = uuid.UUID(str(lookup_val))
                obj = PetDelivery.objects.filter(public_id=val).first()
            except (ValueError, TypeError, AttributeError):
                pass
            if not obj and str(lookup_val).isdigit():
                obj = PetDelivery.objects.filter(id=int(lookup_val)).first()
            if not obj:
                obj = PetDelivery.objects.filter(public_id=lookup_val).first()
        if not obj:
            raise NotFound({"detail": f"Delivery with ID '{lookup_val}' not found."})
        return obj

    @extend_schema(responses={200: ShelterDeliverySerializer(many=True)})
    def list(self, request):
        shelter = self.get_shelter_profile()
        deliveries = PetDelivery.objects.filter(application__pet__shelter=shelter).order_by('-created_at')
        serializer = ShelterDeliverySerializer(deliveries, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(request=DeliveryAssignSerializer, responses={201: DeliveryAssignResponseSerializer})
    @action(detail=False, methods=['post'], url_path='assign')
    def assign(self, request):
        serializer = DeliveryAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        shelter = self.get_shelter_profile()
        app_ref = serializer.validated_data.get('application_ref') or serializer.validated_data.get('application_public_id') or serializer.validated_data.get('application_id')
        driver_ref = serializer.validated_data.get('driver_ref') or serializer.validated_data.get('driver_public_id') or serializer.validated_data.get('driver_id')
        payment_method = serializer.validated_data.get('payment_method', 'unset')

        import uuid
        application = None
        if app_ref:
            try:
                val = uuid.UUID(str(app_ref))
                application = AdoptionApplication.objects.filter(public_id=val).first()
            except (ValueError, TypeError, AttributeError):
                pass
            if not application and str(app_ref).isdigit():
                application = AdoptionApplication.objects.filter(id=int(app_ref)).first()
            if not application:
                application = AdoptionApplication.objects.filter(public_id=app_ref).first()
        if not application:
            raise NotFound({"detail": f"Application '{app_ref}' not found."})

        if application.pet.shelter_id != shelter.id:
            return Response({"detail": "Application belongs to a different shelter."}, status=status.HTTP_403_FORBIDDEN)

        driver = None
        if driver_ref:
            try:
                val = uuid.UUID(str(driver_ref))
                driver = DeliveryProfile.objects.filter(public_id=val).first()
            except (ValueError, TypeError, AttributeError):
                pass
            if not driver and str(driver_ref).isdigit():
                driver = DeliveryProfile.objects.filter(id=int(driver_ref)).first()
            if not driver:
                driver = DeliveryProfile.objects.filter(public_id=driver_ref).first()
            if not driver and str(driver_ref).isdigit():
                driver = DeliveryProfile.objects.filter(user_id=int(driver_ref)).first()
        if not driver:
            raise NotFound({"detail": f"Driver '{driver_ref}' not found."})

        if driver.shelter_id != shelter.id:
            return Response({"detail": "Driver is not registered under your shelter."}, status=status.HTTP_400_BAD_REQUEST)

        delivery = assign_delivery_task(application=application, driver=driver, payment_method=payment_method)
        response_serializer = ShelterDeliverySerializer(delivery)
        data = dict(response_serializer.data)
        if hasattr(delivery, 'raw_pickup_otp') and delivery.raw_pickup_otp:
            data['pickup_otp'] = delivery.raw_pickup_otp
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(request=DeliveryReissueOTPRequestSerializer, responses={200: DeliveryReissueOTPResponseSerializer})
    @action(detail=True, methods=['post'], url_path='reissue-otp')
    def reissue_otp(self, request, *args, **kwargs):
        delivery = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser):
            shelter = self.get_shelter_profile()
            if not delivery.application or not delivery.application.pet or delivery.application.pet.shelter_id != shelter.id:
                return Response({"detail": "Delivery belongs to a different shelter."}, status=status.HTTP_403_FORBIDDEN)

        purpose = request.data.get('purpose')
        if not purpose or purpose not in ['pickup', 'doorstep']:
            return Response({"detail": "Purpose must be 'pickup' or 'doorstep'."}, status=status.HTTP_400_BAD_REQUEST)

        from rest_framework.exceptions import APIException
        try:
            result = reissue_delivery_otp(delivery, purpose)
            return Response(result, status=status.HTTP_200_OK)
        except APIException as e:
            return Response({"detail": e.detail}, status=e.status_code)

    @extend_schema(responses={200: DriverListSerializer(many=True)})
    @action(detail=False, methods=['get'], url_path='drivers')
    def drivers(self, request):
        shelter = self.get_shelter_profile()
        drivers = DeliveryProfile.objects.filter(shelter=shelter).order_by('id')
        serializer = DriverListSerializer(drivers, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class DriverDeliveryViewSet(viewsets.GenericViewSet):
    """
    Endpoints for delivery drivers and tracking.
    - GET  /api/v1/delivery/tasks/active/ or GET /api/deliveries/driver-active/
    - POST /api/v1/delivery/tasks/<id>/pickup/
    - POST /api/v1/delivery/tasks/<id>/verify-doorstep/ or POST /api/deliveries/<id>/verify-otp/
    - POST /api/v1/delivery/tasks/<id>/ping/ or POST /api/deliveries/<id>/ping/
    - GET  /api/v1/delivery/tasks/<id>/tracking/ or GET /api/deliveries/<id>/track/
    """
    serializer_class = DeliveryDriverTaskSerializer
    permission_classes = [IsAuthenticated]
    queryset = PetDelivery.objects.all()
    lookup_field = 'public_id'

    def get_object(self):
        lookup_val = self.kwargs.get(self.lookup_field) or self.kwargs.get('id') or self.kwargs.get('pk')
        import uuid
        obj = None
        if lookup_val:
            try:
                val = uuid.UUID(str(lookup_val))
                obj = PetDelivery.objects.filter(public_id=val).first()
            except (ValueError, TypeError, AttributeError):
                pass
            if not obj and str(lookup_val).isdigit():
                obj = PetDelivery.objects.filter(id=int(lookup_val)).first()
            if not obj:
                obj = PetDelivery.objects.filter(public_id=lookup_val).first()
        if not obj:
            raise NotFound({"detail": f"Delivery with ID '{lookup_val}' not found."})
        self.check_object_permissions(self.request, obj)
        return obj

    def get_permissions(self):
        if self.action in ['pickup', 'verify_doorstep', 'update_status', 'ping']:
            return [IsAuthenticated(), IsAssignedDeliveryDriver()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action in ['pickup', 'verify_doorstep', 'verify_otp']:
            self.throttle_scope = 'otp_verify'
            return [ScopedRateThrottle()]
        return super().get_throttles()


    @extend_schema(responses={200: DeliveryDriverTaskSerializer})
    @action(detail=False, methods=['get'], url_path='active')
    def active(self, request):
        if not hasattr(request.user, 'delivery_profile'):
            return Response({"detail": "User is not registered as a delivery driver."}, status=status.HTTP_403_FORBIDDEN)

        driver_profile = request.user.delivery_profile
        delivery = PetDelivery.objects.filter(
            driver=driver_profile,
            status__in=['assigned', 'out_for_delivery']
        ).first()

        if not delivery:
            return Response({"detail": "No active delivery task found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = DeliveryDriverTaskSerializer(delivery)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(responses={200: DeliveryDriverTaskSerializer})
    @action(detail=False, methods=['get'], url_path='driver-active')
    def driver_active(self, request):
        return self.active(request)

    @extend_schema(request=OTPVerificationSerializer, responses={200: DeliveryPickupResponseSerializer})
    @action(detail=True, methods=['post'], url_path='pickup',
            permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def pickup(self, request, *args, **kwargs):
        raw_otp = request.data.get('otp') or request.data.get('code') or request.data.get('secret_otp')
        if not raw_otp:
            return Response({"detail": "OTP is required."}, status=status.HTTP_400_BAD_REQUEST)

        val = str(raw_otp).strip()
        if not re.match(r'^\d{6}$', val):
            return Response({"detail": "OTP must be a 6-digit numeric string."}, status=status.HTTP_400_BAD_REQUEST)

        delivery_obj = self.get_object()
        result = verify_pickup_service(delivery_obj.id, val)

        if result.outcome == OTPVerificationOutcome.WRONG_STATE:
            return Response({"detail": result.detail}, status=status.HTTP_409_CONFLICT)
        elif result.outcome == OTPVerificationOutcome.LOCKED:
            return Response({"detail": result.detail}, status=423)
        elif result.outcome == OTPVerificationOutcome.EXPIRED:
            return Response({"detail": result.detail}, status=status.HTTP_410_GONE)
        elif result.outcome == OTPVerificationOutcome.INVALID:
            return Response({
                "detail": result.detail,
                "attempts_remaining": result.attempts_remaining
            }, status=status.HTTP_400_BAD_REQUEST)
        elif result.outcome == OTPVerificationOutcome.SUCCESS:
            updated_serializer = DeliveryDriverTaskSerializer(result.delivery)
            return Response({
                "message": "Pickup OTP verified successfully. Task is now out for delivery.",
                "delivery": updated_serializer.data
            }, status=status.HTTP_200_OK)

        return Response({"detail": result.detail or "Verification failed."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(request=DeliveryStatusUpdateRequestSerializer, responses={200: DeliveryDriverTaskSerializer})
    @action(detail=True, methods=['patch', 'post'], url_path='status', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def update_status(self, request, *args, **kwargs):
        delivery = self.get_object()
        raw_status = request.data.get('status')
        if not raw_status:
            return Response({"detail": "Status field is required."}, status=status.HTTP_400_BAD_REQUEST)

        new_status = str(raw_status).strip().lower()

        # Reject any status other than 'failed'
        if new_status != 'failed':
            return Response({
                "detail": "Invalid status transition. Drivers may only report status as 'failed'. Status 'out_for_delivery' requires pickup OTP verification, and status 'delivered' requires doorstep OTP verification."
            }, status=status.HTTP_400_BAD_REQUEST)

        if delivery.status not in ['assigned', 'out_for_delivery']:
            return Response({
                "detail": f"Cannot mark delivery as failed from '{delivery.status}' state."
            }, status=status.HTTP_409_CONFLICT)

        delivery.status = 'failed'
        delivery.save(update_fields=['status'])

        if delivery.driver:
            delivery.driver.is_available = True
            delivery.driver.save(update_fields=['is_available'])

        updated_serializer = DeliveryDriverTaskSerializer(delivery)
        return Response(updated_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(request=OTPVerificationSerializer, responses={200: DeliveryDoorstepResponseSerializer})
    @action(detail=True, methods=['post'], url_path='verify-doorstep',
            permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def verify_doorstep(self, request, *args, **kwargs):
        raw_otp = request.data.get('otp') or request.data.get('code') or request.data.get('secret_otp')
        if not raw_otp:
            return Response({"detail": "OTP is required."}, status=status.HTTP_400_BAD_REQUEST)

        val = str(raw_otp).strip()
        if not re.match(r'^\d{6}$', val):
            return Response({"detail": "OTP must be a 6-digit numeric string."}, status=status.HTTP_400_BAD_REQUEST)

        delivery_obj = self.get_object()
        result = verify_doorstep_service(delivery_obj.id, val)

        if result.outcome == OTPVerificationOutcome.ALREADY_COMPLETED:
            updated_serializer = DeliveryDriverTaskSerializer(result.delivery)
            return Response({
                "message": "Delivery already completed.",
                "delivery": updated_serializer.data
            }, status=status.HTTP_200_OK)
        elif result.outcome == OTPVerificationOutcome.WRONG_STATE:
            return Response({"detail": result.detail}, status=status.HTTP_409_CONFLICT)
        elif result.outcome == OTPVerificationOutcome.LOCKED:
            return Response({"detail": result.detail}, status=423)
        elif result.outcome == OTPVerificationOutcome.EXPIRED:
            return Response({"detail": result.detail}, status=status.HTTP_410_GONE)
        elif result.outcome == OTPVerificationOutcome.INVALID:
            return Response({
                "detail": result.detail,
                "attempts_remaining": result.attempts_remaining
            }, status=status.HTTP_400_BAD_REQUEST)
        elif result.outcome == OTPVerificationOutcome.SUCCESS:
            updated_serializer = DeliveryDriverTaskSerializer(result.delivery)
            return Response({
                "message": "Doorstep OTP verified. Delivery marked completed!",
                "delivery": updated_serializer.data
            }, status=status.HTTP_200_OK)

        return Response({"detail": result.detail or "Verification failed."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(request=OTPVerificationSerializer, responses={200: DeliveryDoorstepResponseSerializer})
    @action(detail=True, methods=['post'], url_path='verify-otp',
            permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def verify_otp(self, request, *args, **kwargs):
        return self.verify_doorstep(request, *args, **kwargs)

    @extend_schema(request=LocationPingSerializer, responses={201: DeliveryPingResponseSerializer})
    @action(detail=True, methods=['post'], url_path='ping', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def ping(self, request, *args, **kwargs):
        delivery = self.get_object()
        serializer = LocationPingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        lat = serializer.validated_data['latitude']
        lon = serializer.validated_data['longitude']
        now = timezone.now()

        # Ingest ping record
        ping_record = DeliveryLocationPing.objects.create(
            delivery=delivery,
            latitude=lat,
            longitude=lon
        )

        # Update delivery coordinates with update_fields for high-frequency performance
        delivery.current_latitude = lat
        delivery.current_longitude = lon
        delivery.last_location_update = now
        delivery.save(update_fields=['current_latitude', 'current_longitude', 'last_location_update'])

        # Update driver profile coordinates
        if delivery.driver:
            driver = delivery.driver
            driver.current_latitude = lat
            driver.current_longitude = lon
            driver.save(update_fields=['current_latitude', 'current_longitude'])

        ping_serializer = LocationPingSerializer(ping_record)
        return Response({
            "message": "Location ping ingested successfully.",
            "ping": ping_serializer.data,
            "current_location": {
                "latitude": str(lat),
                "longitude": str(lon),
                "updated_at": now.isoformat()
            }
        }, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: DeliveryTrackingResponseSerializer})
    @action(detail=True, methods=['get'], url_path='tracking', permission_classes=[IsAuthenticated, IsDeliveryParticipant])
    def tracking(self, request, *args, **kwargs):
        delivery = self.get_object()
        pings = delivery.location_pings.order_by('-pinged_at')[:50]
        pings_serializer = LocationPingSerializer(pings, many=True)

        # Customize detail payload based on user role (e.g. reveal doorstep_otp to customer)
        if request.user.role == 'customer' or request.user.id == delivery.application.customer_id:
            delivery_data = DeliveryCustomerSerializer(delivery).data
        elif hasattr(request.user, 'delivery_profile') and request.user.delivery_profile.id == delivery.driver_id:
            delivery_data = DeliveryDriverTaskSerializer(delivery).data
        else:
            delivery_data = ShelterDeliverySerializer(delivery).data

        return Response({
            "status": delivery.status,
            "delivery": delivery_data,
            "current_coordinates": {
                "latitude": str(delivery.current_latitude) if delivery.current_latitude else None,
                "longitude": str(delivery.current_longitude) if delivery.current_longitude else None,
                "last_updated": delivery.last_location_update
            },
            "breadcrumbs": pings_serializer.data
        }, status=status.HTTP_200_OK)

    @extend_schema(responses={200: DeliveryTrackingResponseSerializer})
    @action(detail=True, methods=['get'], url_path='track', permission_classes=[IsAuthenticated, IsDeliveryParticipant])
    def track(self, request, *args, **kwargs):
        return self.tracking(request, *args, **kwargs)


class PaymentViewSet(viewsets.GenericViewSet):
    """
    Razorpay payment processing endpoints:
    - POST /api/v1/payments/create-order/
    - POST /api/v1/payments/verify/
    - POST /api/v1/payments/webhook/
    """
    serializer_class = PaymentOrderCreateSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post'], url_path='create-order')
    def create_order(self, request):
        serializer = PaymentOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        delivery_public_id = serializer.validated_data['delivery_public_id']
        amount_in_paise = serializer.validated_data['amount_in_paise']

        delivery = get_object_or_404(PetDelivery, public_id=delivery_public_id)
        payment = create_razorpay_order(delivery, amount_in_paise)
        return Response(PaymentLedgerSerializer(payment).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='verify')
    def verify(self, request):
        serializer = PaymentVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        razorpay_order_id = serializer.validated_data['razorpay_order_id']
        razorpay_payment_id = serializer.validated_data['razorpay_payment_id']
        razorpay_signature = serializer.validated_data['razorpay_signature']

        payment = verify_razorpay_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature)
        return Response(PaymentLedgerSerializer(payment).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='webhook', permission_classes=[AllowAny], authentication_classes=[])
    def webhook(self, request):
        signature = request.headers.get('X-Razorpay-Signature') or request.META.get('HTTP_X_RAZORPAY_SIGNATURE', '')
        if not signature:
            return Response({"detail": "Missing X-Razorpay-Signature header."}, status=status.HTTP_400_BAD_REQUEST)

        process_razorpay_webhook(request.data, signature, raw_body=request.body)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)

