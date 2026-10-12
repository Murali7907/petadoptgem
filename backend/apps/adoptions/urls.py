from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CustomerApplicationViewSet,
    ShelterApplicationViewSet,
    ApplicationReviewCreateView,
    ShelterReviewListView,
    DeliveryReviewListView
)

router = DefaultRouter()
router.register(r'applications', CustomerApplicationViewSet, basename='customer-applications')
router.register(r'shelter/applications', ShelterApplicationViewSet, basename='shelter-applications')

urlpatterns = [
    path('adoptions/applications/<uuid:public_id>/review/', ApplicationReviewCreateView.as_view(), name='application-review-prefixed'),
    path('applications/<uuid:public_id>/review/', ApplicationReviewCreateView.as_view(), name='application-review'),
    path('shelter/reviews/', ShelterReviewListView.as_view(), name='shelter-reviews'),
    path('delivery/reviews/', DeliveryReviewListView.as_view(), name='delivery-reviews'),
    path('', include(router.urls)),
]

