from rest_framework.permissions import BasePermission

class IsAdminRole(BasePermission):
    """
    Allows access only to authenticated users with admin role or superuser status.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            (request.user.role == 'admin' or request.user.is_superuser)
        )


class IsShelterRole(BasePermission):
    """
    Allows access only to authenticated users with shelter role.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.role == 'shelter'
        )


class IsVerifiedShelter(BasePermission):
    """
    Allows access only to verified shelter organizations or platform administrators.
    Unverified shelters receive 403 with {"code": "shelter_not_verified"}.
    """
    message = {
        "detail": "Shelter organization has not been verified by platform administrators.",
        "code": "shelter_not_verified"
    }

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        # Admin bypass
        if request.user.role == 'admin' or request.user.is_superuser or request.user.is_staff:
            return True

        if request.user.role != 'shelter':
            self.message = "You do not have permission to perform this action."
            return False

        shelter_profile = getattr(request.user, 'shelter_profile', None)
        if not shelter_profile or not shelter_profile.is_verified:
            self.message = {
                "detail": "Shelter organization has not been verified by platform administrators.",
                "code": "shelter_not_verified"
            }
            return False

        return True

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


class IsDeliveryPartnerRole(BasePermission):
    """
    Allows access only to authenticated users with delivery role.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.role == 'delivery'
        )


# Alias for flexibility
IsDeliveryRole = IsDeliveryPartnerRole


class IsCustomerRole(BasePermission):
    """
    Allows access only to authenticated users with customer role.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.role == 'customer'
        )
