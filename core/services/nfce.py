import base64
import hashlib
import hmac
import random
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from xml.etree import ElementTree as ET

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


@dataclass(frozen=True)
class MaterialAssinatura:
    private_key_pem: bytes
    certificate_pem: bytes


@dataclass(frozen=True)
class ConfigQRCodeDesenvolvimento:
    url_consulta: str
    csc_id: str
    csc: str


def gerar_nfce_local(venda, *, material_assinatura, qr_config, codigo_numerico=None):
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
            tipo_emissao="1",
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
            tipo_emissao="1",
            status=NFCeHub.STATUS_GERANDO,
            emitida_em=emitida_em,
        )

        try:
            xml = montar_xml_nfce(nfce, config)
            xml_assinado = assinar_xml_nfce(xml, material_assinatura)
            qr_payload = gerar_qr_code_payload(nfce, config, qr_config)
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
            nfce.status = NFCeHub.STATUS_GERADA
            nfce.save(update_fields=["xml_sem_assinatura", "xml_assinado", "qr_code_payload", "status", "atualizado_em"])
            resultado = nfce
    if erro_para_relancar:
        raise erro_para_relancar
    return resultado


def _validar_venda_finalizada(venda):
    if venda.status != VendaHub.STATUS_FINALIZADA:
        raise NFCeErroDominio("VENDA_NAO_FINALIZADA")


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


def montar_xml_nfce(nfce, config):
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

    root = ET.Element(q("NFe"))
    inf = ET.SubElement(root, q("infNFe"), {"versao": "4.00", "Id": f"NFe{nfce.chave_acesso}"})
    _montar_ide(inf, nfce, config)
    _montar_emit(inf, config)
    _montar_dest(inf, venda)
    for indice, item in enumerate(itens, start=1):
        _montar_det(inf, item, indice, config.uf)
    _montar_total(inf, venda)
    ET.SubElement(inf, q("transp")).append(_el("modFrete", "9"))
    _montar_pag(inf, pagamentos, nfce.hub)
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


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


def _montar_det(inf, item, indice, uf):
    fiscal = item.fiscal or {}
    _validar_fiscal_item(fiscal)
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
        ("vProd", dec(item.total_item, 2)),
        ("cEANTrib", item.ean13 or "SEM GTIN"),
        ("uTrib", item.unidade_codigo or "UN"),
        ("qTrib", dec(item.quantidade, 4)),
        ("vUnTrib", dec(item.preco_unitario, 10)),
        ("indTot", "1"),
    ):
        prod.append(_el(tag, valor))
    imposto = ET.SubElement(det, q("imposto"))
    icms = ET.SubElement(ET.SubElement(imposto, q("ICMS")), q("ICMSSN102"))
    icms.append(_el("orig", str(fiscal.get("origem_mercadoria", 0))))
    icms.append(_el("CSOSN", str(fiscal.get("csosn_ou_cst_icms"))))
    pis = ET.SubElement(ET.SubElement(imposto, q("PIS")), q("PISAliq"))
    pis.append(_el("CST", fiscal.get("cst_pis")))
    pis.append(_el("vBC", dec(item.total_item, 2)))
    pis.append(_el("pPIS", dec(Decimal(str(fiscal.get("aliq_pis") or "0")), 4)))
    pis.append(_el("vPIS", "0.00"))
    cofins = ET.SubElement(ET.SubElement(imposto, q("COFINS")), q("COFINSAliq"))
    cofins.append(_el("CST", fiscal.get("cst_cofins")))
    cofins.append(_el("vBC", dec(item.total_item, 2)))
    cofins.append(_el("pCOFINS", dec(Decimal(str(fiscal.get("aliq_cofins") or "0")), 4)))
    cofins.append(_el("vCOFINS", "0.00"))


def _validar_fiscal_item(fiscal):
    if not fiscal.get("ncm"):
        raise NFCeErroDominio("PRODUTO_SEM_NCM")
    if not (fiscal.get("cfop_venda_dentro") or fiscal.get("cfop_venda_fora")):
        raise NFCeErroDominio("PRODUTO_SEM_CFOP")
    for campo in ("csosn_ou_cst_icms", "cst_pis", "cst_cofins"):
        if fiscal.get(campo) in (None, ""):
            raise NFCeErroDominio("PRODUTO_SEM_TRIBUTACAO")


