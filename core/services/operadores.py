from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.utils import timezone

from core.models import OperadorHub, SessaoOperadorHub


LOGIN_INVALIDO = "Operador ou credencial inválidos."
MOTIVO_LOGOUT = "LOGOUT"
MOTIVO_NOVO_LOGIN = "NOVO_LOGIN"


class OperadorAuthenticationError(Exception):
    """Erro controlado na autenticação local do operador."""


def serializar_operador(operador):
    perfil = None
    if operador.perfil_retaguarda_id is not None:
        perfil = {
            "id": operador.perfil_retaguarda_id,
            "nome": operador.perfil_nome,
        }

    return {
        "usuario_id": operador.retaguarda_usuario_id,
        "codigo": operador.codigo,
        "nome": operador.nome,
        "tipo": operador.tipo,
        "perfil": perfil,
    }


def autenticar_operador_terminal(terminal, *, codigo, senha, ip=None):
    codigo = (codigo or "").strip()
    senha = senha or ""

    try:
        operador = OperadorHub.objects.get(
            hub=terminal.hub,
            ativo=True,
            codigo=codigo,
        )
    except OperadorHub.DoesNotExist as exc:
        raise OperadorAuthenticationError(LOGIN_INVALIDO) from exc

    if not operador.credencial_hash:
        raise OperadorAuthenticationError(LOGIN_INVALIDO)
    if not check_password(senha, operador.credencial_hash):
        raise OperadorAuthenticationError(LOGIN_INVALIDO)

    agora = timezone.now()
    with transaction.atomic():
        SessaoOperadorHub.objects.select_for_update().filter(
            terminal=terminal,
            ativa=True,
        ).update(
            ativa=False,
            encerrada_em=agora,
            motivo_encerramento=MOTIVO_NOVO_LOGIN,
        )

        sessao = SessaoOperadorHub(
            terminal=terminal,
            operador=operador,
            ultima_atividade_em=agora,
            ultimo_ip=ip,
        )
        token = sessao.gerar_token()
        sessao.save()

    return sessao, token


def validar_sessao_operador(terminal, token, *, ip=None):
    if not token:
        raise OperadorAuthenticationError("Sessão de operador inválida.")

    token_hash = SessaoOperadorHub.hash_token(token)
    try:
        sessao = (
            SessaoOperadorHub.objects.select_related("terminal", "terminal__hub", "operador", "operador__hub")
            .get(token_hash=token_hash, ativa=True)
        )
    except SessaoOperadorHub.DoesNotExist as exc:
        raise OperadorAuthenticationError("Sessão de operador inválida.") from exc

    if sessao.terminal_id != terminal.id:
        raise OperadorAuthenticationError("Sessão de operador inválida.")
    if sessao.operador.hub_id != terminal.hub_id:
        raise OperadorAuthenticationError("Sessão de operador inválida.")
    if not sessao.operador.ativo:
        raise OperadorAuthenticationError("Sessão de operador inválida.")

    atualizar_atividade_sessao(sessao, ip=ip)
    return sessao


def atualizar_atividade_sessao(sessao, *, ip=None):
    sessao.ultima_atividade_em = timezone.now()
    update_fields = ["ultima_atividade_em"]
    if ip:
        sessao.ultimo_ip = ip
        update_fields.append("ultimo_ip")
    sessao.save(update_fields=update_fields)


def encerrar_sessao(sessao, motivo=MOTIVO_LOGOUT):
    if not sessao.ativa:
        return sessao
    sessao.ativa = False
    sessao.encerrada_em = timezone.now()
    sessao.motivo_encerramento = motivo
    sessao.save(
        update_fields=[
            "ativa",
            "encerrada_em",
            "motivo_encerramento",
        ]
    )
    return sessao


def encerrar_sessoes_ativas_operador(operador, motivo):
    agora = timezone.now()
    return SessaoOperadorHub.objects.filter(
        operador=operador,
        ativa=True,
    ).update(
        ativa=False,
        encerrada_em=agora,
        motivo_encerramento=motivo,
    )
