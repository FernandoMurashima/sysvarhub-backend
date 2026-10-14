import uuid
import json
import hashlib
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    CashbackMovimentoHub,
    EventoSyncHub,
    NFCeHub,
    RecebimentoMercadoriaHub,
    ValeTrocaHub,
    ValeTrocaMovimentoHub,
    VendaDevolucaoHub,
    VendaHub,
    VendaPagamentoHub,
)
from core.services.recebimentos import (
    ORIGEM_DISTRIBUICAO,
    aplicar_movimentos_recebimento_confirmado,
    rejeitar_movimentos_recebimento_pendentes,
)
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


TIPO_VENDA_FINALIZADA = "VENDA_FINALIZADA"
TIPO_NFCE_ATUALIZADA = "NFCE_ATUALIZADA"
TIPO_DEVOLUCAO_FINALIZADA = "DEVOLUCAO_FINALIZADA"
TIPO_RECEBIMENTO_LOJA_CONFERIDO = "RECEBIMENTO_LOJA_CONFERIDO"
TIPO_DISTRIBUICAO_LOJA_CONFERIDA = "DISTRIBUICAO_LOJA_CONFERIDA"
STATUS_CONFIRMADOS = {"PROCESSADO", "DUPLICADO"}
SEGREDOS_BLOQUEADOS = {"certificado", "private_key", "chave_privada", "senha", "csc", "token_csc"}
ZERO = Decimal("0.00")


class ConciliacaoDevolucaoConflito(Exception):
    pass


def enfileirar_venda_finalizada(venda):
    venda = _venda_queryset().get(pk=venda.pk)
    payload = payload_venda_finalizada(venda)
    pendencias = _dependencias_devolucao_venda(venda)
    if pendencias:
        payload["dependencias"] = {"devolucoes_uuid": pendencias}
    return _criar_ou_atualizar_evento(
        hub=venda.hub,
        tipo=TIPO_VENDA_FINALIZADA,
        chave=f"VENDA:{venda.venda_uuid}:FINALIZADA",
        payload=payload,
        evento_uuid=_uuid_deterministico(venda.hub_id, TIPO_VENDA_FINALIZADA, str(venda.venda_uuid)),
    )


def enfileirar_nfce_atualizada(nfce):
    nfce = NFCeHub.objects.select_related("hub", "venda").get(pk=nfce.pk)
    enfileirar_venda_finalizada(nfce.venda)
    if nfce.sync_versao <= 0:
        nfce.sync_versao = 1
        nfce.save(update_fields=["sync_versao", "atualizado_em"])
    payload = payload_nfce_atualizada(nfce)
    return _criar_ou_atualizar_evento(
        hub=nfce.hub,
        tipo=TIPO_NFCE_ATUALIZADA,
        chave=f"NFCE:{nfce.nfce_uuid}:V:{nfce.sync_versao}",
        payload=payload,
        evento_uuid=_uuid_deterministico(nfce.hub_id, TIPO_NFCE_ATUALIZADA, str(nfce.nfce_uuid), str(nfce.sync_versao)),
    )


def enfileirar_devolucao_finalizada(devolucao):
    devolucao = (
        VendaDevolucaoHub.objects.select_related("hub", "venda_origem", "operador", "terminal")
        .prefetch_related("itens")
        .get(pk=devolucao.pk)
    )
    payload = payload_devolucao_finalizada(devolucao)
    return _criar_ou_atualizar_evento(
        hub=devolucao.hub,
        tipo=TIPO_DEVOLUCAO_FINALIZADA,
        chave=f"DEVOLUCAO:{devolucao.devolucao_uuid}:FINALIZADA",
        payload=payload,
        evento_uuid=_uuid_deterministico(devolucao.hub_id, TIPO_DEVOLUCAO_FINALIZADA, str(devolucao.devolucao_uuid)),
    )


