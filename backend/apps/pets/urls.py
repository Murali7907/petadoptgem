from django.urls import path, include
from rest_framework.routers import DefaultRouter, SimpleRouter
from .views import (
    PetViewSet,
    ShelterPetViewSet,
    PetImageCreateView,
    PetMedicalRecordListCreateView,
    PublicPetViewSet,
)

router = SimpleRouter()
router.register(r'public/pets', PublicPetViewSet, basename='public-pets')

public_router = DefaultRouter()
public_router.register('', PetViewSet, basename='pet')

shelter_router = DefaultRouter()
shelter_router.register('', ShelterPetViewSet, basename='shelter-pet')
urlpatterns = [
    path('shelter-pets/', include(shelter_router.urls)),
    path('<str:pet_id>/images/', PetImageCreateView.as_view(), name='pet_images_upload'),
    path('<str:pet_id>/medical/', PetMedicalRecordListCreateView.as_view(), name='pet_medical_log'),
    path('<uuid:pet_public_id>/images/', PetImageCreateView.as_view(), name='pet_image_create'),
    path('<uuid:pet_public_id>/medical-records/', PetMedicalRecordListCreateView.as_view(), name='pet_medical_records'),
    path('', include(router.urls)),
    path('', include(public_router.urls)),
]

