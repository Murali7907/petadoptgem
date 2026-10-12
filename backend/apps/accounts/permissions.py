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
