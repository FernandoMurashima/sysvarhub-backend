from django.db import transaction
from django.db.models import F

from core.models import FaixaNumeracaoDevolucaoHub
from integracao.services.retaguarda import RetaguardaError


TAMANHO_FAIXA_DEVOLUCAO_PADRAO = 100
LIMITE_REPOSICAO_PREVENTIVA_DEVOLUCAO = 20
LIMITE_NUMERO_DEVOLUCAO = 9999999


class NumeracaoDevolucaoHubError(Exception):
    pass


def formatar_documento_devolucao_hub(numero):
    try:
        sequencial = int(numero)
    except (TypeError, ValueError) as exc:
        raise NumeracaoDevolucaoHubError("Faixa de numeracao de devolucoes invalida.") from exc
    if sequencial <= 0 or sequencial > LIMITE_NUMERO_DEVOLUCAO:
        raise NumeracaoDevolucaoHubError("Numero invalido para documento comercial da devolucao.")
    return f"DEV-{sequencial:07d}"


def numeros_disponiveis_devolucao(hub):
    disponiveis = 0
    for faixa in FaixaNumeracaoDevolucaoHub.objects.filter(hub=hub, proximo_numero__lte=F("fim")):
        disponiveis += faixa.fim - faixa.proximo_numero + 1
    return disponiveis


@transaction.atomic
def consumir_documento_devolucao(hub):
    faixa = (
        FaixaNumeracaoDevolucaoHub.objects.select_for_update()
        .filter(hub=hub, proximo_numero__lte=F("fim"))
        .order_by("inicio", "id")
        .first()
    )
    if not faixa:
        raise NumeracaoDevolucaoHubError(
            "Faixa de numeracao de devolucoes esgotada. Comunique com a Central para receber nova faixa."
        )
    numero = faixa.proximo_numero
    faixa.proximo_numero = numero + 1
    faixa.save(update_fields=["proximo_numero"])
    return formatar_documento_devolucao_hub(numero)


@transaction.atomic
def registrar_faixa_devolucao_hub(hub, payload):
    empresa_id, inicio, fim = _validar_payload_faixa(hub, payload)
    existente = FaixaNumeracaoDevolucaoHub.objects.select_for_update().filter(hub=hub, inicio=inicio, fim=fim).first()
    if existente:
        return existente, False
    sobreposta = (
        FaixaNumeracaoDevolucaoHub.objects.select_for_update()
        .filter(hub=hub, inicio__lte=fim, fim__gte=inicio)
        .first()
    )
    if sobreposta:
        raise NumeracaoDevolucaoHubError("Faixa de numeracao de devolucoes sobreposta localmente.")
    return (
        FaixaNumeracaoDevolucaoHub.objects.create(
            hub=hub,
            inicio=inicio,
            fim=fim,
            proximo_numero=inicio,
        ),
        True,
    )


def garantir_faixa_devolucao_suficiente(hub, client):
    if numeros_disponiveis_devolucao(hub) > LIMITE_REPOSICAO_PREVENTIVA_DEVOLUCAO:
        return None
    resposta = client.reservar_faixa_devolucoes(token=hub.retaguarda_token)
    faixa, _criada = registrar_faixa_devolucao_hub(hub, resposta)
    return faixa


def tentar_repor_faixa_devolucao(hub, client, logger=None):
    try:
        return garantir_faixa_devolucao_suficiente(hub, client)
    except (RetaguardaError, NumeracaoDevolucaoHubError):
        if logger:
            logger.warning("Falha ao repor faixa de numeracao de devolucoes do Hub.", exc_info=True)
        return None


def _validar_payload_faixa(hub, payload):
    payload = payload or {}
    try:
        empresa_id = int(payload.get("empresa_id"))
        inicio = int(payload.get("inicio"))
        fim = int(payload.get("fim"))
    except (TypeError, ValueError) as exc:
        raise NumeracaoDevolucaoHubError("Resposta invalida da Central para faixa de devolucoes.") from exc
    if empresa_id != int(hub.empresa_id or 0):
        raise NumeracaoDevolucaoHubError("Faixa de devolucoes pertence a outra empresa.")
    if inicio <= 0 or fim < inicio or fim > LIMITE_NUMERO_DEVOLUCAO:
        raise NumeracaoDevolucaoHubError("Intervalo de faixa de devolucoes invalido.")
    return empresa_id, inicio, fim
