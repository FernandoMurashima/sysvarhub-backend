import re
import uuid
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Sum
from django.utils import timezone

from core.models import (
    CatalogoItemHub,
    ClienteHub,
    ContextoVendaTerminalHub,
    EstoqueMovimentoHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    SessaoCaixaHub,
    Terminal,
    VendaEventoHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
    VendaPagamentoParcelaHub,
    VendedorHub,
)
from core.services.caixa import obter_caixa_terminal, obter_sessao_caixa_aberta
from core.services.beneficios import aplicar_promocao, registrar_beneficios_venda, validar_pagamento_beneficio
from core.services.operadores import serializar_operador
from core.services.nfce import (
    NFCeErroDominio,
    nfce_habilitada_para_hub,
    preparar_nfce_para_venda_finalizada,
    processar_nfce_preparada,
    validar_nfce_para_finalizacao,
)
from core.services.sync import enfileirar_nfce_atualizada, enfileirar_venda_finalizada


ZERO_2 = Decimal("0.00")
QUANTIDADE_DECIMAL = Decimal("1")
QUANTIDADE_ESTOQUE = Decimal("0.001")
VALOR_PAGAMENTO_MAXIMO = Decimal("9999999999999999.99")
DINHEIRO = "DINHEIRO"
ERRO_PAGAMENTO_ALTERAR_VENDA = "Remova os pagamentos antes de alterar a venda."
ERRO_OPERACAO_PAGAMENTO_DIVERGENTE = "Operação de pagamento já utilizada com dados diferentes."


class VendaError(Exception):
    """Erro de domínio controlado para operações de venda local."""


class VendaConflictError(VendaError):
    pass


class SaldoInsuficienteError(VendaConflictError):
    def __init__(self, mensagem, estoque_disponivel):
        super().__init__(mensagem)
        self.estoque_disponivel = estoque_disponivel


class VendaNotFoundError(VendaError):
    pass


class VendaValidationError(VendaError):
    pass


def obter_sessao_caixa_terminal(terminal):
    caixa = obter_caixa_terminal(terminal, exigir_ativo=True)
    sessao = obter_sessao_caixa_aberta(caixa)
    if not sessao:
        raise VendaConflictError("Caixa não está aberto.")
    return sessao


def obter_venda_aberta_terminal(terminal):
    return (
        VendaHub.objects.select_related(
            "hub",
            "sessao_caixa",
            "terminal",
            "operador_criacao",
            "sessao_operador_criacao",
        )
        .prefetch_related("itens")
        .filter(
            hub=terminal.hub,
            terminal=terminal,
            status=VendaHub.STATUS_ABERTA,
        )
        .first()
    )


def venda_atual(terminal):
    obter_sessao_caixa_terminal(terminal)
    venda = obter_venda_aberta_terminal(terminal)
    if venda:
        return venda, None, None
    contexto = obter_contexto_venda_terminal(terminal)
    return (
        None,
        contexto.cliente_preselecionado if contexto else None,
        contexto.vendedor_preselecionado if contexto else None,
    )


