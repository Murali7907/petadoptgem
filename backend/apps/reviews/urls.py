from django.urls import path
from .views import ReviewCreateAPIView

urlpatterns = [
    path('', ReviewCreateAPIView.as_view(), name='review_create'),
    path('create/', ReviewCreateAPIView.as_view(), name='review_create_alias'),
]
