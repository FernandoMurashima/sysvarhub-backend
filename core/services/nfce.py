import base64
import hashlib
import random
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from xml.etree import ElementTree as ET

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    ConfiguracaoFiscalHub,
    FormaPagamentoFiscalMapHub,
    NFCeHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
)


NFE_NS = "http://www.portalfiscal.inf.br/nfe"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
CANONICALIZATION_ALGORITHM = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
SIGNATURE_ALGORITHM = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
DIGEST_ALGORITHM = "http://www.w3.org/2000/09/xmldsig#sha1"
ENVELOPED_SIGNATURE_ALGORITHM = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
ET.register_namespace("", NFE_NS)
ET.register_namespace("ds", DS_NS)

UF_CODIGOS = {
    "RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17",
    "MA": "21", "PI": "22", "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27",
    "SE": "28", "BA": "29", "MG": "31", "ES": "32", "RJ": "33", "SP": "35", "PR": "41",
    "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53",
}


class NFCeErroDominio(Exception):
    def __init__(self, codigo, mensagem=None):
        self.codigo = codigo
        super().__init__(mensagem or codigo)


class NFCeConfiguracaoErro(NFCeErroDominio):
    pass


@dataclass(frozen=True)
class MaterialAssinatura:
    private_key_pem: bytes
    certificate_pem: bytes


@dataclass(frozen=True)
class ConfigQRCodeDesenvolvimento:
    url_qrcode: str
    url_chave: str
    versao: int = 3
    csc_id: str = ""
    csc: str = ""


@dataclass(frozen=True)
class ResultadoSefazNFCe:
    status: str
    mensagem: str = ""
    simulacao: bool = False


class SefazNFCeClient:
    AUTORIZADA = "AUTORIZADA"
    REJEITADA = "REJEITADA"
    INDISPONIVEL = "INDISPONIVEL"
    TIMEOUT = "TIMEOUT"

    def transmitir(self, nfce):
        raise NotImplementedError


class SefazNFCeClientDesenvolvimento(SefazNFCeClient):
    def __init__(self, resultado=None):
        self.resultado = resultado or SefazNFCeClient.AUTORIZADA

    def transmitir(self, nfce):
        mensagens = {
            self.AUTORIZADA: "SIMULACAO_SEFAZ_AUTORIZADA_SEM_VALOR_FISCAL",
            self.REJEITADA: "SIMULACAO_SEFAZ_REJEITADA_SEM_VALOR_FISCAL",
            self.INDISPONIVEL: "SIMULACAO_SEFAZ_INDISPONIVEL",
            self.TIMEOUT: "SIMULACAO_SEFAZ_TIMEOUT",
        }
        return ResultadoSefazNFCe(
            status=self.resultado,
            mensagem=mensagens.get(self.resultado, "SIMULACAO_SEFAZ_RESULTADO_DESCONHECIDO"),
            simulacao=True,
        )


class NFCeMaterialProvider:
    def obter(self):
        raise NotImplementedError


class NFCeMaterialProviderDesenvolvimento(NFCeMaterialProvider):
    def obter(self):
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError as exc:
            raise NFCeConfiguracaoErro("MATERIAL_ASSINATURA_INDISPONIVEL") from exc

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sysvar Hub Desenvolvimento Sem Validade Fiscal")])
        agora = timezone.now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - timezone.timedelta(days=1))
            .not_valid_after(agora + timezone.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        return MaterialAssinatura(
            private_key_pem=key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM),
        )


def emitir_nfce_para_venda_finalizada(venda, *, material_provider=None, sefaz_client=None, qr_config=None):
    material = (material_provider or obter_material_provider_configurado()).obter()
    client = sefaz_client or obter_sefaz_client_configurado()
    qr = qr_config or obter_qr_config_configurado()
    nfce = gerar_nfce_local(venda, material_assinatura=material, qr_config=qr, tipo_emissao="1")
    resultado = client.transmitir(nfce)
    if resultado.status in {SefazNFCeClient.INDISPONIVEL, SefazNFCeClient.TIMEOUT}:
        config = _obter_config_bloqueada(venda.hub)
        nfce = regenerar_nfce_em_contingencia(
            nfce,
            config,
            material,
            qr,
            resultado.mensagem or "SEFAZ indisponivel no ambiente local.",
        )
        return nfce, ResultadoSefazNFCe(status=resultado.status, mensagem=resultado.mensagem, simulacao=resultado.simulacao)
    if resultado.status == SefazNFCeClient.REJEITADA:
        nfce.status = NFCeHub.STATUS_REJEITADA
        nfce.mensagem_retorno = resultado.mensagem
        nfce.save(update_fields=["status", "mensagem_retorno", "atualizado_em"])
        raise NFCeErroDominio("NFCE_REJEITADA", resultado.mensagem or "NFCE_REJEITADA")
    nfce.mensagem_retorno = resultado.mensagem
    nfce.save(update_fields=["mensagem_retorno", "atualizado_em"])
    return nfce, resultado


