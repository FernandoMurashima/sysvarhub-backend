from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.models import (
    CatalogoItemHub,
    RecebimentoMercadoriaHub,
    RecebimentoMercadoriaItemHub,
    RecebimentoMercadoriaMovimentoEstoqueHub,
    Terminal,
)


QUANTIDADE_ESTOQUE = Decimal("0.001")


class RecebimentoError(Exception):
    """Erro de dominio controlado para recebimento local."""


class RecebimentoConflictError(RecebimentoError):
    pass


class RecebimentoNotFoundError(RecebimentoError):
    pass


class RecebimentoValidationError(RecebimentoError):
    pass


def listar_recebimentos(hub, filtros=None):
    filtros = filtros or {}
    qs = RecebimentoMercadoriaHub.objects.filter(hub=hub).order_by("-recebido_central_em", "-id")
    status_param = str(filtros.get("status") or "").strip().upper()
    if status_param:
        qs = qs.filter(status_local=status_param)
    status_retaguarda = str(filtros.get("status_retaguarda") or "").strip().upper()
    if status_retaguarda:
        qs = qs.filter(status_retaguarda=status_retaguarda)
    return {
        "total": qs.count(),
        "recebimentos": [serializar_recebimento(recebimento) for recebimento in qs[:100]],
    }


def detalhar_recebimento(hub, recebimento_id):
    recebimento = _obter_recebimento(hub, recebimento_id)
    return serializar_recebimento(recebimento, detalhado=True)


@transaction.atomic
def atualizar_recebimento_por_payload(hub, payload, *, detalhado=False):
    if not isinstance(payload, dict):
        raise RecebimentoValidationError("Recebimento da Central inválido.")
    retaguarda_id = _inteiro_obrigatorio(payload.get("id"), "id")
    recebimento_uuid = payload.get("recebimento_uuid") or None
    loja = payload.get("loja") or {}
    fornecedor = payload.get("fornecedor") or {}
    agora = timezone.now()

    defaults = {"status_local": RecebimentoMercadoriaHub.STATUS_LOCAL_PENDENTE}
    if recebimento_uuid:
        defaults["recebimento_uuid"] = recebimento_uuid
    recebimento, criado = RecebimentoMercadoriaHub.objects.select_for_update().get_or_create(
        hub=hub,
        retaguarda_id=retaguarda_id,
        defaults=defaults,
    )
    update_fields = [
        "origem",
        "status_retaguarda",
        "empresa_retaguarda_id",
        "loja_retaguarda_id",
        "loja_nome",
        "fornecedor_retaguarda_id",
        "fornecedor_nome",
        "fornecedor_documento",
        "documento_fiscal",
        "pedidos",
        "resumo",
        "contrato_retorno",
        "recebido_central_em",
        "atualizado_em",
    ]
    if recebimento_uuid and criado:
        update_fields.append("recebimento_uuid")
    recebimento.origem = str(payload.get("origem") or "")
    recebimento.status_retaguarda = str(payload.get("status") or "")
    recebimento.empresa_retaguarda_id = hub.empresa_id
    recebimento.loja_retaguarda_id = loja.get("id") or hub.loja_id
    recebimento.loja_nome = str(loja.get("nome") or loja.get("apelido") or "")[:150]
    recebimento.fornecedor_retaguarda_id = fornecedor.get("id")
    recebimento.fornecedor_nome = str(fornecedor.get("nome") or "")[:150]
    recebimento.fornecedor_documento = str(fornecedor.get("documento") or "")[:20]
    recebimento.documento_fiscal = payload.get("documento_fiscal") or {}
    recebimento.pedidos = payload.get("pedidos") or []
    recebimento.resumo = payload.get("resumo") or {}
    recebimento.contrato_retorno = payload.get("contrato_retorno") or {}
    recebimento.recebido_central_em = agora
    recebimento.save(update_fields=update_fields)

    if detalhado and recebimento.status_local == RecebimentoMercadoriaHub.STATUS_LOCAL_PENDENTE:
        _atualizar_itens_recebimento(recebimento, payload.get("itens") or [])
    return recebimento