def enfileirar_recebimento_conferido(recebimento):
    recebimento = (
        RecebimentoMercadoriaHub.objects.select_related("hub", "operador_conferencia")
        .prefetch_related("itens")
        .get(pk=recebimento.pk)
    )
    tipo = TIPO_DISTRIBUICAO_LOJA_CONFERIDA if recebimento.origem == ORIGEM_DISTRIBUICAO else TIPO_RECEBIMENTO_LOJA_CONFERIDO
    payload = payload_recebimento_conferido(recebimento)
    chave_prefixo = "DISTRIBUICAO" if recebimento.origem == ORIGEM_DISTRIBUICAO else "RECEBIMENTO"
    return _criar_ou_atualizar_evento(
        hub=recebimento.hub,
        tipo=tipo,
        chave=f"{chave_prefixo}:{recebimento.retaguarda_id}:CONFERIDO",
        payload=payload,
        evento_uuid=_uuid_deterministico(
            recebimento.hub_id,
            tipo,
            str(recebimento.retaguarda_id),
        ),
    )


def avancar_versao_sync_nfce(nfce):
    nfce.sync_versao = int(nfce.sync_versao or 0) + 1
    nfce.save(update_fields=["sync_versao", "atualizado_em"])
    return nfce.sync_versao


def marcar_nfce_alterada_para_sync(nfce):
    avancar_versao_sync_nfce(nfce)
    transaction.on_commit(lambda: enfileirar_nfce_atualizada(nfce))


def payload_venda_finalizada(venda):
    pagamentos = venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).order_by("id")
    return {
        "venda_uuid": str(venda.venda_uuid),
        "documento": venda.documento,
        "caixa_retaguarda_id": venda.sessao_caixa.caixa.retaguarda_id,
        "cliente_uuid": str(venda.cliente_uuid) if venda.cliente_uuid else None,
        "cliente_retaguarda_id": venda.cliente_retaguarda_id,
        "vendedor_retaguarda_id": venda.vendedor_retaguarda_id,
        "operador_retaguarda_usuario_id": venda.operador_finalizacao.retaguarda_usuario_id if venda.operador_finalizacao_id else None,
        "finalizado_em": venda.finalizada_em.isoformat() if venda.finalizada_em else None,
        "subtotal": f"{venda.subtotal:.2f}",
        "desconto_geral": f"{venda.desconto_geral:.2f}",
        "total": f"{venda.total:.2f}",
        "valor_recebido": f"{venda.valor_recebido:.2f}",
        "troco": f"{venda.troco:.2f}",
        "itens": [
            {
                "produto_retaguarda_id": item.retaguarda_produto_id,
                "sku_retaguarda_id": item.retaguarda_sku_id,
                "ean": item.ean13,
                "quantidade": item.quantidade,
                "preco_unitario": f"{item.preco_unitario:.4f}",
                "desconto": f"{item.desconto:.2f}",
                "descricao": item.descricao,
                "cor": item.cor_descricao,
                "tamanho": item.tamanho_descricao,
                "promocao": {
                    "id": item.promocao_retaguarda_id,
                    "nome": item.promocao_nome,
                    "tipo": item.promocao_tipo,
                    "valor": f"{item.promocao_valor:.4f}",
                    "acumula_cashback": item.promocao_acumula_cashback,
                } if item.promocao_retaguarda_id else None,
            }
            for item in venda.itens.order_by("id")
        ],
        "pagamentos": [_payload_pagamento_venda_finalizada(pagamento) for pagamento in pagamentos],
    }


