from django.contrib.auth.hashers import identify_hasher
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import OperadorHub
from core.services.operadores import encerrar_sessoes_ativas_operador


OPERADORES_VERSOES_SUPORTADAS = {1}
MOTIVO_CREDENCIAL_ALTERADA = "CREDENCIAL_ALTERADA"
MOTIVO_OPERADOR_INATIVO = "OPERADOR_INATIVO"


class OperadoresValidationError(Exception):
    """Erro controlado na validação de operadores da retaguarda."""


def sincronizar_operadores(hub, resposta):
    dados = _validar_operadores(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        usuarios_recebidos = set()
        sessoes_revogadas = 0

        for operador_payload in dados["operadores"]:
            usuarios_recebidos.add(operador_payload["retaguarda_usuario_id"])
            operador_existente = (
                OperadorHub.objects.select_for_update()
                .filter(
                    hub=hub,
                    retaguarda_usuario_id=operador_payload["retaguarda_usuario_id"],
                )
                .first()
            )
            deve_revogar = False
            if operador_existente is not None:
                hash_mudou = (
                    operador_existente.ativo
                    and operador_existente.credencial_hash
                    and operador_existente.credencial_hash != operador_payload["credencial_hash"]
                )
                inativou = operador_existente.ativo and not operador_payload["ativo"]
                deve_revogar = hash_mudou or inativou

            operador, _created = OperadorHub.objects.update_or_create(
                hub=hub,
                retaguarda_usuario_id=operador_payload["retaguarda_usuario_id"],
                defaults={
                    **operador_payload,
                    "credencial_hash": (
                        operador_payload["credencial_hash"]
                        if operador_payload["ativo"]
                        else ""
                    ),
                    "sincronizado_em": sincronizado_em,
                },
            )

            if deve_revogar:
                motivo = (
                    MOTIVO_CREDENCIAL_ALTERADA
                    if operador_payload["ativo"]
                    else MOTIVO_OPERADOR_INATIVO
                )
                sessoes_revogadas += encerrar_sessoes_ativas_operador(operador, motivo)

        ausentes = (
            OperadorHub.objects.select_for_update()
            .filter(hub=hub, ativo=True)
            .exclude(retaguarda_usuario_id__in=usuarios_recebidos)
        )
        for operador in ausentes:
            operador.ativo = False
            operador.credencial_hash = ""
            operador.sincronizado_em = sincronizado_em
            operador.save(
                update_fields=[
                    "ativo",
                    "credencial_hash",
                    "sincronizado_em",
                    "atualizado_em",
                ]
            )
            sessoes_revogadas += encerrar_sessoes_ativas_operador(
                operador,
                MOTIVO_OPERADOR_INATIVO,
            )

        hub.operadores_versao = resposta["operadores_versao"]
        hub.operadores_gerado_em = dados["gerado_em"]
        hub.operadores_sincronizado_em = sincronizado_em
        hub.save(
            update_fields=[
                "operadores_versao",
                "operadores_gerado_em",
                "operadores_sincronizado_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_id"],
        "loja": dados["loja_id"],
        "versao": resposta["operadores_versao"],
        "operadores_recebidos": len(dados["operadores"]),
        "operadores_ativos": OperadorHub.objects.filter(hub=hub, ativo=True).count(),
        "operadores_inativados": OperadorHub.objects.filter(hub=hub, ativo=False).count(),
        "sessoes_revogadas": sessoes_revogadas,
    }


def _validar_operadores(hub, resposta):
    if not isinstance(resposta, dict):
        raise OperadoresValidationError("Resposta de operadores inválida.")

    _exigir_campos(
        resposta,
        ("operadores_versao", "gerado_em", "hub", "empresa", "loja", "operadores"),
    )
    if resposta["operadores_versao"] not in OPERADORES_VERSOES_SUPORTADAS:
        raise OperadoresValidationError("Versão de operadores não suportada.")

    gerado_em = parse_datetime(str(resposta["gerado_em"]))
    if gerado_em is None:
        raise OperadoresValidationError("Operadores retornou gerado_em inválido.")
    if timezone.is_naive(gerado_em):
        gerado_em = timezone.make_aware(gerado_em, timezone.get_current_timezone())

    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    operadores = resposta["operadores"]
    if (
        not isinstance(hub_payload, dict)
        or not isinstance(empresa_payload, dict)
        or not isinstance(loja_payload, dict)
    ):
        raise OperadoresValidationError("Resposta de operadores inválida.")
    if not isinstance(operadores, list):
        raise OperadoresValidationError("Resposta de operadores inválida: operadores deve ser lista.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade("loja_id", loja_payload["id"], hub.loja_id)

    operadores_validados = []
    usuarios = set()
    codigos = set()
    for item in operadores:
        validado = _validar_operador(item)
        if validado["retaguarda_usuario_id"] in usuarios:
            raise OperadoresValidationError("Operadores retornou usuario_id duplicado.")
        if validado["codigo"] in codigos:
            raise OperadoresValidationError("Operadores retornou codigo duplicado.")
        usuarios.add(validado["retaguarda_usuario_id"])
        codigos.add(validado["codigo"])
        operadores_validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_id": empresa_payload["id"],
        "loja_id": loja_payload["id"],
        "operadores": operadores_validados,
    }


def _validar_operador(item):
    if not isinstance(item, dict):
        raise OperadoresValidationError("Operadores retornou item inválido.")
    _exigir_campos(
        item,
        ("usuario_id", "codigo", "nome", "tipo", "credencial_hash", "ativo"),
    )
    if not isinstance(item["ativo"], bool):
        raise OperadoresValidationError("Operadores retornou ativo inválido.")

    perfil = _validar_perfil(item.get("perfil"))
    credencial_hash = str(item["credencial_hash"])
    if len(credencial_hash) > 128:
        raise OperadoresValidationError("Operadores retornou credencial inválida.")
    try:
        identify_hasher(credencial_hash)
    except ValueError as exc:
        raise OperadoresValidationError("Operadores retornou credencial inválida.") from exc

    return {
        "retaguarda_usuario_id": _inteiro_positivo(item["usuario_id"], "usuario_id"),
        "codigo": _texto_obrigatorio(item["codigo"], "codigo", max_length=30),
        "nome": _texto_obrigatorio(item["nome"], "nome", max_length=150),
        "tipo": str(item["tipo"])[:30],
        "perfil_retaguarda_id": perfil["id"] if perfil else None,
        "perfil_nome": perfil["nome"] if perfil else "",
        "credencial_hash": credencial_hash,
        "ativo": item["ativo"],
    }


def _validar_perfil(perfil):
    if perfil is None:
        return None
    if not isinstance(perfil, dict):
        raise OperadoresValidationError("Operadores retornou perfil inválido.")
    _exigir_campos(perfil, ("id", "nome"))
    return {
        "id": _inteiro_positivo(perfil["id"], "perfil.id"),
        "nome": _texto_obrigatorio(perfil["nome"], "perfil.nome", max_length=150),
    }


def _texto_obrigatorio(valor, campo, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise OperadoresValidationError(f"Operadores retornou {campo} inválido.")
    texto = valor.strip()
    if len(texto) > max_length:
        raise OperadoresValidationError(f"Operadores retornou {campo} inválido.")
    return texto


def _inteiro_positivo(valor, campo):
    if not isinstance(valor, int) or valor <= 0:
        raise OperadoresValidationError(f"Operadores retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos):
    faltando = [campo for campo in campos if payload.get(campo) in (None, "")]
    if faltando:
        raise OperadoresValidationError("Resposta de operadores incompleta.")


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise OperadoresValidationError(
            f"Operadores retornou {campo} diferente da configuração local."
        )