def regenerar_nfce_em_contingencia(nfce, config, material_assinatura, qr_config, justificativa):
    entrada = timezone.now()
    chave, dv = gerar_chave_acesso(
        uf=config.uf,
        emissao=nfce.emitida_em,
        cnpj=config.cnpj,
        serie=nfce.serie,
        numero=nfce.numero,
        tipo_emissao="9",
        codigo_numerico=nfce.codigo_numerico,
    )
    nfce.tipo_emissao = "9"
    nfce.chave_acesso = chave
    nfce.digito_verificador = dv
    nfce.entrada_contingencia_em = entrada
    nfce.justificativa_contingencia = justificativa[:255]
    nfce.status = NFCeHub.STATUS_GERANDO
    nfce.save(update_fields=[
        "tipo_emissao",
        "chave_acesso",
        "digito_verificador",
        "entrada_contingencia_em",
        "justificativa_contingencia",
        "status",
        "atualizado_em",
    ])
    qr_payload = gerar_qr_code_payload(nfce, config, qr_config, material_assinatura=material_assinatura)
    xml = montar_xml_nfce(nfce, config, qr_payload, qr_config.url_chave)
    xml_assinado = assinar_xml_nfce(xml, material_assinatura)
    nfce.xml_sem_assinatura = xml
    nfce.xml_assinado = xml_assinado
    nfce.qr_code_payload = qr_payload
    nfce.status = NFCeHub.STATUS_CONTINGENCIA
    nfce.mensagem_retorno = justificativa[:255]
    nfce.save(update_fields=[
        "xml_sem_assinatura",
        "xml_assinado",
        "qr_code_payload",
        "status",
        "mensagem_retorno",
        "atualizado_em",
    ])
    return nfce


def obter_material_provider_configurado():
    modo = (getattr(settings, "SYSVARHUB_NFCE_MATERIAL_MODE", "") or "").upper()
    if modo == "DESENVOLVIMENTO":
        if getattr(settings, "SYSVARHUB_NFCE_SEFAZ_CLIENT", "").upper() != "DESENVOLVIMENTO":
            raise NFCeConfiguracaoErro("MATERIAL_DESENVOLVIMENTO_REQUER_SEFAZ_DESENVOLVIMENTO")
        return NFCeMaterialProviderDesenvolvimento()
    raise NFCeConfiguracaoErro("MATERIAL_ASSINATURA_NFCE_NAO_CONFIGURADO")


def obter_sefaz_client_configurado():
    modo = (getattr(settings, "SYSVARHUB_NFCE_SEFAZ_CLIENT", "") or "").upper()
    if modo == "DESENVOLVIMENTO":
        resultado = (getattr(settings, "SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO", "") or "AUTORIZADA").upper()
        return SefazNFCeClientDesenvolvimento(resultado)
    raise NFCeConfiguracaoErro("CLIENTE_SEFAZ_NFCE_NAO_CONFIGURADO")


def obter_qr_config_configurado():
    url_qrcode = getattr(settings, "SYSVARHUB_NFCE_QRCODE_URL", "") or ""
    url_chave = getattr(settings, "SYSVARHUB_NFCE_URL_CHAVE", "") or ""
    if not url_qrcode or not url_chave:
        raise NFCeConfiguracaoErro("QR_CODE_NFCE_NAO_CONFIGURADO")
    return ConfigQRCodeDesenvolvimento(url_qrcode=url_qrcode, url_chave=url_chave)


def validar_nfce_para_finalizacao(venda):
    config = _obter_config_bloqueada(venda.hub)
    _validar_conteudo_nfce(venda, config)
    _validar_material_desenvolvimento_fora_de_producao(config)
    obter_material_provider_configurado()
    obter_sefaz_client_configurado()
    obter_qr_config_configurado()
    return config


def _validar_material_desenvolvimento_fora_de_producao(config):
    modo = (getattr(settings, "SYSVARHUB_NFCE_MATERIAL_MODE", "") or "").upper()
    if config.ambiente_fiscal == "PRODUCAO" and modo == "DESENVOLVIMENTO":
        raise NFCeConfiguracaoErro("MATERIAL_DESENVOLVIMENTO_NAO_PERMITIDO_EM_PRODUCAO")


