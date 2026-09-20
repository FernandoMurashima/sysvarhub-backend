import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import FormaPagamentoFiscalMapHub, FormaPagamentoHub, FormaPagamentoParcelaHub


FORMAS_PAGAMENTO_VERSOES_SUPORTADAS = {1}


class FormasPagamentoValidationError(Exception):
    """Erro controlado na validação de formas de pagamento da retaguarda."""


def sincronizar_formas_pagamento(hub, resposta):
    dados = _validar_formas_pagamento(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        ids_recebidos = set()
        formas_ativas = 0
        formas_inativas = 0
        total_parcelas = 0
        mapas_recebidos = set()

        for forma_payload in dados["formas_pagamento"]:
            ids_recebidos.add(forma_payload["retaguarda_id"])
            parcelas = forma_payload.pop("parcelas")
            forma, _created = FormaPagamentoHub.objects.update_or_create(
                hub=hub,
                retaguarda_id=forma_payload["retaguarda_id"],
                defaults={
                    **forma_payload,
                    "sincronizado_em": sincronizado_em,
                },
            )
            if forma.ativo:
                formas_ativas += 1
            else:
                formas_inativas += 1

            ordens_recebidas = set()
            for parcela_payload in parcelas:
                ordens_recebidas.add(parcela_payload["ordem"])
                FormaPagamentoParcelaHub.objects.update_or_create(
                    forma=forma,
                    ordem=parcela_payload["ordem"],
                    defaults={
                        **parcela_payload,
                        "sincronizado_em": sincronizado_em,
                    },
                )
                total_parcelas += 1

            forma.parcelas.exclude(ordem__in=ordens_recebidas).delete()

        for mapa in dados["mapas_fiscais"]:
            mapas_recebidos.add((mapa["forma_pagamento_retaguarda_id"], mapa["codigo_tpag"]))
            FormaPagamentoFiscalMapHub.objects.update_or_create(
                hub=hub,
                forma_pagamento_retaguarda_id=mapa["forma_pagamento_retaguarda_id"],
                codigo_tpag=mapa["codigo_tpag"],
                defaults={
                    "descricao_fiscal": mapa["descricao_fiscal"],
                    "sincronizado_em": sincronizado_em,
                },
            )

        mapas_ausentes_removidos = 0
        for mapa in FormaPagamentoFiscalMapHub.objects.filter(hub=hub):
            chave = (mapa.forma_pagamento_retaguarda_id, mapa.codigo_tpag)
            if chave not in mapas_recebidos:
                mapa.delete()
                mapas_ausentes_removidos += 1

        formas_ausentes_inativadas = (
            FormaPagamentoHub.objects.filter(hub=hub, ativo=True)
            .exclude(retaguarda_id__in=ids_recebidos)
            .update(ativo=False, sincronizado_em=sincronizado_em)
        )

        hub.formas_pagamento_versao = resposta["formas_pagamento_versao"]
        hub.formas_pagamento_gerado_em = dados["gerado_em"]
        hub.formas_pagamento_sincronizado_em = sincronizado_em
        hub.save(
            update_fields=[
                "formas_pagamento_versao",
                "formas_pagamento_gerado_em",
                "formas_pagamento_sincronizado_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_id"],
        "loja": dados["loja_id"],
        "versao": resposta["formas_pagamento_versao"],
        "formas_recebidas": len(dados["formas_pagamento"]),
        "formas_ativas": formas_ativas,
        "formas_inativas": formas_inativas,
        "formas_ausentes_inativadas": formas_ausentes_inativadas,
        "parcelas": total_parcelas,
        "mapas_fiscais": len(dados["mapas_fiscais"]),
        "mapas_fiscais_ausentes_removidos": mapas_ausentes_removidos,
    }


def _validar_formas_pagamento(hub, resposta):
    if not isinstance(resposta, dict):
        raise FormasPagamentoValidationError("Resposta de formas de pagamento inválida.")

    _exigir_campos(
        resposta,
        ("formas_pagamento_versao", "gerado_em", "hub", "empresa", "loja", "formas_pagamento"),
    )
    if resposta["formas_pagamento_versao"] not in FORMAS_PAGAMENTO_VERSOES_SUPORTADAS:
        raise FormasPagamentoValidationError("Versão de formas de pagamento não suportada.")

    gerado_em = parse_datetime(str(resposta["gerado_em"]))
    if gerado_em is None:
        raise FormasPagamentoValidationError("Formas de pagamento retornou gerado_em inválido.")
    if timezone.is_naive(gerado_em):
        gerado_em = timezone.make_aware(gerado_em, timezone.get_current_timezone())

    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    formas = resposta["formas_pagamento"]
    mapas = resposta.get("mapas_fiscais") or []
    if (
        not isinstance(hub_payload, dict)
        or not isinstance(empresa_payload, dict)
        or not isinstance(loja_payload, dict)
    ):
        raise FormasPagamentoValidationError("Resposta de formas de pagamento inválida.")
    if not isinstance(formas, list):
        raise FormasPagamentoValidationError("Resposta de formas de pagamento inválida: formas_pagamento deve ser lista.")
    if not isinstance(mapas, list):
        raise FormasPagamentoValidationError("Resposta de formas de pagamento inválida: mapas_fiscais deve ser lista.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade_inteira("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade_inteira("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade_inteira("loja_id", loja_payload["id"], hub.loja_id)

    formas_validadas = []
    ids = set()
    codigos = set()
    for item in formas:
        validado = _validar_forma(item)
        if validado["retaguarda_id"] in ids:
            raise FormasPagamentoValidationError("Formas de pagamento retornou id duplicado.")
        if validado["codigo"] in codigos:
            raise FormasPagamentoValidationError("Formas de pagamento retornou codigo duplicado.")
        ids.add(validado["retaguarda_id"])
        codigos.add(validado["codigo"])
        formas_validadas.append(validado)

    mapas_validados = []
    mapas_chaves = set()
    for item in mapas:
        validado = _validar_mapa_fiscal(item)
        if validado["forma_pagamento_retaguarda_id"] not in ids:
            raise FormasPagamentoValidationError("Mapa fiscal retornou forma_pagamento_id desconhecido.")
        chave = (validado["forma_pagamento_retaguarda_id"], validado["codigo_tpag"])
        if chave in mapas_chaves:
            raise FormasPagamentoValidationError("Mapa fiscal duplicado.")
        mapas_chaves.add(chave)
        mapas_validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_id": empresa_payload["id"],
        "loja_id": loja_payload["id"],
        "formas_pagamento": formas_validadas,
        "mapas_fiscais": mapas_validados,
    }


def _validar_forma(item):
    if not isinstance(item, dict):
        raise FormasPagamentoValidationError("Formas de pagamento retornou item inválido.")
    _exigir_campos(
        item,
        (
            "id",
            "codigo",
            "descricao",
            "tipo",
            "num_parcelas",
            "ativo",
            "prazo_pagamento",
            "adquirente",
            "conta_liquidacao_id",
            "gera_recebivel_bancario",
            "prazo_credito_dias",
            "taxa_percentual",
            "taxa_fixa",
            "tef_habilitado",
            "tef_modalidade",
            "tef_adquirente_codigo",
            "tef_terminal_logico",
            "parcelas",
        ),
        permitir_nulos={"prazo_pagamento", "adquirente", "conta_liquidacao_id"},
        permitir_vazios={
            "tef_modalidade",
            "tef_adquirente_codigo",
            "tef_terminal_logico",
        },
    )
    if not isinstance(item["ativo"], bool):
        raise FormasPagamentoValidationError("Formas de pagamento retornou ativo inválido.")
    if not isinstance(item["gera_recebivel_bancario"], bool):
        raise FormasPagamentoValidationError("Formas de pagamento retornou gera_recebivel_bancario inválido.")
    if not isinstance(item["tef_habilitado"], bool):
        raise FormasPagamentoValidationError("Formas de pagamento retornou tef_habilitado inválido.")
    if not isinstance(item["parcelas"], list):
        raise FormasPagamentoValidationError("Formas de pagamento retornou parcelas inválidas.")

    prazo = _validar_prazo(item["prazo_pagamento"])
    parcelas = []
    ordens = set()
    for parcela in item["parcelas"]:
        parcela_validada = _validar_parcela(parcela)
        if parcela_validada["ordem"] in ordens:
            raise FormasPagamentoValidationError("Formas de pagamento retornou ordem de parcela duplicada.")
        ordens.add(parcela_validada["ordem"])
        parcelas.append(parcela_validada)

    return {
        "retaguarda_id": _inteiro_positivo(item["id"], "id"),
        "codigo": _texto_obrigatorio(item["codigo"], "codigo", max_length=10),
        "descricao": _texto_obrigatorio(item["descricao"], "descricao", max_length=120),
        "tipo": _texto_obrigatorio(item["tipo"], "tipo", max_length=24),
        "num_parcelas": _inteiro_positivo(item["num_parcelas"], "num_parcelas"),
        "ativo": item["ativo"],
        "prazo_retaguarda_id": prazo["id"] if prazo else None,
        "prazo_codigo": prazo["codigo"] if prazo else "",
        "prazo_descricao": prazo["descricao"] if prazo else "",
        "prazo_num_parcelas": prazo["num_parcelas"] if prazo else None,
        "prazo_intervalo_dias": prazo["intervalo_dias"] if prazo else None,
        "adquirente": _texto_opcional(item["adquirente"], "adquirente", max_length=80),
        "conta_liquidacao_retaguarda_id": _inteiro_positivo_opcional(
            item["conta_liquidacao_id"],
            "conta_liquidacao_id",
        ),
        "gera_recebivel_bancario": item["gera_recebivel_bancario"],
        "prazo_credito_dias": _inteiro_nao_negativo(item["prazo_credito_dias"], "prazo_credito_dias"),
        "taxa_percentual": _decimal_string(item["taxa_percentual"], "taxa_percentual", 4),
        "taxa_fixa": _decimal_string(item["taxa_fixa"], "taxa_fixa", 2),
        "tef_habilitado": item["tef_habilitado"],
        "tef_modalidade": _texto_opcional(item["tef_modalidade"], "tef_modalidade", max_length=20) or "",
        "tef_adquirente_codigo": _texto_opcional(item["tef_adquirente_codigo"], "tef_adquirente_codigo", max_length=40) or "",
        "tef_terminal_logico": _texto_opcional(item["tef_terminal_logico"], "tef_terminal_logico", max_length=40) or "",
        "parcelas": sorted(parcelas, key=lambda parcela: parcela["ordem"]),
    }


def _validar_prazo(prazo):
    if prazo is None:
        return None
    if not isinstance(prazo, dict):
        raise FormasPagamentoValidationError("Formas de pagamento retornou prazo_pagamento inválido.")
    _exigir_campos(prazo, ("id", "codigo", "descricao", "num_parcelas", "intervalo_dias"))
    return {
        "id": _inteiro_positivo(prazo["id"], "prazo_pagamento.id"),
        "codigo": _texto_obrigatorio(prazo["codigo"], "prazo_pagamento.codigo", max_length=12),
        "descricao": _texto_obrigatorio(prazo["descricao"], "prazo_pagamento.descricao", max_length=120),
        "num_parcelas": _inteiro_positivo(prazo["num_parcelas"], "prazo_pagamento.num_parcelas"),
        "intervalo_dias": _inteiro_nao_negativo(prazo["intervalo_dias"], "prazo_pagamento.intervalo_dias"),
    }


def _validar_parcela(parcela):
    if not isinstance(parcela, dict):
        raise FormasPagamentoValidationError("Formas de pagamento retornou parcela inválida.")
    _exigir_campos(
        parcela,
        ("ordem", "dias", "percentual", "valor_fixo"),
        permitir_nulos={"percentual", "valor_fixo"},
    )
    return {
        "ordem": _inteiro_positivo(parcela["ordem"], "parcela.ordem"),
        "dias": _inteiro_nao_negativo(parcela["dias"], "parcela.dias"),
        "percentual": _decimal_string_opcional(parcela["percentual"], "parcela.percentual", 6),
        "valor_fixo": _decimal_string_opcional(parcela["valor_fixo"], "parcela.valor_fixo", 2),
    }


def _validar_mapa_fiscal(item):
    if not isinstance(item, dict):
        raise FormasPagamentoValidationError("Mapa fiscal inválido.")
    _exigir_campos(item, ("forma_pagamento_id", "codigo_tpag", "descricao_fiscal"), permitir_vazios={"descricao_fiscal"})
    codigo = _texto_obrigatorio(item["codigo_tpag"], "codigo_tpag", max_length=2)
    if not codigo.isdigit() or len(codigo) != 2:
        raise FormasPagamentoValidationError("Mapa fiscal retornou codigo_tpag inválido.")
    return {
        "forma_pagamento_retaguarda_id": _inteiro_positivo(item["forma_pagamento_id"], "forma_pagamento_id"),
        "codigo_tpag": codigo,
        "descricao_fiscal": _texto_opcional(item["descricao_fiscal"], "descricao_fiscal", max_length=80) or "",
    }


def _decimal_string_opcional(valor, campo, casas):
    if valor is None:
        return None
    return _decimal_string(valor, campo, casas)


def _decimal_string(valor, campo, casas):
    if not isinstance(valor, str):
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    pattern = rf"^-?\d+\.\d{{{casas}}}$"
    if not re.fullmatch(pattern, valor):
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    try:
        decimal = Decimal(valor)
    except (InvalidOperation, ValueError) as exc:
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.") from exc
    if not decimal.is_finite():
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    return decimal


def _texto_obrigatorio(valor, campo, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    texto = valor.strip()
    if len(texto) > max_length:
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    return texto


def _texto_opcional(valor, campo, *, max_length):
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    texto = valor.strip()
    if len(texto) > max_length:
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    return texto


def _inteiro_positivo(valor, campo):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    return valor


def _inteiro_positivo_opcional(valor, campo):
    if valor is None:
        return None
    return _inteiro_positivo(valor, campo)


def _inteiro_nao_negativo(valor, campo):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise FormasPagamentoValidationError(f"Formas de pagamento retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos, *, permitir_nulos=None, permitir_vazios=None):
    permitir_nulos = permitir_nulos or set()
    permitir_vazios = permitir_vazios or set()
    faltando = []
    for campo in campos:
        if campo not in payload:
            faltando.append(campo)
        elif campo not in permitir_nulos and payload.get(campo) is None:
            faltando.append(campo)
        elif campo not in permitir_vazios and payload.get(campo) == "":
            faltando.append(campo)
    if faltando:
        raise FormasPagamentoValidationError("Resposta de formas de pagamento incompleta.")


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise FormasPagamentoValidationError(
            f"Formas de pagamento retornou {campo} diferente da configuração local."
        )


def _validar_identidade_inteira(campo, recebido, esperado):
    if isinstance(recebido, bool) or not isinstance(recebido, int) or recebido != esperado:
        raise FormasPagamentoValidationError(
            f"Formas de pagamento retornou {campo} diferente da configuração local."
        )