def _payload_pagamento_venda_finalizada(pagamento):
    payload = {
        "codigo": pagamento.codigo,
        "descricao": pagamento.descricao,
        "valor": f"{pagamento.valor:.2f}",
        "autorizacao": pagamento.autorizacao,
        "tipo": pagamento.tipo,
        "forma_pagamento_id": pagamento.retaguarda_forma_pagamento_id,
        "forma_pagamento_condicao_id": pagamento.retaguarda_forma_pagamento_condicao_id,
        "prazo_pagamento_id": pagamento.retaguarda_prazo_pagamento_id,
        "prazo_codigo": pagamento.prazo_codigo,
        "prazo_descricao": pagamento.prazo_descricao,
        "num_parcelas": pagamento.num_parcelas,
        "taxa_percentual": f"{pagamento.taxa_percentual:.4f}",
        "taxa_fixa": f"{pagamento.taxa_fixa:.2f}",
        "parcelas": [
            {
                "ordem": parcela.ordem,
                "dias": parcela.dias,
                "percentual": f"{parcela.percentual:.6f}" if parcela.percentual is not None else None,
                "valor_fixo": f"{parcela.valor_fixo:.2f}" if parcela.valor_fixo is not None else None,
            }
            for parcela in pagamento.parcelas_snapshot.order_by("ordem", "id")
        ],
        "vale_troca_documento": pagamento.vale_troca_documento,
        "vale_troca_retaguarda_id": pagamento.vale_troca_retaguarda_id,
        "vale_troca_reserva_id": pagamento.vale_troca_reserva_id,
        "operacao_uuid": str(pagamento.operacao_uuid),
    }
    if pagamento.tipo == "CASHBACK" and pagamento.cashback_origem == "CENTRAL":
        payload["cashback_reserva_id"] = pagamento.cashback_reserva_id
    return payload


def payload_nfce_atualizada(nfce):
    payload = {
        "nfce_uuid": str(nfce.nfce_uuid),
        "venda_uuid": str(nfce.venda.venda_uuid),
        "versao_evento": nfce.sync_versao,
        "ambiente": nfce.ambiente,
        "modelo": nfce.modelo,
        "serie": nfce.serie,
        "numero": nfce.numero,
        "status": nfce.status,
        "tipo_emissao": nfce.tipo_emissao,
        "chave_acesso": nfce.chave_acesso,
        "protocolo": nfce.protocolo,
        "xml_assinado": nfce.xml_autorizado or nfce.xml_assinado,
        "qr_code_payload": nfce.qr_code_payload,
        "retorno_codigo": nfce.codigo_retorno,
        "retorno_mensagem": nfce.mensagem_retorno,
        "emitida_em": nfce.emitida_em.isoformat() if nfce.emitida_em else None,
        "autorizada_em": nfce.autorizada_em.isoformat() if nfce.autorizada_em else None,
        "entrada_contingencia_em": nfce.entrada_contingencia_em.isoformat() if nfce.entrada_contingencia_em else None,
        "justificativa_contingencia": nfce.justificativa_contingencia,
    }
    for segredo in SEGREDOS_BLOQUEADOS:
        payload.pop(segredo, None)
    return payload


def payload_devolucao_finalizada(devolucao):
    vale = getattr(devolucao, "vale_troca", None)
    return {
        "devolucao_uuid": str(devolucao.devolucao_uuid),
        "documento": devolucao.documento,
        "origem": devolucao.origem,
        "venda_uuid": str(devolucao.venda_origem.venda_uuid) if devolucao.venda_origem_id else None,
        "venda_origem_retaguarda_id": devolucao.venda_origem_retaguarda_id,
        "documento_venda": devolucao.venda_origem_documento,
        "loja_origem_retaguarda_id": devolucao.loja_origem_retaguarda_id,
        "loja_origem_nome": devolucao.loja_origem_nome,
        "loja_recebimento_retaguarda_id": devolucao.hub.loja_id,
        "loja_recebimento_nome": devolucao.hub.loja_nome,
        "dados_origem": devolucao.dados_origem or {},
        "cliente_uuid": str(devolucao.cliente_uuid) if devolucao.cliente_uuid else None,
        "cliente_retaguarda_id": devolucao.cliente_retaguarda_id,
        "operador_retaguarda_usuario_id": devolucao.operador.retaguarda_usuario_id,
        "terminal": devolucao.terminal.codigo,
        "motivo": devolucao.motivo,
        "valor_total": f"{devolucao.valor_total:.2f}",
        "finalizada_em": devolucao.finalizada_em.isoformat(),
        "itens": [
            {
                "item_uuid": str(item.venda_item.item_uuid) if item.venda_item_id else None,
                "sku_retaguarda_id": item.retaguarda_sku_id,
                "produto_retaguarda_id": item.retaguarda_produto_id,
                "ean": item.ean13,
                "descricao": item.descricao,
                "quantidade": item.quantidade,
                "preco_unitario": f"{item.preco_unitario:.4f}",
                "desconto": f"{item.desconto:.2f}",
                "total_item": f"{item.total_item:.2f}",
            }
            for item in devolucao.itens.order_by("id")
        ],
        "vale_troca": {
            "documento": vale.documento,
            "valor": f"{vale.valor_original:.2f}",
            "saldo": f"{vale.saldo:.2f}",
            "validade": vale.validade.isoformat() if vale.validade else None,
            "provisorio": vale.provisorio,
        } if vale else None,
    }