def gerar_nfce_local(venda, *, material_assinatura, qr_config, codigo_numerico=None, tipo_emissao="1", justificativa_contingencia=""):
    erro_para_relancar = None
    resultado = None
    with transaction.atomic():
        venda = (
            VendaHub.objects.select_for_update()
            .select_related("hub")
            .get(pk=venda.pk)
        )
        existente = NFCeHub.objects.filter(venda=venda).first()
        if existente:
            return existente
        _validar_venda_finalizada(venda)
        config = _obter_config_bloqueada(venda.hub)
        serie, numero = _reservar_numero(config)
        c_nf = codigo_numerico or gerar_codigo_numerico()
        emitida_em = timezone.now()
        chave, dv = gerar_chave_acesso(
            uf=config.uf,
            emissao=emitida_em,
            cnpj=config.cnpj,
            serie=serie,
            numero=numero,
            tipo_emissao=tipo_emissao,
            codigo_numerico=c_nf,
        )
        nfce = NFCeHub.objects.create(
            hub=venda.hub,
            venda=venda,
            ambiente=config.ambiente_fiscal,
            serie=serie,
            numero=numero,
            codigo_numerico=c_nf,
            digito_verificador=dv,
            chave_acesso=chave,
            tipo_emissao=tipo_emissao,
            status=NFCeHub.STATUS_GERANDO,
            emitida_em=emitida_em,
            entrada_contingencia_em=emitida_em if tipo_emissao == "9" else None,
            justificativa_contingencia=justificativa_contingencia if tipo_emissao == "9" else "",
        )

        try:
            qr_payload = gerar_qr_code_payload(nfce, config, qr_config, material_assinatura=material_assinatura)
            xml = montar_xml_nfce(nfce, config, qr_payload, qr_config.url_chave)
            xml_assinado = assinar_xml_nfce(xml, material_assinatura)
        except NFCeErroDominio as exc:
            nfce.status = NFCeHub.STATUS_ERRO_GERACAO
            nfce.mensagem_retorno = exc.codigo
            nfce.save(update_fields=["status", "mensagem_retorno", "atualizado_em"])
            erro_para_relancar = exc
        except Exception as exc:
            nfce.status = NFCeHub.STATUS_ERRO_GERACAO
            nfce.mensagem_retorno = "DADOS_FISCAIS_INSUFICIENTES"
            nfce.save(update_fields=["status", "mensagem_retorno", "atualizado_em"])
            erro_para_relancar = NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
            erro_para_relancar.__cause__ = exc

        if erro_para_relancar:
            resultado = nfce
        else:
            nfce.xml_sem_assinatura = xml
            nfce.xml_assinado = xml_assinado
            nfce.qr_code_payload = qr_payload
            nfce.status = NFCeHub.STATUS_CONTINGENCIA if tipo_emissao == "9" else NFCeHub.STATUS_GERADA
            nfce.save(update_fields=["xml_sem_assinatura", "xml_assinado", "qr_code_payload", "status", "atualizado_em"])
            resultado = nfce
    if erro_para_relancar:
        raise erro_para_relancar
    return resultado


def _validar_venda_finalizada(venda):
    if venda.status != VendaHub.STATUS_FINALIZADA:
        raise NFCeErroDominio("VENDA_NAO_FINALIZADA")


def nfce_habilitada_para_hub(hub):
    return ConfiguracaoFiscalHub.objects.filter(hub=hub, emite_nfce=True).exists()


def _obter_config_bloqueada(hub):
    config = ConfiguracaoFiscalHub.objects.select_for_update().filter(hub=hub).first()
    if not config:
        raise NFCeErroDominio("CONFIG_FISCAL_AUSENTE")
    if not config.emite_nfce:
        raise NFCeErroDominio("NFCE_DESABILITADA")
    _validar_config_basica(config)
    return config


def _validar_config_basica(config):
    cnpj = somente_digitos(config.cnpj)
    if len(cnpj) != 14:
        raise NFCeErroDominio("CNPJ_INVALIDO")
    if not (config.inscricao_estadual or "").strip():
        raise NFCeErroDominio("IE_AUSENTE")
    if not (config.codigo_municipio_ibge or "").strip():
        raise NFCeErroDominio("MUNICIPIO_IBGE_AUSENTE")
    if int(config.serie_nfce or 0) <= 0:
        raise NFCeErroDominio("SERIE_INVALIDA")
    if config.uf not in UF_CODIGOS:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")


def _reservar_numero(config):
    serie = config.serie_nfce
    numero = config.proximo_numero_nfce
    config.proximo_numero_nfce = numero + 1
    config.save(update_fields=["proximo_numero_nfce", "atualizado_em"])
    return serie, numero


