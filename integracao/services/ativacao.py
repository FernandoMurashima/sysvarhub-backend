import socket
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from core.models import HubConfig
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError
from sysvarhub.version import VERSION


class AtivacaoHubError(Exception):
    """Erro controlado na ativacao local do Hub."""


def normalizar_codigo_ativacao(codigo):
    return str(codigo or "").strip().upper()


def normalizar_retaguarda_url(retaguarda_url):
    url = str(retaguarda_url or "").strip()
    if not url:
        raise AtivacaoHubError("Informe a URL da Central.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AtivacaoHubError("URL da Central invalida.")
    return url.rstrip("/")


def obter_configuracao_unica(*, retaguarda_url=""):
    total = HubConfig.objects.count()
    if total > 1:
        raise AtivacaoHubError("Existe mais de uma configuracao local do Hub.")
    if total == 1:
        return HubConfig.objects.get()
    return HubConfig.objects.create(retaguarda_url=normalizar_retaguarda_url(retaguarda_url), ativo=False)


def configuracao_ativada(hub):
    return bool(
        hub
        and hub.ativo
        and hub.retaguarda_token
        and hub.retaguarda_hub_id
        and hub.empresa_id
        and hub.loja_id
        and hub.retaguarda_url
    )


def serializar_status_ativacao(hub):
    return {
        "ativado": configuracao_ativada(hub),
        "possui_credencial": bool(hub and hub.retaguarda_token),
        "hub_uuid": str(hub.hub_uuid) if hub else None,
        "nome": hub.nome if hub else "Sysvar Hub",
        "empresa_id": hub.empresa_id if hub else None,
        "empresa_nome": hub.empresa_nome if hub else "",
        "loja_id": hub.loja_id if hub else None,
        "loja_nome": hub.loja_nome if hub else "",
        "retaguarda_url": hub.retaguarda_url if hub else "",
        "ativado_em": hub.ativado_em if hub else None,
    }


def _validar_resposta_ativacao(resposta, hub):
    obrigatorios = ("token", "hub_uuid", "hub_id", "loja_id", "empresa_id")
    faltando = [campo for campo in obrigatorios if not resposta.get(campo)]
    if faltando:
        raise AtivacaoHubError("Resposta de ativacao incompleta da Central.")
    if str(resposta["hub_uuid"]) != str(hub.hub_uuid):
        raise AtivacaoHubError("Central retornou hub_uuid diferente do Hub local.")


def ativar_hub_local(codigo, retaguarda_url=None, *, client_factory=RetaguardaClient):
    codigo_normalizado = normalizar_codigo_ativacao(codigo)
    if not codigo_normalizado:
        raise AtivacaoHubError("Informe o codigo de ativacao.")

    hub = obter_configuracao_unica(retaguarda_url=retaguarda_url or "")
    url = normalizar_retaguarda_url(retaguarda_url or hub.retaguarda_url)
    hostname = socket.gethostname()
    client = client_factory(url)

    try:
        resposta = client.ativar_hub(
            codigo=codigo_normalizado,
            hub_uuid=hub.hub_uuid,
            nome=hub.nome,
            hostname=hostname,
            versao=VERSION,
        )
    except RetaguardaError as exc:
        raise AtivacaoHubError(str(exc)) from exc

    _validar_resposta_ativacao(resposta, hub)

    with transaction.atomic():
        hub = HubConfig.objects.select_for_update().get(pk=hub.pk)
        hub.retaguarda_token = resposta["token"]
        hub.retaguarda_hub_id = resposta["hub_id"]
        hub.empresa_id = resposta["empresa_id"]
        hub.empresa_nome = resposta.get("empresa_nome", "")
        hub.loja_id = resposta["loja_id"]
        hub.loja_nome = resposta.get("loja_nome", "")
        hub.ativado_em = timezone.now()
        hub.ativo = True
        hub.retaguarda_url = url
        hub.save(
            update_fields=[
                "retaguarda_token",
                "retaguarda_hub_id",
                "empresa_id",
                "empresa_nome",
                "loja_id",
                "loja_nome",
                "ativado_em",
                "ativo",
                "retaguarda_url",
                "atualizado_em",
            ]
        )
    return hub