def finalizar_recebimento(terminal, operador, sessao_operador, *, recebimento_id, itens, observacao_divergencia=""):
    if not isinstance(itens, list) or not itens:
        raise RecebimentoValidationError("Informe todos os itens conferidos.")
    observacao_divergencia = str(observacao_divergencia or "").strip()

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        recebimento = _obter_recebimento_bloqueado(terminal_bloqueado.hub, recebimento_id)
        if recebimento.status_local == RecebimentoMercadoriaHub.STATUS_LOCAL_SINCRONIZADO:
            return recebimento
        if recebimento.status_local == RecebimentoMercadoriaHub.STATUS_LOCAL_CONFERIDO:
            return recebimento
        if recebimento.status_local == RecebimentoMercadoriaHub.STATUS_LOCAL_CONFLITO:
            raise RecebimentoConflictError("Recebimento em conflito deve ser reconciliado antes de nova conferência.")
        if recebimento.status_retaguarda and recebimento.status_retaguarda != "EM_CONFERENCIA":
            raise RecebimentoConflictError("Recebimento deve estar em conferência na Central.")

        itens_locais = {
            item.conferencia_item_id: item
            for item in RecebimentoMercadoriaItemHub.objects.select_for_update()
            .filter(recebimento=recebimento)
            .select_related("catalogo_item")
        }
        if not itens_locais:
            raise RecebimentoConflictError("Recebimento sem itens de conferência sincronizados.")
        quantidades = _normalizar_itens_conferencia(itens, itens_locais)
        possui_divergencia = False
        for conferencia_item_id, quantidade in quantidades.items():
            item = itens_locais[conferencia_item_id]
            item.quantidade_recebida = quantidade
            item.diferenca = (quantidade - item.quantidade_esperada).quantize(QUANTIDADE_ESTOQUE)
            item.situacao = "OK" if item.diferenca == 0 else "SOBRA" if item.diferenca > 0 else "FALTA"
            item.save(update_fields=["quantidade_recebida", "diferenca", "situacao", "atualizado_em"])
            possui_divergencia = possui_divergencia or item.diferenca != 0
        if possui_divergencia and not observacao_divergencia:
            raise RecebimentoValidationError("Informe observacao_divergencia para recebimento divergente.")

        _registrar_movimentos_estoque_pendentes(recebimento)
        agora = timezone.now()
        recebimento.status_local = RecebimentoMercadoriaHub.STATUS_LOCAL_CONFERIDO
        recebimento.observacao_divergencia = observacao_divergencia
        recebimento.operador_conferencia = operador
        recebimento.terminal_conferencia = terminal_bloqueado
        recebimento.sessao_operador_conferencia = sessao_operador
        recebimento.conferido_em = agora
        recebimento.mensagem_sync = ""
        recebimento.save(update_fields=[
            "status_local",
            "observacao_divergencia",
            "operador_conferencia",
            "terminal_conferencia",
            "sessao_operador_conferencia",
            "conferido_em",
            "mensagem_sync",
            "atualizado_em",
        ])
        from core.services.sync import enfileirar_recebimento_conferido
        evento = enfileirar_recebimento_conferido(recebimento)
        recebimento.evento_sync = evento
        recebimento.save(update_fields=["evento_sync", "atualizado_em"])
    return recebimento


