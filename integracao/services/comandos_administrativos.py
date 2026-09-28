import logging

from django.db import transaction
from django.utils import timezone

from core.models import CaixaHub, ComandoAdministrativoRecebidoHub, Terminal
from core.services.terminais import (
    PareamentoTerminalError,
    TerminalValidationError,
    configurar_terminal,
    gerar_pareamento_terminal,
)
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


logger = logging.getLogger(__name__)


def executar_comando_administrativo(hub, comando, client=None):
    if not comando or not comando.get("id"):
        return None
    if int(comando.get("hub_id") or hub.retaguarda_hub_id or 0) not in (0, int(hub.retaguarda_hub_id or 0)):
        raise RetaguardaError("Comando administrativo pertence a outro Hub.")
    client = client or RetaguardaClient(hub.retaguarda_url)
    local, _criado = ComandoAdministrativoRecebidoHub.objects.get_or_create(
        hub=hub,
        retaguarda_comando_id=comando["id"],
        defaults={"tipo": comando.get("tipo") or "", "payload": comando.get("payload") or {}},
    )
    if local.status in ComandoAdministrativoRecebidoHub.STATUS_TERMINAIS:
        _informar_resultado(client, hub, local)
        return local

    _marcar_status(local, ComandoAdministrativoRecebidoHub.STATUS_PROCESSANDO)
    try:
        resultado = _executar_local(hub, local.tipo, local.payload)
        _marcar_concluido(local, resultado)
    except (TerminalValidationError, PareamentoTerminalError, Terminal.DoesNotExist) as exc:
        _marcar_erro(local, str(exc))
    except Exception as exc:
        _marcar_erro(local, str(exc) or exc.__class__.__name__)
        logger.exception("Falha ao executar comando administrativo do Hub.")

    try:
        _informar_resultado(client, hub, local)
    except RetaguardaError:
        logger.warning("Falha ao informar resultado do comando administrativo à Central.", exc_info=True)
    return local


def reenviar_resultados_pendentes(hub, client=None):
    client = client or RetaguardaClient(hub.retaguarda_url)
    for local in ComandoAdministrativoRecebidoHub.objects.filter(
        hub=hub,
        status__in=ComandoAdministrativoRecebidoHub.STATUS_TERMINAIS,
    ).order_by("processado_em", "id")[:5]:
        _informar_resultado(client, hub, local)


def _executar_local(hub, tipo, payload):
    if tipo == ComandoAdministrativoRecebidoHub.TIPO_CONFIGURAR_TERMINAL:
        terminal = configurar_terminal(
            hub,
            payload.get("codigo"),
            payload.get("nome"),
            payload.get("caixa_retaguarda_id"),
            hostname=payload.get("hostname"),
        )
        return {"terminal": _serializar_terminal(terminal)}
    if tipo == ComandoAdministrativoRecebidoHub.TIPO_GERAR_PAREAMENTO:
        terminal = _obter_terminal(hub, payload)
        pareamento, codigo = gerar_pareamento_terminal(terminal)
        return {
            "terminal": _serializar_terminal_resumo(terminal),
            "codigo": codigo,
            "expira_em": pareamento.expira_em.isoformat(),
            "estado": "CODIGO_DISPONIVEL",
        }
    raise TerminalValidationError("Tipo de comando administrativo inválido.")


def _obter_terminal(hub, payload):
    qs = Terminal.objects.filter(hub=hub)
    if payload.get("terminal_uuid"):
        return qs.get(terminal_uuid=payload.get("terminal_uuid"))
    return qs.get(codigo=payload.get("codigo"))


def _serializar_terminal(terminal):
    caixa = CaixaHub.objects.filter(hub=terminal.hub, retaguarda_id=terminal.caixa_retaguarda_id).first()
    return {
        "terminal_uuid": str(terminal.terminal_uuid),
        "codigo": terminal.codigo,
        "nome": terminal.nome,
        "ativo": terminal.ativo,
        "hostname": terminal.hostname,
        "caixa": _serializar_caixa(caixa),
    }


def _serializar_terminal_resumo(terminal):
    return {
        "terminal_uuid": str(terminal.terminal_uuid),
        "codigo": terminal.codigo,
        "nome": terminal.nome,
    }


def _serializar_caixa(caixa):
    if not caixa:
        return None
    return {
        "id": caixa.retaguarda_id,
        "codigo": caixa.codigo,
        "descricao": caixa.descricao,
        "ativo": caixa.ativo,
    }


def _marcar_status(local, status):
    local.status = status
    local.save(update_fields=["status", "atualizado_em"])


@transaction.atomic
def _marcar_concluido(local, resultado):
    local = ComandoAdministrativoRecebidoHub.objects.select_for_update().get(pk=local.pk)
    local.status = ComandoAdministrativoRecebidoHub.STATUS_CONCLUIDO
    local.resultado = resultado
    local.mensagem_erro = ""
    local.processado_em = timezone.now()
    local.save(update_fields=["status", "resultado", "mensagem_erro", "processado_em", "atualizado_em"])


@transaction.atomic
def _marcar_erro(local, mensagem):
    local = ComandoAdministrativoRecebidoHub.objects.select_for_update().get(pk=local.pk)
    local.status = ComandoAdministrativoRecebidoHub.STATUS_ERRO
    local.mensagem_erro = str(mensagem or "")[:500]
    local.resultado = {}
    local.processado_em = timezone.now()
    local.save(update_fields=["status", "resultado", "mensagem_erro", "processado_em", "atualizado_em"])


def _informar_resultado(client, hub, local):
    client.atualizar_resultado_comando_administrativo(
        token=hub.retaguarda_token,
        comando_id=local.retaguarda_comando_id,
        status=local.status,
        resultado=local.resultado,
        mensagem_erro=local.mensagem_erro,
    )
