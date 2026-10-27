from django.urls import path, include
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenVerifyView
from .views import (
    CustomTokenObtainPairView, 
    CustomTokenRefreshView,
    LogoutView,
    RegisterView, 
    UserProfileView,
    ShelterDriverCreateView,
    AdminShelterVerificationViewSet,
    AdminPlatformDashboardView,
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
    PasswordResetView,
    PasswordResetConfirmView,
    PasswordChangeView,
)

router = SimpleRouter()
router.register(r'admin/shelters', AdminShelterVerificationViewSet, basename='admin-shelters')

urlpatterns = [
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='auth_login'),
    path('logout/', LogoutView.as_view(), name='auth_logout'),
    path('drivers/create/', ShelterDriverCreateView.as_view(), name='auth_driver_create'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='auth_token_refresh'),
    path('token/verify/', TokenVerifyView.as_view(), name='auth_token_verify'),
    path('password-reset/', PasswordResetView.as_view(), name='auth_password_reset'),
    path('password-reset/confirm/', PasswordResetConfirmView.as_view(), name='auth_password_reset_confirm'),
    path('password/change/', PasswordChangeView.as_view(), name='auth_password_change'),
    path('me/', UserProfileView.as_view(), name='auth_me'),
    path('notifications/', NotificationListView.as_view(), name='notification-list'),
    path('notifications/<uuid:public_id>/read/', NotificationMarkReadView.as_view(), name='notification-mark-read'),
    path('notifications/read-all/', NotificationMarkAllReadView.as_view(), name='notification-mark-all-read'),
    path('admin/dashboard/stats/', AdminPlatformDashboardView.as_view(), name='admin-dashboard-stats'),
    path('', include(router.urls)),
]