def aplicar_movimentos_recebimento_confirmado(recebimento, *, agora=None):
    agora = agora or timezone.now()
    with transaction.atomic():
        recebimento = RecebimentoMercadoriaHub.objects.select_for_update().get(pk=recebimento.pk)
        movimentos = list(
            RecebimentoMercadoriaMovimentoEstoqueHub.objects.select_for_update()
            .select_related("catalogo_item")
            .filter(recebimento=recebimento)
            .order_by("id")
        )
        aplicou = False
        for movimento in movimentos:
            if movimento.status != RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_PENDENTE:
                continue
            catalogo_item = CatalogoItemHub.objects.select_for_update().get(pk=movimento.catalogo_item_id)
            saldo_anterior = catalogo_item.estoque_fisico
            catalogo_item.estoque_fisico = (catalogo_item.estoque_fisico + movimento.quantidade).quantize(QUANTIDADE_ESTOQUE)
            catalogo_item.estoque_disponivel = (catalogo_item.estoque_disponivel + movimento.quantidade).quantize(QUANTIDADE_ESTOQUE)
            _atualizar_bloqueio_sem_estoque(catalogo_item)
            catalogo_item.save(update_fields=["estoque_fisico", "estoque_disponivel", "motivos_bloqueio", "vendavel", "atualizado_em"])
            movimento.saldo_anterior = saldo_anterior
            movimento.saldo_posterior = catalogo_item.estoque_fisico
            movimento.status = RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_APLICADO
            movimento.aplicado_em = agora
            movimento.sincronizado_central_em = agora
            movimento.mensagem_status = ""
            movimento.save(update_fields=[
                "saldo_anterior",
                "saldo_posterior",
                "status",
                "aplicado_em",
                "sincronizado_central_em",
                "mensagem_status",
            ])
            aplicou = True
        if aplicou and recebimento.estoque_efetivado_em is None:
            recebimento.estoque_efetivado_em = agora
            recebimento.save(update_fields=["estoque_efetivado_em", "atualizado_em"])


def rejeitar_movimentos_recebimento_pendentes(recebimento, *, mensagem="", agora=None):
    agora = agora or timezone.now()
    mensagem = str(mensagem or "")[:255]
    with transaction.atomic():
        movimentos = (
            RecebimentoMercadoriaMovimentoEstoqueHub.objects.select_for_update()
            .filter(recebimento=recebimento)
            .order_by("id")
        )
        for movimento in movimentos:
            if movimento.status == RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_PENDENTE:
                movimento.status = RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_REJEITADO
                movimento.rejeitado_em = agora
                movimento.mensagem_status = mensagem
                movimento.save(update_fields=["status", "rejeitado_em", "mensagem_status"])
            elif movimento.status == RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_APLICADO:
                movimento.mensagem_status = mensagem or "Movimento aplicado antes do conflito; exige reconciliacao manual segura."
                movimento.save(update_fields=["mensagem_status"])


def serializar_recebimento(recebimento, detalhado=False):
    payload = {
        "local_id": recebimento.pk,
        "id": recebimento.retaguarda_id,
        "recebimento_uuid": str(recebimento.recebimento_uuid),
        "origem": recebimento.origem,
        "status_retaguarda": recebimento.status_retaguarda,
        "status_local": recebimento.status_local,
        "loja": {
            "id": recebimento.loja_retaguarda_id,
            "nome": recebimento.loja_nome,
        },
        "fornecedor": {
            "id": recebimento.fornecedor_retaguarda_id,
            "nome": recebimento.fornecedor_nome,
            "documento": recebimento.fornecedor_documento,
        } if recebimento.fornecedor_retaguarda_id or recebimento.fornecedor_nome else None,
        "documento_fiscal": recebimento.documento_fiscal,
        "pedidos": recebimento.pedidos,
        "resumo": recebimento.resumo,
        "observacao_divergencia": recebimento.observacao_divergencia,
        "conferido_em": recebimento.conferido_em.isoformat() if recebimento.conferido_em else None,
        "estoque_efetivado_em": recebimento.estoque_efetivado_em.isoformat() if recebimento.estoque_efetivado_em else None,
        "confirmado_central_em": recebimento.confirmado_central_em.isoformat() if recebimento.confirmado_central_em else None,
        "mensagem_sync": recebimento.mensagem_sync,
    }
    if detalhado:
        payload["itens"] = [
            serializar_item_recebimento(item)
            for item in recebimento.itens.select_related("catalogo_item").order_by("id")
        ]
        payload["contrato_retorno"] = recebimento.contrato_retorno
        payload["resposta_central"] = recebimento.resposta_central
    return payload


def serializar_item_recebimento(item):
    return {
        "conferencia_item_id": item.conferencia_item_id,
        "pedido_id": item.pedido_retaguarda_id,
        "pedido_item_id": item.pedido_item_retaguarda_id,
        "produto_retaguarda_id": item.retaguarda_produto_id,
        "sku_retaguarda_id": item.retaguarda_sku_id,
        "ean": item.ean13,
        "referencia": item.referencia,
        "descricao": item.descricao,
        "cor": {"id": item.cor_retaguarda_id, "nome": item.cor_descricao},
        "tamanho": {"id": item.tamanho_retaguarda_id, "nome": item.tamanho_descricao},
        "quantidade_esperada": f"{item.quantidade_esperada:.3f}",
        "quantidade_recebida": f"{item.quantidade_recebida:.3f}",
        "diferenca": f"{item.diferenca:.3f}",
        "situacao": item.situacao,
    }


