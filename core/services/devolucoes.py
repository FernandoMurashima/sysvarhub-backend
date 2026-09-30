import uuid
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from core.models import CatalogoItemHub, ValeTrocaHub, ValeTrocaMovimentoHub, VendaDevolucaoHub, VendaDevolucaoItemHub, VendaHub, VendaItemHub
from core.services.central_status import CENTRAL_STATUS_ONLINE, calcular_status_central
from core.services.sync import enfileirar_devolucao_finalizada
from core.services.vendas import VendaConflictError, VendaNotFoundError, VendaValidationError, money
from integracao.services.retaguarda import RetaguardaClient, RetaguardaCredencialInvalidaError, RetaguardaError


def _exigir_central_online(hub):
    status = calcular_status_central(hub)
    if status["status"] != CENTRAL_STATUS_ONLINE:
        raise VendaConflictError("Central OFFLINE. A devolução online ficará disponível quando a Central retornar.")


def _client(hub):
    return RetaguardaClient(hub.retaguarda_url)


def _token(hub):
    token = hub.retaguarda_token
    if not token:
        raise VendaConflictError("Credencial da Central não configurada para este Hub.")
    return token


def _chamar_central(hub, func, *args, **kwargs):
    _exigir_central_online(hub)
    try:
        resposta = func(*args, **kwargs)
        hub.ultimo_heartbeat_em = timezone.now()
        hub.ultima_tentativa_central_em = hub.ultimo_heartbeat_em
        hub.save(update_fields=["ultimo_heartbeat_em", "ultima_tentativa_central_em", "atualizado_em"])
        return resposta
    except RetaguardaCredencialInvalidaError as exc:
        raise VendaConflictError("Credencial do Hub inválida ou revogada na Central.") from exc
    except RetaguardaError as exc:
        hub.ultima_tentativa_central_em = timezone.now()
        hub.save(update_fields=["ultima_tentativa_central_em", "atualizado_em"])
        raise VendaConflictError(str(exc)) from exc


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
    hub = terminal.hub
    if not hub.retaguarda_token:
        return consultar_venda_para_devolucao_por_documento_local(terminal, documento)
    client = _client(hub)
    try:
        resposta = _chamar_central(hub, client.devolucao_venda, token=_token(hub), documento=documento)
    except VendaConflictError:
        return consultar_venda_para_devolucao_por_documento_local(terminal, documento)
    return resposta.get("venda") or resposta


def consultar_venda_para_devolucao_online(terminal, venda_id):
    hub = terminal.hub
    client = _client(hub)
    resposta = _chamar_central(hub, client.devolucao_venda_detalhe, token=_token(hub), venda_id=venda_id)
    return resposta.get("venda") or resposta


def pesquisar_clientes_devolucao(terminal, *, termo="", documento="", nome=""):
    hub = terminal.hub
    client = _client(hub)
    return _chamar_central(hub, client.devolucao_clientes, token=_token(hub), termo=termo, documento=documento, nome=nome)


def listar_vendas_cliente_devolucao(terminal, cliente_id):
    hub = terminal.hub
    client = _client(hub)
    return _chamar_central(hub, client.devolucao_cliente_vendas, token=_token(hub), cliente_id=cliente_id)


def finalizar_devolucao_online(terminal, operador, *, itens, venda_id=None, venda_uuid=None, documento_venda="", motivo="", devolucao_uuid=None):
    if not itens:
        raise VendaValidationError("Informe os itens da devolução.")
    hub = terminal.hub
    devolucao_uuid = uuid.UUID(str(devolucao_uuid or uuid.uuid4()))
    existente = VendaDevolucaoHub.objects.filter(hub=hub, devolucao_uuid=devolucao_uuid).first()
    if existente:
        return existente
    client = _client(hub)
    resposta = _chamar_central(
        hub,
        client.devolucao_finalizar,
        token=_token(hub),
        payload={
            "devolucao_uuid": str(devolucao_uuid),
            "venda_id": venda_id,
            "documento_venda": documento_venda or venda_uuid or "",
            "itens": itens,
            "motivo": motivo,
            "operador_hub_id": operador.pk,
        },
    )
    return _espelhar_devolucao_online(hub, terminal, operador, resposta.get("devolucao") or resposta, devolucao_uuid)


