from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import CatalogoItemHub


CATALOGO_VERSOES_SUPORTADAS = {1}
CATALOGO_TABELA_PRECO_CODIGO_V1 = "PADRAO"
CATALOGO_TABELA_PRECO_NOME_V1 = "Tabela Padrão"
MOTIVOS_BLOQUEIO_V1 = {"SEM_PRECO", "SEM_ESTOQUE"}


class CatalogoValidationError(Exception):
    """Erro controlado na validação do catálogo da retaguarda."""


def sincronizar_catalogo(hub, resposta):
    dados = _validar_catalogo(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        sku_ids_recebidos = set()
        vendaveis = 0
        nao_vendaveis = 0

        for item in dados["itens"]:
            sku_ids_recebidos.add(item["retaguarda_sku_id"])
            CatalogoItemHub.objects.update_or_create(
                hub=hub,
                retaguarda_sku_id=item["retaguarda_sku_id"],
                defaults={
                    **item,
                    "ativo": True,
                    "sincronizado_em": sincronizado_em,
                },
            )
            if item["vendavel"]:
                vendaveis += 1
            else:
                nao_vendaveis += 1

        itens_inativados = (
            CatalogoItemHub.objects.filter(hub=hub, ativo=True)
            .exclude(retaguarda_sku_id__in=sku_ids_recebidos)
            .update(ativo=False, vendavel=False, sincronizado_em=sincronizado_em)
        )

        tabela = dados["tabela_preco"]
        hub.catalogo_versao = resposta["catalogo_versao"]
        hub.catalogo_gerado_em = dados["gerado_em"]
        hub.catalogo_sincronizado_em = sincronizado_em
        hub.tabela_preco_retaguarda_id = tabela["id"] if tabela else None
        hub.tabela_preco_codigo = tabela["codigo"] if tabela else ""
        hub.tabela_preco_nome = tabela["nome"] if tabela else ""
        hub.save(
            update_fields=[
                "catalogo_versao",
                "catalogo_gerado_em",
                "catalogo_sincronizado_em",
                "tabela_preco_retaguarda_id",
                "tabela_preco_codigo",
                "tabela_preco_nome",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_nome"],
        "loja": dados["loja_nome"],
        "versao": resposta["catalogo_versao"],
        "tabela": hub.tabela_preco_codigo or "não configurada",
        "itens_recebidos": len(dados["itens"]),
        "itens_ativos": CatalogoItemHub.objects.filter(hub=hub, ativo=True).count(),
        "itens_inativados": itens_inativados,
        "vendaveis": vendaveis,
        "nao_vendaveis": nao_vendaveis,
    }


def _validar_catalogo(hub, resposta):
    if not isinstance(resposta, dict):
        raise CatalogoValidationError("Resposta de catálogo inválida.")

    _exigir_campos(
        resposta,
        ("catalogo_versao", "gerado_em", "hub", "empresa", "loja", "total_itens", "itens"),
    )
    if resposta["catalogo_versao"] not in CATALOGO_VERSOES_SUPORTADAS:
        raise CatalogoValidationError("Versão de catálogo não suportada.")

    gerado_em = parse_datetime(str(resposta["gerado_em"]))
    if gerado_em is None:
        raise CatalogoValidationError("Catálogo retornou gerado_em inválido.")
    if timezone.is_naive(gerado_em):
        gerado_em = timezone.make_aware(gerado_em, timezone.get_current_timezone())

    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    itens = resposta["itens"]
    if (
        not isinstance(hub_payload, dict)
        or not isinstance(empresa_payload, dict)
        or not isinstance(loja_payload, dict)
    ):
        raise CatalogoValidationError("Resposta de catálogo inválida.")
    if not isinstance(itens, list):
        raise CatalogoValidationError("Resposta de catálogo inválida: itens deve ser lista.")
    if not isinstance(resposta["total_itens"], int):
        raise CatalogoValidationError("Catálogo retornou total_itens inválido.")
    if resposta["total_itens"] != len(itens):
        raise CatalogoValidationError("Catálogo retornou total_itens divergente.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade("loja_id", loja_payload["id"], hub.loja_id)

    tabela_preco = _validar_tabela_preco(resposta.get("tabela_preco"))
    itens_validados = []
    sku_ids = set()
    for indice, item in enumerate(itens, start=1):
        validado = _validar_item(item, indice)
        sku_id = validado["retaguarda_sku_id"]
        if sku_id in sku_ids:
            raise CatalogoValidationError("Catálogo retornou sku_id duplicado.")
        sku_ids.add(sku_id)
        itens_validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_nome": empresa_payload.get("nome") or hub.empresa_nome or hub.empresa_id,
        "loja_nome": loja_payload.get("nome") or loja_payload.get("apelido") or hub.loja_nome or hub.loja_id,
        "tabela_preco": tabela_preco,
        "itens": itens_validados,
    }


def _validar_tabela_preco(tabela_preco):
    if tabela_preco is None:
        return None
    if not isinstance(tabela_preco, dict):
        raise CatalogoValidationError("Catálogo retornou tabela_preco inválida.")
    _exigir_campos(tabela_preco, ("id", "codigo", "nome"))
    if tabela_preco["codigo"] != CATALOGO_TABELA_PRECO_CODIGO_V1:
        raise CatalogoValidationError("Catálogo V1 retornou tabela de preço incompatível.")
    return {
        "id": tabela_preco["id"],
        "codigo": str(tabela_preco["codigo"]),
        "nome": str(tabela_preco["nome"]),
    }


def _validar_item(item, indice):
    if not isinstance(item, dict):
        raise CatalogoValidationError("Catálogo retornou item inválido.")
    _exigir_campos(
        item,
        (
            "produto_id",
            "sku_id",
            "tipo_produto",
            "descricao",
            "vendavel",
            "motivos_bloqueio",
            "estoque_fisico",
            "reserva",
            "estoque_disponivel",
            "fiscal",
        ),
    )
    if not isinstance(item["vendavel"], bool):
        raise CatalogoValidationError("Catálogo retornou vendavel inválido.")
    if not isinstance(item["motivos_bloqueio"], list):
        raise CatalogoValidationError("Catálogo retornou motivos_bloqueio inválido.")
    if not isinstance(item["fiscal"], dict):
        raise CatalogoValidationError("Catálogo retornou fiscal inválido.")

    motivos = item["motivos_bloqueio"]
    desconhecidos = set(motivos) - MOTIVOS_BLOQUEIO_V1
    if desconhecidos:
        raise CatalogoValidationError("Catálogo V1 retornou motivo de bloqueio desconhecido.")

    preco = _decimal_opcional(item.get("preco"), "preco")
    preco_promocional = _decimal_opcional(item.get("preco_promocional"), "preco_promocional")
    preco_venda = _decimal_opcional(item.get("preco_venda"), "preco_venda")
    estoque_fisico = _decimal_obrigatorio(item["estoque_fisico"], "estoque_fisico")
    reserva = _decimal_obrigatorio(item["reserva"], "reserva")
    estoque_disponivel = _decimal_obrigatorio(item["estoque_disponivel"], "estoque_disponivel")

    if estoque_disponivel != estoque_fisico - reserva:
        raise CatalogoValidationError("Catálogo retornou estoque_disponivel incoerente.")
    if item["vendavel"]:
        if preco_venda is None or preco_venda <= 0:
            raise CatalogoValidationError("Catálogo retornou SKU vendável sem preço válido.")
        if estoque_disponivel <= 0:
            raise CatalogoValidationError("Catálogo retornou SKU vendável sem estoque.")
        if motivos:
            raise CatalogoValidationError("Catálogo retornou SKU vendável com bloqueio.")
    if ("SEM_PRECO" in motivos or "SEM_ESTOQUE" in motivos) and item["vendavel"]:
        raise CatalogoValidationError("Catálogo retornou bloqueio incompatível com vendável.")

    cor = _normalizar_objeto_opcional(item.get("cor"), "cor")
    tamanho = _normalizar_objeto_opcional(item.get("tamanho"), "tamanho")
    unidade = _normalizar_objeto_opcional(item.get("unidade"), "unidade")
    ean13 = item.get("ean13") or ""

    return {
        "retaguarda_produto_id": _inteiro_obrigatorio(item["produto_id"], "produto_id"),
        "retaguarda_sku_id": _inteiro_obrigatorio(item["sku_id"], "sku_id"),
        "tipo_produto": str(item["tipo_produto"]),
        "referencia": str(item.get("referencia") or ""),
        "descricao": str(item["descricao"]),
        "descricao_reduzida": str(item.get("descricao_reduzida") or ""),
        "ean13": str(ean13),
        "codigo_item_ref": str(item.get("codigo_item_ref") or ""),
        "cor_retaguarda_id": cor.get("id"),
        "cor_descricao": cor.get("descricao", ""),
        "tamanho_retaguarda_id": tamanho.get("id"),
        "tamanho_descricao": tamanho.get("descricao", ""),
        "unidade_retaguarda_id": unidade.get("id"),
        "unidade_codigo": unidade.get("codigo", ""),
        "unidade_descricao": unidade.get("descricao", ""),
        "preco": preco,
        "preco_promocional": preco_promocional,
        "preco_venda": preco_venda,
        "estoque_fisico": estoque_fisico,
        "reserva": reserva,
        "estoque_disponivel": estoque_disponivel,
        "vendavel": item["vendavel"],
        "motivos_bloqueio": motivos,
        "fiscal": item["fiscal"],
    }


def _normalizar_objeto_opcional(valor, campo):
    if valor is None:
        return {}
    if not isinstance(valor, dict):
        raise CatalogoValidationError(f"Catálogo retornou {campo} inválido.")
    normalizado = {}
    if valor.get("id") not in (None, ""):
        normalizado["id"] = _inteiro_obrigatorio(valor["id"], f"{campo}.id")
    if valor.get("codigo") not in (None, ""):
        normalizado["codigo"] = str(valor["codigo"])
    if valor.get("descricao") not in (None, ""):
        normalizado["descricao"] = str(valor["descricao"])
    return normalizado


def _decimal_opcional(valor, campo):
    if valor in (None, ""):
        return None
    return _decimal(valor, campo)


def _decimal_obrigatorio(valor, campo):
    if valor in (None, ""):
        raise CatalogoValidationError(f"Catálogo retornou {campo} vazio.")
    return _decimal(valor, campo)


def _decimal(valor, campo):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise CatalogoValidationError(f"Catálogo retornou {campo} inválido.") from exc


def _inteiro_obrigatorio(valor, campo):
    if valor in (None, ""):
        raise CatalogoValidationError(f"Catálogo retornou {campo} vazio.")
    if not isinstance(valor, int):
        raise CatalogoValidationError(f"Catálogo retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos):
    faltando = [campo for campo in campos if payload.get(campo) in (None, "")]
    if faltando:
        raise CatalogoValidationError("Resposta de catálogo incompleta.")


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise CatalogoValidationError(
            f"Catálogo retornou {campo} diferente da configuração local."
        )