def _montar_total(inf, venda):
    total = ET.SubElement(inf, q("total"))
    icms_tot = ET.SubElement(total, q("ICMSTot"))
    for tag, valor in (
        ("vBC", "0.00"), ("vICMS", "0.00"), ("vICMSDeson", "0.00"), ("vFCP", "0.00"),
        ("vBCST", "0.00"), ("vST", "0.00"), ("vFCPST", "0.00"), ("vFCPSTRet", "0.00"),
        ("vProd", dec(venda.subtotal, 2)), ("vFrete", "0.00"), ("vSeg", "0.00"),
        ("vDesc", dec(venda.desconto_itens + venda.desconto_geral, 2)), ("vII", "0.00"),
        ("vIPI", "0.00"), ("vIPIDevol", "0.00"), ("vPIS", "0.00"), ("vCOFINS", "0.00"),
        ("vOutro", "0.00"), ("vNF", dec(venda.total, 2)),
    ):
        icms_tot.append(_el(tag, valor))


def _montar_pag(inf, pagamentos, hub):
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


def assinar_xml_nfce(xml, material):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES") from exc
    root = ET.fromstring(xml)
    inf = root.find(f"{{{NFE_NS}}}infNFe")
    if inf is None:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES")
    referencia = "#" + inf.attrib["Id"]
    inf_bytes = ET.tostring(inf, encoding="utf-8")
    key = serialization.load_pem_private_key(material.private_key_pem, password=None)
    assinatura = key.sign(inf_bytes, padding.PKCS1v15(), hashes.SHA256())
    cert = x509.load_pem_x509_certificate(material.certificate_pem)
    signature = ET.SubElement(root, qds("Signature"))
    signed_info = ET.SubElement(signature, qds("SignedInfo"))
    signed_info.append(_elds("CanonicalizationMethod", attrs={"Algorithm": "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"}))
    signed_info.append(_elds("SignatureMethod", attrs={"Algorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"}))
    ref = ET.SubElement(signed_info, qds("Reference"), {"URI": referencia})
    ref.append(_elds("DigestMethod", attrs={"Algorithm": "http://www.w3.org/2001/04/xmlenc#sha256"}))
    ref.append(_elds("DigestValue", base64.b64encode(hashlib.sha256(inf_bytes).digest()).decode("ascii")))
    signature.append(_elds("SignatureValue", base64.b64encode(assinatura).decode("ascii")))
    key_info = ET.SubElement(signature, qds("KeyInfo"))
    x509_data = ET.SubElement(key_info, qds("X509Data"))
    x509_data.append(_elds("X509Certificate", base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")))
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def verificar_assinatura_nfce(xml_assinado):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise NFCeErroDominio("DADOS_FISCAIS_INSUFICIENTES") from exc
    root = ET.fromstring(xml_assinado)
    inf = root.find(f"{{{NFE_NS}}}infNFe")
    sig = root.find(f"{{{DS_NS}}}Signature")
    assinatura = base64.b64decode(sig.findtext(f"{{{DS_NS}}}SignatureValue"))
    cert_der = base64.b64decode(sig.find(f"{{{DS_NS}}}KeyInfo/{{{DS_NS}}}X509Data/{{{DS_NS}}}X509Certificate").text)
    cert = x509.load_der_x509_certificate(cert_der)
    cert.public_key().verify(assinatura, ET.tostring(inf, encoding="utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return True


def gerar_qr_code_payload(nfce, config, qr_config):
    base = f"{qr_config.url_consulta}?chNFe={nfce.chave_acesso}&nVersao=100&tpAmb={_tp_amb(config.ambiente_fiscal)}&cIdToken={qr_config.csc_id}"
    digest = hmac.new(qr_config.csc.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).hexdigest().upper()
    return f"{base}&cHashQRCode={digest}"


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


def dec(valor, casas):
    return f"{Decimal(valor).quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP):.{casas}f}"


def _tp_amb(ambiente):
    return "1" if ambiente == "PRODUCAO" else "2"
