from decimal import Decimal

from django.db import transaction

from core.models import VendaEventoHub, VendaHub, VendaPagamentoHub
from core.services.beneficios import validar_cashback_central
from core.services.vendas import (
    CASHBACK,
    ERRO_OPERACAO_PAGAMENTO_DIVERGENTE,
    ZERO_2,
    VendaConflictError,
    VendaNotFoundError,
    VendaValidationError,
    calcular_pendente,
    calcular_total_pago,
    dados_pagamento,
    money,
    obter_sessao_caixa_terminal,
    obter_venda_terminal_por_uuid_bloqueada,
    registrar_evento,
    tem_troco,
    validar_valor_pagamento,
    validar_uuid_obrigatorio,
)
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


def _client(hub):
    return RetaguardaClient(hub.retaguarda_url)


def consultar_cashback_online_venda(terminal, venda_uuid):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    with transaction.atomic():
        terminal_bloqueado = terminal.__class__.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status != VendaHub.STATUS_ABERTA:
            raise VendaConflictError("Venda não está aberta.")
        if not venda.cliente_retaguarda_id:
            raise VendaConflictError("Cashback online exige cliente identificado na Central.")
        if venda.cliente_padrao:
            raise VendaConflictError("Cashback online não pode ser usado para Consumidor Final.")
    try:
        return _client(terminal.hub).cashback_saldo(token=terminal.hub.retaguarda_token, cliente_id=venda.cliente_retaguarda_id)
    except RetaguardaError as exc:
        raise VendaConflictError(f"Central ONLINE indisponível para consultar Cashback: {exc}") from exc


def adicionar_pagamento_cashback_online(terminal, operador, sessao_operador, *, venda_uuid, operacao_uuid, valor):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    operacao_uuid = validar_uuid_obrigatorio(operacao_uuid, "Operação inválida.")
    valor = validar_valor_pagamento(valor)

    with transaction.atomic():
        terminal_bloqueado = terminal.__class__.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
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
            if (
                pagamento_existente.tipo != CASHBACK
                or pagamento_existente.cashback_origem != VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL
                or money(pagamento_existente.valor) != valor
                or not pagamento_existente.cashback_reserva_id
            ):
                raise VendaConflictError(ERRO_OPERACAO_PAGAMENTO_DIVERGENTE)
            return venda, False

        if venda.vendedor_retaguarda_id is None:
            raise VendaConflictError("Selecione um vendedor antes de registrar pagamentos.")
        if not venda.cliente_retaguarda_id:
            raise VendaConflictError("Cashback online exige cliente identificado na Central.")
        if venda.cliente_padrao:
            raise VendaConflictError("Cashback online não pode ser usado para Consumidor Final.")

        try:
            validar_cashback_central(venda, valor)
        except ValueError as exc:
            raise VendaConflictError(str(exc)) from exc

        total_pago_atual = calcular_total_pago(venda)
        pendente = calcular_pendente(venda, total_pago_atual)
        if pendente <= ZERO_2:
            raise VendaConflictError("Venda já está paga.")
        if tem_troco(venda, total_pago_atual):
            raise VendaConflictError("Venda já possui troco.")
        if valor > pendente:
            raise VendaConflictError("Cashback não pode gerar troco; use apenas o saldo pendente da venda.")

        try:
            resposta = _client(terminal_bloqueado.hub).cashback_reservar_venda(
                token=terminal_bloqueado.hub.retaguarda_token,
                payload={
                    "venda_uuid": str(venda.venda_uuid),
                    "operacao_uuid": str(operacao_uuid),
                    "cliente_id": venda.cliente_retaguarda_id,
                    "valor": f"{valor:.2f}",
                },
            )
        except RetaguardaError as exc:
            raise VendaConflictError(f"Central ONLINE obrigatória para reservar Cashback: {exc}") from exc
        reserva = resposta.get("reserva") or {}
        reserva_id = reserva.get("id")
        if not reserva_id:
            raise VendaConflictError("Reserva de Cashback não retornada pela Central.")
        if int(reserva.get("cliente_id") or 0) != int(venda.cliente_retaguarda_id):
            raise VendaConflictError("Reserva de Cashback pertence a outro cliente.")
        if money(Decimal(str(reserva.get("valor") or "0.00"))) != valor:
            raise VendaConflictError("Valor da reserva de Cashback diverge do pagamento.")
        if str(reserva.get("status") or "") != "RESERVADA":
            raise VendaConflictError("Reserva de Cashback não está ativa.")

        pagamento = VendaPagamentoHub.objects.create(
            operacao_uuid=operacao_uuid,
            venda=venda,
            forma_pagamento=None,
            retaguarda_forma_pagamento_id=None,
            codigo=CASHBACK,
            descricao="Cashback Online",
            tipo=CASHBACK,
            num_parcelas=1,
            gera_recebivel_bancario=False,
            valor=valor,
            autorizacao="",
            cashback_origem=VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL,
            cashback_reserva_id=reserva_id,
            cashback_valor_reservado=valor,
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=terminal_bloqueado,
            operador_inclusao=operador,
            sessao_operador_inclusao=sessao_operador,
        )
        registrar_evento(venda, VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO, terminal_bloqueado, operador, sessao_operador, dados_pagamento(pagamento))
    return venda, True


def liberar_reserva_pagamento_cashback(pagamento):
    if (pagamento.tipo or "").upper() != CASHBACK:
        return
    if pagamento.cashback_origem != VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL or not pagamento.cashback_reserva_id:
        return
    try:
        _client(pagamento.venda.hub).cashback_cancelar_reserva(
            token=pagamento.venda.hub.retaguarda_token,
            payload={
                "reserva_id": pagamento.cashback_reserva_id,
                "venda_uuid": str(pagamento.venda.venda_uuid),
                "operacao_uuid": str(pagamento.operacao_uuid),
            },
        )
    except RetaguardaError as exc:
        raise VendaConflictError(f"Não foi possível liberar reserva de Cashback na Central: {exc}") from exc
