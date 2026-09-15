import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.models import ClienteHub


class ClienteError(Exception):
    """Erro de dominio controlado para cadastro local de clientes."""


class ClienteValidationError(ClienteError):
    pass


class ClienteConflictError(ClienteError):
    def __init__(self, mensagem, cliente=None):
        super().__init__(mensagem)
        self.cliente = cliente


TIPOS_PESSOA = {"PF", "PJ"}
DOCUMENTO_CLIENTE_PADRAO = "00000000000"
CPF_PESOS = ((10, 9, 8, 7, 6, 5, 4, 3, 2), (11, 10, 9, 8, 7, 6, 5, 4, 3, 2))
CNPJ_PESOS = ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
PROTEGIDOS = {
    "hub",
    "hub_id",
    "empresa",
    "empresa_id",
    "loja",
    "loja_id",
    "retaguarda_id",
    "origem",
    "presente_retaguarda",
    "cliente_padrao",
    "bloqueio",
    "motivo_bloqueio",
    "ativo",
    "sincronizado_em",
    "cliente_uuid",
    "clientes_versao",
    "clientes_sincronizado_em",
    "clientes_gerado_em",
    "mala_direta",
    "aceita_email",
    "aceita_whatsapp",
    "aceita_sms",
    "consentimento_em",
    "origem_consentimento",
}


def cadastrar_cliente_local(
    terminal,
    operador,
    sessao_operador,
    *,
    tipo_pessoa,
    documento,
    nome_cliente,
    apelido="",
    telefone1="",
    telefone2="",
    email="",
    aniversario=None,
    endereco="",
    numero="",
    complemento="",
    cep="",
    bairro="",
    cidade="",
    estado="",
):
    del operador, sessao_operador
    tipo_pessoa = validar_tipo_pessoa(tipo_pessoa)
    documento = validar_documento(tipo_pessoa, documento)
    nome_cliente = texto_obrigatorio(nome_cliente, "Informe o nome do cliente.", max_length=150)
    dados = {
        "apelido": texto_opcional(apelido, "Apelido inválido.", max_length=100),
        "telefone1": telefone(telefone1),
        "telefone2": telefone(telefone2),
        "email": email_normalizado(email),
        "aniversario": data_aniversario(aniversario),
        "endereco": texto_opcional(endereco, "Endereço inválido.", max_length=150),
        "numero": texto_opcional(numero, "Número inválido.", max_length=20),
        "complemento": texto_opcional(complemento, "Complemento inválido.", max_length=100),
        "cep": cep_normalizado(cep),
        "bairro": texto_opcional(bairro, "Bairro inválido.", max_length=100),
        "cidade": texto_opcional(cidade, "Cidade inválida.", max_length=100),
        "estado": uf(estado),
    }

    with transaction.atomic():
        existente = (
            ClienteHub.objects.select_for_update()
            .filter(hub=terminal.hub, documento=documento)
            .first()
        )
        if existente:
            raise ClienteConflictError(mensagem_documento_duplicado(tipo_pessoa), existente)

        cliente = ClienteHub(
            hub=terminal.hub,
            retaguarda_id=None,
            origem=ClienteHub.ORIGEM_LOCAL,
            presente_retaguarda=False,
            tipo_pessoa=tipo_pessoa,
            documento=documento,
            cliente_padrao=False,
            nome_cliente=nome_cliente,
            bloqueio=False,
            motivo_bloqueio=None,
            ativo=True,
            mala_direta=False,
            aceita_email=False,
            aceita_whatsapp=False,
            aceita_sms=False,
            consentimento_em=None,
            origem_consentimento="",
            sincronizado_em=None,
            **dados,
        )
        try:
            with transaction.atomic():
                cliente.save()
        except IntegrityError as exc:
            existente = (
                ClienteHub.objects.select_for_update()
                .filter(hub=terminal.hub, documento=documento)
                .first()
            )
            if existente:
                raise ClienteConflictError(mensagem_documento_duplicado(tipo_pessoa), existente) from exc
            raise

    return cliente