def gerar_codigo_numerico():
    return f"{random.SystemRandom().randint(0, 99999999):08d}"


def gerar_chave_acesso(*, uf, emissao, cnpj, serie, numero, tipo_emissao, codigo_numerico):
    if uf not in UF_CODIGOS:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    cnpj_digits = somente_digitos(cnpj)
    if len(cnpj_digits) != 14:
        raise NFCeErroDominio("CNPJ_INVALIDO")
    if int(serie or 0) <= 0 or int(numero or 0) <= 0:
        raise NFCeErroDominio("SERIE_INVALIDA")
    c_nf = somente_digitos(codigo_numerico)
    if len(c_nf) != 8:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    base = (
        UF_CODIGOS[uf]
        + emissao.strftime("%y%m")
        + cnpj_digits
        + "65"
        + f"{int(serie):03d}"
        + f"{int(numero):09d}"
        + str(tipo_emissao)
        + c_nf
    )
    dv = calcular_dv_chave(base)
    return base + dv, dv


def calcular_dv_chave(base43):
    if not re.fullmatch(r"\d{43}", base43):
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    pesos = list(range(2, 10))
    soma = 0
    for indice, digito in enumerate(reversed(base43)):
        soma += int(digito) * pesos[indice % len(pesos)]
    resto = soma % 11
    dv = 11 - resto
    return "0" if dv >= 10 else str(dv)


def montar_xml_nfce(nfce, config, qr_code_payload, url_chave):
    venda = (
        VendaHub.objects
        .prefetch_related("itens", "pagamentos")
        .get(pk=nfce.venda_id)
    )
    itens = list(venda.itens.all().order_by("id"))
    pagamentos = list(venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).order_by("id"))
    if not itens:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    if not pagamentos:
        raise NFCeErroDominio("PAGAMENTO_SEM_TPAG")
    if Decimal(venda.desconto_geral or 0) != Decimal("0"):
        raise NFCeErroDominio("DESCONTO_GERAL_FISCAL_NAO_SUPORTADO")

    root = ET.Element(q("NFe"))
    inf = ET.SubElement(root, q("infNFe"), {"versao": "4.00", "Id": f"NFe{nfce.chave_acesso}"})
    _montar_ide(inf, nfce, config)
    _montar_emit(inf, config)
    _montar_dest(inf, venda)
    totais = _totais_zerados()
    for indice, item in enumerate(itens, start=1):
        item_totais = _montar_det(inf, item, indice, config)
        for chave, valor in item_totais.items():
            totais[chave] += valor
    _montar_total(inf, venda, totais)
    ET.SubElement(inf, q("transp")).append(_el("modFrete", "9"))
    _montar_pag(inf, pagamentos, nfce.hub, venda)
    supl = ET.SubElement(root, q("infNFeSupl"))
    supl.append(_el("qrCode", qr_code_payload))
    supl.append(_el("urlChave", url_chave))
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def _validar_conteudo_nfce(venda, config):
    itens = list(venda.itens.all().order_by("id"))
    pagamentos = list(venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).order_by("id"))
    if not itens:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    if not pagamentos:
        raise NFCeErroDominio("PAGAMENTO_SEM_TPAG")
    if Decimal(venda.desconto_geral or 0) != Decimal("0"):
        raise NFCeErroDominio("DESCONTO_GERAL_FISCAL_NAO_SUPORTADO")
    for item in itens:
        fiscal = item.fiscal or {}
        _validar_fiscal_item(fiscal)
        v_prod = money(Decimal(item.quantidade) * Decimal(item.preco_unitario))
        v_bc = money(v_prod - money(item.desconto))
        imposto = ET.Element(q("imposto"))
        _montar_icms(imposto, fiscal, config, v_bc)
        _montar_pis(imposto, fiscal, v_bc)
        _montar_cofins(imposto, fiscal, v_bc)
    for pagamento in pagamentos:
        mapas = list(
            FormaPagamentoFiscalMapHub.objects.filter(
                hub=venda.hub,
                forma_pagamento_retaguarda_id=pagamento.retaguarda_forma_pagamento_id,
            ).order_by("codigo_tpag")
        )
        if not mapas:
            raise NFCeErroDominio("PAGAMENTO_SEM_TPAG")
        if len(mapas) > 1:
            raise NFCeErroDominio("PAGAMENTO_TPAG_AMBIGUO")


