from django.urls import path
from .views import CustomerFavoritePetsView, CustomerFavoritePetActionView

urlpatterns = [
    path('', CustomerFavoritePetsView.as_view(), name='customer-favorites-list'),
    path('<uuid:pet_public_id>/', CustomerFavoritePetActionView.as_view(), name='customer-favorite-action'),
]
