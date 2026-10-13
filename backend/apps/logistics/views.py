import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError, NotFound

from .models import PetDelivery, DeliveryLocationPing, Payment
from .permissions import IsShelterForDelivery, IsAssignedDeliveryDriver, IsDeliveryParticipant
from .serializers import (
    DeliveryAssignSerializer,
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
from .services import (
    assign_delivery_task,
    verify_pickup_otp,
    verify_doorstep_otp,
    dispatch_doorstep_otp
)
from .payment_services import (
    create_razorpay_order,
    verify_razorpay_payment,
    process_razorpay_webhook
)
from apps.accounts.models import DeliveryProfile, ShelterProfile
from apps.adoptions.models import AdoptionApplication


class ShelterDeliveryViewSet(viewsets.GenericViewSet):
    """
    Endpoints for shelter admins to assign and manage deliveries.
    - POST /api/v1/shelter/deliveries/assign/
    - GET  /api/v1/shelter/deliveries/drivers/
    - GET  /api/v1/shelter/deliveries/
    """
    serializer_class = ShelterDeliverySerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'public_id'

    def get_shelter_profile(self):
        user = self.request.user
        if not hasattr(user, 'shelter_profile') or not user.shelter_profile:
            raise ValidationError({"detail": "User is not associated with a shelter profile."})
        return user.shelter_profile

    def list(self, request):
        shelter = self.get_shelter_profile()
        deliveries = PetDelivery.objects.filter(application__pet__shelter=shelter).order_by('-created_at')
        serializer = ShelterDeliverySerializer(deliveries, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

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
        dispatch_doorstep_otp(delivery)
        response_serializer = ShelterDeliverySerializer(delivery)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

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

    @action(detail=False, methods=['get'], url_path='driver-active')
    def driver_active(self, request):
        return self.active(request)

    @action(detail=True, methods=['post'], url_path='pickup', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def pickup(self, request, *args, **kwargs):
        delivery = self.get_object()
        serializer = OTPVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp_code = serializer.validated_data['otp']
        verify_pickup_otp(delivery, otp_code)
        dispatch_doorstep_otp(delivery)

        updated_serializer = DeliveryDriverTaskSerializer(delivery)
        return Response({
            "message": "Pickup OTP verified successfully. Task is now out for delivery.",
            "delivery": updated_serializer.data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['patch', 'post'], url_path='status', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def update_status(self, request, *args, **kwargs):
        delivery = self.get_object()
        new_status = request.data.get('status')
        if not new_status:
            return Response({"detail": "Status field is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Do not allow bypassing doorstep OTP verification
        if new_status in ['delivered', 'completed']:
            return Response({
                "detail": "Cannot mark delivery as delivered directly. Doorstep OTP verification is required."
            }, status=status.HTTP_400_BAD_REQUEST)

        previous_status = delivery.status
        normalized_status = 'out_for_delivery' if new_status in ['in_transit', 'out_for_delivery'] else new_status
        delivery.status = normalized_status
        if normalized_status == 'out_for_delivery' and not delivery.dispatched_at:
            delivery.dispatched_at = timezone.now()
        delivery.save()

        # Dispatch OTP only when newly transitioning into out_for_delivery
        if normalized_status == 'out_for_delivery' and previous_status != 'out_for_delivery':
            dispatch_doorstep_otp(delivery)

        updated_serializer = DeliveryDriverTaskSerializer(delivery)
        return Response(updated_serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='verify-doorstep', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def verify_doorstep(self, request, *args, **kwargs):
        delivery = self.get_object()
        serializer = OTPVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp_code = serializer.validated_data['otp']
        verify_doorstep_otp(delivery, otp_code)

        updated_serializer = DeliveryDriverTaskSerializer(delivery)
        return Response({
            "message": "Doorstep OTP verified. Delivery marked completed!",
            "delivery": updated_serializer.data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='verify-otp', permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver])
    def verify_otp(self, request, *args, **kwargs):
        return self.verify_doorstep(request, *args, **kwargs)

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

