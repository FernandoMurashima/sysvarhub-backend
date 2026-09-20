import re

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.models import ClienteHub, ValeTrocaHub


CLIENTES_VERSOES_SUPORTADAS = {1}
TIPOS_PESSOA = {"PF", "PJ"}


class ClientesValidationError(Exception):
    """Erro controlado na validação de clientes da retaguarda."""


def sincronizar_clientes(hub, resposta):
    dados = _validar_clientes(hub, resposta)
    sincronizado_em = timezone.now()

    with transaction.atomic():
        ids_recebidos = set()
        clientes_ativos = 0
        clientes_inativos = 0
        clientes_bloqueados = 0
        reconciliados = 0

        for cliente_payload in dados["clientes"]:
            vales_troca = cliente_payload.pop("vales_troca", [])
            retaguarda_id = cliente_payload["retaguarda_id"]
            ids_recebidos.add(retaguarda_id)
            cliente = (
                ClienteHub.objects.select_for_update()
                .filter(hub=hub, retaguarda_id=retaguarda_id)
                .first()
            )
            if cliente is None and cliente_payload["documento"]:
                cliente = (
                    ClienteHub.objects.select_for_update()
                    .filter(hub=hub, documento=cliente_payload["documento"])
                    .first()
                )
                if cliente is not None and cliente.retaguarda_id is None:
                    reconciliados += 1
            if cliente is None:
                cliente = ClienteHub(hub=hub)

            origem = cliente.origem or ClienteHub.ORIGEM_RETAGUARDA
            if cliente.pk is None:
                origem = ClienteHub.ORIGEM_RETAGUARDA

            for campo, valor in cliente_payload.items():
                setattr(cliente, campo, valor)
            cliente.origem = origem
            cliente.presente_retaguarda = True
            cliente.sincronizado_em = sincronizado_em
            cliente.save()
            _sincronizar_vales_cliente(hub, cliente, vales_troca, sincronizado_em)

            if cliente.ativo:
                clientes_ativos += 1
            else:
                clientes_inativos += 1
            if cliente.bloqueio:
                clientes_bloqueados += 1

        ausentes = (
            ClienteHub.objects.select_for_update()
            .filter(hub=hub, presente_retaguarda=True, retaguarda_id__isnull=False)
            .exclude(retaguarda_id__in=ids_recebidos)
            .update(presente_retaguarda=False, sincronizado_em=sincronizado_em)
        )

        hub.clientes_versao = resposta["clientes_versao"]
        hub.clientes_gerado_em = dados["gerado_em"]
        hub.clientes_sincronizado_em = sincronizado_em
        hub.save(
            update_fields=[
                "clientes_versao",
                "clientes_gerado_em",
                "clientes_sincronizado_em",
                "atualizado_em",
            ]
        )

    return {
        "empresa": dados["empresa_id"],
        "loja": dados["loja_id"],
        "versao": resposta["clientes_versao"],
        "clientes_recebidos": len(dados["clientes"]),
        "clientes_ativos": clientes_ativos,
        "clientes_inativos": clientes_inativos,
        "clientes_bloqueados": clientes_bloqueados,
        "clientes_ausentes_marcados": ausentes,
        "clientes_reconciliados_por_documento": reconciliados,
    }


def _validar_clientes(hub, resposta):
    if not isinstance(resposta, dict):
        raise ClientesValidationError("Resposta de clientes inválida.")
    _exigir_campos(resposta, ("clientes_versao", "gerado_em", "hub", "empresa", "loja", "clientes"))
    if resposta["clientes_versao"] not in CLIENTES_VERSOES_SUPORTADAS:
        raise ClientesValidationError("Versão de clientes não suportada.")

    gerado_em = _datetime(resposta["gerado_em"], "gerado_em")
    hub_payload = resposta["hub"]
    empresa_payload = resposta["empresa"]
    loja_payload = resposta["loja"]
    clientes = resposta["clientes"]
    if not all(isinstance(payload, dict) for payload in (hub_payload, empresa_payload, loja_payload)):
        raise ClientesValidationError("Resposta de clientes inválida.")
    if not isinstance(clientes, list):
        raise ClientesValidationError("Resposta de clientes inválida: clientes deve ser lista.")

    _exigir_campos(hub_payload, ("id", "hub_uuid"))
    _exigir_campos(empresa_payload, ("id",))
    _exigir_campos(loja_payload, ("id",))
    _validar_identidade_inteira("hub_id", hub_payload["id"], hub.retaguarda_hub_id)
    _validar_identidade("hub_uuid", str(hub_payload["hub_uuid"]), str(hub.hub_uuid))
    _validar_identidade_inteira("empresa_id", empresa_payload["id"], hub.empresa_id)
    _validar_identidade_inteira("loja_id", loja_payload["id"], hub.loja_id)

    validados = []
    ids = set()
    documentos = set()
    for item in clientes:
        validado = _validar_cliente(item)
        if validado["retaguarda_id"] in ids:
            raise ClientesValidationError("Clientes retornou id duplicado.")
        documento = validado["documento"]
        if documento and documento in documentos:
            raise ClientesValidationError("Clientes retornou documento duplicado.")
        ids.add(validado["retaguarda_id"])
        if documento:
            documentos.add(documento)
        validados.append(validado)

    return {
        "gerado_em": gerado_em,
        "empresa_id": empresa_payload["id"],
        "loja_id": loja_payload["id"],
        "clientes": validados,
    }


