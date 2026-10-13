import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ShelterDeliveryViewSet, DriverDeliveryViewSet, PaymentViewSet

router = DefaultRouter()
router.register(r'shelter/deliveries', ShelterDeliveryViewSet, basename='shelter-deliveries')
router.register(r'delivery/tasks', DriverDeliveryViewSet, basename='driver-tasks')
router.register(r'payments', PaymentViewSet, basename='payments')

urlpatterns = [
    # Direct contract endpoints:
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
    path('<str:id>/tracking/', DriverDeliveryViewSet.as_view({'get': 'tracking'}), name='delivery-tracking'),
    # POST /api/deliveries/<id>/verify-otp/
    path('<str:id>/verify-otp/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-otp'),
    path('<uuid:public_id>/verify-otp/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-otp-uuid'),
    path('<str:id>/verify-doorstep/', DriverDeliveryViewSet.as_view({'post': 'verify_doorstep'}), name='delivery-verify-doorstep'),
    # PATCH/POST /api/deliveries/<id>/status/
    path('<str:id>/status/', DriverDeliveryViewSet.as_view({'patch': 'update_status', 'post': 'update_status'}), name='delivery-status'),
    path('<uuid:public_id>/status/', DriverDeliveryViewSet.as_view({'patch': 'update_status', 'post': 'update_status'}), name='delivery-status-uuid'),
    path('', include(router.urls)),
]