def payload_recebimento_conferido(recebimento):
    if recebimento.origem == ORIGEM_DISTRIBUICAO:
        payload = {
            "pedido_distribuicao_id": recebimento.retaguarda_id,
            "loja_destino_id": recebimento.loja_retaguarda_id,
            "operador_retaguarda_usuario_id": (
                recebimento.operador_conferencia.retaguarda_usuario_id
                if recebimento.operador_conferencia_id
                else None
            ),
            "itens": [
                {
                    "transito_id": item.conferencia_item_id,
                    "sku_retaguarda_id": item.retaguarda_sku_id,
                    "ean": item.ean13,
                    "quantidade_recebida": f"{item.quantidade_recebida:.3f}",
                }
                for item in recebimento.itens.order_by("id")
            ],
        }
        documento = recebimento.documento_fiscal or {}
        if documento.get("numero"):
            payload["nfe_numero"] = documento.get("numero")
        if documento.get("chave_acesso"):
            payload["nfe_chave"] = documento.get("chave_acesso")
        if recebimento.observacao_divergencia:
            payload["observacao_divergencia"] = recebimento.observacao_divergencia
        return payload

    payload = {
        "recebimento_id": recebimento.retaguarda_id,
        "operador_retaguarda_usuario_id": (
            recebimento.operador_conferencia.retaguarda_usuario_id
            if recebimento.operador_conferencia_id
            else None
        ),
        "itens": [
            {
                "conferencia_item_id": item.conferencia_item_id,
                "sku_retaguarda_id": item.retaguarda_sku_id,
                "ean": item.ean13,
                "quantidade_recebida": f"{item.quantidade_recebida:.3f}",
            }
            for item in recebimento.itens.order_by("id")
        ],
    }
    if recebimento.observacao_divergencia:
        payload["observacao_divergencia"] = recebimento.observacao_divergencia
    return payload