def _espelhar_devolucao_online(hub, terminal, operador, payload, devolucao_uuid):
    with transaction.atomic():
        existente = VendaDevolucaoHub.objects.select_for_update().filter(hub=hub, devolucao_uuid=devolucao_uuid).first()
        if existente:
            return existente
        venda_origem = payload.get("venda_origem") or {}
        loja_origem = venda_origem.get("loja_origem") or {}
        cliente = payload.get("cliente") or venda_origem.get("cliente") or {}
        devolucao = VendaDevolucaoHub.objects.create(
            devolucao_uuid=devolucao_uuid,
            hub=hub,
            venda_origem=None,
            venda_origem_retaguarda_id=venda_origem.get("id"),
            venda_origem_documento=venda_origem.get("documento") or "",
            loja_origem_retaguarda_id=loja_origem.get("id"),
            loja_origem_nome=loja_origem.get("nome") or "",
            documento_central=payload.get("documento") or "",
            retaguarda_id=payload.get("id"),
            confirmado_central_em=timezone.now(),
            operador=operador,
            terminal=terminal,
            cliente_retaguarda_id=cliente.get("id"),
            motivo=str(payload.get("motivo") or "")[:255],
            valor_total=Decimal(str(payload.get("valor_total") or 0)),
            finalizada_em=timezone.now(),
        )
        for item in payload.get("itens") or []:
            sku_id = item.get("sku") or item.get("sku_id")
            quantidade = int(item.get("quantidade") or 0)
            catalogo = CatalogoItemHub.objects.filter(hub=hub, retaguarda_sku_id=sku_id).first()
            if catalogo and quantidade > 0:
                CatalogoItemHub.objects.filter(pk=catalogo.pk).update(
                    estoque_fisico=models.F("estoque_fisico") + Decimal(quantidade),
                    estoque_disponivel=models.F("estoque_disponivel") + Decimal(quantidade),
                )
            if catalogo and quantidade > 0:
                VendaDevolucaoItemHub.objects.create(
                    devolucao=devolucao,
                    venda_item=None,
                    catalogo_item=catalogo,
                    retaguarda_produto_id=item.get("produto") or catalogo.retaguarda_produto_id,
                    retaguarda_sku_id=sku_id,
                    ean13=item.get("ean") or "",
                    descricao=item.get("descricao") or catalogo.descricao,
                    quantidade=quantidade,
                    preco_unitario=Decimal(str(item.get("preco_unitario") or 0)),
                    desconto=Decimal(str(item.get("desconto") or 0)),
                    total_item=Decimal(str(item.get("total_item") or item.get("valor_liquido_disponivel") or 0)),
                )
        vale_payload = payload.get("vale_troca") or {}
        if vale_payload:
            vale = ValeTrocaHub.objects.create(
                hub=hub,
                devolucao=devolucao,
                retaguarda_id=vale_payload.get("id"),
                cliente_retaguarda_id=cliente.get("id"),
                documento=vale_payload.get("documento") or f"VT-{devolucao.devolucao_uuid.hex[:20]}",
                valor_original=Decimal(str(vale_payload.get("valor_original") or payload.get("valor_total") or 0)),
                saldo=Decimal(str(vale_payload.get("saldo") or 0)),
                status=vale_payload.get("status") or ValeTrocaHub.STATUS_ABERTO,
                validade=vale_payload.get("validade") or None,
                origem_devolucao_uuid=devolucao.devolucao_uuid,
                sincronizado_em=timezone.now(),
            )
            ValeTrocaMovimentoHub.objects.create(
                vale=vale,
                tipo=ValeTrocaMovimentoHub.TIPO_CREDITO,
                valor=vale.valor_original,
                saldo_apos=vale.saldo,
                observacao=f"Crédito confirmado pela Central na devolução {devolucao.documento_central}",
            )
    return devolucao


def consultar_venda_para_devolucao_por_documento_local(terminal, documento):
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


def finalizar_devolucao(terminal, operador, *, venda_uuid=None, venda_id=None, documento_venda="", itens, motivo="", devolucao_uuid=None):
    if not venda_id and venda_uuid:
        return finalizar_devolucao_local(terminal, operador, venda_uuid=venda_uuid, itens=itens, motivo=motivo)
    return finalizar_devolucao_online(
        terminal,
        operador,
        venda_id=venda_id,
        venda_uuid=venda_uuid,
        documento_venda=documento_venda,
        itens=itens,
        motivo=motivo,
        devolucao_uuid=devolucao_uuid,
    )


def finalizar_devolucao_local(terminal, operador, *, venda_uuid, itens, motivo=""):
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
    venda_uuid = str(devolucao.venda_origem.venda_uuid) if devolucao.venda_origem_id else ""
    return {
        "uuid": str(devolucao.devolucao_uuid),
        "venda_uuid": venda_uuid,
        "documento": devolucao.documento_central or str(devolucao.devolucao_uuid),
        "venda_documento": devolucao.venda_origem_documento,
        "loja_origem": devolucao.loja_origem_nome,
        "loja_recebimento": devolucao.hub.loja_nome,
        "valor_total": f"{devolucao.valor_total:.2f}",
        "finalizada_em": devolucao.finalizada_em.isoformat(),
        "vale_troca": {
            "documento": vale.documento,
            "saldo": f"{vale.saldo:.2f}",
        } if vale else None,
    }