def _montar_ide(inf, nfce, config):
    ide = ET.SubElement(inf, q("ide"))
    for tag, valor in (
        ("cUF", UF_CODIGOS[config.uf]),
        ("cNF", nfce.codigo_numerico),
        ("natOp", "VENDA"),
        ("mod", "65"),
        ("serie", str(nfce.serie)),
        ("nNF", str(nfce.numero)),
        ("dhEmi", nfce.emitida_em.isoformat()),
        ("tpNF", "1"),
        ("idDest", "1"),
        ("cMunFG", config.codigo_municipio_ibge),
        ("tpImp", "4"),
        ("tpEmis", nfce.tipo_emissao),
        ("cDV", nfce.digito_verificador),
        ("tpAmb", _tp_amb(config.ambiente_fiscal)),
        ("finNFe", "1"),
        ("indFinal", "1"),
        ("indPres", "1"),
        ("procEmi", "0"),
        ("verProc", "SysvarHub-1"),
    ):
        ide.append(_el(tag, valor))
    if nfce.tipo_emissao == "9":
        ide.append(_el("dhCont", nfce.entrada_contingencia_em.isoformat()))
        ide.append(_el("xJust", nfce.justificativa_contingencia))


def _montar_emit(inf, config):
    emit = ET.SubElement(inf, q("emit"))
    emit.append(_el("CNPJ", somente_digitos(config.cnpj)))
    emit.append(_el("xNome", config.razao_social))
    emit.append(_el("xFant", config.nome_fantasia or config.razao_social))
    ender = ET.SubElement(emit, q("enderEmit"))
    for tag, valor in (
        ("xLgr", config.endereco or config.logradouro),
        ("nro", config.numero or "S/N"),
        ("xCpl", config.complemento),
        ("xBairro", config.bairro),
        ("cMun", config.codigo_municipio_ibge),
        ("xMun", config.cidade),
        ("UF", config.uf),
        ("CEP", somente_digitos(config.cep)),
        ("cPais", "1058"),
        ("xPais", "BRASIL"),
    ):
        if valor not in (None, ""):
            ender.append(_el(tag, valor))
    emit.append(_el("IE", config.inscricao_estadual))
    emit.append(_el("CRT", "1" if config.regime_tributario == "SIMPLES" else "3"))


def _montar_dest(inf, venda):
    documento = somente_digitos(venda.cliente_documento or "")
    if not documento or venda.cliente_padrao:
        return
    dest = ET.SubElement(inf, q("dest"))
    dest.append(_el("CPF" if len(documento) == 11 else "CNPJ", documento))
    dest.append(_el("xNome", venda.cliente_nome or "CONSUMIDOR"))
    dest.append(_el("indIEDest", "9"))


def _montar_det(inf, item, indice, config):
    fiscal = item.fiscal or {}
    _validar_fiscal_item(fiscal)
    v_prod = money(Decimal(item.quantidade) * Decimal(item.preco_unitario))
    v_desc = money(item.desconto)
    v_bc = money(v_prod - v_desc)
    det = ET.SubElement(inf, q("det"), {"nItem": str(indice)})
    prod = ET.SubElement(det, q("prod"))
    cfop = fiscal.get("cfop_venda_dentro") or fiscal.get("cfop_venda_fora")
    for tag, valor in (
        ("cProd", str(item.retaguarda_sku_id)),
        ("cEAN", item.ean13 or "SEM GTIN"),
        ("xProd", item.descricao),
        ("NCM", fiscal.get("ncm")),
        ("CFOP", cfop),
        ("uCom", item.unidade_codigo or "UN"),
        ("qCom", dec(item.quantidade, 4)),
        ("vUnCom", dec(item.preco_unitario, 10)),
        ("vProd", dec(v_prod, 2)),
        ("cEANTrib", item.ean13 or "SEM GTIN"),
        ("uTrib", item.unidade_codigo or "UN"),
        ("qTrib", dec(item.quantidade, 4)),
        ("vUnTrib", dec(item.preco_unitario, 10)),
    ):
        prod.append(_el(tag, valor))
    if v_desc > Decimal("0"):
        prod.append(_el("vDesc", dec(v_desc, 2)))
    prod.append(_el("indTot", "1"))
    imposto = ET.SubElement(det, q("imposto"))
    v_icms, v_bc_icms = _montar_icms(imposto, fiscal, config, v_bc)
    v_pis = _montar_pis(imposto, fiscal, v_bc)
    v_cofins = _montar_cofins(imposto, fiscal, v_bc)
    return {
        "vBC": v_bc_icms,
        "vICMS": v_icms,
        "vProd": v_prod,
        "vDesc": v_desc,
        "vPIS": v_pis,
        "vCOFINS": v_cofins,
    }


