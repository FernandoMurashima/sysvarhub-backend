from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum

from core.models import MovimentacaoCaixaHub, SessaoCaixaHub, VendaHub, VendaPagamentoHub
from core.services.caixa import (
    CaixaConflictError,
    CaixaError,
    obter_caixa_terminal,
    serializar_sessao_caixa,
)
from core.services.movimentacoes_caixa import serializar_movimentacao_caixa


ZERO = Decimal("0.00")


def obter_resumo_caixa(terminal):
    caixa = obter_caixa_terminal(terminal, exigir_ativo=True)

    with transaction.atomic():
        sessao = (
            SessaoCaixaHub.objects.select_related(
                "caixa",
                "terminal_abertura",
                "operador_abertura",
                "sessao_operador_abertura",
            )
            .filter(caixa=caixa, status=SessaoCaixaHub.STATUS_ABERTO)
            .first()
        )
        if not sessao:
            raise CaixaConflictError("Caixa não está aberto.")

        return calcular_resumo_sessao(terminal, sessao)


def calcular_resumo_sessao(terminal, sessao):
    vendas = VendaHub.objects.filter(
        hub=terminal.hub,
        sessao_caixa=sessao,
        status=VendaHub.STATUS_FINALIZADA,
    )
    vendas_totais = vendas.aggregate(
        quantidade=Count("id"),
        total=Sum("total"),
        valor_recebido=Sum("valor_recebido"),
        troco=Sum("troco"),
    )
    quantidade_vendas = vendas_totais["quantidade"] or 0
    total_vendas = _decimal(vendas_totais["total"])
    valor_recebido = _decimal(vendas_totais["valor_recebido"])
    troco = _decimal(vendas_totais["troco"])

    pagamentos = VendaPagamentoHub.objects.filter(
        venda__hub=terminal.hub,
        venda__sessao_caixa=sessao,
        venda__status=VendaHub.STATUS_FINALIZADA,
        status=VendaPagamentoHub.STATUS_ATIVO,
    )
    formas = [
        {
            "id": item["retaguarda_forma_pagamento_id"],
            "codigo": item["codigo"],
            "descricao": item["descricao"],
            "tipo": item["tipo"],
            "quantidade": item["quantidade"],
            "valor": _moeda(item["valor"]),
        }
        for item in pagamentos.values(
            "retaguarda_forma_pagamento_id",
            "codigo",
            "descricao",
            "tipo",
        )
        .annotate(quantidade=Count("id"), valor=Sum("valor"))
        .order_by("tipo", "descricao", "codigo", "retaguarda_forma_pagamento_id")
    ]
    dinheiro_bruto = _decimal(
        pagamentos.filter(tipo="DINHEIRO").aggregate(total=Sum("valor")).get("total")
    )
    dinheiro_liquido = dinheiro_bruto - troco

    movimentacoes = (
        MovimentacaoCaixaHub.objects.select_related(
            "caixa",
            "sessao_caixa",
            "terminal",
            "operador",
        )
        .filter(
            hub=terminal.hub,
            sessao_caixa=sessao,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
        )
        .order_by("ocorrido_em", "id")
    )
    totais_movimentacoes = _totais_movimentacoes(movimentacoes)

    total_despesas = totais_movimentacoes[MovimentacaoCaixaHub.TIPO_DESPESA]["total"]
    total_sangrias = totais_movimentacoes[MovimentacaoCaixaHub.TIPO_SANGRIA]["total"]
    total_suprimentos = totais_movimentacoes[MovimentacaoCaixaHub.TIPO_SUPRIMENTO]["total"]
    dinheiro_esperado = (
        sessao.valor_abertura
        + dinheiro_liquido
        + total_suprimentos
        - total_sangrias
        - total_despesas
    )

    return {
        "sessao": serializar_sessao_caixa(sessao),
        "vendas": {
            "quantidade": quantidade_vendas,
            "total": _moeda(total_vendas),
            "valor_recebido": _moeda(valor_recebido),
            "troco": _moeda(troco),
        },
        "pagamentos": {
            "formas": formas,
            "dinheiro_bruto": _moeda(dinheiro_bruto),
            "troco": _moeda(troco),
            "dinheiro_liquido": _moeda(dinheiro_liquido),
        },
        "movimentacoes": {
            "despesas": _serializar_total_movimento(totais_movimentacoes[MovimentacaoCaixaHub.TIPO_DESPESA]),
            "sangrias": _serializar_total_movimento(totais_movimentacoes[MovimentacaoCaixaHub.TIPO_SANGRIA]),
            "suprimentos": _serializar_total_movimento(totais_movimentacoes[MovimentacaoCaixaHub.TIPO_SUPRIMENTO]),
            "itens": [serializar_movimentacao_caixa(movimento) for movimento in movimentacoes],
        },
        "dinheiro": {
            "valor_abertura": _moeda(sessao.valor_abertura),
            "vendas_dinheiro_bruto": _moeda(dinheiro_bruto),
            "troco": _moeda(troco),
            "vendas_dinheiro_liquido": _moeda(dinheiro_liquido),
            "suprimentos": _moeda(total_suprimentos),
            "sangrias": _moeda(total_sangrias),
            "despesas": _moeda(total_despesas),
            "esperado": _moeda(dinheiro_esperado),
        },
    }


def _totais_movimentacoes(queryset):
    totais = {
        MovimentacaoCaixaHub.TIPO_DESPESA: {"quantidade": 0, "total": ZERO},
        MovimentacaoCaixaHub.TIPO_SANGRIA: {"quantidade": 0, "total": ZERO},
        MovimentacaoCaixaHub.TIPO_SUPRIMENTO: {"quantidade": 0, "total": ZERO},
    }
    for item in queryset.values("tipo").annotate(quantidade=Count("id"), total=Sum("valor")):
        totais[item["tipo"]] = {
            "quantidade": item["quantidade"] or 0,
            "total": _decimal(item["total"]),
        }
    return totais


def _serializar_total_movimento(total):
    return {
        "quantidade": total["quantidade"],
        "total": _moeda(total["total"]),
    }


def _decimal(valor):
    return valor or ZERO


def _moeda(valor):
    return f"{_decimal(valor):.2f}"
