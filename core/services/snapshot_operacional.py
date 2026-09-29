from datetime import timedelta

from django.utils import timezone

from core.models import CaixaHub, SessaoCaixaHub, Terminal


TERMINAL_ONLINE_TTL_SECONDS = 45
MAX_TERMINAIS_SNAPSHOT = 100


def montar_snapshot_operacional(hub):
    agora = timezone.now()
    terminais = list(
        Terminal.objects.filter(hub=hub)
        .order_by("codigo", "nome")[:MAX_TERMINAIS_SNAPSHOT]
    )
    caixas_por_retaguarda_id = {
        caixa.retaguarda_id: caixa
        for caixa in CaixaHub.objects.filter(
            hub=hub,
            retaguarda_id__in=[
                terminal.caixa_retaguarda_id
                for terminal in terminais
                if terminal.caixa_retaguarda_id is not None
            ],
        )
    }
    sessoes_abertas_por_caixa_id = {}
    for sessao in (
        SessaoCaixaHub.objects.select_related(
            "caixa",
            "terminal_abertura",
            "operador_abertura",
        )
        .filter(
            caixa__hub=hub,
            status=SessaoCaixaHub.STATUS_ABERTO,
            caixa__retaguarda_id__in=caixas_por_retaguarda_id.keys(),
        )
        .order_by("caixa_id", "-aberto_em")
    ):
        sessoes_abertas_por_caixa_id.setdefault(sessao.caixa_id, sessao)

    return {
        "gerado_em": agora.isoformat(),
        "terminais": [
            _serializar_terminal_operacional(
                terminal,
                agora=agora,
                caixa=caixas_por_retaguarda_id.get(terminal.caixa_retaguarda_id),
                sessoes_abertas_por_caixa_id=sessoes_abertas_por_caixa_id,
            )
            for terminal in terminais
        ],
    }


def _serializar_terminal_operacional(terminal, *, agora, caixa, sessoes_abertas_por_caixa_id):
    sessao = sessoes_abertas_por_caixa_id.get(caixa.pk) if caixa else None
    return {
        "terminal_uuid": str(terminal.terminal_uuid),
        "codigo": terminal.codigo,
        "nome": terminal.nome,
        "ativo": terminal.ativo,
        "pareado": bool(terminal.ativo and terminal.token_hash and terminal.pareado_em),
        "pareado_em": terminal.pareado_em.isoformat() if terminal.pareado_em else None,
        "hostname": terminal.hostname,
        "ultimo_ip": str(terminal.ultimo_ip) if terminal.ultimo_ip else None,
        "ultima_conexao_em": terminal.ultima_conexao_em.isoformat() if terminal.ultima_conexao_em else None,
        "online": _terminal_online(terminal, agora),
        "caixa": _serializar_caixa_operacional(caixa),
        "caixa_status": _caixa_status(terminal, caixa, sessao),
        "sessao_caixa": _serializar_sessao_caixa_operacional(sessao),
    }


def _terminal_online(terminal, agora):
    if not terminal.ativo:
        return False
    if not terminal.token_hash or not terminal.pareado_em:
        return False
    if not terminal.ultima_conexao_em:
        return False
    return terminal.ultima_conexao_em >= agora - timedelta(seconds=TERMINAL_ONLINE_TTL_SECONDS)


def _caixa_status(terminal, caixa, sessao):
    if terminal.caixa_retaguarda_id is None:
        return "SEM_CAIXA"
    if caixa is None:
        return "CAIXA_NAO_ENCONTRADO"
    if sessao:
        return "ABERTO"
    return "FECHADO"


def _serializar_caixa_operacional(caixa):
    if caixa is None:
        return None
    return {
        "id": caixa.retaguarda_id,
        "codigo": caixa.codigo,
        "descricao": caixa.descricao,
        "ativo": caixa.ativo,
    }


def _serializar_sessao_caixa_operacional(sessao):
    if sessao is None:
        return None
    return {
        "uuid": str(sessao.sessao_uuid),
        "status": sessao.status,
        "aberto_em": sessao.aberto_em.isoformat(),
        "operador": {
            "codigo": sessao.operador_abertura.codigo,
            "nome": sessao.operador_abertura.nome,
        },
        "terminal_abertura": {
            "codigo": sessao.terminal_abertura.codigo,
            "nome": sessao.terminal_abertura.nome,
        },
    }