def _atualizar_itens_recebimento(recebimento, itens_payload):
    if not isinstance(itens_payload, list):
        raise RecebimentoValidationError("Itens do recebimento inválidos.")
    recebidos = set()
    for row in itens_payload:
        if not isinstance(row, dict):
            raise RecebimentoValidationError("Item do recebimento inválido.")
        conferencia_item_id = _inteiro_obrigatorio(row.get("conferencia_item_id"), "conferencia_item_id")
        recebidos.add(conferencia_item_id)
        sku_id = _inteiro_obrigatorio(row.get("sku_retaguarda_id"), "sku_retaguarda_id")
        catalogo_item = CatalogoItemHub.objects.filter(hub=recebimento.hub, retaguarda_sku_id=sku_id).first()
        cor = row.get("cor") or {}
        tamanho = row.get("tamanho") or {}
        quantidade_esperada = _decimal(row.get("quantidade_esperada")).quantize(QUANTIDADE_ESTOQUE)
        quantidade_recebida = _decimal(row.get("quantidade_recebida")).quantize(QUANTIDADE_ESTOQUE)
        diferenca = _decimal(row.get("diferenca", quantidade_recebida - quantidade_esperada)).quantize(QUANTIDADE_ESTOQUE)
        RecebimentoMercadoriaItemHub.objects.update_or_create(
            recebimento=recebimento,
            conferencia_item_id=conferencia_item_id,
            defaults={
                "catalogo_item": catalogo_item,
                "pedido_retaguarda_id": row.get("pedido_id"),
                "pedido_item_retaguarda_id": row.get("pedido_item_id"),
                "retaguarda_produto_id": _inteiro_obrigatorio(row.get("produto_retaguarda_id"), "produto_retaguarda_id"),
                "retaguarda_sku_id": sku_id,
                "ean13": str(row.get("ean") or "")[:13],
                "referencia": str(row.get("referencia") or "")[:80],
                "descricao": str(row.get("descricao") or "")[:200],
                "cor_retaguarda_id": cor.get("id"),
                "cor_descricao": str(cor.get("nome") or cor.get("descricao") or "")[:80],
                "tamanho_retaguarda_id": tamanho.get("id"),
                "tamanho_descricao": str(tamanho.get("nome") or tamanho.get("descricao") or "")[:80],
                "quantidade_esperada": quantidade_esperada,
                "quantidade_recebida": quantidade_recebida,
                "diferenca": diferenca,
                "situacao": str(row.get("situacao") or "")[:10],
            },
        )
    RecebimentoMercadoriaItemHub.objects.filter(recebimento=recebimento).exclude(conferencia_item_id__in=recebidos).delete()


def _normalizar_itens_conferencia(itens_payload, itens_locais):
    recebidos = {}
    for row in itens_payload:
        if not isinstance(row, dict):
            raise RecebimentoValidationError("Item de conferência inválido.")
        conferencia_item_id = _inteiro_obrigatorio(row.get("conferencia_item_id") or row.get("id"), "conferencia_item_id")
        if conferencia_item_id in recebidos:
            raise RecebimentoValidationError("Item de conferência duplicado.")
        item = itens_locais.get(conferencia_item_id)
        if item is None:
            raise RecebimentoValidationError("Item de conferência não pertence ao recebimento.")
        sku_id = row.get("sku_retaguarda_id")
        if sku_id and int(sku_id) != item.retaguarda_sku_id:
            raise RecebimentoValidationError("SKU incompatível no item de conferência.")
        ean = str(row.get("ean") or "").strip()
        if ean and ean != item.ean13:
            raise RecebimentoValidationError("EAN incompatível no item de conferência.")
        quantidade = _decimal(row.get("quantidade_recebida")).quantize(QUANTIDADE_ESTOQUE)
        if quantidade < 0:
            raise RecebimentoValidationError("Quantidade recebida não pode ser negativa.")
        recebidos[conferencia_item_id] = quantidade
    if set(recebidos.keys()) != set(itens_locais.keys()):
        raise RecebimentoValidationError("Payload deve conter todos os itens da conferência.")
    return recebidos


