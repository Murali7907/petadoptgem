from django.urls import path
from .views import ReviewCreateAPIView

urlpatterns = [
    path('', ReviewCreateAPIView.as_view(), name='review_create'),
]

