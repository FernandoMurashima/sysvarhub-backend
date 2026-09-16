from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import TipoDespesaPdvHub


TIPOS_DESPESA_PDV_VERSOES_SUPORTADAS = {1}


class TiposDespesaPdvValidationError(Exception):
    """Erro controlado na validação de tipos de despesa PDV da retaguarda."""


def sincronizar_tipos_despesa_pdv(hub, resposta):
    dados = _validar_tipos_despesa_pdv(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        ids_recebidos = set()
        for tipo_payload in dados["tipos"]:
            retaguarda_id = tipo_payload["retaguarda_id"]
            ids_recebidos.add(retaguarda_id)
            defaults = {
                campo: valor
                for campo, valor in tipo_payload.items()
                if campo != "retaguarda_id"
            }
            TipoDespesaPdvHub.objects.update_or_create(
                hub=hub,
                retaguarda_id=retaguarda_id,
                defaults={
                    **defaults,
                    "presente_retaguarda": True,
                    "sincronizado_em": sincronizado_em,
                },
            )

        ausentes = (
            TipoDespesaPdvHub.objects.select_for_update()
            .filter(hub=hub, presente_retaguarda=True)
            .exclude(retaguarda_id__in=ids_recebidos)
            .update(presente_retaguarda=False, sincronizado_em=sincronizado_em)
        )

        hub.tipos_despesa_pdv_versao = resposta["tipos_despesa_pdv_versao"]
        hub.tipos_despesa_pdv_gerado_em = dados["gerado_em"]
        hub.tipos_despesa_pdv_sincronizado_em = sincronizado_em
        hub.save(
            update_fields=[
                "tipos_despesa_pdv_versao",
                "tipos_despesa_pdv_gerado_em",
                "tipos_despesa_pdv_sincronizado_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_id"],
        "loja": dados["loja_id"],
        "versao": resposta["tipos_despesa_pdv_versao"],
        "tipos_recebidos": len(dados["tipos"]),
        "tipos_ausentes_marcados": ausentes,
    }


def _validar_tipos_despesa_pdv(hub, resposta):
    if not isinstance(resposta, dict):
        raise TiposDespesaPdvValidationError("Resposta de tipos de despesa PDV inválida.")
    _exigir_campos(resposta, ("tipos_despesa_pdv_versao", "gerado_em", "hub", "empresa", "loja", "tipos_despesa_pdv"))
    if resposta["tipos_despesa_pdv_versao"] not in TIPOS_DESPESA_PDV_VERSOES_SUPORTADAS:
        raise TiposDespesaPdvValidationError("Versão de tipos de despesa PDV não suportada.")

    gerado_em = _datetime(resposta["gerado_em"], "gerado_em")
    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    tipos = resposta["tipos_despesa_pdv"]
    if not all(isinstance(payload, dict) for payload in (hub_payload, empresa_payload, loja_payload)):
        raise TiposDespesaPdvValidationError("Resposta de tipos de despesa PDV inválida.")
    if not isinstance(tipos, list):
        raise TiposDespesaPdvValidationError("Resposta de tipos de despesa PDV inválida: tipos_despesa_pdv deve ser lista.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade_inteira("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade_inteira("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade_inteira("loja_id", loja_payload["id"], hub.loja_id)

    tipos_validados = []
    ids = set()
    for item in tipos:
        validado = _validar_tipo(item)
        if validado["retaguarda_id"] in ids:
            raise TiposDespesaPdvValidationError("Tipos de despesa PDV retornou id duplicado.")
        ids.add(validado["retaguarda_id"])
        tipos_validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_id": empresa_payload["id"],
        "loja_id": loja_payload["id"],
        "tipos": tipos_validados,
    }


def _validar_tipo(item):
    if not isinstance(item, dict):
        raise TiposDespesaPdvValidationError("Tipos de despesa PDV retornou item inválido.")
    _exigir_campos(item, ("id", "codigo", "descricao", "exige_documento", "ativo", "natureza"))
    for campo in ("exige_documento", "ativo"):
        if not isinstance(item[campo], bool):
            raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    natureza = _validar_natureza(item["natureza"])
    return {
        "retaguarda_id": _inteiro_positivo(item["id"], "id"),
        "codigo": _texto_obrigatorio(item["codigo"], "codigo", max_length=20),
        "descricao": _texto_obrigatorio(item["descricao"], "descricao", max_length=120),
        "exige_documento": item["exige_documento"],
        "ativo": item["ativo"],
        **natureza,
    }


def _validar_natureza(natureza):
    if not isinstance(natureza, dict):
        raise TiposDespesaPdvValidationError("Tipos de despesa PDV retornou natureza inválida.")
    _exigir_campos(
        natureza,
        (
            "id",
            "codigo",
            "descricao",
            "categoria_principal",
            "subcategoria",
            "tipo",
            "status",
            "tipo_natureza",
            "natureza_operacao",
            "categoria_gerencial",
            "movimenta_financeiro",
            "entra_dre",
        ),
        permitir_nulos={
            "categoria_principal",
            "subcategoria",
            "categoria_gerencial",
        },
        permitir_vazios={
            "categoria_principal",
            "subcategoria",
            "categoria_gerencial",
        },
    )
    for campo in ("movimenta_financeiro", "entra_dre"):
        if not isinstance(natureza[campo], bool):
            raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou natureza.{campo} inválido.")
    return {
        "natureza_retaguarda_id": _inteiro_positivo(natureza["id"], "natureza.id"),
        "natureza_codigo": _texto_obrigatorio(natureza["codigo"], "natureza.codigo", max_length=10),
        "natureza_descricao": _texto_obrigatorio(natureza["descricao"], "natureza.descricao", max_length=255),
        "natureza_categoria_principal": _texto_opcional(natureza["categoria_principal"], "natureza.categoria_principal", max_length=50) or "",
        "natureza_subcategoria": _texto_opcional(natureza["subcategoria"], "natureza.subcategoria", max_length=50) or "",
        "natureza_tipo": _texto_obrigatorio(natureza["tipo"], "natureza.tipo", max_length=20),
        "natureza_status": _texto_obrigatorio(natureza["status"], "natureza.status", max_length=10),
        "natureza_tipo_natureza": _texto_obrigatorio(natureza["tipo_natureza"], "natureza.tipo_natureza", max_length=10),
        "natureza_operacao": _texto_obrigatorio(natureza["natureza_operacao"], "natureza.natureza_operacao", max_length=20),
        "natureza_categoria_gerencial": _texto_opcional(natureza["categoria_gerencial"], "natureza.categoria_gerencial", max_length=50) or "",
        "natureza_movimenta_financeiro": natureza["movimenta_financeiro"],
        "natureza_entra_dre": natureza["entra_dre"],
    }


def _datetime(valor, campo):
    if not isinstance(valor, str):
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    data = parse_datetime(valor)
    if data is None:
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    if timezone.is_naive(data):
        data = timezone.make_aware(data, timezone.get_current_timezone())
    return data


def _texto_obrigatorio(valor, campo, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_opcional(valor, campo, *, max_length):
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_tamanho(texto, campo, max_length):
    if len(texto) > max_length:
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    return texto


def _inteiro_positivo(valor, campo):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos, *, permitir_nulos=None, permitir_vazios=None):
    permitir_nulos = permitir_nulos or set()
    permitir_vazios = permitir_vazios or set()
    for campo in campos:
        if campo not in payload:
            raise TiposDespesaPdvValidationError(
                f"Tipos de despesa PDV retornou campo obrigatório ausente: {campo}."
            )
        if campo not in permitir_nulos and payload.get(campo) is None:
            raise TiposDespesaPdvValidationError(
                f"Tipos de despesa PDV retornou campo obrigatório nulo: {campo}."
            )
        if campo not in permitir_vazios and payload.get(campo) == "":
            raise TiposDespesaPdvValidationError(
                f"Tipos de despesa PDV retornou campo obrigatório vazio: {campo}."
            )


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} diferente da configuração local.")


def _validar_identidade_inteira(campo, recebido, esperado):
    if isinstance(recebido, bool) or not isinstance(recebido, int) or recebido != esperado:
        raise TiposDespesaPdvValidationError(f"Tipos de despesa PDV retornou {campo} diferente da configuração local.")
