from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.authentication import TerminalOperadorAuthentication, TerminalTokenAuthentication
from core.models import CaixaHub, CatalogoItemHub, ClienteHub, TipoDespesaPdvHub, VendedorHub
from core.permissions import IsOperadorAuthenticated, IsTerminalAuthenticated
from core.services.caixa import (
    CaixaConflictError,
    CaixaError,
    ObservacaoFechamentoError,
    ValorAberturaError,
    ValorContadoError,
    abrir_caixa,
    consultar_status_caixa,
    fechar_caixa,
    serializar_sessao_caixa,
)
from core.services.clientes import (
    ClienteConflictError,
    ClienteError,
    ClienteValidationError,
    cadastrar_cliente_local,
    validar_payload_cadastro_cliente,
)
from core.services.movimentacoes_caixa import (
    MovimentacaoCaixaConflictError,
    MovimentacaoCaixaError,
    MovimentacaoCaixaValidationError,
    listar_movimentacoes_sessao_aberta,
    registrar_movimentacao_caixa,
    serializar_movimentacao_caixa,
)
from core.services.operadores import (
    OperadorAuthenticationError,
    autenticar_operador_terminal,
    encerrar_sessao,
    serializar_operador,
)
from core.services.resumo_caixa import obter_resumo_caixa
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
    iniciar_venda,
    listar_formas_pagamento,
    remover_pagamento,
    remover_item,
    remover_cliente,
    remover_vendedor,
    serializar_cliente_preselecionado,
    serializar_vendedor_preselecionado,
    serializar_venda,
    selecionar_cliente,
    selecionar_vendedor,
    venda_atual,
)


CATALOGO_TERMINAL_LIMIT_DEFAULT = 40
CATALOGO_TERMINAL_LIMIT_MAX = 100
CLIENTES_TERMINAL_LIMIT = 50
VENDEDORES_TERMINAL_LIMIT = 50


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
            sessao, fechamento = fechar_caixa(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                valor_contado=request.data.get("valor_contado"),
                observacao=request.data.get("observacao", ""),
            )
        except (ValorContadoError, ObservacaoFechamentoError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except CaixaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except CaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"status": "ok", "sessao": serializar_sessao_caixa(sessao), "fechamento": fechamento})


class CaixaMovimentacoesView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        try:
            sessao_caixa, movimentacoes = listar_movimentacoes_sessao_aberta(request.sysvar_terminal)
        except MovimentacaoCaixaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except MovimentacaoCaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "sessao_caixa_uuid": str(sessao_caixa.sessao_uuid),
                "total": movimentacoes.count(),
                "movimentacoes": [
                    serializar_movimentacao_caixa(movimentacao)
                    for movimentacao in movimentacoes
                ],
            }
        )

    def post(self, request):
        try:
            movimentacao = registrar_movimentacao_caixa(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                tipo=request.data.get("tipo"),
                valor=request.data.get("valor"),
                tipo_despesa_id=request.data.get("tipo_despesa_id"),
                documento=request.data.get("documento") or "",
                historico=request.data.get("historico") or "",
            )
        except MovimentacaoCaixaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except MovimentacaoCaixaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MovimentacaoCaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"movimentacao": serializar_movimentacao_caixa(movimentacao)},
            status=status.HTTP_201_CREATED,
        )


class CaixaResumoView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        try:
            return Response(obter_resumo_caixa(request.sysvar_terminal))
        except CaixaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except CaixaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class VendaAtualView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        try:
            venda, cliente_preselecionado, vendedor_preselecionado = venda_atual(request.sysvar_terminal)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "venda": serializar_venda(venda),
                "cliente_preselecionado": serializar_cliente_preselecionado(cliente_preselecionado),
                "vendedor_preselecionado": serializar_vendedor_preselecionado(vendedor_preselecionado),
            }
        )


class VendaIniciarView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda, criada = iniciar_venda(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"venda": serializar_venda(venda)},
            status=status.HTTP_201_CREATED if criada else status.HTTP_200_OK,
        )


class VendaClienteView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def put(self, request):
        try:
            venda = selecionar_cliente(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                cliente_uuid=request.data.get("cliente_uuid"),
            )
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})

    def delete(self, request):
        try:
            venda = remover_cliente(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"venda": serializar_venda(venda)})