def _validar_fiscal_item(fiscal):
    if not fiscal.get("ncm"):
        raise NFCeErroDominio("PRODUTO_SEM_NCM")
    if not (fiscal.get("cfop_venda_dentro") or fiscal.get("cfop_venda_fora")):
        raise NFCeErroDominio("PRODUTO_SEM_CFOP")
    for campo in ("csosn_ou_cst_icms", "cst_pis", "cst_cofins"):
        if fiscal.get(campo) in (None, ""):
            raise NFCeErroDominio("PRODUTO_SEM_TRIBUTACAO")


def _montar_icms(imposto, fiscal, config, v_bc):
    icms_container = ET.SubElement(imposto, q("ICMS"))
    origem = str(fiscal.get("origem_mercadoria", 0))
    codigo = str(fiscal.get("csosn_ou_cst_icms") or "").strip()
    if config.regime_tributario == "SIMPLES":
        if codigo not in {"102", "103", "300", "400"}:
            raise NFCeErroDominio("TRIBUTACAO_ICMS_NAO_SUPORTADA")
        icms = ET.SubElement(icms_container, q("ICMSSN102"))
        icms.append(_el("orig", origem))
        icms.append(_el("CSOSN", codigo))
        return Decimal("0.00"), Decimal("0.00")
    if config.regime_tributario not in {"LUCRO_REAL", "LUCRO_PRESUMIDO"}:
        raise NFCeErroDominio("TRIBUTACAO_ICMS_NAO_SUPORTADA")
    cst = _normalizar_cst_icms(codigo, origem)
    if cst != "00":
        raise NFCeErroDominio("TRIBUTACAO_ICMS_NAO_SUPORTADA")
    aliquota = Decimal(str(fiscal.get("aliquota_icms") or "0"))
    v_icms = money(v_bc * aliquota / Decimal("100"))
    icms = ET.SubElement(icms_container, q("ICMS00"))
    for tag, valor in (
        ("orig", origem),
        ("CST", cst),
        ("modBC", "3"),
        ("vBC", dec(v_bc, 2)),
        ("pICMS", dec(aliquota, 4)),
        ("vICMS", dec(v_icms, 2)),
    ):
        icms.append(_el(tag, valor))
    return v_icms, v_bc


def _normalizar_cst_icms(codigo, origem):
    if codigo == "00":
        return "00"
    if len(codigo) == 3 and codigo[0] == origem:
        return codigo[1:]
    raise NFCeErroDominio("TRIBUTACAO_ICMS_NAO_SUPORTADA")


def _montar_pis(imposto, fiscal, v_bc):
    cst = str(fiscal.get("cst_pis") or "").zfill(2)
    aliquota = Decimal(str(fiscal.get("aliq_pis") or "0"))
    v_pis = money(v_bc * aliquota / Decimal("100"))
    if cst in {"01", "02"}:
        grupo = "PISAliq"
    elif cst == "49":
        grupo = "PISOutr"
    else:
        raise NFCeErroDominio("TRIBUTACAO_PIS_NAO_SUPORTADA")
    pis = ET.SubElement(ET.SubElement(imposto, q("PIS")), q(grupo))
    pis.append(_el("CST", cst))
    pis.append(_el("vBC", dec(v_bc, 2)))
    pis.append(_el("pPIS", dec(aliquota, 4)))
    pis.append(_el("vPIS", dec(v_pis, 2)))
    return v_pis


def _montar_cofins(imposto, fiscal, v_bc):
    cst = str(fiscal.get("cst_cofins") or "").zfill(2)
    aliquota = Decimal(str(fiscal.get("aliq_cofins") or "0"))
    v_cofins = money(v_bc * aliquota / Decimal("100"))
    if cst in {"01", "02"}:
        grupo = "COFINSAliq"
    elif cst == "49":
        grupo = "COFINSOutr"
    else:
        raise NFCeErroDominio("TRIBUTACAO_COFINS_NAO_SUPORTADA")
    cofins = ET.SubElement(ET.SubElement(imposto, q("COFINS")), q(grupo))
    cofins.append(_el("CST", cst))
    cofins.append(_el("vBC", dec(v_bc, 2)))
    cofins.append(_el("pCOFINS", dec(aliquota, 4)))
    cofins.append(_el("vCOFINS", dec(v_cofins, 2)))
    return v_cofins


def _totais_zerados():
    return {
        "vBC": Decimal("0.00"),
        "vICMS": Decimal("0.00"),
        "vProd": Decimal("0.00"),
        "vDesc": Decimal("0.00"),
        "vPIS": Decimal("0.00"),
        "vCOFINS": Decimal("0.00"),
    }


