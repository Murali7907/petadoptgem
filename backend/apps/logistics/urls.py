import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework.routers import DefaultRouter
from .views import ShelterDeliveryViewSet, DriverDeliveryViewSet, PaymentViewSet

router = DefaultRouter()
router.register(r'shelter/deliveries', ShelterDeliveryViewSet, basename='shelter-deliveries')
router.register(r'delivery/tasks', DriverDeliveryViewSet, basename='driver-tasks')
router.register(r'payments', PaymentViewSet, basename='payments')

urlpatterns = router.urls
