from django.db import transaction
from django.utils import timezone

from core.models import CaixaHub, CashbackConfigHub, ConfiguracaoFiscalHub


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
        fiscal_atualizada = _sincronizar_fiscal(hub, loja.get("fiscal"), sincronizado_em)
        _sincronizar_cashback(hub, resposta.get("cashback_config"), sincronizado_em)
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
        "configuracao_fiscal_atualizada": fiscal_atualizada,
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

    fiscal = loja_payload.get("fiscal")
    if fiscal is not None:
        _validar_fiscal(fiscal)
    cashback = resposta.get("cashback_config")
    if cashback is not None and not isinstance(cashback, dict):
        raise BootstrapValidationError("Bootstrap retornou cashback_config inválido.")


def _sincronizar_fiscal(hub, fiscal, sincronizado_em):
    if fiscal is None:
        return False
    atual = ConfiguracaoFiscalHub.objects.select_for_update().filter(hub=hub).first()
    serie_recebida = fiscal["serie_nfce"]
    numero_recebido = fiscal["proximo_numero_nfce"]
    if atual and atual.serie_nfce == serie_recebida:
        proximo_numero = max(atual.proximo_numero_nfce, numero_recebido)
    else:
        proximo_numero = numero_recebido
    defaults = {
        "emite_nfce": fiscal["emite_nfce"],
        "ambiente_fiscal": fiscal["ambiente_fiscal"],
        "regime_tributario": fiscal["regime_tributario"],
        "inscricao_estadual": fiscal.get("inscricao_estadual") or "",
        "serie_nfce": serie_recebida,
        "proximo_numero_nfce": proximo_numero,
        "razao_social": fiscal["razao_social"],
        "nome_fantasia": fiscal.get("nome_fantasia") or "",
        "cnpj": fiscal["cnpj"],
        "logradouro": fiscal.get("logradouro") or "",
        "endereco": fiscal.get("endereco") or "",
        "numero": fiscal.get("numero") or "",
        "complemento": fiscal.get("complemento") or "",
        "bairro": fiscal.get("bairro") or "",
        "cidade": fiscal.get("cidade") or "",
        "uf": fiscal.get("uf") or fiscal.get("estado") or "",
        "cep": fiscal.get("cep") or "",
        "codigo_municipio_ibge": fiscal.get("codigo_municipio_ibge") or "",
        "sincronizado_em": sincronizado_em,
    }
    ConfiguracaoFiscalHub.objects.update_or_create(hub=hub, defaults=defaults)
    return True


def _validar_fiscal(fiscal):
    if not isinstance(fiscal, dict):
        raise BootstrapValidationError("Bootstrap retornou fiscal inválido.")
    _exigir_campos(
        fiscal,
        (
            "emite_nfce",
            "ambiente_fiscal",
            "regime_tributario",
            "serie_nfce",
            "proximo_numero_nfce",
            "razao_social",
            "cnpj",
        ),
    )
    if not isinstance(fiscal["emite_nfce"], bool):
        raise BootstrapValidationError("Bootstrap retornou fiscal.emite_nfce inválido.")
    for campo in ("serie_nfce", "proximo_numero_nfce"):
        if isinstance(fiscal[campo], bool) or not isinstance(fiscal[campo], int) or fiscal[campo] <= 0:
            raise BootstrapValidationError("Bootstrap retornou numeração fiscal inválida.")
    for campo in ("ambiente_fiscal", "regime_tributario", "razao_social", "cnpj"):
        if not isinstance(fiscal[campo], str) or not fiscal[campo].strip():
            raise BootstrapValidationError("Bootstrap retornou fiscal incompleto.")


def _sincronizar_cashback(hub, config, sincronizado_em):
    if not config:
        CashbackConfigHub.objects.filter(hub=hub, ativo=True).update(ativo=False, sincronizado_em=sincronizado_em)
        return False
    defaults = {
        "nome": config.get("nome") or "Regra padrão",
        "ativo": bool(config.get("ativo")),
        "percentual": config.get("percentual") or 0,
        "validade_dias": int(config.get("validade_dias") or 0),
        "valor_minimo_geracao": config.get("valor_minimo_geracao") or 0,
        "valor_minimo_uso": config.get("valor_minimo_uso") or 0,
        "limite_uso_percentual": config.get("limite_uso_percentual") or 0,
        "consumidor_final_participa": bool(config.get("consumidor_final_participa")),
        "sincronizado_em": sincronizado_em,
    }
    CashbackConfigHub.objects.update_or_create(
        hub=hub,
        retaguarda_id=config["retaguarda_id"],
        defaults=defaults,
    )
    return True


def _exigir_campos(payload, campos):
    faltando = [campo for campo in campos if payload.get(campo) in (None, "")]
    if faltando:
        raise BootstrapValidationError("Resposta de bootstrap incompleta.")


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise BootstrapValidationError(
            f"Bootstrap retornou {campo} diferente da configuração local."
        )