def iniciar_venda(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        sessao_caixa = obter_sessao_caixa_terminal(terminal_bloqueado)
        venda, criada = obter_ou_criar_venda_aberta(
            terminal_bloqueado,
            operador,
            sessao_operador,
            sessao_caixa,
        )
        if criada:
            materializar_cliente_preselecionado(venda, terminal_bloqueado)
            materializar_vendedor_preselecionado(venda, terminal_bloqueado)

    return venda, criada


def adicionar_item(terminal, operador, sessao_operador, *, sku_id, quantidade=1):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        if not venda:
            raise VendaConflictError("Inicie a venda antes de incluir produtos.")
        validar_venda_sem_pagamento_ativo(venda)

        quantidade = validar_quantidade(quantidade)
        sku_id = validar_sku_id(sku_id)
        catalogo_item = obter_catalogo_item_bloqueado(terminal_bloqueado.hub, sku_id)
        validar_catalogo_vendavel(catalogo_item)
        validar_disponibilidade(catalogo_item, quantidade)
        item = VendaItemHub.objects.select_for_update().filter(
            venda=venda,
            retaguarda_sku_id=sku_id,
        ).first()
        quantidade_anterior = item.quantidade if item else 0
        quantidade_nova = quantidade_anterior + quantidade

        if item:
            item.quantidade = quantidade_nova
            aplicar_precificacao_item(item, catalogo_item)
            item.save(update_fields=[
                "quantidade",
                "preco_unitario",
                "desconto",
                "total_item",
                "promocao_retaguarda_id",
                "promocao_nome",
                "promocao_tipo",
                "promocao_valor",
                "promocao_acumula_cashback",
                "atualizado_em",
            ])
            tipo_evento = VendaEventoHub.TIPO_ITEM_QUANTIDADE_ALTERADA
        else:
            item = criar_item_venda(venda, catalogo_item, quantidade, operador, sessao_operador, terminal_bloqueado)
            tipo_evento = VendaEventoHub.TIPO_ITEM_ADICIONADO

        recalcular_totais(venda)
        registrar_evento(
            venda,
            tipo_evento,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_item(item, quantidade_anterior, item.quantidade),
        )

    return venda


def alterar_quantidade_item(terminal, operador, sessao_operador, *, item_uuid, quantidade):
    quantidade = validar_quantidade(quantidade)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        if not venda:
            raise VendaNotFoundError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        item = VendaItemHub.objects.select_for_update().filter(venda=venda, item_uuid=item_uuid).first()
        if not item:
            raise VendaNotFoundError("Item da venda não encontrado.")

        catalogo_item = CatalogoItemHub.objects.select_for_update().get(pk=item.catalogo_item_id)
        quantidade_anterior = item.quantidade
        delta = quantidade - quantidade_anterior
        if delta > 0:
            validar_catalogo_vendavel(catalogo_item)
            validar_disponibilidade(catalogo_item, delta)

        item.quantidade = quantidade
        aplicar_precificacao_item(item, catalogo_item)
        item.save(update_fields=[
            "quantidade",
            "preco_unitario",
            "desconto",
            "total_item",
            "promocao_retaguarda_id",
            "promocao_nome",
            "promocao_tipo",
            "promocao_valor",
            "promocao_acumula_cashback",
            "atualizado_em",
        ])
        recalcular_totais(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_ITEM_QUANTIDADE_ALTERADA,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_item(item, quantidade_anterior, item.quantidade),
        )

    return venda


def remover_item(terminal, operador, sessao_operador, *, item_uuid):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        if not venda:
            raise VendaNotFoundError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        item = VendaItemHub.objects.select_for_update().filter(venda=venda, item_uuid=item_uuid).first()
        if not item:
            raise VendaNotFoundError("Item da venda não encontrado.")

        evento_dados = dados_item(item, item.quantidade, 0)
        item.delete()
        recalcular_totais(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_ITEM_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            evento_dados,
        )

    return venda


def selecionar_cliente(terminal, operador, sessao_operador, *, cliente_uuid):
    cliente_uuid = validar_uuid_obrigatorio(cliente_uuid, "Cliente inválido.")

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        sessao_caixa = obter_sessao_caixa_terminal(terminal_bloqueado)
        cliente = obter_cliente_bloqueado(terminal_bloqueado.hub, cliente_uuid)
        validar_cliente_selecionavel(cliente)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        validar_venda_sem_pagamento_ativo(venda)
        if not venda:
            contexto, _created = ContextoVendaTerminalHub.objects.select_for_update().get_or_create(
                terminal=terminal_bloqueado,
            )
            if contexto.cliente_preselecionado_id != cliente.id:
                contexto.cliente_preselecionado = cliente
                contexto.save(update_fields=["cliente_preselecionado", "atualizado_em"])
            return None

        anterior = dados_cliente_evento(venda)
        if venda.cliente_uuid == cliente.cliente_uuid:
            return venda

        aplicar_snapshot_cliente(venda, cliente)
        venda.save(
            update_fields=[
                "cliente_uuid",
                "cliente_retaguarda_id",
                "cliente_tipo_pessoa",
                "cliente_documento",
                "cliente_padrao",
                "cliente_nome",
                "atualizada_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_CLIENTE_SELECIONADO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {
                "cliente_anterior": anterior,
                "cliente_atual": dados_cliente_evento(venda),
            },
        )

    return venda


def remover_cliente(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        if not venda:
            limpar_cliente_preselecionado(terminal_bloqueado)
            return None
        validar_venda_sem_pagamento_ativo(venda)
        if venda.cliente_uuid is None:
            return venda

        anterior = dados_cliente_evento(venda)
        limpar_snapshot_cliente(venda)
        venda.save(
            update_fields=[
                "cliente_uuid",
                "cliente_retaguarda_id",
                "cliente_tipo_pessoa",
                "cliente_documento",
                "cliente_padrao",
                "cliente_nome",
                "atualizada_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_CLIENTE_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {
                "cliente_anterior": anterior,
                "cliente_atual": dados_cliente_evento(venda),
            },
        )

    return venda


def selecionar_vendedor(terminal, operador, sessao_operador, *, vendedor_id):
    vendedor_id = validar_inteiro_positivo(vendedor_id, "Vendedor inválido.")

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        vendedor = obter_vendedor_bloqueado(terminal_bloqueado.hub, vendedor_id)
        validar_vendedor_selecionavel(vendedor)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        validar_venda_sem_pagamento_ativo(venda)
        if not venda:
            contexto, _created = ContextoVendaTerminalHub.objects.select_for_update().get_or_create(
                terminal=terminal_bloqueado,
            )
            if contexto.vendedor_preselecionado_id != vendedor.id:
                contexto.vendedor_preselecionado = vendedor
                contexto.save(update_fields=["vendedor_preselecionado", "atualizado_em"])
            return None

        if venda.vendedor_retaguarda_id == vendedor.retaguarda_id:
            return venda

        anterior = dados_vendedor_evento(venda)
        aplicar_snapshot_vendedor(venda, vendedor)
        salvar_snapshot_vendedor(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_VENDEDOR_SELECIONADO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {
                "vendedor_anterior": anterior,
                "vendedor_atual": dados_vendedor_evento(venda),
            },
        )

    return venda


def remover_vendedor(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal_bloqueada(terminal_bloqueado)
        if not venda:
            limpar_vendedor_preselecionado(terminal_bloqueado)
            return None
        validar_venda_sem_pagamento_ativo(venda)
        if venda.vendedor_retaguarda_id is None:
            return venda

        anterior = dados_vendedor_evento(venda)
        limpar_snapshot_vendedor(venda)
        salvar_snapshot_vendedor(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_VENDEDOR_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {
                "vendedor_anterior": anterior,
                "vendedor_atual": dados_vendedor_evento(venda),
            },
        )

    return venda


def cancelar_venda(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        if not venda:
            raise VendaConflictError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        venda.status = VendaHub.STATUS_CANCELADA
        venda.cancelada_em = timezone.now()
        venda.operador_cancelamento = operador
        venda.sessao_operador_cancelamento = sessao_operador
        venda.terminal_cancelamento = terminal_bloqueado
        venda.chave_venda_aberta_terminal = None
        venda.save(
            update_fields=[
                "status",
                "cancelada_em",
                "operador_cancelamento",
                "sessao_operador_cancelamento",
                "terminal_cancelamento",
                "chave_venda_aberta_terminal",
                "atualizada_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_VENDA_CANCELADA,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {},
        )

    return venda


def listar_formas_pagamento(terminal):
    parcelas_ordenadas = FormaPagamentoParcelaHub.objects.order_by("ordem", "id")
    formas = (
        FormaPagamentoHub.objects.filter(hub=terminal.hub, ativo=True)
        .prefetch_related(Prefetch("parcelas", queryset=parcelas_ordenadas))
        .order_by("codigo", "retaguarda_id")
    )
    return {
        "versao": terminal.hub.formas_pagamento_versao,
        "sincronizado_em": (
            terminal.hub.formas_pagamento_sincronizado_em.isoformat()
            if terminal.hub.formas_pagamento_sincronizado_em
            else None
        ),
        "formas": [serializar_forma_pagamento(forma) for forma in formas],
    }


def adicionar_pagamento(
    terminal,
    operador,
    sessao_operador,
    *,
    venda_uuid,
    operacao_uuid,
    forma_pagamento_id,
    valor,
    autorizacao="",
):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    operacao_uuid = validar_uuid_obrigatorio(operacao_uuid, "Operação inválida.")
    forma_pagamento_id = validar_inteiro_positivo(forma_pagamento_id, "Forma de pagamento inválida.")
    valor = validar_valor_pagamento(valor)
    autorizacao = validar_autorizacao(autorizacao)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status != VendaHub.STATUS_ABERTA:
            raise VendaConflictError("Venda não está aberta.")

        pagamento_existente = (
            VendaPagamentoHub.objects.select_for_update()
            .filter(venda=venda, operacao_uuid=operacao_uuid)
            .first()
        )
        if pagamento_existente:
            validar_retry_pagamento(
                pagamento_existente,
                forma_pagamento_id=forma_pagamento_id,
                valor=valor,
                autorizacao=autorizacao,
            )
            return venda, False

        if venda.vendedor_retaguarda_id is None:
            raise VendaConflictError("Selecione um vendedor antes de registrar pagamentos.")

        parcelas_ordenadas = FormaPagamentoParcelaHub.objects.order_by("ordem", "id")
        forma = (
            FormaPagamentoHub.objects.select_for_update()
            .prefetch_related(Prefetch("parcelas", queryset=parcelas_ordenadas))
            .filter(pk=forma_pagamento_id, hub=terminal_bloqueado.hub, ativo=True)
            .first()
        )
        if not forma:
            raise VendaValidationError("Forma de pagamento inválida.")
        if forma.tef_habilitado:
            raise VendaConflictError("Forma de pagamento exige integração TEF.")
        try:
            validar_pagamento_beneficio(venda, forma, valor, autorizacao)
        except ValueError as exc:
            raise VendaConflictError(str(exc)) from exc

        total_pago_atual = calcular_total_pago(venda)
        pendente = calcular_pendente(venda, total_pago_atual)
        if pendente <= ZERO_2:
            raise VendaConflictError("Venda já está paga.")
        if tem_troco(venda, total_pago_atual):
            raise VendaConflictError("Venda já possui troco.")
        if forma.tipo != DINHEIRO and valor > pendente:
            raise VendaConflictError("Valor do pagamento excede o valor pendente.")

        pagamento = VendaPagamentoHub.objects.create(
            operacao_uuid=operacao_uuid,
            venda=venda,
            forma_pagamento=forma,
            retaguarda_forma_pagamento_id=forma.retaguarda_id,
            codigo=forma.codigo,
            descricao=forma.descricao,
            tipo=forma.tipo,
            num_parcelas=forma.num_parcelas,
            adquirente=forma.adquirente,
            conta_liquidacao_retaguarda_id=forma.conta_liquidacao_retaguarda_id,
            gera_recebivel_bancario=forma.gera_recebivel_bancario,
            prazo_credito_dias=forma.prazo_credito_dias,
            taxa_percentual=forma.taxa_percentual,
            taxa_fixa=forma.taxa_fixa,
            valor=valor,
            autorizacao=autorizacao,
            vale_troca_documento=autorizacao if forma.tipo in ("TROCA", "VALE_TROCA") else "",
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=terminal_bloqueado,
            operador_inclusao=operador,
            sessao_operador_inclusao=sessao_operador,
        )
        for parcela in forma.parcelas.all():
            VendaPagamentoParcelaHub.objects.create(
                pagamento=pagamento,
                ordem=parcela.ordem,
                dias=parcela.dias,
                percentual=parcela.percentual,
                valor_fixo=parcela.valor_fixo,
            )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_pagamento(pagamento),
        )

    return venda, True


def remover_pagamento(terminal, operador, sessao_operador, *, pagamento_uuid):
    pagamento_uuid = validar_uuid_obrigatorio(pagamento_uuid, "Pagamento inválido.")
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        pagamento = (
            VendaPagamentoHub.objects.select_for_update()
            .select_related("venda", "venda__terminal", "venda__hub")
            .filter(pagamento_uuid=pagamento_uuid, venda__hub=terminal_bloqueado.hub)
            .first()
        )
        if not pagamento:
            raise VendaNotFoundError("Pagamento não encontrado.")
        venda = pagamento.venda
        if venda.terminal_id != terminal_bloqueado.id:
            raise VendaNotFoundError("Pagamento não encontrado.")
        if venda.status == VendaHub.STATUS_FINALIZADA:
            raise VendaConflictError("Venda finalizada não permite remover pagamento.")
        if pagamento.status == VendaPagamentoHub.STATUS_REMOVIDO:
            return venda

        pagamento.status = VendaPagamentoHub.STATUS_REMOVIDO
        pagamento.removido_em = timezone.now()
        pagamento.terminal_remocao = terminal_bloqueado
        pagamento.operador_remocao = operador
        pagamento.sessao_operador_remocao = sessao_operador
        pagamento.save(
            update_fields=[
                "status",
                "removido_em",
                "terminal_remocao",
                "operador_remocao",
                "sessao_operador_remocao",
                "atualizado_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_PAGAMENTO_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_pagamento(pagamento),
        )
    return venda


def finalizar_venda(terminal, operador, sessao_operador, *, venda_uuid):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    nfce_para_processar = None
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status == VendaHub.STATUS_FINALIZADA:
            return venda
        if venda.status == VendaHub.STATUS_CANCELADA:
            raise VendaConflictError("Venda cancelada não pode ser finalizada.")
        sessao_caixa = obter_sessao_caixa_terminal(terminal_bloqueado)
        if venda.sessao_caixa_id != sessao_caixa.id:
            raise VendaConflictError("Venda não pertence ao Caixa aberto.")
        if venda.vendedor_retaguarda_id is None:
            raise VendaConflictError("Selecione um vendedor antes de finalizar a venda.")

        itens = list(
            VendaItemHub.objects.select_for_update()
            .filter(venda=venda)
            .select_related("catalogo_item")
            .order_by("catalogo_item_id", "id")
        )
        if not itens:
            raise VendaConflictError("Venda sem itens.")
        if venda.total <= ZERO_2:
            raise VendaConflictError("Venda sem itens.")

        pagamentos = list(
            VendaPagamentoHub.objects.select_for_update().filter(
                venda=venda,
                status=VendaPagamentoHub.STATUS_ATIVO,
            )
        )
        if not pagamentos:
            raise VendaConflictError("Venda sem pagamento.")

        total_pago = calcular_total_pago_lista(pagamentos)
        if total_pago < venda.total:
            raise VendaConflictError("Pagamento insuficiente.")
        if total_pago > venda.total and not any(pagamento.tipo == DINHEIRO for pagamento in pagamentos):
            raise VendaConflictError("Valor do pagamento excede o valor pendente.")
        for pagamento in pagamentos:
            try:
                validar_pagamento_beneficio(venda, pagamento.forma_pagamento, pagamento.valor, pagamento.autorizacao)
            except ValueError as exc:
                raise VendaConflictError(str(exc)) from exc

        emitir_nfce = nfce_habilitada_para_hub(venda.hub)
        if emitir_nfce:
            try:
                validar_nfce_para_finalizacao(venda)
            except NFCeErroDominio as exc:
                raise VendaConflictError(exc.codigo) from exc

        catalogo_ids = sorted({item.catalogo_item_id for item in itens})
        catalogo_por_id = {
            item.id: item
            for item in CatalogoItemHub.objects.select_for_update().filter(id__in=catalogo_ids).order_by("id")
        }
        for item in itens:
            catalogo_item = catalogo_por_id[item.catalogo_item_id]
            disponivel = calcular_disponivel_local(catalogo_item, excluir_venda=venda)
            if Decimal(item.quantidade) > disponivel:
                raise SaldoInsuficienteError("Saldo disponível insuficiente.", disponivel)

        for item in itens:
            EstoqueMovimentoHub.objects.get_or_create(
                venda_item=item,
                defaults={
                    "hub": venda.hub,
                    "venda": venda,
                    "catalogo_item": catalogo_por_id[item.catalogo_item_id],
                    "retaguarda_produto_id": item.retaguarda_produto_id,
                    "retaguarda_sku_id": item.retaguarda_sku_id,
                    "ean13": item.ean13,
                    "referencia": item.referencia,
                    "tipo": EstoqueMovimentoHub.TIPO_SAIDA_VENDA,
                    "quantidade": Decimal(item.quantidade).quantize(QUANTIDADE_ESTOQUE),
                },
            )
        try:
            registrar_beneficios_venda(venda, pagamentos)
        except ValueError as exc:
            raise VendaConflictError(str(exc)) from exc

        agora = timezone.now()
        troco = calcular_troco(venda, total_pago)
        venda.status = VendaHub.STATUS_FINALIZADA
        venda.finalizada_em = agora
        venda.terminal_finalizacao = terminal_bloqueado
        venda.operador_finalizacao = operador
        venda.sessao_operador_finalizacao = sessao_operador
        venda.valor_recebido = total_pago
        venda.troco = troco
        venda.chave_venda_aberta_terminal = None
        venda.save(
            update_fields=[
                "status",
                "finalizada_em",
                "terminal_finalizacao",
                "operador_finalizacao",
                "sessao_operador_finalizacao",
                "valor_recebido",
                "troco",
                "chave_venda_aberta_terminal",
                "atualizada_em",
            ]
        )
        if not VendaEventoHub.objects.filter(venda=venda, tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).exists():
            registrar_evento(
                venda,
                VendaEventoHub.TIPO_VENDA_FINALIZADA,
                terminal_bloqueado,
                operador,
                sessao_operador,
                {
                    "valor_recebido": f"{total_pago:.2f}",
                    "troco": f"{troco:.2f}",
                },
            )
        if emitir_nfce:
            nfce_para_processar = preparar_nfce_para_venda_finalizada(venda)
        transaction.on_commit(lambda venda_id=venda.pk: enfileirar_venda_finalizada(VendaHub.objects.get(pk=venda_id)))

    if nfce_para_processar is not None:
        processar_nfce_preparada(nfce_para_processar)
        transaction.on_commit(lambda nfce_id=nfce_para_processar.pk: enfileirar_nfce_atualizada(nfce_para_processar.__class__.objects.get(pk=nfce_id)))
        venda = VendaHub.objects.get(pk=venda.pk)
    return venda


def existe_venda_aberta_sessao_caixa(sessao_caixa):
    return VendaHub.objects.filter(
        sessao_caixa=sessao_caixa,
        status=VendaHub.STATUS_ABERTA,
    ).exists()


def obter_ou_criar_venda_aberta(terminal, operador, sessao_operador, sessao_caixa):
    venda = VendaHub.objects.select_for_update().filter(
        hub=terminal.hub,
        terminal=terminal,
        status=VendaHub.STATUS_ABERTA,
    ).first()
    if venda:
        return venda, False

    venda = VendaHub(
        hub=terminal.hub,
        sessao_caixa=sessao_caixa,
        terminal=terminal,
        status=VendaHub.STATUS_ABERTA,
        chave_venda_aberta_terminal=terminal.pk,
        operador_criacao=operador,
        sessao_operador_criacao=sessao_operador,
        subtotal=ZERO_2,
        desconto_itens=ZERO_2,
        desconto_geral=ZERO_2,
        total=ZERO_2,
    )
    try:
        with transaction.atomic():
            venda.save()
    except IntegrityError as exc:
        venda = VendaHub.objects.select_for_update().filter(
            hub=terminal.hub,
            terminal=terminal,
            status=VendaHub.STATUS_ABERTA,
        ).first()
        if venda:
            return venda, False
        raise exc

    registrar_evento(
        venda,
        VendaEventoHub.TIPO_VENDA_CRIADA,
        terminal,
        operador,
        sessao_operador,
        {},
    )
    return venda, True


def obter_contexto_venda_terminal(terminal):
    return (
        ContextoVendaTerminalHub.objects.select_related(
            "cliente_preselecionado",
            "vendedor_preselecionado",
        )
        .filter(terminal=terminal)
        .first()
    )


def materializar_cliente_preselecionado(venda, terminal):
    contexto = (
        ContextoVendaTerminalHub.objects.select_for_update()
        .select_related("cliente_preselecionado")
        .filter(terminal=terminal)
        .first()
    )
    if not contexto:
        return

    cliente = contexto.cliente_preselecionado
    if cliente is not None:
        cliente = obter_cliente_bloqueado(terminal.hub, cliente.cliente_uuid)
        validar_cliente_selecionavel(cliente)
        aplicar_snapshot_cliente(venda, cliente)
        venda.save(
            update_fields=[
                "cliente_uuid",
                "cliente_retaguarda_id",
                "cliente_tipo_pessoa",
                "cliente_documento",
                "cliente_padrao",
                "cliente_nome",
                "atualizada_em",
            ]
        )
    contexto.cliente_preselecionado = None
    contexto.save(update_fields=["cliente_preselecionado", "atualizado_em"])
    remover_contexto_vazio(contexto)


def materializar_vendedor_preselecionado(venda, terminal):
    contexto = (
        ContextoVendaTerminalHub.objects.select_for_update()
        .select_related("vendedor_preselecionado")
        .filter(terminal=terminal)
        .first()
    )
    if not contexto:
        return

    vendedor = contexto.vendedor_preselecionado
    if vendedor is not None:
        vendedor = obter_vendedor_bloqueado(terminal.hub, vendedor.retaguarda_id)
        validar_vendedor_selecionavel(vendedor)
        aplicar_snapshot_vendedor(venda, vendedor)
        salvar_snapshot_vendedor(venda)
    contexto.vendedor_preselecionado = None
    contexto.save(update_fields=["vendedor_preselecionado", "atualizado_em"])
    remover_contexto_vazio(contexto)


def limpar_contexto_venda_terminal(terminal):
    ContextoVendaTerminalHub.objects.filter(terminal=terminal).delete()


def limpar_cliente_preselecionado(terminal):
    contexto = ContextoVendaTerminalHub.objects.select_for_update().filter(terminal=terminal).first()
    if not contexto:
        return
    if contexto.cliente_preselecionado_id is not None:
        contexto.cliente_preselecionado = None
        contexto.save(update_fields=["cliente_preselecionado", "atualizado_em"])
    remover_contexto_vazio(contexto)


def limpar_vendedor_preselecionado(terminal):
    contexto = ContextoVendaTerminalHub.objects.select_for_update().filter(terminal=terminal).first()
    if not contexto:
        return
    if contexto.vendedor_preselecionado_id is not None:
        contexto.vendedor_preselecionado = None
        contexto.save(update_fields=["vendedor_preselecionado", "atualizado_em"])
    remover_contexto_vazio(contexto)


def remover_contexto_vazio(contexto):
    if contexto.cliente_preselecionado_id is None and contexto.vendedor_preselecionado_id is None:
        contexto.delete()


def obter_venda_aberta_terminal_bloqueada(terminal):
    return (
        VendaHub.objects.select_for_update()
        .select_related(
            "hub",
            "sessao_caixa",
            "terminal",
            "operador_criacao",
            "sessao_operador_criacao",
        )
        .filter(
            hub=terminal.hub,
            terminal=terminal,
            status=VendaHub.STATUS_ABERTA,
        )
        .first()
    )


def obter_catalogo_item_bloqueado(hub, sku_id):
    try:
        return CatalogoItemHub.objects.select_for_update().get(hub=hub, retaguarda_sku_id=sku_id)
    except CatalogoItemHub.DoesNotExist as exc:
        raise VendaValidationError("Produto não encontrado no catálogo local.") from exc


def obter_cliente_bloqueado(hub, cliente_uuid):
    cliente = (
        ClienteHub.objects.select_for_update()
        .filter(hub=hub, cliente_uuid=cliente_uuid)
        .first()
    )
    if not cliente:
        raise VendaValidationError("Cliente não encontrado no cadastro local.")
    return cliente


def obter_vendedor_bloqueado(hub, vendedor_id):
    vendedor = (
        VendedorHub.objects.select_for_update()
        .filter(hub=hub, retaguarda_id=vendedor_id)
        .first()
    )
    if not vendedor:
        raise VendaValidationError("Vendedor não está disponível para venda.")
    return vendedor


def validar_cliente_selecionavel(cliente):
    if not cliente.ativo:
        raise VendaConflictError("Cliente inativo no cadastro local.")
    if cliente.bloqueio:
        raise VendaConflictError("Cliente bloqueado no cadastro local.")
    if not cliente.presente_retaguarda and not (
        cliente.origem == ClienteHub.ORIGEM_LOCAL and cliente.retaguarda_id is None
    ):
        raise VendaConflictError("Cliente indisponível no cadastro local.")


def validar_vendedor_selecionavel(vendedor):
    if not (
        vendedor.presente_retaguarda
        and vendedor.ativo
        and vendedor.situacao == "ATIVO"
        and vendedor.participa_vendas
    ):
        raise VendaValidationError("Vendedor não está disponível para venda.")


def aplicar_snapshot_cliente(venda, cliente):
    venda.cliente_uuid = cliente.cliente_uuid
    venda.cliente_retaguarda_id = cliente.retaguarda_id
    venda.cliente_tipo_pessoa = cliente.tipo_pessoa
    venda.cliente_documento = cliente.documento
    venda.cliente_padrao = cliente.cliente_padrao
    venda.cliente_nome = cliente.nome_cliente


def limpar_snapshot_cliente(venda):
    venda.cliente_uuid = None
    venda.cliente_retaguarda_id = None
    venda.cliente_tipo_pessoa = ""
    venda.cliente_documento = None
    venda.cliente_padrao = False
    venda.cliente_nome = ""


def aplicar_snapshot_vendedor(venda, vendedor):
    venda.vendedor_retaguarda_id = vendedor.retaguarda_id
    venda.vendedor_matricula = vendedor.matricula
    venda.vendedor_nome = vendedor.nome
    venda.vendedor_apelido = vendedor.apelido
    venda.vendedor_cargo_retaguarda_id = vendedor.cargo_retaguarda_id
    venda.vendedor_cargo_codigo = vendedor.cargo_codigo
    venda.vendedor_cargo_descricao = vendedor.cargo_descricao
    venda.vendedor_comissionado = vendedor.comissionado
    venda.vendedor_comissao_percentual = vendedor.comissao_percentual


def limpar_snapshot_vendedor(venda):
    venda.vendedor_retaguarda_id = None
    venda.vendedor_matricula = ""
    venda.vendedor_nome = ""
    venda.vendedor_apelido = ""
    venda.vendedor_cargo_retaguarda_id = None
    venda.vendedor_cargo_codigo = ""
    venda.vendedor_cargo_descricao = ""
    venda.vendedor_comissionado = False
    venda.vendedor_comissao_percentual = ZERO_2


def salvar_snapshot_vendedor(venda):
    venda.save(
        update_fields=[
            "vendedor_retaguarda_id",
            "vendedor_matricula",
            "vendedor_nome",
            "vendedor_apelido",
            "vendedor_cargo_retaguarda_id",
            "vendedor_cargo_codigo",
            "vendedor_cargo_descricao",
            "vendedor_comissionado",
            "vendedor_comissao_percentual",
            "atualizada_em",
        ]
    )


def validar_catalogo_vendavel(item):
    if not item.ativo:
        raise VendaConflictError("Produto inativo no catálogo local.")
    if not item.vendavel:
        raise VendaConflictError("Produto não vendável.")
    if item.preco_venda is None:
        raise VendaConflictError("Produto sem preço de venda.")


def validar_quantidade(quantidade):
    valor = validar_inteiro_positivo(quantidade, "Quantidade inválida.")
    return valor


def validar_sku_id(sku_id):
    valor = validar_inteiro_positivo(sku_id, "Produto inválido.")
    return valor


def validar_inteiro_positivo(valor, mensagem):
    if valor is None or isinstance(valor, bool):
        raise VendaValidationError(mensagem)
    if isinstance(valor, int):
        inteiro = valor
    elif isinstance(valor, str) and valor.isdecimal():
        inteiro = int(valor)
    else:
        raise VendaValidationError(mensagem)

    if inteiro <= 0:
        raise VendaValidationError(mensagem)
    return inteiro


def validar_disponibilidade(catalogo_item, quantidade_adicional):
    disponivel = calcular_disponivel_local(catalogo_item)
    if Decimal(quantidade_adicional) > disponivel:
        raise SaldoInsuficienteError("Saldo disponível insuficiente.", disponivel)


def calcular_disponivel_local(catalogo_item, *, excluir_venda=None):
    reservas = calcular_reserva_sku(
        catalogo_item.hub,
        catalogo_item.retaguarda_sku_id,
        excluir_venda=excluir_venda,
    )
    movimentos = calcular_movimento_sku(catalogo_item.hub, catalogo_item.retaguarda_sku_id)
    return catalogo_item.estoque_disponivel - movimentos - reservas


def calcular_reserva_sku(hub, sku_id, *, excluir_venda=None):
    queryset = VendaItemHub.objects.filter(
        venda__hub=hub,
        venda__status=VendaHub.STATUS_ABERTA,
        retaguarda_sku_id=sku_id,
    )
    if excluir_venda is not None:
        queryset = queryset.exclude(venda=excluir_venda)
    total = queryset.aggregate(total=Sum("quantidade")).get("total") or 0
    return Decimal(total)


def calcular_movimento_sku(hub, sku_id):
    total = (
        EstoqueMovimentoHub.objects.filter(
            hub=hub,
            retaguarda_sku_id=sku_id,
            reconciliado_em__isnull=True,
        ).aggregate(total=Sum("quantidade")).get("total")
        or Decimal("0.000")
    )
    return Decimal(total)


def criar_item_venda(venda, catalogo_item, quantidade, operador, sessao_operador, terminal):
    preco_unitario, desconto, promo = aplicar_promocao(catalogo_item, quantidade)
    return VendaItemHub.objects.create(
        venda=venda,
        catalogo_item=catalogo_item,
        retaguarda_produto_id=catalogo_item.retaguarda_produto_id,
        retaguarda_sku_id=catalogo_item.retaguarda_sku_id,
        ean13=catalogo_item.ean13,
        referencia=catalogo_item.referencia,
        codigo_item_ref=catalogo_item.codigo_item_ref,
        descricao=catalogo_item.descricao,
        descricao_reduzida=catalogo_item.descricao_reduzida,
        cor_descricao=catalogo_item.cor_descricao,
        tamanho_descricao=catalogo_item.tamanho_descricao,
        unidade_codigo=catalogo_item.unidade_codigo,
        quantidade=quantidade,
        preco_unitario=preco_unitario,
        desconto=desconto,
        total_item=calcular_total_item(quantidade, preco_unitario, desconto),
        promocao_retaguarda_id=promo.get("promocao_retaguarda_id"),
        promocao_nome=promo.get("promocao_nome", ""),
        promocao_tipo=promo.get("promocao_tipo", ""),
        promocao_valor=promo.get("promocao_valor", ZERO_2),
        promocao_acumula_cashback=promo.get("promocao_acumula_cashback", True),
        fiscal=deepcopy(catalogo_item.fiscal or {}),
        operador_inclusao=operador,
        sessao_operador_inclusao=sessao_operador,
        terminal_inclusao=terminal,
    )


def aplicar_precificacao_item(item, catalogo_item):
    preco_unitario, desconto, promo = aplicar_promocao(catalogo_item, item.quantidade)
    item.preco_unitario = preco_unitario
    item.desconto = desconto
    item.total_item = calcular_total_item(item.quantidade, preco_unitario, desconto)
    item.promocao_retaguarda_id = promo.get("promocao_retaguarda_id")
    item.promocao_nome = promo.get("promocao_nome", "")
    item.promocao_tipo = promo.get("promocao_tipo", "")
    item.promocao_valor = promo.get("promocao_valor", ZERO_2)
    item.promocao_acumula_cashback = promo.get("promocao_acumula_cashback", True)


def calcular_total_item(quantidade, preco_unitario, desconto):
    total = Decimal(quantidade) * preco_unitario - desconto
    return money(total)


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recalcular_totais(venda):
    subtotal = venda.itens.aggregate(total=Sum("total_item")).get("total") or ZERO_2
    venda.subtotal = money(subtotal)
    venda.desconto_itens = ZERO_2
    venda.desconto_geral = ZERO_2
    venda.total = venda.subtotal
    venda.save(update_fields=["subtotal", "desconto_itens", "desconto_geral", "total", "atualizada_em"])


def validar_venda_sem_pagamento_ativo(venda):
    if venda and venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).exists():
        raise VendaConflictError(ERRO_PAGAMENTO_ALTERAR_VENDA)


def validar_uuid_obrigatorio(valor, mensagem):
    if valor in (None, "") or isinstance(valor, bool):
        raise VendaValidationError(mensagem)
    try:
        return uuid.UUID(str(valor))
    except (TypeError, ValueError) as exc:
        raise VendaValidationError(mensagem) from exc


def validar_valor_pagamento(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"^\d+\.\d{2}$", valor):
        raise VendaValidationError("Valor do pagamento inválido.")
    try:
        decimal = Decimal(valor)
    except InvalidOperation as exc:
        raise VendaValidationError("Valor do pagamento inválido.") from exc
    if not decimal.is_finite() or decimal <= ZERO_2 or decimal > VALOR_PAGAMENTO_MAXIMO:
        raise VendaValidationError("Valor do pagamento inválido.")
    return money(decimal)


def validar_autorizacao(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise VendaValidationError("Autorização inválida.")
    texto = valor.strip()
    if len(texto) > 120:
        raise VendaValidationError("Autorização inválida.")
    return texto


def validar_retry_pagamento(pagamento, *, forma_pagamento_id, valor, autorizacao):
    if (
        pagamento.forma_pagamento_id != forma_pagamento_id
        or pagamento.valor != valor
        or pagamento.autorizacao != autorizacao
    ):
        raise VendaConflictError(ERRO_OPERACAO_PAGAMENTO_DIVERGENTE)


def obter_venda_terminal_por_uuid_bloqueada(terminal, venda_uuid):
    venda = (
        VendaHub.objects.select_for_update()
        .select_related("hub", "terminal", "sessao_caixa", "operador_criacao")
        .filter(venda_uuid=venda_uuid, hub=terminal.hub, terminal=terminal)
        .first()
    )
    if not venda:
        raise VendaNotFoundError("Venda não encontrada.")
    return venda


def calcular_total_pago(venda):
    total = (
        venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO)
        .aggregate(total=Sum("valor"))
        .get("total")
        or ZERO_2
    )
    return money(total)


def calcular_total_pago_lista(pagamentos):
    total = sum((pagamento.valor for pagamento in pagamentos), ZERO_2)
    return money(total)


def calcular_pendente(venda, total_pago=None):
    if total_pago is None:
        total_pago = calcular_total_pago(venda)
    pendente = venda.total - total_pago
    return money(max(pendente, ZERO_2))


def calcular_troco(venda, total_pago=None):
    if total_pago is None:
        total_pago = calcular_total_pago(venda)
    troco = total_pago - venda.total
    return money(max(troco, ZERO_2))


def tem_troco(venda, total_pago=None):
    return calcular_troco(venda, total_pago) > ZERO_2


def registrar_evento(venda, tipo, terminal, operador, sessao_operador, dados):
    VendaEventoHub.objects.create(
        venda=venda,
        tipo=tipo,
        terminal=terminal,
        operador=operador,
        sessao_operador=sessao_operador,
        dados=dados,
    )


def dados_item(item, quantidade_anterior, quantidade_nova):
    return {
        "sku_id": item.retaguarda_sku_id,
        "item_uuid": str(item.item_uuid),
        "quantidade_anterior": quantidade_anterior,
        "quantidade_nova": quantidade_nova,
        "preco_unitario": f"{item.preco_unitario:.4f}",
        "total_item": f"{item.total_item:.2f}",
        "promocao": {
            "id": item.promocao_retaguarda_id,
            "nome": item.promocao_nome,
            "tipo": item.promocao_tipo,
            "valor": f"{item.promocao_valor:.4f}",
            "acumula_cashback": item.promocao_acumula_cashback,
        } if item.promocao_retaguarda_id else None,
    }


def dados_pagamento(pagamento):
    return {
        "pagamento_uuid": str(pagamento.pagamento_uuid),
        "forma_id": pagamento.forma_pagamento_id,
        "codigo": pagamento.codigo,
        "tipo": pagamento.tipo,
        "valor": f"{pagamento.valor:.2f}",
        "status": pagamento.status,
    }


def dados_cliente_evento(venda):
    if venda.cliente_uuid is None:
        return {
            "cliente_uuid": None,
            "retaguarda_id": None,
        }
    return {
        "cliente_uuid": str(venda.cliente_uuid),
        "retaguarda_id": venda.cliente_retaguarda_id,
    }


def dados_vendedor_evento(venda):
    return serializar_vendedor_venda(venda)


def serializar_venda(venda):
    if venda is None:
        return None

    itens = venda.itens.select_related("catalogo_item").order_by("criado_em", "id")
    pagamentos = venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).order_by("criado_em", "id")
    total_pago = calcular_total_pago(venda)
    pendente = calcular_pendente(venda, total_pago)
    troco = venda.troco if venda.status == VendaHub.STATUS_FINALIZADA else calcular_troco(venda, total_pago)
    return {
        "uuid": str(venda.venda_uuid),
        "status": venda.status,
        "criada_em": venda.criada_em.isoformat(),
        "subtotal": f"{venda.subtotal:.2f}",
        "desconto_itens": f"{venda.desconto_itens:.2f}",
        "desconto_geral": f"{venda.desconto_geral:.2f}",
        "total": f"{venda.total:.2f}",
        "total_pago": f"{total_pago:.2f}",
        "pendente": f"{pendente:.2f}",
        "troco": f"{troco:.2f}",
        "cliente": serializar_cliente_venda(venda),
        "vendedor": serializar_vendedor_venda(venda),
        "operador_criacao": serializar_operador(venda.operador_criacao),
        "itens": [serializar_item(item) for item in itens],
        "pagamentos": [serializar_pagamento(pagamento) for pagamento in pagamentos],
        "fiscal": serializar_fiscal_venda(venda),
    }


def serializar_fiscal_venda(venda):
    if not nfce_habilitada_para_hub(venda.hub):
        return {"emite_nfce": False}
    try:
        nfce = venda.nfce
    except Exception:
        return {"emite_nfce": True, "mensagem": "NFCE_AINDA_NAO_GERADA"}
    return {
        "emite_nfce": True,
        "nfce_uuid": str(nfce.nfce_uuid),
        "status": nfce.status,
        "chave_acesso": nfce.chave_acesso,
        "serie": nfce.serie,
        "numero": nfce.numero,
        "tipo_emissao": nfce.tipo_emissao,
        "contingencia": nfce.status == nfce.STATUS_CONTINGENCIA,
        "mensagem": nfce.mensagem_retorno,
    }


def serializar_cliente_venda(venda):
    if venda.cliente_uuid is None:
        return None
    return {
        "cliente_uuid": str(venda.cliente_uuid),
        "retaguarda_id": venda.cliente_retaguarda_id,
        "tipo_pessoa": venda.cliente_tipo_pessoa,
        "documento": venda.cliente_documento,
        "cliente_padrao": venda.cliente_padrao,
        "nome_cliente": venda.cliente_nome,
    }


def serializar_cliente_preselecionado(cliente):
    if cliente is None:
        return None
    return {
        "cliente_uuid": str(cliente.cliente_uuid),
        "retaguarda_id": cliente.retaguarda_id,
        "tipo_pessoa": cliente.tipo_pessoa,
        "documento": cliente.documento,
        "cliente_padrao": cliente.cliente_padrao,
        "nome_cliente": cliente.nome_cliente,
    }


def serializar_vendedor_venda(venda):
    if venda is None or venda.vendedor_retaguarda_id is None:
        return None
    return {
        "id": venda.vendedor_retaguarda_id,
        "matricula": venda.vendedor_matricula,
        "nome": venda.vendedor_nome,
        "apelido": venda.vendedor_apelido,
        "cargo": serializar_cargo_vendedor(
            venda.vendedor_cargo_retaguarda_id,
            venda.vendedor_cargo_codigo,
            venda.vendedor_cargo_descricao,
        ),
        "comissionado": venda.vendedor_comissionado,
        "comissao_percentual": f"{venda.vendedor_comissao_percentual:.2f}",
    }


def serializar_vendedor_preselecionado(vendedor):
    if vendedor is None:
        return None
    return {
        "id": vendedor.retaguarda_id,
        "matricula": vendedor.matricula,
        "nome": vendedor.nome,
        "apelido": vendedor.apelido,
        "cargo": serializar_cargo_vendedor(
            vendedor.cargo_retaguarda_id,
            vendedor.cargo_codigo,
            vendedor.cargo_descricao,
        ),
        "comissionado": vendedor.comissionado,
        "comissao_percentual": f"{vendedor.comissao_percentual:.2f}",
    }


def serializar_cargo_vendedor(cargo_id, codigo, descricao):
    if not cargo_id:
        return None
    return {
        "id": cargo_id,
        "codigo": codigo,
        "descricao": descricao,
    }


def serializar_item(item):
    return {
        "uuid": str(item.item_uuid),
        "produto_id": item.retaguarda_produto_id,
        "sku_id": item.retaguarda_sku_id,
        "ean13": item.ean13 or None,
        "referencia": item.referencia,
        "codigo_item_ref": item.codigo_item_ref,
        "descricao": item.descricao,
        "descricao_reduzida": item.descricao_reduzida,
        "cor": item.cor_descricao,
        "tamanho": item.tamanho_descricao,
        "unidade": item.unidade_codigo,
        "quantidade": item.quantidade,
        "preco_unitario": f"{item.preco_unitario:.4f}",
        "desconto": f"{item.desconto:.2f}",
        "total_item": f"{item.total_item:.2f}",
        "promocao": {
            "id": item.promocao_retaguarda_id,
            "nome": item.promocao_nome,
            "tipo": item.promocao_tipo,
            "valor": f"{item.promocao_valor:.4f}",
            "acumula_cashback": item.promocao_acumula_cashback,
        } if item.promocao_retaguarda_id else None,
    }


def serializar_pagamento(pagamento):
    return {
        "uuid": str(pagamento.pagamento_uuid),
        "forma_pagamento_id": pagamento.forma_pagamento_id,
        "forma_retaguarda_id": pagamento.retaguarda_forma_pagamento_id,
        "codigo": pagamento.codigo,
        "descricao": pagamento.descricao,
        "tipo": pagamento.tipo,
        "num_parcelas": pagamento.num_parcelas,
        "valor": f"{pagamento.valor:.2f}",
        "autorizacao": pagamento.autorizacao,
        "vale_troca_documento": pagamento.vale_troca_documento,
        "origem_captura": pagamento.origem_captura,
        "criado_em": pagamento.criado_em.isoformat(),
    }


def serializar_forma_pagamento(forma):
    parcelas = forma.parcelas.all()
    return {
        "id": forma.id,
        "retaguarda_id": forma.retaguarda_id,
        "codigo": forma.codigo,
        "descricao": forma.descricao,
        "tipo": forma.tipo,
        "num_parcelas": forma.num_parcelas,
        "tef_habilitado": forma.tef_habilitado,
        "parcelas": [
            {
                "ordem": parcela.ordem,
                "dias": parcela.dias,
                "percentual": f"{parcela.percentual:.6f}" if parcela.percentual is not None else None,
                "valor_fixo": f"{parcela.valor_fixo:.2f}" if parcela.valor_fixo is not None else None,
            }
            for parcela in parcelas
        ],
    }
