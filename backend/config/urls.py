from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework.routers import DefaultRouter
from apps.pets.views import ShelterPetViewSet

shelter_router = DefaultRouter()
shelter_router.register('', ShelterPetViewSet, basename='shelter-pet')

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.pets.urls import router as public_pets_router

urlpatterns = [
    path('admin/', admin.site.urls),
    # OpenAPI 3.0 & Swagger UI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    # Application Routes
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/', include('apps.accounts.urls')),
    path('api/v1/customer/favorites/', include('apps.pets.favorite_urls')),
    path('api/v1/shelter/pets/', include(shelter_router.urls)),
    path('api/v1/pets/', include('apps.pets.urls')),
    path('api/v1/', include(public_pets_router.urls)),
    path('api/v1/', include('apps.adoptions.urls')),
    path('api/v1/', include('apps.logistics.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
