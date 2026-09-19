from decimal import Decimal, InvalidOperation
import logging
import re
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import CatalogoItemHub
from integracao.services.retaguarda import RetaguardaError


logger = logging.getLogger(__name__)

CATALOGO_VERSOES_SUPORTADAS = {1, 2}
CATALOGO_TABELA_PRECO_CODIGO_V1 = "PADRAO"
CATALOGO_TABELA_PRECO_NOME_V1 = "Tabela Padrão"
MOTIVOS_BLOQUEIO_V1 = {"SEM_PRECO", "SEM_ESTOQUE"}


class CatalogoValidationError(Exception):
    """Erro controlado na validação do catálogo da retaguarda."""


def sincronizar_catalogo(hub, resposta, client=None):
    dados = _validar_catalogo(hub, resposta)
    sincronizado_em = timezone.now()
    imagens_cache = {}

    with transaction.atomic():
        sku_ids_recebidos = set()
        vendaveis = 0
        nao_vendaveis = 0

        for item in dados["itens"]:
            sku_ids_recebidos.add(item["retaguarda_sku_id"])
            imagem = item["imagem"]
            item_catalogo = {chave: valor for chave, valor in item.items() if chave != "imagem"}
            imagem_defaults = _imagem_defaults_para_item(hub, item["retaguarda_produto_id"], imagem)
            CatalogoItemHub.objects.update_or_create(
                hub=hub,
                retaguarda_sku_id=item["retaguarda_sku_id"],
                defaults={
                    **item_catalogo,
                    **imagem_defaults,
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

    if client and hub.retaguarda_token:
        _sincronizar_imagens(hub, dados["itens"], client, imagens_cache)
        _limpar_arquivos_orfaos()

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
        "imagem": _validar_imagem(item.get("imagem")),
    }


def _validar_imagem(imagem):
    if imagem is None:
        return None
    if not isinstance(imagem, dict):
        raise CatalogoValidationError("Catálogo retornou imagem inválida.")
    _exigir_campos(imagem, ("id", "versao", "tipo"))
    tipo = str(imagem["tipo"])
    if tipo not in ("reduzida", "original"):
        raise CatalogoValidationError("Catálogo retornou imagem.tipo inválido.")
    return {
        "id": _inteiro_obrigatorio(imagem["id"], "imagem.id"),
        "versao": str(imagem["versao"]),
        "tipo": tipo,
    }


def _imagem_defaults_para_item(hub, produto_id, imagem):
    if not imagem:
        return {
            "imagem_retaguarda_id": None,
            "imagem_versao": "",
            "imagem_tipo": "",
            "imagem_local": "",
        }
    existente = (
        CatalogoItemHub.objects.filter(
            hub=hub,
            retaguarda_produto_id=produto_id,
            imagem_retaguarda_id=imagem["id"],
            imagem_versao=imagem["versao"],
            imagem_local__gt="",
        )
        .first()
    )
    try:
        arquivo_existente = _resolver_imagem_local(existente.imagem_local) if existente else None
    except CatalogoValidationError as exc:
        logger.warning(
            "Falha ao resolver imagem local existente produto=%s imagem=%s: %s",
            produto_id,
            imagem["id"],
            exc,
        )
        arquivo_existente = None
    if arquivo_existente and _arquivo_cache_valido(arquivo_existente):
        return {
            "imagem_retaguarda_id": imagem["id"],
            "imagem_versao": imagem["versao"],
            "imagem_tipo": imagem["tipo"],
            "imagem_local": existente.imagem_local,
        }
    return {
        "imagem_retaguarda_id": imagem["id"],
        "imagem_versao": imagem["versao"],
        "imagem_tipo": imagem["tipo"],
        "imagem_local": "",
    }


def _sincronizar_imagens(hub, itens, client, imagens_cache):
    imagens_por_produto = {}
    for item in itens:
        imagem = item.get("imagem")
        if imagem:
            imagens_por_produto.setdefault(item["retaguarda_produto_id"], imagem)

    for produto_id, imagem in imagens_por_produto.items():
        chave = (imagem["id"], imagem["versao"])
        try:
            destino_relativo_base = _caminho_relativo_imagem_base(produto_id, imagem)
            destino_base = _resolver_imagem_local(destino_relativo_base)
            arquivo_cache = _arquivo_cache_existente(destino_base)
        except (CatalogoValidationError, OSError) as exc:
            logger.warning(
                "Falha ao preparar cache de imagem produto=%s imagem=%s: %s",
                produto_id,
                imagem["id"],
                exc,
            )
            imagens_cache[chave] = None
            continue
        if arquivo_cache:
            _associar_imagem_produto(hub, produto_id, imagem, _relativo_data_dir(arquivo_cache))
            continue
        if chave not in imagens_cache:
            try:
                arquivo_baixado = client.baixar_catalogo_imagem(
                    token=hub.retaguarda_token,
                    imagem_id=imagem["id"],
                    destino=destino_base,
                )
                if arquivo_baixado and _arquivo_existe(arquivo_baixado):
                    imagens_cache[chave] = _relativo_data_dir(arquivo_baixado)
                else:
                    imagens_cache[chave] = None
            except (RetaguardaError, OSError) as exc:
                logger.warning(
                    "Falha ao baixar imagem do catálogo produto=%s imagem=%s: %s",
                    produto_id,
                    imagem["id"],
                    exc,
                )
                imagens_cache[chave] = None
        if imagens_cache[chave]:
            _associar_imagem_produto(hub, produto_id, imagem, imagens_cache[chave])


def _associar_imagem_produto(hub, produto_id, imagem, caminho_relativo):
    CatalogoItemHub.objects.filter(
        hub=hub,
        retaguarda_produto_id=produto_id,
        imagem_retaguarda_id=imagem["id"],
        imagem_versao=imagem["versao"],
    ).update(
        imagem_tipo=imagem["tipo"],
        imagem_local=caminho_relativo,
        atualizado_em=timezone.now(),
    )


def _catalogo_imagens_dir():
    return Path(settings.SYSVARHUB_DATA_DIR) / "catalogo-imagens"


def _caminho_relativo_imagem_base(produto_id, imagem):
    versao = re.sub(r"[^A-Za-z0-9._-]", "-", imagem["versao"])[:80]
    return f"catalogo-imagens/produto-{produto_id}/imagem-{imagem['id']}-{versao}"


def _arquivo_cache_existente(destino_base):
    for extensao in (".jpg", ".png", ".webp", ".gif"):
        candidato = destino_base.with_suffix(extensao)
        if _arquivo_cache_valido(candidato):
            return candidato
    return None


def _arquivo_cache_valido(caminho):
    return caminho.suffix.lower() in (".jpg", ".png", ".webp", ".gif") and _arquivo_existe(caminho)


def _arquivo_existe(caminho):
    try:
        return caminho.is_file()
    except OSError:
        return False


def _resolver_imagem_local(caminho_relativo):
    base = Path(settings.SYSVARHUB_DATA_DIR).resolve()
    destino = (base / caminho_relativo).resolve()
    if base != destino and base not in destino.parents:
        raise CatalogoValidationError("Caminho local de imagem inválido.")
    return destino


def _relativo_data_dir(caminho):
    return caminho.relative_to(Path(settings.SYSVARHUB_DATA_DIR).resolve()).as_posix()


def _limpar_arquivos_orfaos():
    base = _catalogo_imagens_dir()
    try:
        if not base.exists():
            return
        arquivos = list(base.glob("produto-*/*"))
    except OSError as exc:
        logger.warning("Falha ao varrer imagens órfãs do catálogo: %s", exc)
        return
    usados = set(
        CatalogoItemHub.objects.exclude(imagem_local="")
        .values_list("imagem_local", flat=True)
        .distinct()
    )
    for arquivo in arquivos:
        try:
            relativo = arquivo.relative_to(Path(settings.SYSVARHUB_DATA_DIR)).as_posix()
            if arquivo.is_file() and relativo not in usados:
                arquivo.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Falha ao limpar imagem órfã do catálogo %s: %s", arquivo, exc)


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