def sincronizar_eventos_pendentes(hub=None, *, client=None, limite=50, agora=None, evento_ids=None):
    agora = agora or timezone.now()
    with transaction.atomic():
        qs = EventoSyncHub.objects.select_for_update().filter(
            status__in=[EventoSyncHub.STATUS_PENDENTE, EventoSyncHub.STATUS_ERRO, EventoSyncHub.STATUS_PROCESSANDO],
        )
        if hub is not None:
            qs = qs.filter(hub=hub)
        if evento_ids is not None:
            qs = qs.filter(pk__in=evento_ids)
        qs = qs.filter(Q(proxima_tentativa_em__isnull=True) | Q(proxima_tentativa_em__lte=agora))
        candidatos = list(qs.order_by("criado_em", "id")[:limite])
        eventos = []
        for evento in candidatos:
            if _evento_bloqueado_por_dependencia(evento):
                evento.ultimo_erro = "Aguardando devolução provisória ser confirmada pela Central."
                evento.proxima_tentativa_em = agora + timedelta(seconds=60)
                evento.save(update_fields=["ultimo_erro", "proxima_tentativa_em", "atualizado_em"])
                continue
            eventos.append(evento)
        for evento in eventos:
            evento.status = EventoSyncHub.STATUS_PROCESSANDO
            evento.tentativas += 1
            evento.save(update_fields=["status", "tentativas", "atualizado_em"])
    if not eventos:
        return {"enviados": 0, "sincronizados": 0, "conflitos": 0, "erros": 0}
    hub = eventos[0].hub
    client = client or RetaguardaClient(hub.retaguarda_url)
    try:
        resposta = client.sync_push(token=hub.retaguarda_token, eventos=[_serializar_evento(e) for e in eventos])
    except RetaguardaError as exc:
        for evento in eventos:
            _marcar_retry(evento, str(exc), agora)
        return {"enviados": len(eventos), "sincronizados": 0, "conflitos": 0, "erros": len(eventos)}
    resultados = {r.get("chave_idempotencia"): r for r in resposta.get("resultados", [])}
    contadores = {"enviados": len(eventos), "sincronizados": 0, "conflitos": 0, "erros": 0}
    for evento in eventos:
        resultado = resultados.get(evento.chave_idempotencia, {})
        status = resultado.get("status")
        if status in STATUS_CONFIRMADOS:
            try:
                with transaction.atomic():
                    evento = EventoSyncHub.objects.select_for_update().get(pk=evento.pk)
                    _marcar_recebimento_confirmado(evento, resultado, agora)
                    _canonicalizar_devolucao_confirmada(evento, resultado, agora)
                    _marcar_beneficios_centralizados(evento, agora)
                    evento.status = EventoSyncHub.STATUS_SINCRONIZADO
                    evento.sincronizado_em = agora
                    evento.resposta = resultado
                    evento.ultimo_erro = ""
                    evento.proxima_tentativa_em = None
                    evento.save(update_fields=["status", "sincronizado_em", "resposta", "ultimo_erro", "proxima_tentativa_em", "atualizado_em"])
                contadores["sincronizados"] += 1
            except ConciliacaoDevolucaoConflito as exc:
                mensagem = str(exc)[:255]
                with transaction.atomic():
                    evento = EventoSyncHub.objects.select_for_update().get(pk=evento.pk)
                    evento.status = EventoSyncHub.STATUS_CONFLITO
                    evento.resposta = resultado
                    evento.ultimo_erro = mensagem
                    evento.save(update_fields=["status", "resposta", "ultimo_erro", "atualizado_em"])
                    _marcar_devolucao_em_conflito(evento, mensagem)
                contadores["conflitos"] += 1
            except Exception as exc:
                _marcar_retry(evento, str(exc), agora, resposta=resultado)
                contadores["erros"] += 1
        elif status == "CONFLITO":
            evento.status = EventoSyncHub.STATUS_CONFLITO
            evento.resposta = resultado
            evento.ultimo_erro = resultado.get("mensagem", "CONFLITO")[:255]
            _marcar_devolucao_em_conflito(evento, evento.ultimo_erro)
            _marcar_recebimento_em_conflito(evento, evento.ultimo_erro, agora)
            evento.save(update_fields=["status", "resposta", "ultimo_erro", "atualizado_em"])
            contadores["conflitos"] += 1
        else:
            _marcar_retry(evento, resultado.get("mensagem", "ERRO"), agora, resposta=resultado)
            contadores["erros"] += 1
    return contadores


def _criar_ou_atualizar_evento(*, hub, tipo, chave, payload, evento_uuid):
    if any(segredo in str(payload).lower() for segredo in SEGREDOS_BLOQUEADOS):
        raise ValueError("Payload de sync contém segredo fiscal.")
    with transaction.atomic():
        existente = EventoSyncHub.objects.select_for_update().filter(hub=hub, chave_idempotencia=chave).first()
        if existente:
            if _payload_hash(existente.payload) != _payload_hash(payload):
                raise ValueError("Chave idempotente já existe com payload diferente.")
            return existente
        return EventoSyncHub.objects.create(
            hub=hub,
            chave_idempotencia=chave,
            evento_uuid=evento_uuid,
            tipo=tipo,
            payload=payload,
            status=EventoSyncHub.STATUS_PENDENTE,
        )


