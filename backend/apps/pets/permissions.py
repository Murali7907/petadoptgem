import sys
from pathlib import Path

# Ensure backend directory is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rest_framework.permissions import BasePermission

class IsShelterOwner(BasePermission):
    """
    Permission class ensuring only authenticated shelter users with an active ShelterProfile
    can access, and ensuring object-level access is scoped to their own shelter.
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

        shelter_id = request.user.shelter_profile.id

        # Check direct pet model
        if hasattr(obj, 'shelter_id'):
            return obj.shelter_id == shelter_id

        # Check nested models linked to pet
        if hasattr(obj, 'pet') and hasattr(obj.pet, 'shelter_id'):
            return obj.pet.shelter_id == shelter_id

        return False
