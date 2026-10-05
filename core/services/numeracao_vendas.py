from django.db import transaction
from django.db.models import F

from core.models import FaixaNumeracaoVendaHub
from integracao.services.retaguarda import RetaguardaError


TAMANHO_FAIXA_VENDA_PADRAO = 100
LIMITE_REPOSICAO_PREVENTIVA_VENDA = 20
LIMITE_NUMERO_VENDA = 9999999


class NumeracaoVendaHubError(Exception):
    pass


def formatar_documento_venda_hub(loja_id, numero):
    try:
        loja_numero = int(loja_id)
        sequencial = int(numero)
    except (TypeError, ValueError) as exc:
        raise NumeracaoVendaHubError("Faixa de numeracao de vendas invalida.") from exc
    if loja_numero <= 0 or loja_numero > 999:
        raise NumeracaoVendaHubError("Loja invalida para numeracao de vendas.")
    if sequencial <= 0 or sequencial > LIMITE_NUMERO_VENDA:
        raise NumeracaoVendaHubError("Numero invalido para documento comercial da venda.")
    return f"VE{loja_numero:03d}{sequencial:07d}"


def numeros_disponiveis_venda(hub):
    disponiveis = 0
    for faixa in FaixaNumeracaoVendaHub.objects.filter(hub=hub, proximo_numero__lte=F("fim")):
        disponiveis += faixa.fim - faixa.proximo_numero + 1
    return disponiveis


@transaction.atomic
def consumir_documento_venda(hub):
    faixa = (
        FaixaNumeracaoVendaHub.objects.select_for_update()
        .filter(hub=hub, proximo_numero__lte=F("fim"))
        .order_by("inicio", "id")
        .first()
    )
    if not faixa:
        raise NumeracaoVendaHubError(
            "Faixa de numeracao de vendas esgotada. Comunique com a Central para receber nova faixa."
        )
    numero = faixa.proximo_numero
    faixa.proximo_numero = numero + 1
    faixa.save(update_fields=["proximo_numero"])
    return formatar_documento_venda_hub(hub.loja_id, numero)


@transaction.atomic
def registrar_faixa_venda_hub(hub, payload):
    loja_id, inicio, fim = _validar_payload_faixa(hub, payload)
    existente = FaixaNumeracaoVendaHub.objects.select_for_update().filter(hub=hub, inicio=inicio, fim=fim).first()
    if existente:
        return existente, False
    sobreposta = (
        FaixaNumeracaoVendaHub.objects.select_for_update()
        .filter(hub=hub, inicio__lte=fim, fim__gte=inicio)
        .first()
    )
    if sobreposta:
        raise NumeracaoVendaHubError("Faixa de numeracao de vendas sobreposta localmente.")
    return (
        FaixaNumeracaoVendaHub.objects.create(
            hub=hub,
            inicio=inicio,
            fim=fim,
            proximo_numero=inicio,
        ),
        True,
    )


def garantir_faixa_venda_suficiente(hub, client):
    if numeros_disponiveis_venda(hub) > LIMITE_REPOSICAO_PREVENTIVA_VENDA:
        return None
    resposta = client.reservar_faixa_vendas(token=hub.retaguarda_token)
    faixa, _criada = registrar_faixa_venda_hub(hub, resposta)
    return faixa


def tentar_repor_faixa_venda(hub, client, logger=None):
    try:
        return garantir_faixa_venda_suficiente(hub, client)
    except (RetaguardaError, NumeracaoVendaHubError):
        if logger:
            logger.warning("Falha ao repor faixa de numeracao de vendas do Hub.", exc_info=True)
        return None


def _validar_payload_faixa(hub, payload):
    payload = payload or {}
    try:
        loja_id = int(payload.get("loja_id"))
        inicio = int(payload.get("inicio"))
        fim = int(payload.get("fim"))
    except (TypeError, ValueError) as exc:
        raise NumeracaoVendaHubError("Resposta invalida da Central para faixa de vendas.") from exc
    if loja_id != int(hub.loja_id or 0):
        raise NumeracaoVendaHubError("Faixa de vendas pertence a outra loja.")
    if inicio <= 0 or fim < inicio or fim > LIMITE_NUMERO_VENDA:
        raise NumeracaoVendaHubError("Intervalo de faixa de vendas invalido.")
    return loja_id, inicio, fim
