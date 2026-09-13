from rest_framework.permissions import BasePermission


class IsTerminalAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return getattr(request, "sysvar_terminal", None) is not None


class IsOperadorAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return (
            getattr(request, "sysvar_terminal", None) is not None
            and getattr(request, "sysvar_operador", None) is not None
            and getattr(request, "sysvar_operador_sessao", None) is not None
        )
