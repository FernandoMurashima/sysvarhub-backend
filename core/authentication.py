from rest_framework import authentication, exceptions

from core.models import Terminal


class TerminalTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Terminal"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("utf-8")
        if not header:
            return None

        partes = header.split()
        if len(partes) != 2 or partes[0] != self.keyword:
            return None

        token = partes[1]
        token_hash = Terminal.hash_token(token)

        try:
            terminal = Terminal.objects.select_related("hub").get(
                token_hash=token_hash,
                ativo=True,
            )
        except Terminal.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("Token de terminal invalido.") from exc

        request.sysvar_terminal = terminal
        return (None, terminal)
