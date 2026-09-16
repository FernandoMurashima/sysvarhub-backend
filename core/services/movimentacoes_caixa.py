from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.models import CaixaHub, MovimentacaoCaixaHub, SessaoCaixaHub, Terminal, TipoDespesaPdvHub
from core.services.caixa import CaixaError, obter_caixa_terminal, serializar_caixa, serializar_terminal
from core.services.operadores import serializar_operador


class MovimentacaoCaixaError(Exception):
    """Erro de domínio controlado para movimentações locais de caixa."""


class MovimentacaoCaixaConflictError(MovimentacaoCaixaError):
    pass


class MovimentacaoCaixaValidationError(MovimentacaoCaixaError):
    pass


def registrar_movimentacao_caixa(
    terminal,
    operador,
    sessao_operador,
    *,
    tipo,
    valor,
    tipo_despesa_id=None,
    documento="",
    historico="",
):
    tipo_normalizado = _validar_tipo(tipo)
    valor_decimal = validar_valor_movimentacao(valor)
    documento = _texto_opcional(documento, "documento", max_length=80)
    historico = _texto_opcional(historico, "historico", max_length=200)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        try:
            caixa = obter_caixa_terminal(terminal_bloqueado, exigir_ativo=True)
        except CaixaError as exc:
            raise MovimentacaoCaixaValidationError(str(exc)) from exc

        caixa_bloqueado = CaixaHub.objects.select_for_update().get(pk=caixa.pk)
        sessao_caixa = (
            SessaoCaixaHub.objects.select_for_update()
            .select_related("caixa")
            .filter(caixa=caixa_bloqueado, status=SessaoCaixaHub.STATUS_ABERTO)
            .first()
        )
        if not sessao_caixa:
            raise MovimentacaoCaixaConflictError("Caixa não está aberto.")

        tipo_despesa = _obter_tipo_despesa(terminal_bloqueado.hub, tipo_normalizado, tipo_despesa_id)
        if tipo_normalizado == MovimentacaoCaixaHub.TIPO_DESPESA:
            if tipo_despesa.exige_documento and not documento:
                raise MovimentacaoCaixaValidationError("Documento obrigatório para este tipo de despesa.")
            historico = historico or f"Despesa PDV - {tipo_despesa.descricao}"
        else:
            if tipo_normalizado == MovimentacaoCaixaHub.TIPO_SANGRIA:
                historico = historico or "Sangria PDV"
            else:
                historico = historico or "Suprimento PDV"

        ocorrido_em = timezone.now()
        movimento = MovimentacaoCaixaHub.objects.create(
            hub=terminal_bloqueado.hub,
            caixa=caixa_bloqueado,
            sessao_caixa=sessao_caixa,
            terminal=terminal_bloqueado,
            operador=operador,
            sessao_operador=sessao_operador,
            tipo=tipo_normalizado,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
            valor=valor_decimal,
            documento=documento or _gerar_documento_local(tipo_normalizado, ocorrido_em),
            historico=historico[:200],
            ocorrido_em=ocorrido_em,
            **_snapshot_tipo_despesa(tipo_despesa),
        )

    return movimento


def listar_movimentacoes_sessao_aberta(terminal):
    try:
        caixa = obter_caixa_terminal(terminal, exigir_ativo=True)
    except CaixaError as exc:
        raise MovimentacaoCaixaValidationError(str(exc)) from exc

    sessao_caixa = (
        SessaoCaixaHub.objects.select_related("caixa")
        .filter(caixa=caixa, status=SessaoCaixaHub.STATUS_ABERTO)
        .first()
    )
    if not sessao_caixa:
        raise MovimentacaoCaixaConflictError("Caixa não está aberto.")

    movimentacoes = (
        MovimentacaoCaixaHub.objects.select_related(
            "caixa",
            "sessao_caixa",
            "terminal",
            "operador",
        )
        .filter(hub=terminal.hub, sessao_caixa=sessao_caixa)
        .order_by("ocorrido_em", "id")
    )
    return sessao_caixa, movimentacoes


def validar_valor_movimentacao(valor):
    if valor is None or isinstance(valor, (bool, float)):
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.")
    if not isinstance(valor, str):
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.")

    texto = valor.strip()
    if not texto or "e" in texto.lower():
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.")

    try:
        decimal = Decimal(texto)
    except InvalidOperation as exc:
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.") from exc

    if not decimal.is_finite() or decimal <= 0:
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.")
    casas_decimais = max(-decimal.as_tuple().exponent, 0)
    digitos_inteiros = max(decimal.adjusted() + 1, 1)
    if casas_decimais > 2 or digitos_inteiros > 10:
        raise MovimentacaoCaixaValidationError("Valor da movimentação inválido.")
    return decimal.quantize(Decimal("0.01"))


def serializar_movimentacao_caixa(movimento):
    return {
        "uuid": str(movimento.movimento_uuid),
        "tipo": movimento.tipo,
        "status": movimento.status,
        "valor": f"{movimento.valor:.2f}",
        "documento": movimento.documento,
        "historico": movimento.historico,
        "ocorrido_em": movimento.ocorrido_em.isoformat(),
        "caixa": serializar_caixa(movimento.caixa),
        "sessao_caixa_uuid": str(movimento.sessao_caixa.sessao_uuid),
        "terminal": serializar_terminal(movimento.terminal),
        "operador": serializar_operador(movimento.operador),
        "tipo_despesa": _serializar_snapshot_tipo_despesa(movimento),
    }


