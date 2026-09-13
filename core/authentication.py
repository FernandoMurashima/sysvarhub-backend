from rest_framework import authentication, exceptions

from core.models import Terminal
from core.services.operadores import OperadorAuthenticationError, validar_sessao_operador


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


class TerminalOperadorAuthentication(TerminalTokenAuthentication):
    operador_header = "HTTP_X_SYSVAR_OPERADOR_SESSION"

    def authenticate(self, request):
        resultado = super().authenticate(request)
        if resultado is None:
            return None

        _user, terminal = resultado
        token = request.META.get(self.operador_header)
        try:
            sessao = validar_sessao_operador(
                terminal,
                token,
                ip=request.META.get("REMOTE_ADDR"),
            )
        except OperadorAuthenticationError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        request.sysvar_terminal = terminal
        request.sysvar_operador = sessao.operador
        request.sysvar_operador_sessao = sessao
        return (None, sessao)