def _registrar_movimentos_estoque_pendentes(recebimento):
    itens = list(
        RecebimentoMercadoriaItemHub.objects.select_for_update()
        .filter(recebimento=recebimento)
        .order_by("id")
    )
    catalogo_ids = [item.catalogo_item_id for item in itens if item.catalogo_item_id]
    catalogo_por_id = {
        item.id: item
        for item in CatalogoItemHub.objects.select_for_update().filter(id__in=catalogo_ids).order_by("id")
    }
    for item in itens:
        if item.quantidade_recebida <= 0:
            continue
        catalogo_item = catalogo_por_id.get(item.catalogo_item_id)
        if catalogo_item is None:
            catalogo_item = CatalogoItemHub.objects.select_for_update().filter(
                hub=recebimento.hub,
                retaguarda_sku_id=item.retaguarda_sku_id,
            ).first()
        if catalogo_item is None:
            raise RecebimentoConflictError(f"SKU {item.retaguarda_sku_id} não encontrado no catálogo local.")
        quantidade = item.quantidade_recebida.quantize(QUANTIDADE_ESTOQUE)
        item.catalogo_item = catalogo_item
        item.save(update_fields=["catalogo_item", "atualizado_em"])
        RecebimentoMercadoriaMovimentoEstoqueHub.objects.get_or_create(
            item=item,
            defaults={
                "recebimento": recebimento,
                "hub": recebimento.hub,
                "catalogo_item": catalogo_item,
                "retaguarda_produto_id": item.retaguarda_produto_id,
                "retaguarda_sku_id": item.retaguarda_sku_id,
                "ean13": item.ean13,
                "referencia": item.referencia,
                "quantidade": quantidade,
                "saldo_anterior": catalogo_item.estoque_fisico,
                "saldo_posterior": catalogo_item.estoque_fisico,
                "status": RecebimentoMercadoriaMovimentoEstoqueHub.STATUS_PENDENTE,
            },
        )


def _atualizar_bloqueio_sem_estoque(catalogo_item):
    motivos = list(catalogo_item.motivos_bloqueio or [])
    if catalogo_item.estoque_disponivel > 0:
        motivos = [motivo for motivo in motivos if motivo != "SEM_ESTOQUE"]
    elif "SEM_ESTOQUE" not in motivos:
        motivos.append("SEM_ESTOQUE")
    catalogo_item.motivos_bloqueio = motivos
    catalogo_item.vendavel = (
        catalogo_item.ativo
        and bool(catalogo_item.preco_venda and catalogo_item.preco_venda > 0)
        and not motivos
    )


def _obter_recebimento(hub, recebimento_id):
    recebimento = (
        RecebimentoMercadoriaHub.objects
        .filter(hub=hub, retaguarda_id=recebimento_id)
        .first()
    )
    if not recebimento:
        raise RecebimentoNotFoundError("Recebimento não encontrado.")
    return recebimento


def _obter_recebimento_bloqueado(hub, recebimento_id):
    recebimento = (
        RecebimentoMercadoriaHub.objects.select_for_update()
        .filter(hub=hub, retaguarda_id=recebimento_id)
        .first()
    )
    if not recebimento:
        raise RecebimentoNotFoundError("Recebimento não encontrado.")
    return recebimento


def _decimal(valor):
    if valor in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise RecebimentoValidationError("Quantidade inválida.") from exc


def _inteiro_obrigatorio(valor, campo):
    if valor in (None, ""):
        raise RecebimentoValidationError(f"Informe {campo}.")
    try:
        inteiro = int(valor)
    except (TypeError, ValueError) as exc:
        raise RecebimentoValidationError(f"{campo} inválido.") from exc
    if inteiro <= 0:
        raise RecebimentoValidationError(f"{campo} inválido.")
    return inteiro
