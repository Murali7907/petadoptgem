import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.permissions import IsAuthenticated
from .permissions import IsAssignedDeliveryDriver
from .views import ShelterDeliveryViewSet, DriverDeliveryViewSet, PaymentViewSet

router = DefaultRouter()
router.register(r'shelter/deliveries', ShelterDeliveryViewSet, basename='shelter-deliveries')
router.register(r'delivery/tasks', DriverDeliveryViewSet, basename='driver-tasks')
router.register(r'payments', PaymentViewSet, basename='payments')

urlpatterns = [
    # =========================================================================
    # Canonical v1 routes (exposed under /api/v1/ via router):
    # - POST /api/v1/shelter/deliveries/assign/
    # - POST /api/v1/shelter/deliveries/<id>/reissue-otp/
    # - GET  /api/v1/shelter/deliveries/drivers/
    # - GET  /api/v1/shelter/deliveries/
    # - GET  /api/v1/delivery/tasks/active/
    # - POST /api/v1/delivery/tasks/<id>/pickup/
    # - POST /api/v1/delivery/tasks/<id>/verify-doorstep/
    # - POST /api/v1/delivery/tasks/<id>/status/
    # - POST /api/v1/delivery/tasks/<id>/ping/
    # - GET  /api/v1/delivery/tasks/<id>/tracking/
    # - POST /api/v1/payments/create-order/
    # - POST /api/v1/payments/verify/
    # - POST /api/v1/payments/webhook/
    # =========================================================================

    # =========================================================================
    # Legacy backward-compatibility aliases:
    # Direct contract endpoints for clients using /api/deliveries/ or /api/v1/deliveries/
    # =========================================================================
    # POST /api/deliveries/assign/
    path('assign/', ShelterDeliveryViewSet.as_view({'post': 'assign'}), name='delivery-assign'),
    # GET /api/deliveries/driver-active/
    path('driver-active/', DriverDeliveryViewSet.as_view({'get': 'active'}), name='delivery-driver-active'),
    # POST /api/deliveries/<id>/pickup/
    path('<str:id>/pickup/', DriverDeliveryViewSet.as_view({'post': 'pickup'}), name='delivery-pickup'),
    path('<uuid:public_id>/pickup/', DriverDeliveryViewSet.as_view({'post': 'pickup'}), name='delivery-pickup-uuid'),
    # POST /api/deliveries/<id>/ping/
    path('<str:id>/ping/', DriverDeliveryViewSet.as_view({'post': 'ping'}), name='delivery-ping'),
    path('<uuid:public_id>/ping/', DriverDeliveryViewSet.as_view({'post': 'ping'}), name='delivery-ping-uuid'),
    # GET /api/deliveries/<id>/track/
    path('<str:id>/track/', DriverDeliveryViewSet.as_view({'get': 'tracking'}), name='delivery-track'),
    path('<uuid:public_id>/track/', DriverDeliveryViewSet.as_view({'get': 'tracking'}), name='delivery-track-uuid'),
    # POST /api/deliveries/<id>/verify-otp/ & /verify-doorstep/
    path('<str:id>/verify-otp/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-otp'),
    path('<uuid:public_id>/verify-otp/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-otp-uuid'),
    path('<str:id>/verify-doorstep/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-doorstep'),
    # POST /api/deliveries/<id>/collect-cash/
    path('<str:id>/collect-cash/', DriverDeliveryViewSet.as_view({'post': 'collect_cash'}), name='delivery-collect-cash'),
    path('<uuid:public_id>/collect-cash/', DriverDeliveryViewSet.as_view({'post': 'collect_cash'}), name='delivery-collect-cash-uuid'),
    # PATCH/POST /api/deliveries/<id>/status/
    path('<str:id>/status/', DriverDeliveryViewSet.as_view({'patch': 'update_status', 'post': 'update_status'}, permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver]), name='delivery-status'),
    path('<uuid:public_id>/status/', DriverDeliveryViewSet.as_view({'patch': 'update_status', 'post': 'update_status'}, permission_classes=[IsAuthenticated, IsAssignedDeliveryDriver]), name='delivery-status-uuid'),
    # POST /api/v1/shelter/deliveries/<id>/reissue-otp/ & legacy /<id>/reissue-otp/
    path('shelter/deliveries/<str:id>/reissue-otp/', ShelterDeliveryViewSet.as_view({'post': 'reissue_otp'}), name='delivery-reissue-otp'),
    path('shelter/deliveries/<uuid:public_id>/reissue-otp/', ShelterDeliveryViewSet.as_view({'post': 'reissue_otp'}), name='delivery-reissue-otp-uuid'),
    path('<str:id>/reissue-otp/', ShelterDeliveryViewSet.as_view({'post': 'reissue_otp'}), name='delivery-reissue-otp-legacy'),
    path('<uuid:public_id>/reissue-otp/', ShelterDeliveryViewSet.as_view({'post': 'reissue_otp'}), name='delivery-reissue-otp-legacy-uuid'),
    # POST /api/v1/shelter/deliveries/<id>/cancel/ & legacy aliases
    path('shelter/deliveries/<str:id>/cancel/', ShelterDeliveryViewSet.as_view({'post': 'cancel'}), name='delivery-cancel'),
    path('shelter/deliveries/<uuid:public_id>/cancel/', ShelterDeliveryViewSet.as_view({'post': 'cancel'}), name='delivery-cancel-uuid'),
    path('<str:id>/cancel/', ShelterDeliveryViewSet.as_view({'post': 'cancel'}), name='delivery-cancel-legacy'),
    path('<uuid:public_id>/cancel/', ShelterDeliveryViewSet.as_view({'post': 'cancel'}), name='delivery-cancel-legacy-uuid'),
    # POST /api/v1/shelter/deliveries/<id>/cancel-and-refund/
    path('shelter/deliveries/<str:id>/cancel-and-refund/', ShelterDeliveryViewSet.as_view({'post': 'cancel_and_refund'}), name='delivery-cancel-and-refund'),
    path('shelter/deliveries/<uuid:public_id>/cancel-and-refund/', ShelterDeliveryViewSet.as_view({'post': 'cancel_and_refund'}), name='delivery-cancel-and-refund-uuid'),
    path('', include(router.urls)),
]
