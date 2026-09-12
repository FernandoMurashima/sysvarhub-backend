from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.authentication import TerminalTokenAuthentication
from core.models import CaixaHub, CatalogoItemHub
from core.permissions import IsTerminalAuthenticated
from core.services.terminais import (
    PareamentoTerminalError,
    parear_terminal,
    registrar_heartbeat_terminal,
)


CATALOGO_TERMINAL_LIMIT_DEFAULT = 40
CATALOGO_TERMINAL_LIMIT_MAX = 100


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


class TerminalCatalogoView(APIView):
    authentication_classes = [TerminalTokenAuthentication]
    permission_classes = [IsTerminalAuthenticated]

    def get(self, request):
        terminal = request.sysvar_terminal
        hub = terminal.hub
        termo = (request.query_params.get("q") or "").strip()
        limit = _normalizar_limit(request.query_params.get("limit"))

        itens = CatalogoItemHub.objects.filter(hub=hub, ativo=True)
        if termo:
            itens = _aplicar_busca_catalogo(itens, termo)
        else:
            itens = itens.order_by("descricao", "retaguarda_sku_id")

        total = itens.count()
        itens = itens[:limit]

        return Response(
            {
                "catalogo_versao": hub.catalogo_versao,
                "catalogo_sincronizado_em": (
                    hub.catalogo_sincronizado_em.isoformat()
                    if hub.catalogo_sincronizado_em
                    else None
                ),
                "tabela_preco": {
                    "codigo": hub.tabela_preco_codigo,
                    "nome": hub.tabela_preco_nome,
                },
                "q": termo,
                "total": total,
                "limit": limit,
                "itens": [_serializar_catalogo_item(item) for item in itens],
            }
        )


def _normalizar_limit(valor):
    try:
        limit = int(valor)
    except (TypeError, ValueError):
        return CATALOGO_TERMINAL_LIMIT_DEFAULT
    if limit <= 0:
        return CATALOGO_TERMINAL_LIMIT_DEFAULT
    return min(limit, CATALOGO_TERMINAL_LIMIT_MAX)


def _aplicar_busca_catalogo(queryset, termo):
    busca = (
        Q(ean13__icontains=termo)
        | Q(referencia__icontains=termo)
        | Q(codigo_item_ref__icontains=termo)
        | Q(descricao__icontains=termo)
        | Q(descricao_reduzida__icontains=termo)
        | Q(cor_descricao__icontains=termo)
        | Q(tamanho_descricao__icontains=termo)
    )
    prioridade = Case(
        When(ean13__iexact=termo, then=Value(1)),
        When(codigo_item_ref__iexact=termo, then=Value(2)),
        When(referencia__iexact=termo, then=Value(3)),
        When(referencia__icontains=termo, then=Value(4)),
        When(descricao__icontains=termo, then=Value(5)),
        default=Value(6),
        output_field=IntegerField(),
    )
    return queryset.filter(busca).annotate(prioridade_busca=prioridade).order_by(
        "prioridade_busca",
        "descricao",
        "retaguarda_sku_id",
    )


def _serializar_catalogo_item(item):
    return {
        "produto_id": item.retaguarda_produto_id,
        "sku_id": item.retaguarda_sku_id,
        "tipo_produto": item.tipo_produto,
        "referencia": item.referencia,
        "descricao": item.descricao,
        "descricao_reduzida": item.descricao_reduzida,
        "ean13": item.ean13 or None,
        "codigo_item_ref": item.codigo_item_ref,
        "cor": {
            "id": item.cor_retaguarda_id,
            "descricao": item.cor_descricao,
        },
        "tamanho": {
            "id": item.tamanho_retaguarda_id,
            "descricao": item.tamanho_descricao,
        },
        "unidade": {
            "id": item.unidade_retaguarda_id,
            "codigo": item.unidade_codigo,
            "descricao": item.unidade_descricao,
        },
        "preco": _decimal_para_string(item.preco),
        "preco_promocional": _decimal_para_string(item.preco_promocional),
        "preco_venda": _decimal_para_string(item.preco_venda),
        "estoque_fisico": _decimal_para_string(item.estoque_fisico),
        "reserva": _decimal_para_string(item.reserva),
        "estoque_disponivel": _decimal_para_string(item.estoque_disponivel),
        "vendavel": item.vendavel,
        "motivos_bloqueio": item.motivos_bloqueio,
        "fiscal": item.fiscal,
    }


def _decimal_para_string(valor):
    if valor is None:
        return None
    return str(valor)


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