def _serializar_evento(evento):
    return {
        "evento_uuid": str(evento.evento_uuid),
        "chave_idempotencia": evento.chave_idempotencia,
        "tipo": evento.tipo,
        "ocorrido_em": evento.criado_em.isoformat(),
        "payload": evento.payload,
    }


def _marcar_retry(evento, mensagem, agora, resposta=None):
    evento.status = EventoSyncHub.STATUS_ERRO
    evento.ultimo_erro = str(mensagem or "ERRO")[:255]
    evento.resposta = resposta or {}
    evento.proxima_tentativa_em = agora + timedelta(seconds=min(300, 2 ** min(evento.tentativas, 8)))
    evento.save(update_fields=["status", "ultimo_erro", "resposta", "proxima_tentativa_em", "atualizado_em"])


def _marcar_beneficios_centralizados(evento, agora):
    payload = evento.payload or {}
    if evento.tipo == TIPO_VENDA_FINALIZADA:
        venda_uuid = payload.get("venda_uuid")
        if venda_uuid:
            CashbackMovimentoHub.objects.filter(
                hub=evento.hub,
                venda__venda_uuid=venda_uuid,
                centralizado_em__isnull=True,
            ).update(centralizado_em=agora)
    elif evento.tipo == TIPO_DEVOLUCAO_FINALIZADA:
        devolucao_uuid = payload.get("devolucao_uuid")
        vale_payload = payload.get("vale_troca") or {}
        qs = ValeTrocaHub.objects.filter(hub=evento.hub, sincronizado_em__isnull=True)
        if devolucao_uuid:
            atualizados = qs.filter(devolucao__devolucao_uuid=devolucao_uuid).update(sincronizado_em=agora)
            if atualizados:
                return
        documento = vale_payload.get("documento")
        if documento:
            qs.filter(documento=documento).update(sincronizado_em=agora)


def _canonicalizar_devolucao_confirmada(evento, resultado, agora):
    if evento.tipo != TIPO_DEVOLUCAO_FINALIZADA:
        return
    payload = evento.payload or {}
    devolucao_uuid = payload.get("devolucao_uuid")
    if not devolucao_uuid:
        return
    devolucao = VendaDevolucaoHub.objects.select_for_update().filter(hub=evento.hub, devolucao_uuid=devolucao_uuid).first()
    if not devolucao:
        return
    documento_confirmado = resultado.get("documento") or ""
    if devolucao.documento and documento_confirmado and documento_confirmado != devolucao.documento:
        raise ConciliacaoDevolucaoConflito("Central retornou documento de devolucao diferente do enviado pelo Hub.")
    valor_confirmado = _valor_total_devolucao_confirmado(resultado)
    vale = ValeTrocaHub.objects.select_for_update().filter(devolucao=devolucao).first()
    if vale and valor_confirmado is None:
        raise ValueError("Central nao retornou valor_total para conciliacao do Vale-Troca.")
    devolucao.status = VendaDevolucaoHub.STATUS_FINALIZADA
    devolucao.conflito_mensagem = ""
    devolucao.retaguarda_id = resultado.get("devolucao_retaguarda_id") or devolucao.retaguarda_id
    devolucao.documento_central = documento_confirmado or devolucao.documento_central
    devolucao.confirmado_central_em = agora
    devolucao.save(update_fields=["status", "conflito_mensagem", "retaguarda_id", "documento_central", "confirmado_central_em"])
    if vale:
        _conciliar_vale_provisorio_confirmado(vale, resultado, valor_confirmado, agora)


