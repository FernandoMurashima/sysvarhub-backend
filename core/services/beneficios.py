from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.db.models import Case, DecimalField, Sum, Value, When
from django.utils import timezone

from core.models import CashbackConfigHub, CashbackMovimentoHub, PromocaoHub, ValeTrocaHub, ValeTrocaMovimentoHub


ZERO = Decimal("0.00")


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"))


def cliente_padrao(venda):
    documento = "".join(ch for ch in str(venda.cliente_documento or "") if ch.isdigit())
    return venda.cliente_padrao or documento in ("", "00000000000")


def cashback_config_ativa(hub):
    return CashbackConfigHub.objects.filter(hub=hub, ativo=True).order_by("retaguarda_id").first()


def saldo_cashback(venda):
    return saldo_cashback_offline(venda)


def saldo_cashback_offline(venda):
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    hoje = timezone.localdate()
    qs = CashbackMovimentoHub.objects.filter(
        hub=venda.hub,
        status=CashbackMovimentoHub.STATUS_ATIVO,
        centralizado_em__isnull=True,
    )
    qs = _filtrar_cliente(qs, venda)
    return money(
        qs.filter(models.Q(validade__isnull=True) | models.Q(validade__gte=hoje))
        .aggregate(
            saldo=Sum(
                Case(
                    When(tipo=CashbackMovimentoHub.TIPO_CREDITO, then="valor"),
                    default=Value(Decimal("0.00")) - models.F("valor"),
                    output_field=DecimalField(max_digits=18, decimal_places=2),
                )
            )
        )
        .get("saldo")
        or ZERO
    )


def vales_abertos(venda, *, bloquear=False, apenas_utilizaveis_offline=False):
    hoje = timezone.localdate()
    qs = ValeTrocaHub.objects.filter(
        hub=venda.hub,
        status=ValeTrocaHub.STATUS_ABERTO,
        saldo__gt=0,
    )
    if bloquear:
        qs = qs.select_for_update()
    if apenas_utilizaveis_offline:
        qs = qs.filter(sincronizado_em__isnull=True)
    qs = _filtrar_cliente(qs, venda)
    return qs.filter(models.Q(validade__isnull=True) | models.Q(validade__gte=hoje)).order_by("criado_em", "id")


def saldo_vale_troca(venda):
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    return money(vales_abertos(venda, apenas_utilizaveis_offline=True).aggregate(total=Sum("saldo")).get("total") or ZERO)


def validar_pagamento_beneficio(venda, forma, valor, autorizacao=""):
    tipo = (forma.tipo or "").upper()
    if tipo == "CASHBACK":
        return validar_cashback(venda, valor)
    if tipo in ("TROCA", "VALE_TROCA"):
        return validar_vale_troca(venda, valor, autorizacao)
    return None


def validar_cashback(venda, valor):
    config = cashback_config_ativa(venda.hub)
    if not config:
        raise ValueError("Não existe regra de cashback ativa.")
    if cliente_padrao(venda) and not config.consumidor_final_participa:
        raise ValueError("Cashback exige cliente identificado.")
    if money(valor) > saldo_cashback(venda):
        raise ValueError("O cashback informado é maior que o saldo disponível do cliente.")
    if money(valor) > money(venda.total):
        raise ValueError("O cashback não pode ser maior que o total da venda.")
    outros = _total_pago_outros(venda, "CASHBACK")
    if money(valor) > money(max(ZERO, venda.total - outros)):
        raise ValueError("Cashback não pode gerar troco; use apenas o saldo pendente da venda.")
    if config.valor_minimo_uso and money(valor) < money(config.valor_minimo_uso):
        raise ValueError("O valor de cashback usado é menor que o mínimo configurado.")
    limite = money(venda.total * Decimal(config.limite_uso_percentual or 0) / Decimal("100"))
    if money(valor) > limite:
        raise ValueError("O cashback usado ultrapassa o limite permitido para a venda.")


