import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework.permissions import BasePermission

class IsCustomer(BasePermission):
    """
    Permission class ensuring user is authenticated and has role == 'customer'.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.role == 'customer'
        )


class IsApplicationOwner(BasePermission):
    """
    Object-level permission ensuring applicant == request.user.
    """
    def has_object_permission(self, request, view, obj):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            obj.applicant_id == request.user.id
        )


class IsShelterForApplication(BasePermission):
    """
    Object-level permission ensuring obj.pet.shelter == request.user.shelter_profile.
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
        return obj.pet.shelter_id == request.user.shelter_profile.id


class IsDeliveredApplicationCustomer(BasePermission):
    """
    Checks request.user.role == 'customer' and ensures obj.customer_id == request.user.id.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.role == 'customer'
        )

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        customer_id = getattr(obj, 'customer_id', None) or getattr(obj, 'applicant_id', None)
        return customer_id == request.user.id