class VendaVendedorView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def put(self, request):
        try:
            venda = selecionar_vendedor(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                vendedor_id=request.data.get("vendedor_id"),
            )
        except VendaValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        vendedor_preselecionado = None
        if venda is None:
            _venda_atual, _cliente_preselecionado, vendedor_preselecionado = venda_atual(request.sysvar_terminal)
        return Response(
            {
                "venda": serializar_venda(venda),
                "vendedor_preselecionado": serializar_vendedor_preselecionado(vendedor_preselecionado),
            }
        )

    def delete(self, request):
        try:
            venda = remover_vendedor(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
            )
        except VendaConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except VendaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "venda": serializar_venda(venda),
                "vendedor_preselecionado": serializar_vendedor_preselecionado(None),
            }
        )


class FormasPagamentoView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        return Response(listar_formas_pagamento(request.sysvar_terminal))


class TiposDespesaPdvView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        hub = request.sysvar_terminal.hub
        tipos = TipoDespesaPdvHub.objects.filter(
            hub=hub,
            presente_retaguarda=True,
            ativo=True,
        ).order_by("descricao", "codigo", "retaguarda_id")

        return Response(
            {
                "tipos_despesa_pdv_versao": hub.tipos_despesa_pdv_versao,
                "tipos_despesa_pdv_sincronizado_em": (
                    hub.tipos_despesa_pdv_sincronizado_em.isoformat()
                    if hub.tipos_despesa_pdv_sincronizado_em
                    else None
                ),
                "total": tipos.count(),
                "tipos_despesa_pdv": [_serializar_tipo_despesa_pdv(tipo) for tipo in tipos],
            }
        )


class TerminalClientesView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        hub = request.sysvar_terminal.hub
        termo = " ".join((request.query_params.get("q") or "").split())
        clientes = ClienteHub.objects.filter(hub=hub).filter(
            Q(presente_retaguarda=True)
            | Q(origem=ClienteHub.ORIGEM_LOCAL, retaguarda_id__isnull=True)
        )
        if termo:
            clientes = _aplicar_busca_clientes(clientes, termo)
        else:
            clientes = clientes.order_by("nome_cliente", "retaguarda_id", "cliente_uuid")

        total = clientes.count()
        clientes = clientes[:CLIENTES_TERMINAL_LIMIT]

        return Response(
            {
                "clientes_versao": hub.clientes_versao,
                "clientes_sincronizado_em": (
                    hub.clientes_sincronizado_em.isoformat()
                    if hub.clientes_sincronizado_em
                    else None
                ),
                "q": termo,
                "total": total,
                "limit": CLIENTES_TERMINAL_LIMIT,
                "clientes": [_serializar_cliente(cliente) for cliente in clientes],
            }
        )

    def post(self, request):
        try:
            dados = validar_payload_cadastro_cliente(request.data)
            cliente = cadastrar_cliente_local(
                request.sysvar_terminal,
                request.sysvar_operador,
                request.sysvar_operador_sessao,
                **dados,
            )
        except ClienteConflictError as exc:
            payload = {"detail": str(exc)}
            if exc.cliente is not None:
                payload["cliente_uuid"] = str(exc.cliente.cliente_uuid)
            return Response(payload, status=status.HTTP_409_CONFLICT)
        except ClienteValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ClienteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"cliente": _serializar_cliente(cliente)}, status=status.HTTP_201_CREATED)


class TerminalVendedoresView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def get(self, request):
        hub = request.sysvar_terminal.hub
        termo = " ".join((request.query_params.get("q") or "").split())
        vendedores = VendedorHub.objects.filter(
            hub=hub,
            presente_retaguarda=True,
            ativo=True,
            situacao="ATIVO",
            participa_vendas=True,
        )
        if termo:
            vendedores = _aplicar_busca_vendedores(vendedores, termo)
        else:
            vendedores = vendedores.order_by("nome", "retaguarda_id")

        total = vendedores.count()
        vendedores = vendedores[:VENDEDORES_TERMINAL_LIMIT]

        return Response(
            {
                "vendedores_versao": hub.vendedores_versao,
                "vendedores_sincronizado_em": (
                    hub.vendedores_sincronizado_em.isoformat()
                    if hub.vendedores_sincronizado_em
                    else None
                ),
                "q": termo,
                "total": total,
                "limit": VENDEDORES_TERMINAL_LIMIT,
                "vendedores": [_serializar_vendedor(vendedor) for vendedor in vendedores],
            }
        )


