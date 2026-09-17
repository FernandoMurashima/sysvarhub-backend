from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from core.models import (
    FechamentoDiaFormaHub,
    FechamentoDiaHub,
    MovimentacaoCaixaHub,
    SessaoCaixaHub,
    VendaHub,
    VendaPagamentoHub,
)
from core.services.caixa import serializar_terminal
from core.services.operadores import serializar_operador


ZERO = Decimal("0.00")
ORDEM_TIPOS = {"DINHEIRO": 0, "PIX": 1, "CREDITO": 2, "DEBITO": 3}


class FechamentoDiaError(Exception):
    """Erro de domínio controlado para fechamento diário."""


class FechamentoDiaValidationError(FechamentoDiaError):
    pass


class FechamentoDiaConflictError(FechamentoDiaError):
    def __init__(self, mensagem, *, fechamento=None, preview=None):
        super().__init__(mensagem)
        self.fechamento = fechamento
        self.preview = preview


class FechamentoDiaConsistencyError(FechamentoDiaError):
    pass


def obter_previa_fechamento_dia(terminal, data_operacional):
    data = _validar_data_operacional(data_operacional)
    fechado = _obter_fechamento(terminal.hub, data)
    preview = _montar_previa(terminal.hub, data)
    if fechado:
        preview["fechado"] = True
        preview["fechamento"] = serializar_fechamento_dia(fechado)
        preview["pode_fechar"] = False
        preview["impedimentos"].append("Dia operacional já fechado.")
    return preview


def fechar_dia(terminal, operador, data_operacional, formas_pagamento, observacao=""):
    data = _validar_data_operacional(data_operacional)
    observacao = _validar_observacao(observacao)
    if not isinstance(formas_pagamento, list):
        raise FechamentoDiaValidationError("Formas de pagamento inválidas.")

    with transaction.atomic():
        fechamento_existente = (
            FechamentoDiaHub.objects.select_for_update()
            .filter(hub=terminal.hub, data_operacional=data)
            .first()
        )
        if fechamento_existente:
            raise FechamentoDiaConflictError("Dia operacional já fechado.", fechamento=fechamento_existente)

        preview = _montar_previa(terminal.hub, data)
        if not preview["pode_fechar"]:
            raise FechamentoDiaConflictError("Fechamento do dia bloqueado.", preview=preview)

        conferencias = _validar_conferencias(formas_pagamento, preview["formas_pagamento"])
        _validar_consistencia(preview)

        fechado_em = timezone.now()
        formas = []
        todas_ok = True
        total_conferido = ZERO
        for forma in preview["formas_pagamento"]:
            valor_sistema = Decimal(forma["valor_sistema"])
            valor_conferido = conferencias[forma["tipo"]]
            diferenca = (valor_conferido - valor_sistema).quantize(Decimal("0.01"))
            situacao = _situacao_forma(diferenca)
            todas_ok = todas_ok and situacao == FechamentoDiaFormaHub.SITUACAO_OK
            total_conferido += valor_conferido
            formas.append(
                {
                    **forma,
                    "valor_conferido": _moeda(valor_conferido),
                    "diferenca": _moeda(diferenca),
                    "situacao": situacao,
                }
            )

        total_sistema = Decimal(preview["vendas"]["total"])
        diferenca_total = (total_conferido - total_sistema).quantize(Decimal("0.01"))
        situacao = FechamentoDiaHub.SITUACAO_OK if todas_ok else FechamentoDiaHub.SITUACAO_DIVERGENTE
        snapshot = {
            **preview,
            "formas_pagamento": formas,
            "total_sistema": _moeda(total_sistema),
            "total_conferido": _moeda(total_conferido),
            "diferenca_total": _moeda(diferenca_total),
            "situacao": situacao,
            "operador_fechamento": serializar_operador(operador),
            "terminal_fechamento": serializar_terminal(terminal),
            "fechado_em": fechado_em.isoformat(),
            "observacao": observacao,
        }

        try:
            fechamento = FechamentoDiaHub.objects.create(
                hub=terminal.hub,
                data_operacional=data,
                quantidade_vendas=preview["vendas"]["quantidade"],
                total_vendas=total_sistema,
                total_sistema=total_sistema,
                total_conferido=total_conferido,
                diferenca_total=diferenca_total,
                situacao=situacao,
                operador_fechamento=operador,
                terminal_fechamento=terminal,
                fechado_em=fechado_em,
                observacao=observacao,
                resumo_snapshot=snapshot,
            )
        except IntegrityError as exc:
            existente = _obter_fechamento(terminal.hub, data)
            raise FechamentoDiaConflictError("Dia operacional já fechado.", fechamento=existente) from exc

        FechamentoDiaFormaHub.objects.bulk_create(
            [
                FechamentoDiaFormaHub(
                    fechamento=fechamento,
                    tipo=forma["tipo"],
                    descricao=forma["descricao"],
                    quantidade=forma["quantidade"],
                    valor_sistema=Decimal(forma["valor_sistema"]),
                    valor_conferido=Decimal(forma["valor_conferido"]),
                    diferenca=Decimal(forma["diferenca"]),
                    situacao=forma["situacao"],
                    detalhes_snapshot=forma["detalhes"],
                )
                for forma in formas
            ]
        )

    return fechamento


