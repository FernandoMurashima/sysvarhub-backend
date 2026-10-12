from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import RecebimentoMercadoriaHub
from core.services.recebimentos import RecebimentoValidationError, atualizar_recebimento_por_payload


RECEBIMENTOS_VERSOES_SUPORTADAS = {1}


class RecebimentosValidationError(Exception):
    """Erro controlado na validação dos recebimentos da retaguarda."""


def sincronizar_recebimentos(hub, resposta, client=None):
    dados = _validar_resposta(hub, resposta)
    recebidos = []
    for recebimento in dados["recebimentos"]:
        local = atualizar_recebimento_por_payload(hub, recebimento, detalhado=False)
        recebidos.append(local.retaguarda_id)
        if client and local.status_local == RecebimentoMercadoriaHub.STATUS_LOCAL_PENDENTE:
            detalhe = client.recebimento_detalhe(token=hub.retaguarda_token, recebimento_id=local.retaguarda_id)
            sincronizar_recebimento_detalhe(hub, detalhe)
    return {
        "versao": resposta["recebimentos_versao"],
        "gerado_em": dados["gerado_em"].isoformat(),
        "recebimentos": len(recebidos),
        "pendentes_locais": RecebimentoMercadoriaHub.objects.filter(
            hub=hub,
            status_local=RecebimentoMercadoriaHub.STATUS_LOCAL_PENDENTE,
        ).count(),
    }


def sincronizar_recebimento_detalhe(hub, resposta):
    if not isinstance(resposta, dict) or not isinstance(resposta.get("recebimento"), dict):
        raise RecebimentosValidationError("Resposta de detalhe de recebimento inválida.")
    try:
        return atualizar_recebimento_por_payload(hub, resposta["recebimento"], detalhado=True)
    except RecebimentoValidationError as exc:
        raise RecebimentosValidationError(str(exc)) from exc


def _validar_resposta(hub, resposta):
    if not isinstance(resposta, dict):
        raise RecebimentosValidationError("Resposta de recebimentos inválida.")
    for campo in ("recebimentos_versao", "gerado_em", "hub", "empresa", "loja", "recebimentos"):
        if resposta.get(campo) in (None, ""):
            raise RecebimentosValidationError("Resposta de recebimentos incompleta.")
    if resposta["recebimentos_versao"] not in RECEBIMENTOS_VERSOES_SUPORTADAS:
        raise RecebimentosValidationError("Versão de recebimentos não suportada.")
    gerado_em = parse_datetime(str(resposta["gerado_em"]))
    if gerado_em is None:
        raise RecebimentosValidationError("Recebimentos retornaram gerado_em inválido.")
    if timezone.is_naive(gerado_em):
        gerado_em = timezone.make_aware(gerado_em, timezone.get_current_timezone())
    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    if not isinstance(hub_payload, dict) or not isinstance(empresa_payload, dict) or not isinstance(loja_payload, dict):
        raise RecebimentosValidationError("Resposta de recebimentos inválida.")
    if hub_payload.get("id") != hub.retaguarda_hub_id:
        raise RecebimentosValidationError("Recebimentos retornaram Hub diferente da configuração local.")
    if str(hub_payload.get("hub_uuid")) != str(hub.hub_uuid):
        raise RecebimentosValidationError("Recebimentos retornaram hub_uuid diferente da configuração local.")
    if empresa_payload.get("id") != hub.empresa_id:
        raise RecebimentosValidationError("Recebimentos retornaram empresa diferente da configuração local.")
    if loja_payload.get("id") != hub.loja_id:
        raise RecebimentosValidationError("Recebimentos retornaram loja diferente da configuração local.")
    recebimentos = resposta["recebimentos"]
    if not isinstance(recebimentos, list):
        raise RecebimentosValidationError("Recebimentos deve ser lista.")
    return {
        "gerado_em": gerado_em,
        "recebimentos": recebimentos,
    }