def _conciliar_vale_provisorio_confirmado(vale, resultado, valor_confirmado, agora):
    if ValeTrocaMovimentoHub.objects.filter(vale=vale, tipo=ValeTrocaMovimentoHub.TIPO_USO).exists():
        raise ConciliacaoDevolucaoConflito(
            "Vale-Troca provisorio ja utilizado; conciliacao com Vale oficial exige tratamento manual."
        )

    if valor_confirmado <= ZERO:
        _registrar_ajuste_vale(vale, -money(vale.saldo), "Conciliação Central: crédito líquido zero.")
        vale.valor_original = ZERO
        vale.saldo = ZERO
        vale.status = ValeTrocaHub.STATUS_CANCELADO
        vale.provisorio = False
        vale.origem = ValeTrocaHub.ORIGEM_CENTRAL
        vale.conflito_mensagem = ""
        vale.sincronizado_em = agora
        vale.save(update_fields=["valor_original", "saldo", "status", "provisorio", "origem", "conflito_mensagem", "sincronizado_em", "atualizado_em"])
        return

    documento_oficial = str(resultado.get("vale_documento") or "").strip()
    if not documento_oficial:
        raise ValueError("Central nao retornou vale_documento para credito positivo.")
    retaguarda_id = resultado.get("vale_retaguarda_id")
    if not retaguarda_id:
        raise ValueError("Central nao retornou vale_retaguarda_id para credito positivo.")

    _registrar_ajuste_vale(
        vale,
        money(valor_confirmado - vale.saldo),
        f"Conciliação Central: crédito oficial {documento_oficial}.",
    )
    vale.documento = documento_oficial
    vale.retaguarda_id = retaguarda_id
    vale.valor_original = valor_confirmado
    vale.saldo = valor_confirmado
    vale.status = ValeTrocaHub.STATUS_ABERTO
    vale.provisorio = False
    vale.origem = ValeTrocaHub.ORIGEM_CENTRAL
    vale.conflito_mensagem = ""
    vale.sincronizado_em = agora
    vale.save(update_fields=["documento", "retaguarda_id", "valor_original", "saldo", "status", "provisorio", "origem", "conflito_mensagem", "sincronizado_em", "atualizado_em"])


def _registrar_ajuste_vale(vale, delta, observacao):
    delta = money(delta)
    if delta == ZERO:
        return
    if delta > ZERO:
        tipo = ValeTrocaMovimentoHub.TIPO_CREDITO
        valor = delta
    else:
        tipo = ValeTrocaMovimentoHub.TIPO_ESTORNO
        valor = abs(delta)
    ValeTrocaMovimentoHub.objects.create(
        vale=vale,
        tipo=tipo,
        valor=valor,
        saldo_apos=money(vale.saldo + delta),
        observacao=observacao[:255],
    )