def _validar_tipo(tipo):
    if not isinstance(tipo, str):
        raise MovimentacaoCaixaValidationError("Tipo de movimentação inválido.")
    tipo = tipo.strip().upper()
    tipos_validos = {
        MovimentacaoCaixaHub.TIPO_DESPESA,
        MovimentacaoCaixaHub.TIPO_SANGRIA,
        MovimentacaoCaixaHub.TIPO_SUPRIMENTO,
    }
    if tipo not in tipos_validos:
        raise MovimentacaoCaixaValidationError("Tipo de movimentação inválido.")
    return tipo


def _obter_tipo_despesa(hub, tipo, tipo_despesa_id):
    if tipo != MovimentacaoCaixaHub.TIPO_DESPESA:
        if tipo_despesa_id not in (None, ""):
            raise MovimentacaoCaixaValidationError("Tipo de despesa não deve ser informado para esta movimentação.")
        return None

    retaguarda_id = _inteiro_positivo(tipo_despesa_id, "tipo_despesa_id")
    try:
        return TipoDespesaPdvHub.objects.get(
            hub=hub,
            retaguarda_id=retaguarda_id,
            presente_retaguarda=True,
            ativo=True,
        )
    except TipoDespesaPdvHub.DoesNotExist as exc:
        raise MovimentacaoCaixaValidationError("Tipo de despesa PDV inválido.") from exc


def _snapshot_tipo_despesa(tipo_despesa):
    if tipo_despesa is None:
        return {}
    return {
        "tipo_despesa_retaguarda_id": tipo_despesa.retaguarda_id,
        "tipo_despesa_codigo": tipo_despesa.codigo,
        "tipo_despesa_descricao": tipo_despesa.descricao,
        "tipo_despesa_exige_documento": tipo_despesa.exige_documento,
        "natureza_retaguarda_id": tipo_despesa.natureza_retaguarda_id,
        "natureza_codigo": tipo_despesa.natureza_codigo,
        "natureza_descricao": tipo_despesa.natureza_descricao,
        "natureza_categoria_principal": tipo_despesa.natureza_categoria_principal,
        "natureza_subcategoria": tipo_despesa.natureza_subcategoria,
        "natureza_tipo": tipo_despesa.natureza_tipo,
        "natureza_status": tipo_despesa.natureza_status,
        "natureza_tipo_natureza": tipo_despesa.natureza_tipo_natureza,
        "natureza_operacao": tipo_despesa.natureza_operacao,
        "natureza_categoria_gerencial": tipo_despesa.natureza_categoria_gerencial,
        "natureza_movimenta_financeiro": tipo_despesa.natureza_movimenta_financeiro,
        "natureza_entra_dre": tipo_despesa.natureza_entra_dre,
    }


def _serializar_snapshot_tipo_despesa(movimento):
    if movimento.tipo != MovimentacaoCaixaHub.TIPO_DESPESA:
        return None
    return {
        "id": movimento.tipo_despesa_retaguarda_id,
        "codigo": movimento.tipo_despesa_codigo,
        "descricao": movimento.tipo_despesa_descricao,
        "exige_documento": movimento.tipo_despesa_exige_documento,
        "natureza": {
            "id": movimento.natureza_retaguarda_id,
            "codigo": movimento.natureza_codigo,
            "descricao": movimento.natureza_descricao,
            "categoria_principal": movimento.natureza_categoria_principal,
            "subcategoria": movimento.natureza_subcategoria,
            "tipo": movimento.natureza_tipo,
            "status": movimento.natureza_status,
            "tipo_natureza": movimento.natureza_tipo_natureza,
            "natureza_operacao": movimento.natureza_operacao,
            "categoria_gerencial": movimento.natureza_categoria_gerencial,
            "movimenta_financeiro": movimento.natureza_movimenta_financeiro,
            "entra_dre": movimento.natureza_entra_dre,
        },
    }


def _texto_opcional(valor, campo, *, max_length):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise MovimentacaoCaixaValidationError(f"{campo} inválido.")
    return valor.strip()[:max_length]


def _inteiro_positivo(valor, campo):
    if isinstance(valor, bool):
        raise MovimentacaoCaixaValidationError(f"{campo} inválido.")
    try:
        inteiro = int(valor)
    except (TypeError, ValueError) as exc:
        raise MovimentacaoCaixaValidationError(f"{campo} inválido.") from exc
    if inteiro <= 0 or str(valor).strip() != str(inteiro):
        raise MovimentacaoCaixaValidationError(f"{campo} inválido.")
    return inteiro


def _gerar_documento_local(tipo, ocorrido_em):
    prefixos = {
        MovimentacaoCaixaHub.TIPO_DESPESA: "DESP",
        MovimentacaoCaixaHub.TIPO_SANGRIA: "SANG",
        MovimentacaoCaixaHub.TIPO_SUPRIMENTO: "SUP",
    }
    return f"{prefixos[tipo]}-{ocorrido_em:%Y%m%d%H%M%S%f}"[:80]
