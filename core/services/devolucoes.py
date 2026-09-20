import uuid
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from core.models import CatalogoItemHub, ValeTrocaHub, ValeTrocaMovimentoHub, VendaDevolucaoHub, VendaDevolucaoItemHub, VendaHub, VendaItemHub
from core.services.sync import enfileirar_devolucao_finalizada
from core.services.vendas import VendaConflictError, VendaNotFoundError, VendaValidationError, money


def consultar_venda_para_devolucao(terminal, venda_uuid):
    venda = (
        VendaHub.objects.filter(hub=terminal.hub, venda_uuid=venda_uuid, status=VendaHub.STATUS_FINALIZADA)
        .prefetch_related("itens")
        .first()
    )
    if not venda:
        raise VendaNotFoundError("Venda finalizada não encontrada no Hub.")
    return serializar_venda_devolucao(venda)


def consultar_venda_para_devolucao_por_documento(terminal, documento):
    termo = str(documento or "").strip()
    if not termo:
        raise VendaValidationError("Informe a venda ou cupom para devolução.")
    qs = VendaHub.objects.filter(hub=terminal.hub, status=VendaHub.STATUS_FINALIZADA).prefetch_related("itens")
    filtros = models.Q()
    try:
        filtros |= models.Q(venda_uuid=uuid.UUID(termo))
    except (TypeError, ValueError, AttributeError):
        pass
    if termo.isdecimal():
        filtros |= models.Q(nfce__numero=int(termo))
    filtros |= models.Q(nfce__chave_acesso=termo)
    filtros |= models.Q(nfce__protocolo=termo)
    venda = qs.filter(filtros).order_by("-finalizada_em", "-id").first()
    if not venda:
        raise VendaNotFoundError("Venda finalizada não encontrada no Hub.")
    return serializar_venda_devolucao(venda)


