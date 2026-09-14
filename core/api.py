from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.authentication import TerminalOperadorAuthentication, TerminalTokenAuthentication
from core.models import CaixaHub, CatalogoItemHub
from core.permissions import IsOperadorAuthenticated, IsTerminalAuthenticated
from core.services.caixa import (
    CaixaConflictError,
    CaixaError,
    ValorAberturaError,
    abrir_caixa,
    consultar_status_caixa,
    fechar_caixa,
    serializar_sessao_caixa,
)
from core.services.operadores import (
    OperadorAuthenticationError,
    autenticar_operador_terminal,
    encerrar_sessao,
    serializar_operador,
)
from core.services.terminais import (
    PareamentoTerminalError,
    parear_terminal,
    registrar_heartbeat_terminal,
)
from core.services.vendas import (
    SaldoInsuficienteError,
    VendaConflictError,
    VendaError,
    VendaNotFoundError,
    VendaValidationError,
    adicionar_item,
    adicionar_pagamento,
    alterar_quantidade_item,
    cancelar_venda,
    finalizar_venda,
    listar_formas_pagamento,
    remover_pagamento,
    remover_item,
    serializar_venda,
    venda_atual,
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


class OperadorLoginView(APIView):
    authentication_classes = [TerminalTokenAuthentication]
    permission_classes = [IsTerminalAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "operador_login"

    def post(self, request):
        try:
            sessao, token = autenticar_operador_terminal(
                request.sysvar_terminal,
                codigo=request.data.get("codigo"),
                senha=request.data.get("senha"),
                ip=_obter_ip_requisicao(request),
            )
        except OperadorAuthenticationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "sessao_token": token,
                "sessao": {
                    "uuid": str(sessao.sessao_uuid),
                    "iniciada_em": sessao.iniciada_em.isoformat(),
                },
                "operador": serializar_operador(sessao.operador),
            }
        )


class OperadorContextoView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        sessao = request.sysvar_operador_sessao
        return Response(
            {
                "sessao": {
                    "uuid": str(sessao.sessao_uuid),
                    "iniciada_em": sessao.iniciada_em.isoformat(),
                    "ultima_atividade_em": sessao.ultima_atividade_em.isoformat(),
                },
                "operador": serializar_operador(request.sysvar_operador),
            }
        )


class OperadorLogoutView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        encerrar_sessao(request.sysvar_operador_sessao)
        return Response({"status": "ok"})


class CaixaStatusView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        try:
            return Response(consultar_status_caixa(request.sysvar_terminal))
        except CaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class CaixaAbrirView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            sessao = abrir_caixa(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                valor_abertura=request.data.get("valor_abertura"),
            )
        except ValorAberturaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except CaixaConflictError as exc:
            payload = {"detail": str(exc)}
            if exc.sessao:
                payload["sessao"] = serializar_sessao_caixa(exc.sessao)
            return Response(payload, status=status.HTTP_409_CONFLICT)
        except CaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serializar_sessao_caixa(sessao), status=status.HTTP_201_CREATED)


class CaixaFecharView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            sessao = fechar_caixa(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
            )
        except CaixaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except CaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"status": "ok", "sessao": serializar_sessao_caixa(sessao)})


class VendaAtualView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        try:
            venda = venda_atual(request.sysvar_terminal)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


class FormasPagamentoView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        return Response(listar_formas_pagamento(request.sysvar_terminal))


class VendaItemView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda, criou_primeira_linha = adicionar_item(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                sku_id=request.data.get("sku_id"),
                quantidade=request.data.get("quantidade", 1),
            )
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SaldoInsuficienteError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "estoque_disponivel": f"{exc.estoque_disponivel:.3f}",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"venda": serializar_venda(venda)},
            status=status.HTTP_201_CREATED if criou_primeira_linha else status.HTTP_200_OK,
        )


class VendaItemDetalheView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def patch(self, request, item_uuid):
        try:
            venda = alterar_quantidade_item(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                item_uuid=item_uuid,
                quantidade=request.data.get("quantidade"),
            )
        except VendaNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SaldoInsuficienteError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "estoque_disponivel": f"{exc.estoque_disponivel:.3f}",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})

    def delete(self, request, item_uuid):
        try:
            venda = remover_item(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                item_uuid=item_uuid,
            )
        except VendaNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


class VendaCancelarView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda = cancelar_venda(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


class VendaPagamentoView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda = adicionar_pagamento(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                venda_uuid=request.data.get("venda_uuid"),
                operacao_uuid=request.data.get("operacao_uuid"),
                forma_pagamento_id=request.data.get("forma_pagamento_id"),
                valor=request.data.get("valor"),
                autorizacao=request.data.get("autorizacao") or "",
            )
        except VendaNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)}, status=status.HTTP_201_CREATED)


class VendaPagamentoDetalheView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def delete(self, request, pagamento_uuid):
        try:
            venda = remover_pagamento(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                pagamento_uuid=pagamento_uuid,
            )
        except VendaNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


class VendaFinalizarView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda = finalizar_venda(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                venda_uuid=request.data.get("venda_uuid"),
            )
        except VendaNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SaldoInsuficienteError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "estoque_disponivel": f"{exc.estoque_disponivel:.3f}",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


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