def serializar_fechamento_dia(fechamento):
    formas = list(fechamento.formas.all()) if hasattr(fechamento, "_prefetched_objects_cache") else list(fechamento.formas.all())
    return {
        "uuid": str(fechamento.fechamento_uuid),
        "data_operacional": fechamento.data_operacional.isoformat(),
        "quantidade_vendas": fechamento.quantidade_vendas,
        "total_vendas": _moeda(fechamento.total_vendas),
        "total_sistema": _moeda(fechamento.total_sistema),
        "total_conferido": _moeda(fechamento.total_conferido),
        "diferenca_total": _moeda(fechamento.diferenca_total),
        "situacao": fechamento.situacao,
        "operador_fechamento": serializar_operador(fechamento.operador_fechamento),
        "terminal_fechamento": serializar_terminal(fechamento.terminal_fechamento),
        "fechado_em": fechamento.fechado_em.isoformat(),
        "observacao": fechamento.observacao,
        "formas_pagamento": [
            {
                "tipo": forma.tipo,
                "descricao": forma.descricao,
                "quantidade": forma.quantidade,
                "valor_sistema": _moeda(forma.valor_sistema),
                "valor_conferido": _moeda(forma.valor_conferido),
                "diferenca": _moeda(forma.diferenca),
                "situacao": forma.situacao,
                "detalhes": forma.detalhes_snapshot,
            }
            for forma in sorted(formas, key=lambda item: _chave_ordem_tipo(item.tipo))
        ],
        "resumo_snapshot": fechamento.resumo_snapshot,
    }


def _montar_previa(hub, data_operacional):
    inicio, fim = _intervalo_data_operacional(data_operacional)
    vendas = VendaHub.objects.filter(
        hub=hub,
        status=VendaHub.STATUS_FINALIZADA,
        finalizada_em__gte=inicio,
        finalizada_em__lt=fim,
    )
    vendas_totais = vendas.aggregate(quantidade=Count("id"), total=Sum("total"), troco=Sum("troco"))
    total_vendas = _decimal(vendas_totais["total"])
    troco = _decimal(vendas_totais["troco"])

    formas = _consolidar_formas(vendas, troco)
    caixas = _resumo_caixas(hub, inicio, fim)
    movimentacoes = _resumo_movimentacoes(hub, inicio, fim)
    impedimentos = _impedimentos(hub)
    total_sistema = sum((Decimal(forma["valor_sistema"]) for forma in formas), ZERO).quantize(Decimal("0.01"))
    consistencia_ok = total_sistema == total_vendas

    return {
        "data_operacional": data_operacional.isoformat(),
        "fechado": False,
        "vendas": {
            "quantidade": vendas_totais["quantidade"] or 0,
            "total": _moeda(total_vendas),
            "troco": _moeda(troco),
        },
        "formas_pagamento": formas,
        "caixas": caixas,
        "movimentacoes": movimentacoes,
        "consistencia": {
            "ok": consistencia_ok,
            "total_vendas": _moeda(total_vendas),
            "total_formas": _moeda(total_sistema),
            "diferenca": _moeda(total_sistema - total_vendas),
        },
        "pode_fechar": not impedimentos,
        "impedimentos": impedimentos,
    }


