from django.db import transaction
from django.utils import timezone

from core.models import CaixaHub


BOOTSTRAP_VERSOES_SUPORTADAS = {1}


class BootstrapValidationError(Exception):
    """Erro controlado na validação do bootstrap da retaguarda."""


def sincronizar_bootstrap(hub, resposta):
    _validar_bootstrap(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        caixas_recebidos = resposta["caixas"]
        retaguarda_ids_recebidos = set()
        caixas_ativos = 0

        for caixa in caixas_recebidos:
            retaguarda_id = caixa["id"]
            retaguarda_ids_recebidos.add(retaguarda_id)
            defaults = {
                "codigo": str(caixa.get("codigo") or ""),
                "descricao": caixa.get("descricao") or "",
                "ativo": bool(caixa.get("ativo")),
                "sincronizado_em": sincronizado_em,
            }
            CaixaHub.objects.update_or_create(
                hub=hub,
                retaguarda_id=retaguarda_id,
                defaults=defaults,
            )
            if defaults["ativo"]:
                caixas_ativos += 1

        caixas_inativados = (
            CaixaHub.objects.filter(hub=hub, ativo=True)
            .exclude(retaguarda_id__in=retaguarda_ids_recebidos)
            .update(ativo=False, sincronizado_em=sincronizado_em)
        )

        empresa = resposta["empresa"]
        loja = resposta["loja"]
        hub.empresa_nome = empresa.get("nome") or ""
        hub.loja_nome = loja.get("nome_loja") or ""
        hub.loja_apelido = loja.get("apelido_loja") or ""
        hub.loja_cnpj = loja.get("cnpj") or ""
        hub.loja_estado = loja.get("estado") or ""
        hub.bootstrap_versao = resposta["bootstrap_versao"]
        hub.ultima_sincronizacao_em = sincronizado_em
        hub.save(
            update_fields=[
                "empresa_nome",
                "loja_nome",
                "loja_apelido",
                "loja_cnpj",
                "loja_estado",
                "bootstrap_versao",
                "ultima_sincronizacao_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": hub.empresa_nome or hub.empresa_id,
        "loja": hub.loja_nome or hub.loja_id,
        "caixas_ativos": caixas_ativos,
        "caixas_inativados": caixas_inativados,
    }


def _validar_bootstrap(hub, resposta):
    if not isinstance(resposta, dict):
        raise BootstrapValidationError("Resposta de bootstrap inválida.")

    _exigir_campos(resposta, ("bootstrap_versao", "hub", "empresa", "loja", "caixas"))
    if resposta["bootstrap_versao"] not in BOOTSTRAP_VERSOES_SUPORTADAS:
        raise BootstrapValidationError("Versão de bootstrap não suportada.")
    if not isinstance(resposta["caixas"], list):
        raise BootstrapValidationError(
            "Resposta de bootstrap inválida: caixas deve ser lista."
        )

    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    if (
        not isinstance(hub_payload, dict)
        or not isinstance(empresa_payload, dict)
        or not isinstance(loja_payload, dict)
    ):
        raise BootstrapValidationError("Resposta de bootstrap inválida.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))

    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade("loja_id", loja_payload["id"], hub.loja_id)

    for caixa in resposta["caixas"]:
        if not isinstance(caixa, dict):
            raise BootstrapValidationError("Resposta de bootstrap inválida: caixa inválido.")
        _exigir_campos(caixa, ("id", "codigo", "ativo"))


def _exigir_campos(payload, campos):
    faltando = [campo for campo in campos if payload.get(campo) in (None, "")]
    if faltando:
        raise BootstrapValidationError("Resposta de bootstrap incompleta.")


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise BootstrapValidationError(
            f"Bootstrap retornou {campo} diferente da configuração local."
        )