def _validar_cliente(item):
    if not isinstance(item, dict):
        raise ClientesValidationError("Clientes retornou item inválido.")
    _exigir_campos(
        item,
        (
            "id",
            "tipo_pessoa",
            "documento",
            "cliente_padrao",
            "nome_cliente",
            "apelido",
            "endereco",
            "numero",
            "complemento",
            "cep",
            "bairro",
            "cidade",
            "estado",
            "telefone1",
            "telefone2",
            "email",
            "categoria",
            "bloqueio",
            "motivo_bloqueio",
            "aniversario",
            "mala_direta",
            "aceita_email",
            "aceita_whatsapp",
            "aceita_sms",
            "consentimento_em",
            "origem_consentimento",
            "ativo",
        ),
        permitir_nulos={
            "documento",
            "motivo_bloqueio",
            "aniversario",
            "consentimento_em",
            "apelido",
            "endereco",
            "numero",
            "complemento",
            "cep",
            "bairro",
            "cidade",
            "estado",
            "telefone1",
            "telefone2",
            "email",
            "categoria",
            "origem_consentimento",
        },
        permitir_vazios={
            "apelido",
            "endereco",
            "numero",
            "complemento",
            "cep",
            "bairro",
            "cidade",
            "estado",
            "telefone1",
            "telefone2",
            "email",
            "categoria",
            "origem_consentimento",
        },
    )
    tipo_pessoa = _texto_obrigatorio(item["tipo_pessoa"], "tipo_pessoa", max_length=2)
    if tipo_pessoa not in TIPOS_PESSOA:
        raise ClientesValidationError("Clientes retornou tipo_pessoa inválido.")
    for campo in ("cliente_padrao", "bloqueio", "mala_direta", "aceita_email", "aceita_whatsapp", "aceita_sms", "ativo"):
        if not isinstance(item[campo], bool):
            raise ClientesValidationError(f"Clientes retornou {campo} inválido.")

    return {
        "retaguarda_id": _inteiro_positivo(item["id"], "id"),
        "tipo_pessoa": tipo_pessoa,
        "documento": _documento(item["documento"]),
        "cliente_padrao": item["cliente_padrao"],
        "nome_cliente": _texto_obrigatorio(item["nome_cliente"], "nome_cliente", max_length=150),
        "apelido": _texto_opcional(item["apelido"], "apelido", max_length=100) or "",
        "endereco": _texto_opcional(item["endereco"], "endereco", max_length=150) or "",
        "numero": _texto_opcional(item["numero"], "numero", max_length=20) or "",
        "complemento": _texto_opcional(item["complemento"], "complemento", max_length=100) or "",
        "cep": _texto_opcional(item["cep"], "cep", max_length=12) or "",
        "bairro": _texto_opcional(item["bairro"], "bairro", max_length=100) or "",
        "cidade": _texto_opcional(item["cidade"], "cidade", max_length=100) or "",
        "estado": _texto_opcional(item["estado"], "estado", max_length=2) or "",
        "telefone1": _texto_opcional(item["telefone1"], "telefone1", max_length=30) or "",
        "telefone2": _texto_opcional(item["telefone2"], "telefone2", max_length=30) or "",
        "email": _texto_opcional(item["email"], "email", max_length=254) or "",
        "categoria": _texto_opcional(item["categoria"], "categoria", max_length=80) or "",
        "bloqueio": item["bloqueio"],
        "motivo_bloqueio": _texto_opcional(item["motivo_bloqueio"], "motivo_bloqueio", max_length=200),
        "aniversario": _data(item["aniversario"], "aniversario"),
        "mala_direta": item["mala_direta"],
        "aceita_email": item["aceita_email"],
        "aceita_whatsapp": item["aceita_whatsapp"],
        "aceita_sms": item["aceita_sms"],
        "consentimento_em": _datetime_opcional(item["consentimento_em"], "consentimento_em"),
        "origem_consentimento": _texto_opcional(item["origem_consentimento"], "origem_consentimento", max_length=80) or "",
        "ativo": item["ativo"],
        "vales_troca": _validar_vales(item.get("vales_troca") or []),
    }


