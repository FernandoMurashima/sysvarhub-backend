from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.models import CaixaHub, PareamentoTerminal, Terminal


class TerminalValidationError(Exception):
    """Erro controlado na configuracao local de terminais."""


def configurar_terminal(hub, codigo, nome, caixa_retaguarda_id=None, hostname=None):
    codigo = (codigo or "").strip()
    nome = (nome or "").strip()
    hostname = (hostname or "").strip() if hostname is not None else None

    if hub is None:
        raise TerminalValidationError("Hub local nao encontrado.")
    if not codigo:
        raise TerminalValidationError("Codigo do terminal e obrigatorio.")
    if not nome:
        raise TerminalValidationError("Nome do terminal e obrigatorio.")

    with transaction.atomic():
        caixa = None
        if caixa_retaguarda_id is not None:
            caixa = validar_caixa_terminal(hub, caixa_retaguarda_id)

        terminal, _created = Terminal.objects.select_for_update().update_or_create(
            hub=hub,
            codigo=codigo,
            defaults={
                "nome": nome,
                "caixa_retaguarda_id": caixa.retaguarda_id if caixa else None,
                "ativo": True,
                **({"hostname": hostname} if hostname is not None else {}),
            },
        )

    return terminal


def desativar_terminal(hub, codigo):
    codigo = (codigo or "").strip()
    if hub is None:
        raise TerminalValidationError("Hub local nao encontrado.")
    if not codigo:
        raise TerminalValidationError("Codigo do terminal e obrigatorio.")

    with transaction.atomic():
        try:
            terminal = Terminal.objects.select_for_update().get(hub=hub, codigo=codigo)
        except Terminal.DoesNotExist as exc:
            raise TerminalValidationError("Terminal nao encontrado.") from exc

        terminal.ativo = False
        terminal.token_hash = ""
        terminal.token_prefixo = ""
        terminal.save(
            update_fields=[
                "ativo",
                "token_hash",
                "token_prefixo",
                "atualizado_em",
            ]
        )

    return terminal


def validar_caixa_terminal(hub, caixa_retaguarda_id):
    try:
        caixa = CaixaHub.objects.get(
            hub=hub,
            retaguarda_id=caixa_retaguarda_id,
        )
    except CaixaHub.DoesNotExist as exc:
        raise TerminalValidationError("Caixa do Hub nao encontrado.") from exc

    if not caixa.ativo:
        raise TerminalValidationError("Caixa do Hub esta inativo.")

    return caixa


def obter_caixa_terminal(hub, caixa_retaguarda_id):
    if caixa_retaguarda_id is None:
        return None
    return CaixaHub.objects.filter(
        hub=hub,
        retaguarda_id=caixa_retaguarda_id,
    ).first()


class PareamentoTerminalError(Exception):
    """Erro controlado no pareamento de terminais."""


def gerar_pareamento_terminal(terminal, validade_minutos=15):
    if not terminal.ativo:
        raise PareamentoTerminalError("Terminal inativo nao pode gerar pareamento.")

    agora = timezone.now()
    codigo = PareamentoTerminal.gerar_codigo()

    with transaction.atomic():
        terminal = Terminal.objects.select_for_update().get(pk=terminal.pk)
        if not terminal.ativo:
            raise PareamentoTerminalError("Terminal inativo nao pode gerar pareamento.")

        PareamentoTerminal.objects.filter(
            terminal=terminal,
            usado_em__isnull=True,
            revogado_em__isnull=True,
            expira_em__gt=agora,
        ).update(revogado_em=agora)

        pareamento = PareamentoTerminal.objects.create(
            terminal=terminal,
            codigo_hash=PareamentoTerminal.hash_codigo(codigo),
            codigo_prefixo=PareamentoTerminal.normalizar_codigo(codigo)[:4],
            expira_em=agora + timedelta(minutes=validade_minutos),
        )

    return pareamento, codigo


def parear_terminal(codigo, hostname="", ip=None):
    codigo_normalizado = PareamentoTerminal.normalizar_codigo(codigo)
    if not codigo_normalizado:
        raise PareamentoTerminalError("Codigo de pareamento e obrigatorio.")

    agora = timezone.now()

    with transaction.atomic():
        try:
            pareamento = (
                PareamentoTerminal.objects.select_for_update()
                .select_related("terminal", "terminal__hub")
                .get(codigo_hash=PareamentoTerminal.hash_codigo(codigo_normalizado))
            )
        except PareamentoTerminal.DoesNotExist as exc:
            raise PareamentoTerminalError("Codigo de pareamento invalido.") from exc

        if pareamento.usado_em is not None:
            raise PareamentoTerminalError("Codigo de pareamento ja utilizado.")
        if pareamento.revogado_em is not None:
            raise PareamentoTerminalError("Codigo de pareamento revogado.")
        if pareamento.expira_em <= agora:
            raise PareamentoTerminalError("Codigo de pareamento expirado.")

        terminal = pareamento.terminal
        if not terminal.ativo:
            raise PareamentoTerminalError("Terminal inativo nao pode parear.")

        token = terminal.gerar_token()
        terminal.pareado_em = agora
        terminal.ultima_conexao_em = agora
        if hostname:
            terminal.hostname = hostname.strip()
        if ip:
            terminal.ultimo_ip = ip
        terminal.save(
            update_fields=[
                "token_hash",
                "token_prefixo",
                "pareado_em",
                "hostname",
                "ultimo_ip",
                "ultima_conexao_em",
                "atualizado_em",
            ]
        )

        pareamento.usado_em = agora
        pareamento.save(update_fields=["usado_em"])

    return terminal, token


def registrar_heartbeat_terminal(terminal, hostname="", ip=None):
    agora = timezone.now()
    terminal.ultima_conexao_em = agora
    if hostname:
        terminal.hostname = hostname.strip()
    if ip:
        terminal.ultimo_ip = ip
    terminal.save(
        update_fields=[
            "hostname",
            "ultimo_ip",
            "ultima_conexao_em",
            "atualizado_em",
        ]
    )
    return terminal
