import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework.permissions import BasePermission

class IsShelterForDelivery(BasePermission):
    """
    Permission ensuring user is authenticated shelter partner matching delivery origin shelter.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            (request.user.role == 'shelter' or request.user.is_superuser) and
            hasattr(request.user, 'shelter_profile')
        )

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        if not hasattr(request.user, 'shelter_profile'):
            return False
        shelter_id = getattr(obj, 'shelter_id', None) or getattr(getattr(obj, 'application', None), 'pet', None).shelter_id
        return shelter_id == request.user.shelter_profile.id


class IsAssignedDeliveryDriver(BasePermission):
    """
    Permission ensuring user is authenticated delivery driver assigned to the delivery.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            (request.user.role == 'delivery' or request.user.is_superuser) and
            hasattr(request.user, 'delivery_profile')
        )

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        driver_user_id = getattr(getattr(obj, 'delivery_partner', None), 'user_id', None) or getattr(getattr(obj, 'driver', None), 'user_id', None)
        return driver_user_id == request.user.id


class IsDeliveryParticipant(BasePermission):
    """
    Access allowed if user is either the assigned driver, the origin shelter, or the customer of the adoption application.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser or request.user.role == 'admin':
            return True

        # Check driver
        driver_user_id = getattr(getattr(obj, 'delivery_partner', None), 'user_id', None) or getattr(getattr(obj, 'driver', None), 'user_id', None)
        if driver_user_id == request.user.id:
            return True

        # Check shelter
        shelter_id = getattr(obj, 'shelter_id', None) or getattr(getattr(obj, 'application', None), 'pet', None).shelter_id
        if hasattr(request.user, 'shelter_profile') and shelter_id == request.user.shelter_profile.id:
            return True

        # Check customer of the adoption application
        applicant_id = getattr(getattr(obj, 'application', None), 'applicant_id', None) or getattr(getattr(obj, 'application', None), 'customer_id', None)
        if applicant_id == request.user.id:
            return True

        return False