def _consolidar_formas(vendas, troco):
    pagamentos = VendaPagamentoHub.objects.filter(
        venda__in=vendas,
        status=VendaPagamentoHub.STATUS_ATIVO,
    )
    detalhes_por_tipo = defaultdict(list)
    formas_por_tipo = {}
    for item in (
        pagamentos.values(
            "tipo",
            "retaguarda_forma_pagamento_id",
            "codigo",
            "descricao",
            "adquirente",
        )
        .annotate(quantidade=Count("id"), valor=Sum("valor"))
        .order_by("tipo", "descricao", "codigo", "retaguarda_forma_pagamento_id")
    ):
        tipo = item["tipo"]
        valor = _decimal(item["valor"])
        if tipo == "DINHEIRO":
            valor -= troco
        detalhe = {
            "retaguarda_forma_pagamento_id": item["retaguarda_forma_pagamento_id"],
            "codigo": item["codigo"],
            "descricao": item["descricao"],
            "tipo": tipo,
            "adquirente": item["adquirente"],
            "quantidade": item["quantidade"] or 0,
            "valor": _moeda(valor),
        }
        detalhes_por_tipo[tipo].append(detalhe)
        if tipo not in formas_por_tipo:
            formas_por_tipo[tipo] = {
                "tipo": tipo,
                "descricao": _descricao_tipo(tipo),
                "quantidade": 0,
                "valor_sistema": ZERO,
            }
        formas_por_tipo[tipo]["quantidade"] += detalhe["quantidade"]
        formas_por_tipo[tipo]["valor_sistema"] += valor

    formas = []
    for tipo, forma in formas_por_tipo.items():
        formas.append(
            {
                "tipo": tipo,
                "descricao": forma["descricao"],
                "quantidade": forma["quantidade"],
                "valor_sistema": _moeda(forma["valor_sistema"]),
                "detalhes": detalhes_por_tipo[tipo],
            }
        )
    return sorted(formas, key=lambda item: _chave_ordem_tipo(item["tipo"]))


def _resumo_caixas(hub, inicio, fim):
    sessoes = SessaoCaixaHub.objects.filter(caixa__hub=hub).filter(
        Q(aberto_em__gte=inicio, aberto_em__lt=fim)
        | Q(fechado_em__gte=inicio, fechado_em__lt=fim)
        | Q(status=SessaoCaixaHub.STATUS_ABERTO)
    )
    totais = sessoes.aggregate(
        valor_esperado=Sum("valor_esperado_fechamento"),
        valor_contado=Sum("valor_contado_fechamento"),
        diferenca=Sum("diferenca_fechamento"),
    )
    return {
        "sessoes": sessoes.count(),
        "abertos": sessoes.filter(status=SessaoCaixaHub.STATUS_ABERTO).count(),
        "fechados": sessoes.filter(status=SessaoCaixaHub.STATUS_FECHADO).count(),
        "valor_esperado": _moeda(totais["valor_esperado"]),
        "valor_contado": _moeda(totais["valor_contado"]),
        "diferenca": _moeda(totais["diferenca"]),
    }


def _resumo_movimentacoes(hub, inicio, fim):
    totais = {
        MovimentacaoCaixaHub.TIPO_DESPESA: ZERO,
        MovimentacaoCaixaHub.TIPO_SANGRIA: ZERO,
        MovimentacaoCaixaHub.TIPO_SUPRIMENTO: ZERO,
    }
    for item in (
        MovimentacaoCaixaHub.objects.filter(
            hub=hub,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
            ocorrido_em__gte=inicio,
            ocorrido_em__lt=fim,
        )
        .values("tipo")
        .annotate(total=Sum("valor"))
    ):
        totais[item["tipo"]] = _decimal(item["total"])
    return {
        "despesas": _moeda(totais[MovimentacaoCaixaHub.TIPO_DESPESA]),
        "sangrias": _moeda(totais[MovimentacaoCaixaHub.TIPO_SANGRIA]),
        "suprimentos": _moeda(totais[MovimentacaoCaixaHub.TIPO_SUPRIMENTO]),
    }


