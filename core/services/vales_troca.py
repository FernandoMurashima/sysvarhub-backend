from decimal import Decimal
import re

from django.db import transaction

from core.models import VendaEventoHub, VendaHub, VendaPagamentoHub
from core.services.vendas import (
    DINHEIRO,
    ZERO_2,
    VendaConflictError,
    VendaValidationError,
    calcular_pendente,
    calcular_total_pago,
    dados_pagamento,
    money,
    registrar_evento,
    validar_autorizacao,
    validar_valor_pagamento,
    validar_uuid_obrigatorio,
)
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


TIPO_VALE_TROCA = "VALE_TROCA"
DOCUMENTO_VALE_TROCA_RE = re.compile(r"^VT[0-9]{7}$")


def _client(hub):
    return RetaguardaClient(hub.retaguarda_url)


def consultar_vale_troca_online(hub, documento):
    documento = str(documento or "").strip().upper()
    if not DOCUMENTO_VALE_TROCA_RE.match(documento):
        raise VendaValidationError("Número do Vale-Troca deve seguir o formato VT0000001.")
    try:
        return _client(hub).vale_troca_consultar(token=hub.retaguarda_token, documento=documento)
    except RetaguardaError as exc:
        raise VendaConflictError(f"Central ONLINE indisponivel para consultar Vale-Troca: {exc}") from exc


def adicionar_pagamento_vale_troca(terminal, operador, sessao_operador, *, venda_uuid, operacao_uuid, documento, valor):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    operacao_uuid = validar_uuid_obrigatorio(operacao_uuid, "Operação inválida.")
    documento = validar_autorizacao(documento).upper()
    valor = validar_valor_pagamento(valor)
    if not documento:
        raise VendaValidationError("Informe o número do Vale-Troca.")
    if not DOCUMENTO_VALE_TROCA_RE.match(documento):
        raise VendaValidationError("Número do Vale-Troca deve seguir o formato VT0000001.")
    with transaction.atomic():
        from core.services.vendas import obter_sessao_caixa_terminal, obter_venda_terminal_por_uuid_bloqueada, tem_troco
        terminal_bloqueado = terminal.__class__.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status != VendaHub.STATUS_ABERTA:
            raise VendaConflictError("Venda não está aberta.")
        if not venda.cliente_retaguarda_id:
            raise VendaConflictError("Vale-Troca exige cliente identificado.")
        if venda.cliente_padrao:
            raise VendaConflictError("Vale-Troca não pode ser usado para Consumidor Final.")
        existente = VendaPagamentoHub.objects.select_for_update().filter(venda=venda, operacao_uuid=operacao_uuid).first()
        if existente:
            if existente.tipo != TIPO_VALE_TROCA or existente.vale_troca_documento != documento or money(existente.valor) != valor:
                raise VendaConflictError("Operação de Vale-Troca já utilizada com dados diferentes.")
            return venda, False
        total_pago_atual = calcular_total_pago(venda)
        pendente = calcular_pendente(venda, total_pago_atual)
        if pendente <= ZERO_2:
            raise VendaConflictError("Venda já está paga.")
        if tem_troco(venda, total_pago_atual):
            raise VendaConflictError("Venda já possui troco.")
        if valor > pendente:
            raise VendaConflictError("Valor do Vale-Troca excede o valor pendente.")
        consulta = consultar_vale_troca_online(terminal_bloqueado.hub, documento).get("vale_troca") or {}
        documento_oficial = str(consulta.get("documento") or documento).strip().upper()
        if not DOCUMENTO_VALE_TROCA_RE.match(documento_oficial):
            raise VendaConflictError("Central retornou número de Vale-Troca inválido.")
        if VendaPagamentoHub.objects.filter(venda=venda, status=VendaPagamentoHub.STATUS_ATIVO, vale_troca_documento=documento_oficial).exists():
            raise VendaConflictError("O mesmo Vale-Troca não pode ser usado duas vezes na venda.")
        cliente = consulta.get("cliente") or {}
        if int(cliente.get("id") or 0) != int(venda.cliente_retaguarda_id):
            raise VendaConflictError("Vale-Troca pertence a outro cliente.")
        saldo_disponivel = money(Decimal(str(consulta.get("saldo_disponivel") or "0.00")))
        if consulta.get("status") != "ABERTO" or saldo_disponivel <= ZERO_2:
            raise VendaConflictError("Vale-Troca não está disponível para uso.")
        if valor > saldo_disponivel:
            raise VendaConflictError("Valor do Vale-Troca excede o saldo disponível.")
        pagamento = VendaPagamentoHub.objects.create(
            operacao_uuid=operacao_uuid,
            venda=venda,
            forma_pagamento=None,
            retaguarda_forma_pagamento_id=None,
            codigo="VT",
            descricao="Vale-Troca",
            tipo=TIPO_VALE_TROCA,
            num_parcelas=1,
            valor=valor,
            autorizacao=documento_oficial,
            vale_troca_retaguarda_id=consulta.get("id"),
            vale_troca_documento=documento_oficial,
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=terminal_bloqueado,
            operador_inclusao=operador,
            sessao_operador_inclusao=sessao_operador,
        )
        registrar_evento(venda, VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO, terminal_bloqueado, operador, sessao_operador, dados_pagamento(pagamento))
    return venda, True


def preparar_reservas_vale_troca(venda, pagamentos):
    vales = [p for p in pagamentos if (p.tipo or "").upper() == TIPO_VALE_TROCA]
    if not vales:
        return
    payload = {
        "venda_uuid": str(venda.venda_uuid),
        "cliente_id": venda.cliente_retaguarda_id,
        "pagamentos": [
            {
                "operacao_uuid": str(p.operacao_uuid),
                "documento": p.vale_troca_documento,
                "valor": f"{p.valor:.2f}",
            }
            for p in vales
        ],
    }
    try:
        resposta = _client(venda.hub).vale_troca_reservar_venda(token=venda.hub.retaguarda_token, payload=payload)
    except RetaguardaError as exc:
        raise VendaConflictError(f"Central ONLINE obrigatória para reservar Vale-Troca: {exc}") from exc
    reservas = {str(r.get("operacao_uuid")): r for r in resposta.get("reservas") or []}
    for pagamento in vales:
        reserva = reservas.get(str(pagamento.operacao_uuid))
        if not reserva:
            raise VendaConflictError("Reserva de Vale-Troca não retornada pela Central.")
        pagamento.vale_troca_reserva_id = reserva.get("id")
        pagamento.vale_troca_retaguarda_id = reserva.get("vale_id") or pagamento.vale_troca_retaguarda_id
        pagamento.vale_troca_valor_reservado = pagamento.valor
        pagamento.save(update_fields=["vale_troca_reserva_id", "vale_troca_retaguarda_id", "vale_troca_valor_reservado", "atualizado_em"])


def liberar_reserva_pagamento_vale_troca(pagamento):
    if (pagamento.tipo or "").upper() != TIPO_VALE_TROCA:
        return
    try:
        _client(pagamento.venda.hub).vale_troca_cancelar_reserva(
            token=pagamento.venda.hub.retaguarda_token,
            payload={
                "reserva_id": pagamento.vale_troca_reserva_id,
                "venda_uuid": str(pagamento.venda.venda_uuid),
                "operacao_uuid": str(pagamento.operacao_uuid),
            },
        )
    except RetaguardaError as exc:
        raise VendaConflictError(f"Não foi possível liberar reserva de Vale-Troca na Central: {exc}") from exc
