import uuid
import json
import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import CashbackMovimentoHub, EventoSyncHub, NFCeHub, ValeTrocaHub, VendaDevolucaoHub, VendaHub, VendaPagamentoHub
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


TIPO_VENDA_FINALIZADA = "VENDA_FINALIZADA"
TIPO_NFCE_ATUALIZADA = "NFCE_ATUALIZADA"
TIPO_DEVOLUCAO_FINALIZADA = "DEVOLUCAO_FINALIZADA"
STATUS_CONFIRMADOS = {"PROCESSADO", "DUPLICADO"}
SEGREDOS_BLOQUEADOS = {"certificado", "private_key", "chave_privada", "senha", "csc", "token_csc"}


def enfileirar_venda_finalizada(venda):
    venda = _venda_queryset().get(pk=venda.pk)
    payload = payload_venda_finalizada(venda)
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
        "pagamentos": [
            {
                "codigo": pagamento.codigo,
                "descricao": pagamento.descricao,
                "valor": f"{pagamento.valor:.2f}",
                "autorizacao": pagamento.autorizacao,
                "tipo": pagamento.tipo,
                "vale_troca_documento": pagamento.vale_troca_documento,
            }
            for pagamento in pagamentos
        ],
    }


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
        "venda_uuid": str(devolucao.venda_origem.venda_uuid),
        "cliente_uuid": str(devolucao.cliente_uuid) if devolucao.cliente_uuid else None,
        "cliente_retaguarda_id": devolucao.cliente_retaguarda_id,
        "operador_retaguarda_usuario_id": devolucao.operador.retaguarda_usuario_id,
        "terminal": devolucao.terminal.codigo,
        "motivo": devolucao.motivo,
        "valor_total": f"{devolucao.valor_total:.2f}",
        "finalizada_em": devolucao.finalizada_em.isoformat(),
        "itens": [
            {
                "item_uuid": str(item.venda_item.item_uuid),
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
        } if vale else None,
    }


def sincronizar_eventos_pendentes(hub=None, *, client=None, limite=50, agora=None):
    agora = agora or timezone.now()
    with transaction.atomic():
        qs = EventoSyncHub.objects.select_for_update().filter(
            status__in=[EventoSyncHub.STATUS_PENDENTE, EventoSyncHub.STATUS_ERRO, EventoSyncHub.STATUS_PROCESSANDO],
        )
        if hub is not None:
            qs = qs.filter(hub=hub)
        qs = qs.filter(Q(proxima_tentativa_em__isnull=True) | Q(proxima_tentativa_em__lte=agora))
        eventos = list(qs.order_by("criado_em", "id")[:limite])
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
            evento.status = EventoSyncHub.STATUS_SINCRONIZADO
            evento.sincronizado_em = agora
            evento.resposta = resultado
            evento.ultimo_erro = ""
            evento.save(update_fields=["status", "sincronizado_em", "resposta", "ultimo_erro", "atualizado_em"])
            _marcar_beneficios_centralizados(evento, agora)
            contadores["sincronizados"] += 1
        elif status == "CONFLITO":
            evento.status = EventoSyncHub.STATUS_CONFLITO
            evento.resposta = resultado
            evento.ultimo_erro = resultado.get("mensagem", "CONFLITO")[:255]
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