def finalizar_devolucao(terminal, operador, *, venda_uuid, itens, motivo=""):
    if not itens:
        raise VendaValidationError("Informe os itens da devolução.")
    with transaction.atomic():
        venda = (
            VendaHub.objects.select_for_update()
            .filter(hub=terminal.hub, venda_uuid=venda_uuid, status=VendaHub.STATUS_FINALIZADA)
            .first()
        )
        if not venda:
            raise VendaNotFoundError("Venda finalizada não encontrada no Hub.")
        if venda.cliente_retaguarda_id is None and venda.cliente_uuid is None:
            raise VendaConflictError("Devolução com vale-troca exige cliente identificado.")

        itens_por_uuid = {str(item.item_uuid): item for item in VendaItemHub.objects.select_for_update().filter(venda=venda)}
        devolvidos_por_item = {
            item["venda_item_id"]: item["qtd"]
            for item in VendaDevolucaoItemHub.objects.filter(devolucao__venda_origem=venda).values("venda_item_id").annotate(qtd=models.Sum("quantidade"))
        }
        total = Decimal("0.00")
        linhas = []
        for entrada in itens:
            item = itens_por_uuid.get(str(entrada.get("item_uuid") or ""))
            if not item:
                raise VendaValidationError("Item da devolução não pertence à venda.")
            quantidade = int(entrada.get("quantidade") or 0)
            if quantidade <= 0:
                raise VendaValidationError("Quantidade devolvida inválida.")
            ja_devolvido = int(devolvidos_por_item.get(item.id) or 0)
            if ja_devolvido + quantidade > item.quantidade:
                raise VendaConflictError("Quantidade devolvida excede a quantidade vendida.")
            valor = money((item.total_item / Decimal(item.quantidade)) * Decimal(quantidade))
            total = money(total + valor)
            linhas.append((item, quantidade, valor))
        if total <= 0:
            raise VendaValidationError("Valor da devolução inválido.")

        devolucao = VendaDevolucaoHub.objects.create(
            hub=terminal.hub,
            venda_origem=venda,
            operador=operador,
            terminal=terminal,
            cliente_uuid=venda.cliente_uuid,
            cliente_retaguarda_id=venda.cliente_retaguarda_id,
            motivo=str(motivo or "")[:255],
            valor_total=total,
            finalizada_em=timezone.now(),
        )
        for item, quantidade, valor in linhas:
            VendaDevolucaoItemHub.objects.create(
                devolucao=devolucao,
                venda_item=item,
                catalogo_item=item.catalogo_item,
                retaguarda_produto_id=item.retaguarda_produto_id,
                retaguarda_sku_id=item.retaguarda_sku_id,
                ean13=item.ean13,
                descricao=item.descricao,
                quantidade=quantidade,
                preco_unitario=item.preco_unitario,
                desconto=money(item.desconto / Decimal(item.quantidade) * Decimal(quantidade)) if item.desconto else Decimal("0.00"),
                total_item=valor,
            )
            CatalogoItemHub.objects.filter(pk=item.catalogo_item_id).update(
                estoque_fisico=models.F("estoque_fisico") + Decimal(quantidade),
                estoque_disponivel=models.F("estoque_disponivel") + Decimal(quantidade),
            )
        vale = ValeTrocaHub.objects.create(
            hub=terminal.hub,
            devolucao=devolucao,
            cliente_uuid=venda.cliente_uuid,
            cliente_retaguarda_id=venda.cliente_retaguarda_id,
            documento=f"VT-HUB-{devolucao.devolucao_uuid.hex[:20]}",
            valor_original=total,
            saldo=total,
            status=ValeTrocaHub.STATUS_ABERTO,
            origem_devolucao_uuid=devolucao.devolucao_uuid,
        )
        ValeTrocaMovimentoHub.objects.create(
            vale=vale,
            tipo=ValeTrocaMovimentoHub.TIPO_CREDITO,
            valor=total,
            saldo_apos=total,
            observacao=f"Crédito gerado pela devolução Hub {devolucao.devolucao_uuid}",
        )
        transaction.on_commit(lambda devolucao_id=devolucao.pk: enfileirar_devolucao_finalizada(VendaDevolucaoHub.objects.get(pk=devolucao_id)))
    return devolucao


def serializar_venda_devolucao(venda):
    devolvidos = {
        item["venda_item_id"]: item["qtd"]
        for item in VendaDevolucaoItemHub.objects.filter(devolucao__venda_origem=venda).values("venda_item_id").annotate(qtd=models.Sum("quantidade"))
    }
    return {
        "uuid": str(venda.venda_uuid),
        "cliente": {"id": venda.cliente_retaguarda_id, "uuid": str(venda.cliente_uuid) if venda.cliente_uuid else None, "nome": venda.cliente_nome},
        "total": f"{venda.total:.2f}",
        "itens": [
            {
                "item_uuid": str(item.item_uuid),
                "sku_id": item.retaguarda_sku_id,
                "descricao": item.descricao,
                "quantidade": item.quantidade,
                "quantidade_devolvida": int(devolvidos.get(item.id) or 0),
                "quantidade_disponivel": max(0, int(item.quantidade) - int(devolvidos.get(item.id) or 0)),
                "preco_unitario": f"{item.preco_unitario:.4f}",
                "total_item": f"{item.total_item:.2f}",
            }
            for item in venda.itens.order_by("id")
        ],
    }


def serializar_devolucao(devolucao):
    vale = getattr(devolucao, "vale_troca", None)
    return {
        "uuid": str(devolucao.devolucao_uuid),
        "venda_uuid": str(devolucao.venda_origem.venda_uuid),
        "valor_total": f"{devolucao.valor_total:.2f}",
        "finalizada_em": devolucao.finalizada_em.isoformat(),
        "vale_troca": {
            "documento": vale.documento,
            "saldo": f"{vale.saldo:.2f}",
        } if vale else None,
    }
