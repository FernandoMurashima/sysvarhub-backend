from rest_framework.permissions import BasePermission


class IsTerminalAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return getattr(request, "sysvar_terminal", None) is not None