def _validar_vales(vales):
    if not isinstance(vales, list):
        raise ClientesValidationError("Clientes retornou vales_troca inválido.")
    validados = []
    for vale in vales:
        if not isinstance(vale, dict):
            raise ClientesValidationError("Clientes retornou vale_troca inválido.")
        validados.append(
            {
                "retaguarda_id": _inteiro_positivo(vale["id"], "vale.id"),
                "documento": _texto_obrigatorio(vale["documento"], "vale.documento", max_length=80),
                "valor_original": vale.get("valor_original") or vale.get("saldo") or 0,
                "saldo": vale.get("saldo") or 0,
                "validade": _data(vale.get("validade"), "vale.validade") if vale.get("validade") else None,
                "status": vale.get("status") or ValeTrocaHub.STATUS_ABERTO,
            }
        )
    return validados


def _sincronizar_vales_cliente(hub, cliente, vales, sincronizado_em):
    recebidos = set()
    for vale in vales:
        recebidos.add(vale["retaguarda_id"])
        ValeTrocaHub.objects.update_or_create(
            hub=hub,
            retaguarda_id=vale["retaguarda_id"],
            defaults={
                "cliente_uuid": cliente.cliente_uuid,
                "cliente_retaguarda_id": cliente.retaguarda_id,
                "documento": vale["documento"],
                "valor_original": vale["valor_original"],
                "saldo": vale["saldo"],
                "status": vale["status"],
                "validade": vale["validade"],
                "sincronizado_em": sincronizado_em,
            },
        )
    ValeTrocaHub.objects.filter(
        hub=hub,
        cliente_retaguarda_id=cliente.retaguarda_id,
        retaguarda_id__isnull=False,
        status=ValeTrocaHub.STATUS_ABERTO,
    ).exclude(retaguarda_id__in=recebidos).update(status=ValeTrocaHub.STATUS_CANCELADO, sincronizado_em=sincronizado_em)


def _documento(valor):
    texto = _texto_opcional(valor, "documento", max_length=20)
    if not texto:
        return None
    if not re.fullmatch(r"[\d.\-/]+", texto):
        raise ClientesValidationError("Clientes retornou documento inválido.")
    digitos = re.sub(r"\D", "", texto)
    if len(digitos) not in (11, 14):
        raise ClientesValidationError("Clientes retornou documento inválido.")
    return digitos


def _data(valor, campo):
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    data = parse_date(valor)
    if data is None:
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    return data


def _datetime_opcional(valor, campo):
    if valor is None:
        return None
    return _datetime(valor, campo)


def _datetime(valor, campo):
    if not isinstance(valor, str):
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    data = parse_datetime(valor)
    if data is None:
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    if timezone.is_naive(data):
        data = timezone.make_aware(data, timezone.get_current_timezone())
    return data


def _texto_obrigatorio(valor, campo, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_opcional(valor, campo, *, max_length):
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    return _texto_tamanho(valor.strip(), campo, max_length)


def _texto_tamanho(texto, campo, max_length):
    if len(texto) > max_length:
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    return texto


def _inteiro_positivo(valor, campo):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise ClientesValidationError(f"Clientes retornou {campo} inválido.")
    return valor


def _exigir_campos(payload, campos, *, permitir_nulos=None, permitir_vazios=None):
    permitir_nulos = permitir_nulos or set()
    permitir_vazios = permitir_vazios or set()
    for campo in campos:
        if campo not in payload:
            raise ClientesValidationError(
                f"Clientes retornou campo obrigatório ausente: {campo}."
            )
        elif campo not in permitir_nulos and payload.get(campo) is None:
            raise ClientesValidationError(
                f"Clientes retornou campo obrigatório nulo: {campo}."
            )
        elif campo not in permitir_vazios and payload.get(campo) == "":
            raise ClientesValidationError(
                f"Clientes retornou campo obrigatório vazio: {campo}."
            )


def _validar_identidade(campo, recebido, esperado):
    if recebido != esperado:
        raise ClientesValidationError(f"Clientes retornou {campo} diferente da configuração local.")


def _validar_identidade_inteira(campo, recebido, esperado):
    if isinstance(recebido, bool) or not isinstance(recebido, int) or recebido != esperado:
        raise ClientesValidationError(f"Clientes retornou {campo} diferente da configuração local.")