class VendaItemView(APIView):
    authentication_classes = [TerminalOperadorAuthentication]
    permission_classes = [IsOperadorAuthenticated]

    def post(self, request):
        try:
            venda = adicionar_item(
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

        return Response({"venda": serializar_venda(venda)})


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
            venda, criado = adicionar_pagamento(
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

        return Response(
            {"venda": serializar_venda(venda)},
            status=status.HTTP_201_CREATED if criado else status.HTTP_200_OK,
        )


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


def _aplicar_busca_clientes(queryset, termo):
    digitos = "".join(c for c in termo if c.isdigit())
    busca = (
        Q(nome_cliente__icontains=termo)
        | Q(apelido__icontains=termo)
        | Q(email__icontains=termo)
    )
    if digitos:
        busca |= Q(documento__icontains=digitos) | Q(telefone1__icontains=digitos) | Q(telefone2__icontains=digitos)
    prioridade = Case(
        When(documento=digitos, then=Value(1)) if digitos else When(pk__isnull=True, then=Value(9)),
        When(nome_cliente__istartswith=termo, then=Value(2)),
        When(apelido__istartswith=termo, then=Value(3)),
        When(nome_cliente__icontains=termo, then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
    return queryset.filter(busca).annotate(prioridade_busca=prioridade).order_by(
        "prioridade_busca",
        "nome_cliente",
        "retaguarda_id",
        "cliente_uuid",
    )


def _aplicar_busca_vendedores(queryset, termo):
    busca = (
        Q(matricula__icontains=termo)
        | Q(nome__icontains=termo)
        | Q(apelido__icontains=termo)
    )
    prioridade = Case(
        When(matricula__iexact=termo, then=Value(1)),
        When(nome__istartswith=termo, then=Value(2)),
        When(apelido__istartswith=termo, then=Value(3)),
        When(nome__icontains=termo, then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
    return queryset.filter(busca).annotate(prioridade_busca=prioridade).order_by(
        "prioridade_busca",
        "nome",
        "retaguarda_id",
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


def _serializar_cliente(cliente):
    return {
        "cliente_uuid": str(cliente.cliente_uuid),
        "retaguarda_id": cliente.retaguarda_id,
        "origem": cliente.origem,
        "tipo_pessoa": cliente.tipo_pessoa,
        "documento": cliente.documento,
        "cliente_padrao": cliente.cliente_padrao,
        "nome_cliente": cliente.nome_cliente,
        "apelido": cliente.apelido,
        "telefone1": cliente.telefone1,
        "telefone2": cliente.telefone2,
        "email": cliente.email,
        "aniversario": cliente.aniversario.isoformat() if cliente.aniversario else None,
        "endereco": cliente.endereco,
        "numero": cliente.numero,
        "complemento": cliente.complemento,
        "cep": cliente.cep,
        "bairro": cliente.bairro,
        "cidade": cliente.cidade,
        "estado": cliente.estado,
        "bloqueio": cliente.bloqueio,
        "motivo_bloqueio": cliente.motivo_bloqueio,
        "ativo": cliente.ativo,
        "presente_retaguarda": cliente.presente_retaguarda,
        "pendente_sincronizacao": (
            cliente.origem == ClienteHub.ORIGEM_LOCAL
            and cliente.retaguarda_id is None
        ),
    }


def _serializar_vendedor(vendedor):
    cargo = None
    if vendedor.cargo_retaguarda_id:
        cargo = {
            "id": vendedor.cargo_retaguarda_id,
            "codigo": vendedor.cargo_codigo,
            "descricao": vendedor.cargo_descricao,
        }
    return {
        "id": vendedor.retaguarda_id,
        "matricula": vendedor.matricula,
        "nome": vendedor.nome,
        "apelido": vendedor.apelido,
        "cargo": cargo,
        "comissionado": vendedor.comissionado,
        "comissao_percentual": _decimal_para_string(vendedor.comissao_percentual),
    }


def _serializar_tipo_despesa_pdv(tipo):
    return {
        "id": tipo.retaguarda_id,
        "codigo": tipo.codigo,
        "descricao": tipo.descricao,
        "exige_documento": tipo.exige_documento,
        "natureza": {
            "id": tipo.natureza_retaguarda_id,
            "codigo": tipo.natureza_codigo,
            "descricao": tipo.natureza_descricao,
            "categoria_principal": tipo.natureza_categoria_principal,
            "subcategoria": tipo.natureza_subcategoria,
            "tipo": tipo.natureza_tipo,
            "status": tipo.natureza_status,
            "tipo_natureza": tipo.natureza_tipo_natureza,
            "natureza_operacao": tipo.natureza_operacao,
            "categoria_gerencial": tipo.natureza_categoria_gerencial,
            "movimenta_financeiro": tipo.natureza_movimenta_financeiro,
            "entra_dre": tipo.natureza_entra_dre,
        },
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
