from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import VendedorHub


VENDEDORES_VERSOES_SUPORTADAS = {1}


class VendedoresValidationError(Exception):
    """Erro controlado na validação de vendedores da retaguarda."""


def sincronizar_vendedores(hub, resposta):
    dados = _validar_vendedores(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        ids_recebidos = set()

        for vendedor_payload in dados["vendedores"]:
            retaguarda_id = vendedor_payload["retaguarda_id"]
            ids_recebidos.add(retaguarda_id)
            defaults = {
                campo: valor
                for campo, valor in vendedor_payload.items()
                if campo != "retaguarda_id"
            }
            VendedorHub.objects.update_or_create(
                hub=hub,
                retaguarda_id=retaguarda_id,
                defaults={
                    **defaults,
                    "presente_retaguarda": True,
                    "sincronizado_em": sincronizado_em,
                },
            )

        ausentes = (
            VendedorHub.objects.select_for_update()
            .filter(hub=hub, presente_retaguarda=True)
            .exclude(retaguarda_id__in=ids_recebidos)
            .update(presente_retaguarda=False, sincronizado_em=sincronizado_em)
        )

        hub.vendedores_versao = resposta["vendedores_versao"]
        hub.vendedores_gerado_em = dados["gerado_em"]
        hub.vendedores_sincronizado_em = sincronizado_em
        hub.save(
            update_fields=[
                "vendedores_versao",
                "vendedores_gerado_em",
                "vendedores_sincronizado_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_id"],
        "loja": dados["loja_id"],
        "versao": resposta["vendedores_versao"],
        "vendedores_recebidos": len(dados["vendedores"]),
        "vendedores_ausentes_marcados": ausentes,
    }


def _validar_vendedores(hub, resposta):
    if not isinstance(resposta, dict):
        raise VendedoresValidationError("Resposta de vendedores inválida.")
    _exigir_campos(resposta, ("vendedores_versao", "gerado_em", "hub", "empresa", "loja", "vendedores"))
    if resposta["vendedores_versao"] not in VENDEDORES_VERSOES_SUPORTADAS:
        raise VendedoresValidationError("Versão de vendedores não suportada.")

    gerado_em = _datetime(resposta["gerado_em"], "gerado_em")
    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    vendedores = resposta["vendedores"]
    if not all(isinstance(payload, dict) for payload in (hub_payload, empresa_payload, loja_payload)):
        raise VendedoresValidationError("Resposta de vendedores inválida.")
    if not isinstance(vendedores, list):
        raise VendedoresValidationError("Resposta de vendedores inválida: vendedores deve ser lista.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade_inteira("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade_inteira("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade_inteira("loja_id", loja_payload["id"], hub.loja_id)

    vendedores_validados = []
    ids = set()
    for item in vendedores:
        validado = _validar_vendedor(item)
        if validado["retaguarda_id"] in ids:
            raise VendedoresValidationError("Vendedores retornou id duplicado.")
        ids.add(validado["retaguarda_id"])
        vendedores_validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_id": empresa_payload["id"],
        "loja_id": loja_payload["id"],
        "vendedores": vendedores_validados,
    }


def _validar_vendedor(item):
    if not isinstance(item, dict):
        raise VendedoresValidationError("Vendedores retornou item inválido.")
    _exigir_campos(
        item,
        (
            "id",
            "matricula",
            "nome",
            "apelido",
            "cargo",
            "comissionado",
            "comissao_percentual",
            "ativo",
            "situacao",
            "participa_vendas",
        ),
        permitir_nulos={"apelido", "cargo"},
        permitir_vazios={"matricula", "apelido"},
    )
    for campo in ("comissionado", "ativo", "participa_vendas"):
        if not isinstance(item[campo], bool):
            raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    cargo = _validar_cargo(item["cargo"])
    return {
        "retaguarda_id": _inteiro_positivo(item["id"], "id"),
        "matricula": _texto_opcional(item["matricula"], "matricula", max_length=6) or "",
        "nome": _texto_obrigatorio(item["nome"], "nome", max_length=50),
        "apelido": _texto_opcional(item["apelido"], "apelido", max_length=20) or "",
        "cargo_retaguarda_id": cargo["id"] if cargo else None,
        "cargo_codigo": cargo["codigo"] if cargo else "",
        "cargo_descricao": cargo["descricao"] if cargo else "",
        "comissionado": item["comissionado"],
        "comissao_percentual": _decimal(item["comissao_percentual"], "comissao_percentual"),
        "ativo": item["ativo"],
        "situacao": _texto_obrigatorio(item["situacao"], "situacao", max_length=12),
        "participa_vendas": item["participa_vendas"],
    }


def _validar_cargo(cargo):
    if cargo is None:
        return None
    if not isinstance(cargo, dict):
        raise VendedoresValidationError("Vendedores retornou cargo inválido.")
    _exigir_campos(cargo, ("id", "codigo", "descricao"), permitir_vazios={"codigo", "descricao"})
    return {
        "id": _inteiro_positivo(cargo["id"], "cargo.id"),
        "codigo": _texto_opcional(cargo["codigo"], "cargo.codigo", max_length=20) or "",
        "descricao": _texto_opcional(cargo["descricao"], "cargo.descricao", max_length=80) or "",
    }


def _decimal(valor, campo):
    if isinstance(valor, float):
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    try:
        decimal = Decimal(str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.") from exc
    if not decimal.is_finite():
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    return decimal


def _datetime(valor, campo):
    if not isinstance(valor, str):
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    data = parse_datetime(valor)
    if data is None:
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    if timezone.is_naive(data):
        data = timezone.make_aware(data, timezone.get_current_timezone())
    return data


def _texto_obrigatorio(valor, campo, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_opcional(valor, campo, *, max_length):
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_tamanho(texto, campo, max_length):
    if len(texto) > max_length:
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    return texto


def _inteiro_positivo(valor, campo):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise VendedoresValidationError(f"Vendedores retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos, *, permitir_nulos=None, permitir_vazios=None):
    permitir_nulos = permitir_nulos or set()
    permitir_vazios = permitir_vazios or set()
    for campo in campos:
        if campo not in payload:
            raise VendedoresValidationError(
                f"Vendedores retornou campo obrigatório ausente: {campo}."
            )
        if campo not in permitir_nulos and payload.get(campo) is None:
            raise VendedoresValidationError(
                f"Vendedores retornou campo obrigatório nulo: {campo}."
            )
        if campo not in permitir_vazios and payload.get(campo) == "":
            raise VendedoresValidationError(
                f"Vendedores retornou campo obrigatório vazio: {campo}."
            )


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise VendedoresValidationError(f"Vendedores retornou {campo} diferente da configuração local.")


def _validar_identidade_inteira(campo, recebido, esperado):
    if isinstance(recebido, bool) or not isinstance(recebido, int) or recebido != esperado:
        raise VendedoresValidationError(f"Vendedores retornou {campo} diferente da configuração local.")
