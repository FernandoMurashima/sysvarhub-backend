from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import CaixaHub, SessaoCaixaHub, Terminal
from core.services.operadores import serializar_operador


class CaixaError(Exception):
    """Erro de domínio controlado para operações de caixa local."""


class CaixaConflictError(CaixaError):
    def __init__(self, mensagem, sessao=None):
        super().__init__(mensagem)
        self.sessao = sessao


class ValorAberturaError(CaixaError):
    pass


def obter_caixa_terminal(terminal, *, exigir_ativo=True):
    if terminal.caixa_retaguarda_id is None:
        raise CaixaError("Terminal sem Caixa configurado.")

    try:
        caixa = CaixaHub.objects.get(
            hub=terminal.hub,
            retaguarda_id=terminal.caixa_retaguarda_id,
        )
    except CaixaHub.DoesNotExist as exc:
        raise CaixaError("Caixa não encontrado no Hub.") from exc

    if exigir_ativo and not caixa.ativo:
        raise CaixaError("Caixa inativo.")

    return caixa


def obter_sessao_caixa_aberta(caixa):
    return (
        SessaoCaixaHub.objects.select_related(
            "caixa",
            "terminal_abertura",
            "operador_abertura",
            "sessao_operador_abertura",
            "terminal_fechamento",
            "operador_fechamento",
            "sessao_operador_fechamento",
        )
        .filter(caixa=caixa, status=SessaoCaixaHub.STATUS_ABERTO)
        .first()
    )


def consultar_status_caixa(terminal):
    caixa = obter_caixa_terminal(terminal, exigir_ativo=False)
    sessao = obter_sessao_caixa_aberta(caixa)
    return {
        "caixa": serializar_caixa(caixa),
        "aberto": sessao is not None,
        "sessao": serializar_sessao_caixa(sessao) if sessao else None,
    }


def abrir_caixa(terminal, operador, sessao_operador, *, valor_abertura):
    valor = validar_valor_abertura(valor_abertura)
    caixa = obter_caixa_terminal(terminal, exigir_ativo=True)

    with transaction.atomic():
        caixa_bloqueado = CaixaHub.objects.select_for_update().get(pk=caixa.pk)
        sessao_aberta = obter_sessao_caixa_aberta(caixa_bloqueado)
        if sessao_aberta:
            raise CaixaConflictError("Caixa já está aberto.", sessao=sessao_aberta)

        sessao = SessaoCaixaHub(
            caixa=caixa_bloqueado,
            status=SessaoCaixaHub.STATUS_ABERTO,
            chave_caixa_aberto=caixa_bloqueado.pk,
            valor_abertura=valor,
            aberto_em=timezone.now(),
            terminal_abertura=terminal,
            operador_abertura=operador,
            sessao_operador_abertura=sessao_operador,
        )
        try:
            with transaction.atomic():
                sessao.save()
        except IntegrityError as exc:
            sessao_aberta = obter_sessao_caixa_aberta(caixa_bloqueado)
            if sessao_aberta:
                raise CaixaConflictError("Caixa já está aberto.", sessao=sessao_aberta) from exc
            raise

    return sessao


def fechar_caixa(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        caixa = obter_caixa_terminal(terminal_bloqueado, exigir_ativo=False)
        caixa_bloqueado = CaixaHub.objects.select_for_update().get(pk=caixa.pk)
        sessao = (
            SessaoCaixaHub.objects.select_for_update()
            .filter(caixa=caixa_bloqueado, status=SessaoCaixaHub.STATUS_ABERTO)
            .first()
        )
        if not sessao:
            raise CaixaConflictError("Caixa não está aberto.")
        from core.services.vendas import existe_venda_aberta_sessao_caixa

        if existe_venda_aberta_sessao_caixa(sessao):
            raise CaixaConflictError("Existe venda em andamento neste caixa.")
        from core.services.vendas import limpar_contexto_venda_terminal

        limpar_contexto_venda_terminal(terminal_bloqueado)

        sessao.status = SessaoCaixaHub.STATUS_FECHADO
        sessao.fechado_em = timezone.now()
        sessao.terminal_fechamento = terminal_bloqueado
        sessao.operador_fechamento = operador
        sessao.sessao_operador_fechamento = sessao_operador
        sessao.chave_caixa_aberto = None
        sessao.save(
            update_fields=[
                "status",
                "fechado_em",
                "terminal_fechamento",
                "operador_fechamento",
                "sessao_operador_fechamento",
                "chave_caixa_aberto",
                "atualizado_em",
            ]
        )

    return sessao


def validar_valor_abertura(valor):
    if valor is None or isinstance(valor, bool):
        raise ValorAberturaError("Valor de abertura inválido.")
    if not isinstance(valor, str):
        raise ValorAberturaError("Valor de abertura inválido.")

    texto = valor.strip()
    if not texto:
        raise ValorAberturaError("Valor de abertura inválido.")
    if "e" in texto.lower():
        raise ValorAberturaError("Valor de abertura inválido.")

    try:
        decimal = Decimal(texto)
    except InvalidOperation as exc:
        raise ValorAberturaError("Valor de abertura inválido.") from exc

    if not decimal.is_finite() or decimal < 0:
        raise ValorAberturaError("Valor de abertura inválido.")
    casas_decimais = max(-decimal.as_tuple().exponent, 0)
    digitos_inteiros = max(decimal.adjusted() + 1, 1)

    if casas_decimais > 2:
        raise ValorAberturaError("Valor de abertura inválido.")
    if digitos_inteiros > 10:
        raise ValorAberturaError("Valor de abertura inválido.")

    return decimal.quantize(Decimal("0.01"))


def serializar_sessao_caixa(sessao):
    if sessao is None:
        return None

    payload = {
        "uuid": str(sessao.sessao_uuid),
        "status": sessao.status,
        "valor_abertura": f"{sessao.valor_abertura:.2f}",
        "aberto_em": sessao.aberto_em.isoformat(),
        "fechado_em": sessao.fechado_em.isoformat() if sessao.fechado_em else None,
        "caixa": serializar_caixa(sessao.caixa),
        "terminal_abertura": serializar_terminal(sessao.terminal_abertura),
        "operador_abertura": serializar_operador(sessao.operador_abertura),
        "terminal_fechamento": (
            serializar_terminal(sessao.terminal_fechamento)
            if sessao.terminal_fechamento
            else None
        ),
        "operador_fechamento": (
            serializar_operador(sessao.operador_fechamento)
            if sessao.operador_fechamento
            else None
        ),
    }
    return payload


def serializar_caixa(caixa):
    return {
        "id": caixa.retaguarda_id,
        "codigo": caixa.codigo,
        "descricao": caixa.descricao,
        "ativo": caixa.ativo,
    }


def serializar_terminal(terminal):
    return {
        "uuid": str(terminal.terminal_uuid),
        "codigo": terminal.codigo,
        "nome": terminal.nome,
    }
