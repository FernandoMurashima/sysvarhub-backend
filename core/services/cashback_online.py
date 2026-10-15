import uuid
from decimal import Decimal, InvalidOperation

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


def _cancelar_reserva_confirmada(hub, *, reserva_id, venda_uuid, operacao_uuid):
    _client(hub).cashback_cancelar_reserva(
        token=hub.retaguarda_token,
        payload={
            "reserva_id": reserva_id,
            "venda_uuid": str(venda_uuid),
            "operacao_uuid": str(operacao_uuid),
        },
    )


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
                or pagamento_existente.status != VendaPagamentoHub.STATUS_ATIVO
            ):
                raise VendaConflictError(ERRO_OPERACAO_PAGAMENTO_DIVERGENTE)
            validar_contexto_pagamento_cashback(pagamento_existente)
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

        resposta = reservar_na_central(terminal_bloqueado.hub, {
            "venda_uuid": str(venda.venda_uuid),
            "operacao_uuid": str(operacao_uuid),
            "cliente_id": venda.cliente_retaguarda_id,
            "valor": f"{valor:.2f}",
            "total_venda": f"{money(venda.total):.2f}",
        })
        autorizacao = validar_resposta_reserva(resposta, venda, operacao_uuid, valor)
        reserva_id = autorizacao["id"]

        try:
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
            dados_evento = dados_pagamento(pagamento)
            dados_evento["cashback_autorizacao"] = autorizacao
            registrar_evento(venda, VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO, terminal_bloqueado, operador, sessao_operador, dados_evento)
        except Exception as exc:
            transaction.set_rollback(True)
            try:
                _cancelar_reserva_confirmada(
                    terminal_bloqueado.hub,
                    reserva_id=reserva_id,
                    venda_uuid=venda.venda_uuid,
                    operacao_uuid=operacao_uuid,
                )
            except RetaguardaError as cancel_exc:
                raise VendaConflictError(
                    "Falha ao registrar pagamento de Cashback online após reserva confirmada. "
                    f"Reserva {reserva_id} da operação {operacao_uuid} não foi cancelada automaticamente: {cancel_exc}. "
                    f"Falha original: {exc}"
                ) from exc
            raise VendaConflictError(
                "Falha ao registrar pagamento de Cashback online após reserva confirmada. "
                f"Reserva {reserva_id} da operação {operacao_uuid} foi cancelada na Central. "
                f"Falha original: {exc}"
            ) from exc
    return venda, True


def liberar_reserva_pagamento_cashback(pagamento):
    if (pagamento.tipo or "").upper() != CASHBACK:
        return
    if pagamento.cashback_origem != VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL or not pagamento.cashback_reserva_id:
        return
    try:
        _cancelar_reserva_confirmada(
            pagamento.venda.hub,
            reserva_id=pagamento.cashback_reserva_id,
            venda_uuid=pagamento.venda.venda_uuid,
            operacao_uuid=pagamento.operacao_uuid,
        )
    except RetaguardaError as exc:
        raise VendaConflictError(f"Não foi possível liberar reserva de Cashback na Central: {exc}") from exc


def validar_resposta_reserva(resposta, venda, operacao_uuid, valor):
    reserva = resposta.get("reserva") if isinstance(resposta, dict) else None
    if not isinstance(reserva, dict):
        raise VendaConflictError("Reserva de Cashback não retornada pela Central.")
    if type(reserva.get("autorizacao_versao")) is not int or reserva["autorizacao_versao"] != 2:
        raise VendaConflictError("Autorização de Cashback exige resposta oficial da Central na versão 2.")
    reserva_id = reserva.get("id")
    if type(reserva_id) is not int or reserva_id <= 0:
        raise VendaConflictError("Identificação da reserva de Cashback inválida.")
    cliente_id = reserva.get("cliente_id")
    if type(cliente_id) is not int or cliente_id <= 0:
        raise VendaConflictError("Cliente da reserva de Cashback inválido.")
    try:
        valor_reservado = Decimal(str(reserva.get("valor")))
        total_autorizado = Decimal(str(reserva.get("total_venda")))
        limite_uso = Decimal(str(reserva.get("limite_uso")))
        venda_reservada = uuid.UUID(str(reserva.get("venda_uuid")))
        operacao_reservada = uuid.UUID(str(reserva.get("operacao_uuid")))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise VendaConflictError("Reserva de Cashback inválida retornada pela Central.") from exc
    if cliente_id != venda.cliente_retaguarda_id:
        raise VendaConflictError("Reserva de Cashback pertence a outro cliente.")
    if venda_reservada != venda.venda_uuid or operacao_reservada != operacao_uuid:
        raise VendaConflictError("Reserva de Cashback pertence a outra venda ou operação.")
    if not valor_reservado.is_finite() or valor_reservado != valor:
        raise VendaConflictError("Valor da reserva de Cashback diverge do pagamento.")
    if reserva.get("status") != "RESERVADA":
        raise VendaConflictError("Reserva de Cashback não está ativa.")
    if not total_autorizado.is_finite() or total_autorizado != money(venda.total):
        raise VendaConflictError("Total autorizado de Cashback diverge do total comercial da venda; cancele e reautorize o pagamento.")
    if not limite_uso.is_finite() or limite_uso < valor or limite_uso > total_autorizado or limite_uso != money(limite_uso):
        raise VendaConflictError("Limite da autorização de Cashback inválido.")
    return {
        "id": reserva_id, "autorizacao_versao": 2, "venda_uuid": str(venda_reservada),
        "operacao_uuid": str(operacao_reservada), "cliente_id": cliente_id,
        "valor": f"{valor_reservado:.2f}", "total_venda": f"{total_autorizado:.2f}",
        "limite_uso": f"{limite_uso:.2f}", "status": "RESERVADA",
    }


def validar_contexto_pagamento_cashback(pagamento):
    if (pagamento.tipo or "").upper() != CASHBACK or pagamento.cashback_origem != VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL:
        return
    evento = VendaEventoHub.objects.filter(
        venda=pagamento.venda, tipo=VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO,
        dados__pagamento_uuid=str(pagamento.pagamento_uuid),
    ).order_by("-id").first()
    autorizacao = evento.dados.get("cashback_autorizacao") if evento else None
    if not isinstance(autorizacao, dict):
        raise VendaConflictError("Pagamento Cashback sem autorização de versão 2; remova e reautorize online.")
    if autorizacao.get("id") != pagamento.cashback_reserva_id:
        raise VendaConflictError("Reserva do pagamento Cashback diverge da autorização original.")
    validar_resposta_reserva({"reserva": autorizacao}, pagamento.venda, pagamento.operacao_uuid, money(pagamento.valor))


def reservar_na_central(hub, payload):
    client = _client(hub)
    for tentativa in range(2):
        try:
            return client.cashback_reservar_venda(token=hub.retaguarda_token, payload=payload)
        except RetaguardaError as exc:
            causa = exc.__cause__
            timeout = isinstance(causa, TimeoutError) or isinstance(getattr(causa, "reason", None), TimeoutError)
            if tentativa == 0 and timeout:
                continue  # A Central pode ter reservado antes do timeout: repetir a mesma identidade e contexto.
            raise VendaConflictError(
                f"Central ONLINE obrigatória para reservar Cashback: {exc}. Tente novamente com a mesma operação."
            ) from exc