def validar_payload_cadastro_cliente(payload):
    if not isinstance(payload, dict):
        raise ClienteValidationError("Dados do cliente inválidos.")
    protegidos = PROTEGIDOS.intersection(payload.keys())
    if protegidos:
        raise ClienteValidationError("Campos protegidos não podem ser enviados.")
    return {
        "tipo_pessoa": payload.get("tipo_pessoa"),
        "documento": payload.get("documento"),
        "nome_cliente": payload.get("nome_cliente"),
        "apelido": payload.get("apelido", ""),
        "telefone1": payload.get("telefone1", ""),
        "telefone2": payload.get("telefone2", ""),
        "email": payload.get("email", ""),
        "aniversario": payload.get("aniversario"),
        "endereco": payload.get("endereco", ""),
        "numero": payload.get("numero", ""),
        "complemento": payload.get("complemento", ""),
        "cep": payload.get("cep", ""),
        "bairro": payload.get("bairro", ""),
        "cidade": payload.get("cidade", ""),
        "estado": payload.get("estado", ""),
    }


def validar_tipo_pessoa(valor):
    if not isinstance(valor, str):
        raise ClienteValidationError("Tipo de pessoa inválido.")
    tipo_pessoa = valor.strip().upper()
    if tipo_pessoa not in TIPOS_PESSOA:
        raise ClienteValidationError("Tipo de pessoa inválido.")
    return tipo_pessoa


def validar_documento(tipo_pessoa, valor):
    if not isinstance(valor, str):
        raise ClienteValidationError("Informe o documento do cliente.")
    documento = re.sub(r"\D", "", valor)
    if not documento:
        raise ClienteValidationError("Informe o documento do cliente.")
    if documento == DOCUMENTO_CLIENTE_PADRAO:
        raise ClienteValidationError("Documento 00000000000 é reservado ao cliente padrão.")
    if tipo_pessoa == "PF":
        if len(documento) != 11 or not cpf_valido(documento):
            raise ClienteValidationError("CPF inválido.")
    elif len(documento) != 14 or not cnpj_valido(documento):
        raise ClienteValidationError("CNPJ inválido.")
    return documento


def cpf_valido(documento):
    if len(set(documento)) == 1:
        return False
    primeiro = digito_verificador(documento[:9], CPF_PESOS[0])
    segundo = digito_verificador(documento[:9] + str(primeiro), CPF_PESOS[1])
    return documento[-2:] == f"{primeiro}{segundo}"


def cnpj_valido(documento):
    if len(set(documento)) == 1:
        return False
    primeiro = digito_verificador(documento[:12], CNPJ_PESOS[0])
    segundo = digito_verificador(documento[:12] + str(primeiro), CNPJ_PESOS[1])
    return documento[-2:] == f"{primeiro}{segundo}"


def digito_verificador(base, pesos):
    soma = sum(int(digito) * peso for digito, peso in zip(base, pesos))
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def texto_obrigatorio(valor, mensagem, *, max_length):
    if not isinstance(valor, str) or not valor.strip():
        raise ClienteValidationError(mensagem)
    texto = valor.strip()
    if len(texto) > max_length:
        raise ClienteValidationError(mensagem)
    return texto


def texto_opcional(valor, mensagem, *, max_length):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise ClienteValidationError(mensagem)
    texto = valor.strip()
    if len(texto) > max_length:
        raise ClienteValidationError(mensagem)
    return texto


def email_normalizado(valor):
    email = texto_opcional(valor, "E-mail inválido.", max_length=254).lower()
    if not email:
        return ""
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ClienteValidationError("E-mail inválido.") from exc
    return email


def telefone(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise ClienteValidationError("Telefone inválido.")
    digitos = re.sub(r"\D", "", valor)
    if not digitos:
        return ""
    if len(digitos) < 8 or len(digitos) > 11:
        raise ClienteValidationError("Telefone inválido.")
    return digitos


def cep_normalizado(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise ClienteValidationError("CEP inválido.")
    digitos = re.sub(r"\D", "", valor)
    if not digitos:
        return ""
    if len(digitos) != 8:
        raise ClienteValidationError("CEP inválido.")
    return digitos


def uf(valor):
    estado = texto_opcional(valor, "Informe a UF com duas letras.", max_length=2).upper()
    if estado and not re.fullmatch(r"[A-Z]{2}", estado):
        raise ClienteValidationError("Informe a UF com duas letras.")
    return estado


def data_aniversario(valor):
    if valor in (None, ""):
        return None
    if not isinstance(valor, str):
        raise ClienteValidationError("Aniversário inválido.")
    data = parse_date(valor)
    if data is None:
        raise ClienteValidationError("Aniversário inválido.")
    if data > timezone.localdate():
        raise ClienteValidationError("Aniversário não pode ser futuro.")
    return data


def mensagem_documento_duplicado(tipo_pessoa):
    return "Já existe um cliente com este CPF." if tipo_pessoa == "PF" else "Já existe um cliente com este CNPJ."