def _valor_total_devolucao_confirmado(resultado):
    if "valor_total" not in resultado:
        return None
    try:
        valor = money(Decimal(str(resultado.get("valor_total") or "0.00")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Central retornou valor_total invalido para devolucao.") from exc
    if valor < ZERO:
        raise ValueError("Central retornou valor_total negativo para devolucao.")
    return valor


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"))


def _marcar_recebimento_confirmado(evento, resultado, agora):
    if evento.tipo not in {TIPO_RECEBIMENTO_LOJA_CONFERIDO, TIPO_DISTRIBUICAO_LOJA_CONFERIDA}:
        return
    recebimento_id = (evento.payload or {}).get("recebimento_id") or (evento.payload or {}).get("pedido_distribuicao_id")
    if not recebimento_id:
        return
    mapeamento = resultado.get("mapeamento") or {}
    origem = ORIGEM_DISTRIBUICAO if evento.tipo == TIPO_DISTRIBUICAO_LOJA_CONFERIDA else None
    filtros = {"hub": evento.hub, "retaguarda_id": recebimento_id}
    if origem:
        filtros["origem"] = origem
    recebimento = RecebimentoMercadoriaHub.objects.filter(
        **filtros,
    ).first()
    if not recebimento:
        return
    aplicar_movimentos_recebimento_confirmado(recebimento, agora=agora)
    RecebimentoMercadoriaHub.objects.filter(pk=recebimento.pk).update(
        status_local=RecebimentoMercadoriaHub.STATUS_LOCAL_SINCRONIZADO,
        status_retaguarda=mapeamento.get("status") or "CONCLUIDO",
        confirmado_central_em=agora,
        sincronizado_em=agora,
        mensagem_sync="",
        resposta_central=resultado,
    )


def _dependencias_devolucao_venda(venda):
    docs = [
        pagamento.vale_troca_documento
        for pagamento in venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO, tipo__in=["TROCA", "VALE_TROCA"])
        if pagamento.vale_troca_documento
    ]
    if not docs:
        return []
    movimentos = (
        ValeTrocaMovimentoHub.objects
        .select_related("vale", "vale__devolucao")
        .filter(venda=venda, vale__documento__in=docs, vale__provisorio=True, vale__devolucao__isnull=False)
    )
    return sorted({str(mov.vale.devolucao.devolucao_uuid) for mov in movimentos})


def _evento_bloqueado_por_dependencia(evento):
    if evento.tipo != TIPO_VENDA_FINALIZADA:
        return False
    deps = ((evento.payload or {}).get("dependencias") or {}).get("devolucoes_uuid") or []
    if not deps:
        return False
    return ValeTrocaHub.objects.filter(
        hub=evento.hub,
        devolucao__devolucao_uuid__in=deps,
        provisorio=True,
    ).exists()


def _marcar_devolucao_em_conflito(evento, mensagem):
    if evento.tipo != TIPO_DEVOLUCAO_FINALIZADA:
        return
    devolucao_uuid = (evento.payload or {}).get("devolucao_uuid")
    if not devolucao_uuid:
        return
    VendaDevolucaoHub.objects.filter(hub=evento.hub, devolucao_uuid=devolucao_uuid).update(
        status=VendaDevolucaoHub.STATUS_CONFLITO,
        conflito_mensagem=mensagem[:255],
    )
    ValeTrocaHub.objects.filter(hub=evento.hub, devolucao__devolucao_uuid=devolucao_uuid).update(
        conflito_mensagem=mensagem[:255],
    )
    ValeTrocaHub.objects.filter(
        hub=evento.hub,
        devolucao__devolucao_uuid=devolucao_uuid,
        saldo__gt=0,
    ).update(status=ValeTrocaHub.STATUS_CANCELADO)


def _marcar_recebimento_em_conflito(evento, mensagem, agora):
    if evento.tipo not in {TIPO_RECEBIMENTO_LOJA_CONFERIDO, TIPO_DISTRIBUICAO_LOJA_CONFERIDA}:
        return
    recebimento_id = (evento.payload or {}).get("recebimento_id") or (evento.payload or {}).get("pedido_distribuicao_id")
    if not recebimento_id:
        return
    origem = ORIGEM_DISTRIBUICAO if evento.tipo == TIPO_DISTRIBUICAO_LOJA_CONFERIDA else None
    filtros = {"hub": evento.hub, "retaguarda_id": recebimento_id}
    if origem:
        filtros["origem"] = origem
    recebimento = RecebimentoMercadoriaHub.objects.filter(**filtros).first()
    if not recebimento:
        return
    rejeitar_movimentos_recebimento_pendentes(recebimento, mensagem=mensagem, agora=agora)
    RecebimentoMercadoriaHub.objects.filter(pk=recebimento.pk).update(
        status_local=RecebimentoMercadoriaHub.STATUS_LOCAL_CONFLITO,
        mensagem_sync=mensagem[:255],
        resposta_central=evento.resposta or {},
    )


def _uuid_deterministico(*partes):
    return uuid.uuid5(uuid.NAMESPACE_URL, ":".join(str(p) for p in partes))


def _payload_hash(payload):
    normalizado = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()


def _venda_queryset():
    return VendaHub.objects.select_related(
        "hub",
        "sessao_caixa",
        "sessao_caixa__caixa",
        "operador_finalizacao",
    ).prefetch_related("itens", "pagamentos")