def _montar_total(inf, venda, totais):
    total = ET.SubElement(inf, q("total"))
    icms_tot = ET.SubElement(total, q("ICMSTot"))
    for tag, valor in (
        ("vBC", dec(totais["vBC"], 2)), ("vICMS", dec(totais["vICMS"], 2)), ("vICMSDeson", "0.00"), ("vFCP", "0.00"),
        ("vBCST", "0.00"), ("vST", "0.00"), ("vFCPST", "0.00"), ("vFCPSTRet", "0.00"),
        ("vProd", dec(totais["vProd"], 2)), ("vFrete", "0.00"), ("vSeg", "0.00"),
        ("vDesc", dec(totais["vDesc"], 2)), ("vII", "0.00"),
        ("vIPI", "0.00"), ("vIPIDevol", "0.00"), ("vPIS", dec(totais["vPIS"], 2)), ("vCOFINS", dec(totais["vCOFINS"], 2)),
        ("vOutro", "0.00"), ("vNF", dec(venda.total, 2)),
    ):
        icms_tot.append(_el(tag, valor))


def _montar_pag(inf, pagamentos, hub, venda):
    pag = ET.SubElement(inf, q("pag"))
    for pagamento in pagamentos:
        mapas = list(
            FormaPagamentoFiscalMapHub.objects.filter(
                hub=hub,
                forma_pagamento_retaguarda_id=pagamento.retaguarda_forma_pagamento_id,
            ).order_by("codigo_tpag")
        )
        if not mapas:
            raise NFCeErroDominio("PAGAMENTO_SEM_TPAG")
        if len(mapas) > 1:
            raise NFCeErroDominio("PAGAMENTO_TPAG_AMBIGUO")
        det = ET.SubElement(pag, q("detPag"))
        det.append(_el("tPag", mapas[0].codigo_tpag))
        det.append(_el("vPag", dec(pagamento.valor, 2)))
    if Decimal(venda.troco or 0) > Decimal("0"):
        pag.append(_el("vTroco", dec(venda.troco, 2)))


def assinar_xml_nfce(xml, material):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from lxml import etree
    except ImportError as exc:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES") from exc
    root = etree.fromstring(xml.encode("utf-8"), parser=_xml_parser())
    inf = root.find(f"{{{NFE_NS}}}infNFe")
    if inf is None:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    referencia = "#" + inf.attrib["Id"]
    inf_bytes = _canonicalizar(inf)
    digest_value = base64.b64encode(hashlib.sha1(inf_bytes).digest()).decode("ascii")
    key = serialization.load_pem_private_key(material.private_key_pem, password=None)
    cert = x509.load_pem_x509_certificate(material.certificate_pem)
    signature = etree.Element(qds("Signature"), nsmap={"ds": DS_NS})
    signed_info = etree.SubElement(signature, qds("SignedInfo"))
    etree.SubElement(signed_info, qds("CanonicalizationMethod"), Algorithm=CANONICALIZATION_ALGORITHM)
    etree.SubElement(signed_info, qds("SignatureMethod"), Algorithm=SIGNATURE_ALGORITHM)
    ref = etree.SubElement(signed_info, qds("Reference"), URI=referencia)
    transforms = etree.SubElement(ref, qds("Transforms"))
    etree.SubElement(transforms, qds("Transform"), Algorithm=ENVELOPED_SIGNATURE_ALGORITHM)
    etree.SubElement(transforms, qds("Transform"), Algorithm=CANONICALIZATION_ALGORITHM)
    etree.SubElement(ref, qds("DigestMethod"), Algorithm=DIGEST_ALGORITHM)
    digest = etree.SubElement(ref, qds("DigestValue"))
    digest.text = digest_value
    root.append(signature)
    assinatura = key.sign(_canonicalizar(signed_info), padding.PKCS1v15(), hashes.SHA1())
    signature_value = etree.SubElement(signature, qds("SignatureValue"))
    signature_value.text = base64.b64encode(assinatura).decode("ascii")
    key_info = etree.SubElement(signature, qds("KeyInfo"))
    x509_data = etree.SubElement(key_info, qds("X509Data"))
    x509_certificate = etree.SubElement(x509_data, qds("X509Certificate"))
    x509_certificate.text = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")
    return etree.tostring(root, encoding="unicode", pretty_print=False)


