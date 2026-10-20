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

from drf_spectacular.utils import extend_schema, OpenApiTypes, OpenApiResponse

from .models import PetDelivery, DeliveryLocationPing, Payment, PaymentStatus
from .permissions import IsShelterForDelivery, IsAssignedDeliveryDriver, IsDeliveryParticipant
from apps.accounts.permissions import IsVerifiedShelter
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
    PaymentLedgerSerializer,
    CollectCashRequestSerializer,
    CollectCashResponseSerializer,
    DeliveryCancelResponseSerializer
)
from .otp import check_otp, issue_otp
from .services import (
    assign_delivery_task,
    check_pickup_payment_guard,
    check_doorstep_payment_guard,
    verify_pickup_service,
    verify_doorstep_service,
    OTPVerificationOutcome,
    verify_pickup_otp,
    verify_doorstep_otp,
    dispatch_doorstep_otp,
    reissue_delivery_otp,
    send_delivery_completed_notice,
    resolve_delivery_payment_status,
    cancel_delivery_by_shelter,
    ConflictError,
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
    queryset = PetDelivery.objects.none()
    serializer_class = ShelterDeliverySerializer
    permission_classes = [IsAuthenticated]
    ordering = ['-created_at', '-id']
    lookup_field = 'public_id'

    def get_permissions(self):
        if self.action in ['assign', 'reissue_otp', 'cancel']:
            return [IsAuthenticated(), IsVerifiedShelter()]
        return [IsAuthenticated()]

    def get_throttles(self):
        if self.action == 'cancel':
            self.throttle_scope = 'shelter_cancel'
        return super().get_throttles()

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
        self.check_object_permissions(self.request, obj)
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

        try:
            delivery = assign_delivery_task(application=application, driver=driver, payment_method=payment_method)
        except ConflictError as e:
            if isinstance(e.detail, dict):
                return Response(e.detail, status=status.HTTP_409_CONFLICT)
            return Response({"detail": str(e.detail)}, status=status.HTTP_409_CONFLICT)
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

    @extend_schema(
        summary="Cancel delivery task",
        description="Verified shelter or platform administrator cancels an assigned or out_for_delivery task when no cash has been collected.",
        responses={
            200: DeliveryCancelResponseSerializer,
            400: OpenApiResponse(description="Invalid delivery status for cancellation"),
            403: OpenApiResponse(description="Unauthorized or foreign shelter"),
            409: OpenApiResponse(description="Cash already collected or online payment captured (refund required)")
        }
    )
    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, *args, **kwargs):
        delivery = self.get_object()
        user = request.user
        if not (user.is_staff or user.is_superuser or user.role == 'admin'):
            shelter = self.get_shelter_profile()
            if not delivery.application or not delivery.application.pet or delivery.application.pet.shelter_id != shelter.id:
                return Response({"detail": "Delivery belongs to a different shelter."}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = cancel_delivery_by_shelter(delivery, user)
            return Response(result, status=status.HTTP_200_OK)
        except ConflictError as e:
            if isinstance(e.detail, dict):
                return Response(e.detail, status=status.HTTP_409_CONFLICT)
            return Response({"detail": str(e.detail)}, status=status.HTTP_409_CONFLICT)
        except ValidationError as e:
            if isinstance(e.detail, dict):
                return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
            return Response({"detail": str(e.detail)}, status=status.HTTP_400_BAD_REQUEST)

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
    ordering = ['-created_at', '-id']
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
        if self.action in ['pickup', 'verify_doorstep', 'update_status', 'ping', 'collect_cash']:
            return [IsAuthenticated(), IsAssignedDeliveryDriver()]
        if self.action in ['tracking', 'track']:
            return [IsAuthenticated(), IsDeliveryParticipant()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action in ['pickup', 'verify_doorstep', 'verify_otp', 'collect_cash']:
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

    @extend_schema(
        request=OTPVerificationSerializer,
        responses={
            200: DeliveryPickupResponseSerializer,
            409: OpenApiResponse(description="Payment required or conflict"),
        }
    )
    @action(detail=True, methods=['post'], url_path='pickup',
            permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def pickup(self, request, *args, **kwargs):
        delivery_obj = self.get_object()

        # Guard: Validate payment requirements before checking OTP (consumes no attempts)
        allowed, code, detail = check_pickup_payment_guard(delivery_obj)
        if not allowed:
            return Response({"code": code, "detail": detail}, status=status.HTTP_409_CONFLICT)

        raw_otp = request.data.get('otp') or request.data.get('code') or request.data.get('secret_otp')
        if not raw_otp:
            return Response({"detail": "OTP is required."}, status=status.HTTP_400_BAD_REQUEST)

        val = str(raw_otp).strip()
        if not re.match(r'^\d{6}$', val):
            return Response({"detail": "OTP must be a 6-digit numeric string."}, status=status.HTTP_400_BAD_REQUEST)

        result = verify_pickup_service(delivery_obj.id, val)

        if result.outcome == OTPVerificationOutcome.WRONG_STATE:
            return Response({"detail": result.detail}, status=status.HTTP_409_CONFLICT)
        elif result.outcome in (OTPVerificationOutcome.PAYMENT_REQUIRED, OTPVerificationOutcome.PAYMENT_METHOD_REQUIRED):
            return Response({"code": result.code, "detail": result.detail}, status=status.HTTP_409_CONFLICT)
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

        if (delivery.cash_collected or Decimal('0.00')) > Decimal('0.00'):
            return Response({
                "detail": "Cannot mark delivery as failed after cash has been collected. Please contact the shelter.",
                "code": "cash_already_collected"
            }, status=status.HTTP_409_CONFLICT)

        delivery.status = 'failed'
        delivery.save(update_fields=['status'])

        if delivery.driver:
            delivery.driver.is_available = True
            delivery.driver.save(update_fields=['is_available'])

        updated_serializer = DeliveryDriverTaskSerializer(delivery)
        return Response(updated_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=CollectCashRequestSerializer,
        responses={
            200: CollectCashResponseSerializer,
            400: OpenApiResponse(description="Bad request (e.g. wrong amount, total is 0, not COD)"),
            409: OpenApiResponse(description="Conflict (e.g. not out_for_delivery)"),
        }
    )
    @action(detail=True, methods=['post'], url_path='collect-cash', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def collect_cash(self, request, *args, **kwargs):
        delivery_obj = self.get_object()

        raw_amount = request.data.get('amount')
        if raw_amount is None or str(raw_amount).strip() == '':
            return Response({"detail": "Amount is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount = Decimal(str(raw_amount).strip())
        except (ValueError, TypeError, ArithmeticError):
            return Response({"detail": "A valid decimal amount is required."}, status=status.HTTP_400_BAD_REQUEST)

        if amount <= Decimal('0.00'):
            return Response({"detail": "Amount must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            delivery = PetDelivery.objects.select_for_update().get(id=delivery_obj.id)
            application = AdoptionApplication.objects.select_for_update().get(id=delivery.application_id)

            if delivery.status != 'out_for_delivery':
                return Response({
                    "detail": f"Cannot collect cash for delivery in '{delivery.status}' state. Must be 'out_for_delivery'."
                }, status=status.HTTP_409_CONFLICT)

            if application.payment_method != 'cod':
                return Response({
                    "detail": f"Cannot collect cash for non-COD payment method '{application.payment_method}'."
                }, status=status.HTTP_400_BAD_REQUEST)

            total_amount = application.total_amount or Decimal('0.00')
            if total_amount <= Decimal('0.00'):
                return Response({
                    "detail": "Total amount must be greater than zero for COD collection."
                }, status=status.HTTP_400_BAD_REQUEST)

            current_collected = delivery.cash_collected or Decimal('0.00')

            # Idempotency check: if cash has already been collected in full, return 200 without duplicate ledger
            if current_collected >= total_amount:
                return Response({
                    "message": "Cash has already been collected in full.",
                    "cash_collected": str(current_collected),
                    "remaining_amount": "0.00",
                    "payment_status": application.payment_status
                }, status=status.HTTP_200_OK)

            remaining_balance = total_amount - current_collected
            if amount != remaining_balance:
                return Response({
                    "detail": f"Submitted amount ({amount}) must equal remaining balance ({remaining_balance}).",
                    "remaining_amount": str(remaining_balance)
                }, status=status.HTTP_400_BAD_REQUEST)

            # Record collection
            delivery.cash_collected = current_collected + amount
            delivery.payment_status = 'paid'
            delivery.save(update_fields=['cash_collected', 'payment_status'])

            payment = Payment.objects.create(
                delivery=delivery,
                application=application,
                amount=amount,
                amount_in_paise=int(round(Decimal(str(amount)) * 100)),
                currency='INR',
                payment_method='cod',
                status=PaymentStatus.CAPTURED,
                transaction_id=f"cod_{delivery.public_id}_{int(timezone.now().timestamp())}"
            )

            application.payment_status = 'paid'
            application.save(update_fields=['payment_status', 'updated_at'])

            return Response({
                "message": "Cash collected successfully.",
                "cash_collected": str(delivery.cash_collected),
                "remaining_amount": "0.00",
                "payment_status": "paid",
                "payment_id": str(payment.public_id)
            }, status=status.HTTP_200_OK)

    @extend_schema(request=OTPVerificationSerializer, responses={200: DeliveryDoorstepResponseSerializer, 409: OpenApiResponse(description="Cash not collected or wrong state")})
    @action(detail=True, methods=['post'], url_path='verify-doorstep',
            permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def verify_doorstep(self, request, *args, **kwargs):
        delivery_obj = self.get_object()

        # Guard: Validate doorstep payment requirements before checking OTP (consumes NO attempts)
        allowed, code, detail = check_doorstep_payment_guard(delivery_obj)
        if not allowed:
            return Response({"code": code, "detail": detail}, status=status.HTTP_409_CONFLICT)

        raw_otp = request.data.get('otp') or request.data.get('code') or request.data.get('secret_otp')
        if not raw_otp:
            return Response({"detail": "OTP is required."}, status=status.HTTP_400_BAD_REQUEST)

        val = str(raw_otp).strip()
        if not re.match(r'^\d{6}$', val):
            return Response({"detail": "OTP must be a 6-digit numeric string."}, status=status.HTTP_400_BAD_REQUEST)

        result = verify_doorstep_service(delivery_obj.id, val)

        if result.outcome == OTPVerificationOutcome.ALREADY_COMPLETED:
            updated_serializer = DeliveryDriverTaskSerializer(result.delivery)
            return Response({
                "message": "Delivery already completed.",
                "delivery": updated_serializer.data
            }, status=status.HTTP_200_OK)
        elif result.outcome == OTPVerificationOutcome.WRONG_STATE:
            return Response({"detail": result.detail}, status=status.HTTP_409_CONFLICT)
        elif result.outcome == OTPVerificationOutcome.CASH_NOT_COLLECTED:
            return Response({"code": result.code, "detail": result.detail}, status=status.HTTP_409_CONFLICT)
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

    @extend_schema(
        request=PaymentOrderCreateSerializer,
        responses={
            201: PaymentLedgerSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT
        },
        summary="Create Razorpay payment order for adoption delivery (applicant only)"
    )
    @action(detail=False, methods=['post'], url_path='create-order')
    def create_order(self, request):
        serializer = PaymentOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        delivery_public_id = serializer.validated_data.get('delivery_public_id') or serializer.validated_data.get('delivery_id')
        delivery = PetDelivery.objects.filter(public_id=delivery_public_id).select_related('application', 'application__applicant').first()
        if not delivery:
            raise NotFound({"detail": "Delivery not found."})

        payment = create_razorpay_order(delivery, request.user)
        data = PaymentLedgerSerializer(payment).data
        data['order_id'] = payment.razorpay_order_id
        data['key_id'] = getattr(settings, 'RAZORPAY_KEY_ID', '')
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=PaymentVerifySerializer,
        responses={
            200: PaymentLedgerSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT
        },
        summary="Verify Razorpay payment signature and mark captured (applicant only)"
    )
    @action(detail=False, methods=['post'], url_path='verify')
    def verify(self, request):
        serializer = PaymentVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        razorpay_order_id = serializer.validated_data['razorpay_order_id']
        razorpay_payment_id = serializer.validated_data['razorpay_payment_id']
        razorpay_signature = serializer.validated_data['razorpay_signature']

        payment = verify_razorpay_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature, request.user)
        data = PaymentLedgerSerializer(payment).data
        data['message'] = "Payment verified and recorded successfully."
        return Response(data, status=status.HTTP_200_OK)

    @extend_schema(
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT
        },
        summary="Razorpay webhook listener (signature verified over raw body, public auth)"
    )
    @action(detail=False, methods=['post'], url_path='webhook', permission_classes=[AllowAny], authentication_classes=[])
    def webhook(self, request):
        signature = request.headers.get('X-Razorpay-Signature') or request.META.get('HTTP_X_RAZORPAY_SIGNATURE', '')
        if not signature:
            return Response({"detail": "Missing X-Razorpay-Signature header."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = process_razorpay_webhook(raw_body=request.body, signature=signature)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({"detail": e.detail if hasattr(e, 'detail') else str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Webhook processing error: {e}", exc_info=True)
            return Response({"detail": "Webhook processing failed."}, status=status.HTTP_400_BAD_REQUEST)