def validar_vale_troca(venda, valor, autorizacao=""):
    if cliente_padrao(venda):
        raise ValueError("Troca exige cliente identificado.")
    documento = str(autorizacao or "").strip()
    if not documento:
        raise ValueError("Selecione um cupom de troca válido para o pagamento.")
    vale = vales_abertos(venda, bloquear=True, apenas_utilizaveis_offline=True).filter(documento=documento).first()
    if not vale:
        raise ValueError("Cupom de troca inválido para este cliente.")
    if money(valor) > money(vale.saldo):
        raise ValueError("O valor informado é maior que o saldo do cupom de troca selecionado.")
    outros = _total_pago_outros(venda, "TROCA", "VALE_TROCA")
    if money(valor) > money(max(ZERO, venda.total - outros)):
        raise ValueError("Troca não pode gerar troco; use apenas o saldo pendente da venda.")


def aplicar_promocao(catalogo_item, quantidade):
    agora = timezone.now()
    candidatas = (
        PromocaoHub.objects.filter(
            hub=catalogo_item.hub,
            ativo=True,
        )
        .filter(models.Q(inicio__isnull=True) | models.Q(inicio__lte=agora))
        .filter(models.Q(fim__isnull=True) | models.Q(fim__gte=agora))
        .order_by("prioridade", "retaguarda_id")
    )
    promocao = next((p for p in candidatas if _promocao_aplica(p, catalogo_item)), None)
    preco = Decimal(catalogo_item.preco_venda)
    desconto = ZERO
    if not promocao:
        return preco, desconto, {}
    if promocao.tipo == PromocaoHub.TIPO_PERCENTUAL:
        desconto = money(preco * Decimal(quantidade) * Decimal(promocao.valor) / Decimal("100"))
    elif promocao.tipo == PromocaoHub.TIPO_VALOR_FIXO:
        desconto = money(promocao.valor) * Decimal(quantidade)
    elif promocao.tipo == PromocaoHub.TIPO_PRECO_FIXO:
        desconto = money(max(ZERO, (preco - Decimal(promocao.valor)) * Decimal(quantidade)))
    return preco, money(desconto), {
        "promocao_retaguarda_id": promocao.retaguarda_id,
        "promocao_nome": promocao.nome,
        "promocao_tipo": promocao.tipo,
        "promocao_valor": promocao.valor,
        "promocao_acumula_cashback": promocao.acumula_cashback,
    }


def registrar_beneficios_venda(venda, pagamentos):
    with transaction.atomic():
        for pagamento in pagamentos:
            tipo = (pagamento.tipo or "").upper()
            if tipo == "CASHBACK" and not CashbackMovimentoHub.objects.filter(venda=venda, tipo=CashbackMovimentoHub.TIPO_DEBITO).exists():
                CashbackMovimentoHub.objects.create(
                    hub=venda.hub,
                    cliente_uuid=venda.cliente_uuid,
                    cliente_retaguarda_id=venda.cliente_retaguarda_id,
                    venda=venda,
                    tipo=CashbackMovimentoHub.TIPO_DEBITO,
                    valor=pagamento.valor,
                    observacao=f"Uso na venda Hub {venda.venda_uuid}",
                )
            if tipo in ("TROCA", "VALE_TROCA"):
                _consumir_vale(venda, pagamento)
        _gerar_cashback(venda, pagamentos)


def _gerar_cashback(venda, pagamentos):
    config = cashback_config_ativa(venda.hub)
    if not config or (cliente_padrao(venda) and not config.consumidor_final_participa):
        return
    usado = sum((p.valor for p in pagamentos if (p.tipo or "").upper() == "CASHBACK"), ZERO)
    base_itens = sum(
        (item.total_item for item in venda.itens.all() if item.promocao_acumula_cashback),
        ZERO,
    )
    base = money(max(ZERO, base_itens - usado))
    if base < money(config.valor_minimo_geracao):
        return
    credito = money(base * Decimal(config.percentual or 0) / Decimal("100"))
    if credito <= ZERO or CashbackMovimentoHub.objects.filter(venda=venda, tipo=CashbackMovimentoHub.TIPO_CREDITO).exists():
        return
    CashbackMovimentoHub.objects.create(
        hub=venda.hub,
        cliente_uuid=venda.cliente_uuid,
        cliente_retaguarda_id=venda.cliente_retaguarda_id,
        venda=venda,
        tipo=CashbackMovimentoHub.TIPO_CREDITO,
        valor=credito,
        validade=timezone.localdate() + timedelta(days=int(config.validade_dias or 0)),
        observacao=f"Crédito gerado pela venda Hub {venda.venda_uuid}",
    )


