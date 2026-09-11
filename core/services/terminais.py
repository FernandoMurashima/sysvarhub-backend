from django.db import transaction

from core.models import CaixaHub, Terminal


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
        terminal.save(update_fields=["ativo", "atualizado_em"])

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
