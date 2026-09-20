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
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    hoje = timezone.localdate()
    qs = CashbackMovimentoHub.objects.filter(hub=venda.hub, status=CashbackMovimentoHub.STATUS_ATIVO)
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


def vales_abertos(venda):
    hoje = timezone.localdate()
    qs = ValeTrocaHub.objects.select_for_update().filter(
        hub=venda.hub,
        status=ValeTrocaHub.STATUS_ABERTO,
        saldo__gt=0,
    )
    qs = _filtrar_cliente(qs, venda)
    return qs.filter(models.Q(validade__isnull=True) | models.Q(validade__gte=hoje)).order_by("criado_em", "id")


def saldo_vale_troca(venda):
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    return money(vales_abertos(venda).aggregate(total=Sum("saldo")).get("total") or ZERO)


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
    if config.valor_minimo_uso and money(valor) < money(config.valor_minimo_uso):
        raise ValueError("O valor de cashback usado é menor que o mínimo configurado.")


def validar_vale_troca(venda, valor, autorizacao=""):
    if cliente_padrao(venda):
        raise ValueError("Troca exige cliente identificado.")
    if money(valor) > saldo_vale_troca(venda):
        raise ValueError("O valor de troca informado é maior que o saldo disponível do cliente.")
    if autorizacao:
        vale = vales_abertos(venda).filter(documento=autorizacao.strip()).first()
        if not vale:
            raise ValueError("Cupom de troca inválido para este cliente.")
        if money(valor) > money(vale.saldo):
            raise ValueError("O valor informado é maior que o saldo do cupom de troca selecionado.")


def aplicar_promocao(catalogo_item, quantidade):
    agora = timezone.now()
    promocao = (
        PromocaoHub.objects.filter(
            hub=catalogo_item.hub,
            ativo=True,
        )
        .filter(models.Q(sku_retaguarda_id=catalogo_item.retaguarda_sku_id) | models.Q(produto_retaguarda_id=catalogo_item.retaguarda_produto_id))
        .filter(models.Q(inicio__isnull=True) | models.Q(inicio__lte=agora))
        .filter(models.Q(fim__isnull=True) | models.Q(fim__gte=agora))
        .order_by("prioridade", "retaguarda_id")
        .first()
    )
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
    if not all(item.promocao_acumula_cashback for item in venda.itens.all()):
        return
    usado = sum((p.valor for p in pagamentos if (p.tipo or "").upper() == "CASHBACK"), ZERO)
    base = money(venda.total - usado)
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
    qs = vales_abertos(venda)
    if pagamento.vale_troca_documento:
        qs = qs.filter(documento=pagamento.vale_troca_documento)
    for vale in qs:
        if restante <= ZERO:
            break
        uso = money(min(vale.saldo, restante))
        vale.saldo = money(vale.saldo - uso)
        if vale.saldo <= ZERO:
            vale.status = ValeTrocaHub.STATUS_USADO
        vale.save(update_fields=["saldo", "status", "atualizado_em"])
        ValeTrocaMovimentoHub.objects.create(
            vale=vale,
            venda=venda,
            tipo=ValeTrocaMovimentoHub.TIPO_USO,
            valor=uso,
            saldo_apos=vale.saldo,
            observacao=f"Uso na venda Hub {venda.venda_uuid}",
        )
        restante = money(restante - uso)
    if restante > ZERO:
        raise ValueError("Saldo de troca insuficiente para concluir a venda.")


def _filtrar_cliente(qs, venda):
    if venda.cliente_retaguarda_id:
        return qs.filter(cliente_retaguarda_id=venda.cliente_retaguarda_id)
    return qs.filter(cliente_uuid=venda.cliente_uuid)
