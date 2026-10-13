from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CustomerApplicationViewSet,
    ShelterApplicationViewSet,
    ApplicationReviewCreateView,
    ShelterReviewListView,
    DeliveryReviewListView,
    CustomerApplicationApplyView,
    CustomerMyApplicationsView,
    ShelterApplicationsListView,
    AdoptionStatusUpdateView,
)

router = DefaultRouter()
router.register(r'applications', CustomerApplicationViewSet, basename='customer-applications')
router.register(r'shelter/applications', ShelterApplicationViewSet, basename='shelter-applications')

urlpatterns = [
    path('apply/', CustomerApplicationApplyView.as_view(), name='adoption_apply'),
    path('my-applications/', CustomerMyApplicationsView.as_view(), name='my_applications'),
    path('shelter-applications/', ShelterApplicationsListView.as_view(), name='shelter_applications_list'),
    path('<str:id>/status/', AdoptionStatusUpdateView.as_view(), name='adoption_status_update'),
    path('adoptions/applications/<uuid:public_id>/review/', ApplicationReviewCreateView.as_view(), name='application-review-prefixed'),
    path('applications/<uuid:public_id>/review/', ApplicationReviewCreateView.as_view(), name='application-review'),
    path('shelter/reviews/', ShelterReviewListView.as_view(), name='shelter-reviews'),
    path('delivery/reviews/', DeliveryReviewListView.as_view(), name='delivery-reviews'),
    path('', include(router.urls)),
]