def _consumir_vale(venda, pagamento):
    restante = money(pagamento.valor)
    documento = str(pagamento.vale_troca_documento or pagamento.autorizacao or "").strip()
    if not documento:
        raise ValueError("Selecione um cupom de troca válido para o pagamento.")
    vale = vales_abertos(venda, bloquear=True, apenas_utilizaveis_offline=True).filter(documento=documento).first()
    if not vale:
        raise ValueError("Cupom de troca inválido para este cliente.")
    if restante > money(vale.saldo):
        raise ValueError("Saldo de troca insuficiente para concluir a venda.")
    vale.saldo = money(vale.saldo - restante)
    if vale.saldo <= ZERO:
        vale.status = ValeTrocaHub.STATUS_USADO
    vale.save(update_fields=["saldo", "status", "atualizado_em"])
    ValeTrocaMovimentoHub.objects.create(
        vale=vale,
        venda=venda,
        tipo=ValeTrocaMovimentoHub.TIPO_USO,
        valor=restante,
        saldo_apos=vale.saldo,
        observacao=f"Uso na venda Hub {venda.venda_uuid}",
    )


def _filtrar_cliente(qs, venda):
    if venda.cliente_retaguarda_id:
        return qs.filter(cliente_retaguarda_id=venda.cliente_retaguarda_id)
    return qs.filter(cliente_uuid=venda.cliente_uuid)


def _total_pago_outros(venda, *tipos_excluidos):
    tipos = {tipo.upper() for tipo in tipos_excluidos}
    return money(
        venda.pagamentos.filter(status="ATIVO")
        .exclude(tipo__in=tipos)
        .aggregate(total=models.Sum("valor"))
        .get("total")
        or ZERO
    )


def _promocao_aplica(promocao, item):
    if promocao.escopo == PromocaoHub.ESCOPO_TODOS:
        return True
    if promocao.escopo == PromocaoHub.ESCOPO_PRODUTO:
        return item.retaguarda_produto_id in set(promocao.produto_ids or []) or promocao.produto_retaguarda_id == item.retaguarda_produto_id
    if promocao.escopo == PromocaoHub.ESCOPO_COLECAO:
        return bool(item.colecao_retaguarda_id and item.colecao_retaguarda_id in set(promocao.colecao_ids or []))
    if promocao.escopo == PromocaoHub.ESCOPO_GRUPO:
        return bool(item.grupo_retaguarda_id and item.grupo_retaguarda_id in set(promocao.grupo_ids or []))
    if promocao.escopo == PromocaoHub.ESCOPO_SUBGRUPO:
        return bool(item.subgrupo_retaguarda_id and item.subgrupo_retaguarda_id in set(promocao.subgrupo_ids or []))
    return False


def consultar_beneficios_cliente(hub, cliente_uuid):
    from core.models import ClienteHub

    cliente = ClienteHub.objects.filter(hub=hub, cliente_uuid=cliente_uuid, ativo=True, bloqueio=False).first()
    if not cliente:
        return {
            "cashback": {
                "saldo": "0.00",
                "saldo_retaguarda": "0.00",
                "saldo_offline_utilizavel": "0.00",
                "limite_uso_percentual": "0.0000",
                "valor_minimo_uso": "0.00",
            },
            "vales_troca": [],
        }
    base = type("VendaCliente", (), {
        "hub": hub,
        "cliente_uuid": cliente.cliente_uuid,
        "cliente_retaguarda_id": cliente.retaguarda_id,
        "cliente_padrao": cliente.cliente_padrao,
        "cliente_documento": cliente.documento,
    })()
    config = cashback_config_ativa(hub)
    saldo_offline = saldo_cashback_offline(base)
    vales = vales_abertos(base)
    return {
        "cashback": {
            "saldo": f"{saldo_offline:.2f}",
            "saldo_retaguarda": f"{cliente.cashback_saldo_retaguarda:.2f}",
            "saldo_offline_utilizavel": f"{saldo_offline:.2f}",
            "limite_uso_percentual": f"{config.limite_uso_percentual:.4f}" if config else "0.0000",
            "valor_minimo_uso": f"{config.valor_minimo_uso:.2f}" if config else "0.00",
        },
        "vales_troca": [
            {
                "documento": vale.documento,
                "saldo": f"{vale.saldo:.2f}",
                "validade": vale.validade.isoformat() if vale.validade else None,
                "utilizavel_offline": vale.sincronizado_em is None,
            }
            for vale in vales
        ],
    }
