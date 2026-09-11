from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.authentication import TerminalTokenAuthentication
from core.models import CaixaHub
from core.permissions import IsTerminalAuthenticated
from core.services.terminais import (
    PareamentoTerminalError,
    parear_terminal,
    registrar_heartbeat_terminal,
)


class ParearTerminalView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "terminal_pareamento"

    def post(self, request):
        try:
            terminal, token = parear_terminal(
                codigo=request.data.get("codigo"),
                hostname=request.data.get("hostname") or "",
                ip=_obter_ip_requisicao(request),
            )
        except PareamentoTerminalError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "token": token,
                **_montar_contexto_terminal(terminal, incluir_ativo_terminal=False),
            }
        )


class TerminalContextoView(APIView):
    authentication_classes = [TerminalTokenAuthentication]
    permission_classes = [IsTerminalAuthenticated]

    def get(self, request):
        return Response(_montar_contexto_terminal(request.sysvar_terminal))


class TerminalHeartbeatView(APIView):
    authentication_classes = [TerminalTokenAuthentication]
    permission_classes = [IsTerminalAuthenticated]

    def post(self, request):
        terminal = registrar_heartbeat_terminal(
            request.sysvar_terminal,
            hostname=request.data.get("hostname") or "",
            ip=_obter_ip_requisicao(request),
        )
        return Response(
            {
                "status": "ok",
                "terminal_uuid": str(terminal.terminal_uuid),
                "servidor_em": timezone.now().isoformat(),
            }
        )


def _montar_contexto_terminal(terminal, incluir_ativo_terminal=True):
    hub = terminal.hub
    caixa = _obter_caixa(terminal)
    terminal_payload = {
        "uuid": str(terminal.terminal_uuid),
        "codigo": terminal.codigo,
        "nome": terminal.nome,
    }
    if incluir_ativo_terminal:
        terminal_payload.update(
            {
                "hostname": terminal.hostname,
                "ativo": terminal.ativo,
            }
        )

    return {
        "terminal": terminal_payload,
        "caixa": _formatar_caixa(caixa),
        "loja": {
            "id": hub.loja_id,
            "nome": hub.loja_nome,
            "apelido": hub.loja_apelido,
            "estado": hub.loja_estado,
        },
        "empresa": {
            "id": hub.empresa_id,
            "nome": hub.empresa_nome,
        },
    }


def _obter_caixa(terminal):
    if terminal.caixa_retaguarda_id is None:
        return None
    return CaixaHub.objects.filter(
        hub=terminal.hub,
        retaguarda_id=terminal.caixa_retaguarda_id,
    ).first()


def _formatar_caixa(caixa):
    if caixa is None:
        return None
    return {
        "id": caixa.retaguarda_id,
        "codigo": caixa.codigo,
        "descricao": caixa.descricao,
        "ativo": caixa.ativo,
    }


def _obter_ip_requisicao(request):
    return request.META.get("REMOTE_ADDR")
