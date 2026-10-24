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
from apps.logistics.urls import router as logistics_router
from apps.adoptions.urls import v1_urlpatterns as adoptions_v1_urlpatterns

urlpatterns = [
    path('admin/', admin.site.urls),
    # OpenAPI 3.0 & Swagger UI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    # Application Routes - Specification Clean API Paths
    path('api/auth/', include('apps.accounts.urls')),
    path('api/pets/', include('apps.pets.urls')),
    path('api/adoptions/', include('apps.adoptions.urls')),
    path('api/deliveries/', include('apps.logistics.urls')),
    path('api/reviews/', include('apps.reviews.urls')),

    # Backwards compatibility and additional feature routes under /api/v1/
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/', include('apps.accounts.urls')),
    path('api/v1/customer/favorites/', include('apps.pets.favorite_urls')),
    path('api/v1/shelter/pets/', include(shelter_router.urls)),
    path('api/v1/pets/', include('apps.pets.urls')),
    path('api/v1/', include(public_pets_router.urls)),
    path('api/v1/adoptions/', include('apps.adoptions.urls')),
    path('api/v1/', include(adoptions_v1_urlpatterns)),
    path('api/v1/deliveries/', include('apps.logistics.urls')),
    path('api/v1/', include(logistics_router.urls)),
    path('api/v1/reviews/', include('apps.reviews.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