def verificar_assinatura_nfce(xml_assinado):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from lxml import etree
    except ImportError as exc:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES") from exc
    try:
        root = etree.fromstring(xml_assinado.encode("utf-8"), parser=_xml_parser())
        sig = root.find(f"{{{DS_NS}}}Signature")
        signed_info = sig.find(f"{{{DS_NS}}}SignedInfo")
        ref = signed_info.find(f"{{{DS_NS}}}Reference")
        uri = ref.attrib["URI"]
        if not uri.startswith("#"):
            return False
        transforms = [
            t.attrib.get("Algorithm")
            for t in ref.findall(f"{{{DS_NS}}}Transforms/{{{DS_NS}}}Transform")
        ]
        if transforms != [ENVELOPED_SIGNATURE_ALGORITHM, CANONICALIZATION_ALGORITHM]:
            return False
        digest_method = ref.find(f"{{{DS_NS}}}DigestMethod").attrib.get("Algorithm")
        signature_method = signed_info.find(f"{{{DS_NS}}}SignatureMethod").attrib.get("Algorithm")
        if digest_method != DIGEST_ALGORITHM or signature_method != SIGNATURE_ALGORITHM:
            return False
        inf = root.xpath("//*[@Id=$id]", id=uri[1:])
        if len(inf) != 1:
            return False
        digest_calculado = base64.b64encode(hashlib.sha1(_canonicalizar(inf[0])).digest()).decode("ascii")
        if digest_calculado != ref.findtext(f"{{{DS_NS}}}DigestValue"):
            return False
        assinatura = base64.b64decode(sig.findtext(f"{{{DS_NS}}}SignatureValue"))
        cert_der = base64.b64decode(sig.find(f"{{{DS_NS}}}KeyInfo/{{{DS_NS}}}X509Data/{{{DS_NS}}}X509Certificate").text)
        cert = x509.load_der_x509_certificate(cert_der)
        cert.public_key().verify(assinatura, _canonicalizar(signed_info), padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:
        return False


def gerar_qr_code_payload(nfce, config, qr_config, *, material_assinatura=None):
    if qr_config.versao != 3:
        raise NFCeErroDominio("QR_CODE_VERSAO_NAO_SUPORTADA")
    if nfce.tipo_emissao == "9":
        if material_assinatura is None:
            raise NFCeErroDominio("MATERIAL_ASSINATURA_INDISPONIVEL")
        assinatura = assinar_qr_code_offline(nfce, material_assinatura)
        return "|".join([
            f"{qr_config.url_qrcode}?p={nfce.chave_acesso}",
            "3",
            _tp_amb(config.ambiente_fiscal),
            nfce.emitida_em.strftime("%d"),
            dec(nfce.venda.total, 2),
            _tipo_identificacao_destinatario(nfce.venda),
            somente_digitos(nfce.venda.cliente_documento or ""),
            assinatura,
        ])
    return f"{qr_config.url_qrcode}?p={nfce.chave_acesso}|3|{_tp_amb(config.ambiente_fiscal)}"


def assinar_qr_code_offline(nfce, material):
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES") from exc
    payload = "|".join([
        nfce.chave_acesso,
        "3",
        _tp_amb(nfce.ambiente),
        nfce.emitida_em.strftime("%d"),
        dec(nfce.venda.total, 2),
        _tipo_identificacao_destinatario(nfce.venda),
        somente_digitos(nfce.venda.cliente_documento or ""),
    ])
    key = serialization.load_pem_private_key(material.private_key_pem, password=None)
    assinatura = key.sign(payload.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(assinatura).decode("ascii")


def listar_nfces_pendentes_transmissao(hub=None):
    qs = NFCeHub.objects.filter(status__in=[NFCeHub.STATUS_CONTINGENCIA, NFCeHub.STATUS_PENDENTE_TRANSMISSAO]).order_by("emitida_em", "id")
    if hub is not None:
        qs = qs.filter(hub=hub)
    return qs


def q(tag):
    return f"{{{NFE_NS}}}{tag}"


def qds(tag):
    return f"{{{DS_NS}}}{tag}"


def _el(tag, text, attrs=None):
    element = ET.Element(q(tag), attrs or {})
    element.text = str(text)
    return element


def _elds(tag, text=None, attrs=None):
    element = ET.Element(qds(tag), attrs or {})
    if text is not None:
        element.text = str(text)
    return element


def somente_digitos(valor):
    return re.sub(r"\D", "", str(valor or ""))


def _tipo_identificacao_destinatario(venda):
    documento = somente_digitos(venda.cliente_documento or "")
    if len(documento) == 11:
        return "1"
    if len(documento) == 14:
        return "2"
    return ""


def dec(valor, casas):
    return f"{Decimal(valor).quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP):.{casas}f}"


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _xml_parser():
    from lxml import etree

    return etree.XMLParser(remove_blank_text=True)


def _canonicalizar(element):
    from lxml import etree

    return etree.tostring(element, method="c14n", exclusive=False, with_comments=False)


def _tp_amb(ambiente):
    return "1" if ambiente == "PRODUCAO" else "2"