def _impedimentos(hub):
    impedimentos = []
    if SessaoCaixaHub.objects.filter(caixa__hub=hub, status=SessaoCaixaHub.STATUS_ABERTO).exists():
        impedimentos.append("Existe sessão de caixa aberta.")
    if VendaHub.objects.filter(hub=hub, status=VendaHub.STATUS_ABERTA).exists():
        impedimentos.append("Existe venda em andamento.")
    return impedimentos


def _validar_conferencias(payload, formas_sistema):
    tipos_sistema = {forma["tipo"] for forma in formas_sistema}
    conferencias = {}
    for item in payload:
        if not isinstance(item, dict):
            raise FechamentoDiaValidationError("Forma de pagamento inválida.")
        tipo = (item.get("tipo") or "").strip().upper()
        if not tipo:
            raise FechamentoDiaValidationError("Tipo de pagamento obrigatório.")
        if tipo in conferencias:
            raise FechamentoDiaValidationError("Tipo de pagamento duplicado.")
        if tipo not in tipos_sistema:
            raise FechamentoDiaValidationError("Tipo de pagamento desconhecido.")
        conferencias[tipo] = _validar_valor_conferido(item.get("valor_conferido"))

    ausentes = sorted(tipos_sistema - set(conferencias), key=_chave_ordem_tipo)
    if ausentes:
        raise FechamentoDiaValidationError("Conferência incompleta para as formas de pagamento.")
    return conferencias


def _validar_consistencia(preview):
    if not preview["consistencia"]["ok"]:
        raise FechamentoDiaConsistencyError("Pagamentos não reconciliam com o total das vendas finalizadas.")


def _validar_data_operacional(valor):
    if valor is None:
        valor = timezone.localdate().isoformat()
    if not isinstance(valor, str):
        raise FechamentoDiaValidationError("Data operacional inválida.")
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError as exc:
        raise FechamentoDiaValidationError("Data operacional inválida.") from exc


def _intervalo_data_operacional(data):
    tz = timezone.get_current_timezone()
    inicio = timezone.make_aware(datetime.combine(data, datetime.min.time()), tz)
    fim = inicio + timedelta(days=1)
    return inicio, fim


def _validar_valor_conferido(valor):
    if valor is None or isinstance(valor, bool) or not isinstance(valor, str):
        raise FechamentoDiaValidationError("Valor conferido inválido.")
    texto = valor.strip()
    if not texto or "e" in texto.lower():
        raise FechamentoDiaValidationError("Valor conferido inválido.")
    try:
        decimal = Decimal(texto)
    except InvalidOperation as exc:
        raise FechamentoDiaValidationError("Valor conferido inválido.") from exc
    if not decimal.is_finite() or decimal < 0:
        raise FechamentoDiaValidationError("Valor conferido inválido.")
    if max(-decimal.as_tuple().exponent, 0) != 2:
        raise FechamentoDiaValidationError("Valor conferido inválido.")
    if max(decimal.adjusted() + 1, 1) > 16:
        raise FechamentoDiaValidationError("Valor conferido inválido.")
    return decimal.quantize(Decimal("0.01"))


def _validar_observacao(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise FechamentoDiaValidationError("Observação inválida.")
    texto = valor.strip()
    if len(texto) > 500:
        raise FechamentoDiaValidationError("Observação inválida.")
    return texto


def _situacao_forma(diferenca):
    if diferenca == 0:
        return FechamentoDiaFormaHub.SITUACAO_OK
    if diferenca > 0:
        return FechamentoDiaFormaHub.SITUACAO_SOBRA
    return FechamentoDiaFormaHub.SITUACAO_FALTA


def _obter_fechamento(hub, data):
    return (
        FechamentoDiaHub.objects.select_related("operador_fechamento", "terminal_fechamento")
        .prefetch_related("formas")
        .filter(hub=hub, data_operacional=data)
        .first()
    )


def _descricao_tipo(tipo):
    mapa = {
        "DINHEIRO": "Dinheiro",
        "PIX": "PIX",
        "CREDITO": "Crédito",
        "DEBITO": "Débito",
    }
    return mapa.get(tipo, tipo.title())


def _chave_ordem_tipo(tipo):
    return (ORDEM_TIPOS.get(tipo, 99), tipo)


def _decimal(valor):
    return (valor or ZERO).quantize(Decimal("0.01"))


def _moeda(valor):
    return f"{_decimal(valor):.2f}"
