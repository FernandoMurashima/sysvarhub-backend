import base64
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET

from django.utils import timezone

from core.models import NFCeHub


NFE_NS = "http://www.portalfiscal.inf.br/nfe"
NS = {"n": NFE_NS}


class DanfeNFCeErroDominio(Exception):
    def __init__(self, codigo, mensagem=None):
        self.codigo = codigo
        super().__init__(mensagem or codigo)


TPAG_DESCRICOES = {
    "01": "Dinheiro",
    "02": "Cheque",
    "03": "Cartao de Credito",
    "04": "Cartao de Debito",
    "05": "Credito Loja",
    "10": "Vale Alimentacao",
    "11": "Vale Refeicao",
    "12": "Vale Presente",
    "13": "Vale Combustivel",
    "15": "Boleto Bancario",
    "16": "Deposito Bancario",
    "17": "PIX",
    "18": "Transferencia bancaria",
    "19": "Programa de fidelidade",
    "90": "Sem pagamento",
    "99": "Outros",
}


def montar_dados_danfe_nfce(nfce, via="CONSUMIDOR"):
    via = _normalizar_via(via)
    if not nfce.xml_assinado:
        raise DanfeNFCeErroDominio("NFCE_SEM_XML_ASSINADO")
    root = ET.fromstring(nfce.xml_assinado)
    inf = root.find("n:infNFe", NS)
    if inf is None:
        raise DanfeNFCeErroDominio("NFCE_XML_INVALIDO")

    ide = inf.find("n:ide", NS)
    emit = inf.find("n:emit", NS)
    dest = inf.find("n:dest", NS)
    total = inf.find("n:total/n:ICMSTot", NS)
    supl = root.find("n:infNFeSupl", NS)
    protocolo = _protocolo(nfce)
    mensagens = _mensagens(nfce)
    imprimivel, motivo = _imprimibilidade(nfce)
    qr_payload = _texto(supl, "n:qrCode") or nfce.qr_code_payload
    chave = nfce.chave_acesso or (inf.attrib.get("Id", "")[3:] if inf.attrib.get("Id", "").startswith("NFe") else "")

    return {
        "nfce_uuid": str(nfce.nfce_uuid),
        "venda_uuid": str(nfce.venda.venda_uuid),
        "status": nfce.status,
        "imprimivel": imprimivel,
        "motivo_nao_imprimivel": motivo,
        "via": via,
        "via_texto": "Via Consumidor" if via == "CONSUMIDOR" else "Via do Estabelecimento",
        "ambiente": nfce.ambiente,
        "homologacao": nfce.ambiente == "HOMOLOGACAO",
        "contingencia": nfce.status == NFCeHub.STATUS_CONTINGENCIA or _texto(ide, "n:tpEmis") == "9",
        "emitente": _emitente(emit),
        "documento": {
            "modelo": _texto(ide, "n:mod"),
            "serie": _inteiro(_texto(ide, "n:serie")),
            "numero": _inteiro(_texto(ide, "n:nNF")),
            "emitida_em": _data_hora_local(_texto(ide, "n:dhEmi")),
            "emitida_em_iso": _texto(ide, "n:dhEmi"),
            "ambiente": _texto(ide, "n:tpAmb"),
            "tipo_emissao": _texto(ide, "n:tpEmis"),
            "chave_acesso": chave,
            "chave_acesso_formatada": formatar_chave_acesso(chave),
            "url_consulta": _texto(supl, "n:urlChave"),
        },
        "consumidor": _consumidor(dest),
        "itens": [_item(det) for det in inf.findall("n:det", NS)],
        "totais": _totais(total, inf),
        "pagamentos": _pagamentos(inf),
        "troco": _texto(inf, "n:pag/n:vTroco") or "0.00",
        "mensagens": mensagens,
        "protocolo": protocolo,
        "qr_code_payload": qr_payload,
        "qr_code_data_uri": gerar_qr_code_data_uri(qr_payload) if qr_payload else "",
    }


def formatar_chave_acesso(chave):
    return " ".join(chave[i:i + 4] for i in range(0, len(chave), 4))


def gerar_qr_code_data_uri(payload):
    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(border=4, box_size=8, image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(payload)
    qr.make(fit=True)
    buffer = BytesIO()
    qr.make_image().save(buffer)
    svg = buffer.getvalue()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def _normalizar_via(via):
    via = (via or "CONSUMIDOR").upper()
    if via not in {"CONSUMIDOR", "ESTABELECIMENTO"}:
        raise DanfeNFCeErroDominio("VIA_DANFE_INVALIDA")
    return via


def _emitente(emit):
    ender = emit.find("n:enderEmit", NS) if emit is not None else None
    endereco_partes = [
        _texto(ender, "n:xLgr"),
        _texto(ender, "n:nro"),
        _texto(ender, "n:xCpl"),
        _texto(ender, "n:xBairro"),
        _texto(ender, "n:xMun"),
        _texto(ender, "n:UF"),
        _texto(ender, "n:CEP"),
    ]
    return {
        "razao_social": _texto(emit, "n:xNome"),
        "nome_fantasia": _texto(emit, "n:xFant"),
        "cnpj": _texto(emit, "n:CNPJ"),
        "ie": _texto(emit, "n:IE"),
        "endereco": ", ".join(parte for parte in endereco_partes if parte),
    }


def _consumidor(dest):
    cpf = _texto(dest, "n:CPF")
    cnpj = _texto(dest, "n:CNPJ")
    documento = cpf or cnpj
    return {
        "identificado": bool(documento),
        "tipo_documento": "CPF" if cpf else ("CNPJ" if cnpj else ""),
        "documento": documento,
        "nome": _texto(dest, "n:xNome"),
    }


def _item(det):
    prod = det.find("n:prod", NS)
    v_prod = _decimal(_texto(prod, "n:vProd"))
    v_desc = _decimal(_texto(prod, "n:vDesc"))
    return {
        "numero": _inteiro(det.attrib.get("nItem")),
        "codigo": _texto(prod, "n:cProd"),
        "descricao": _texto(prod, "n:xProd"),
        "quantidade": _texto(prod, "n:qCom"),
        "unidade": _texto(prod, "n:uCom"),
        "valor_unitario": _texto(prod, "n:vUnCom"),
        "valor_bruto": _texto(prod, "n:vProd"),
        "desconto": _texto(prod, "n:vDesc") or "0.00",
        "valor_liquido": f"{(v_prod - v_desc):.2f}",
    }


def _totais(total, inf):
    return {
        "quantidade_itens": len(inf.findall("n:det", NS)),
        "vProd": _texto(total, "n:vProd") or "0.00",
        "vDesc": _texto(total, "n:vDesc") or "0.00",
        "vNF": _texto(total, "n:vNF") or "0.00",
        "vPIS": _texto(total, "n:vPIS") or "0.00",
        "vCOFINS": _texto(total, "n:vCOFINS") or "0.00",
        "vICMS": _texto(total, "n:vICMS") or "0.00",
    }


def _pagamentos(inf):
    pagamentos = []
    for det in inf.findall("n:pag/n:detPag", NS):
        codigo = _texto(det, "n:tPag")
        pagamentos.append({
            "tPag": codigo,
            "descricao": TPAG_DESCRICOES.get(codigo, f"Forma de pagamento {codigo}"),
            "valor": _texto(det, "n:vPag") or "0.00",
        })
    return pagamentos


def _mensagens(nfce):
    mensagens = []
    if nfce.ambiente == "HOMOLOGACAO":
        mensagens.append("EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL")
    if nfce.status == NFCeHub.STATUS_CONTINGENCIA:
        mensagens.extend(["EMITIDA EM CONTINGENCIA", "Pendente de autorizacao"])
    if nfce.status == NFCeHub.STATUS_GERADA and not nfce.protocolo:
        mensagens.append("Documento sem protocolo de autorizacao fiscal real")
    return mensagens


def _imprimibilidade(nfce):
    if nfce.status in {NFCeHub.STATUS_CONTINGENCIA, NFCeHub.STATUS_GERADA, NFCeHub.STATUS_AUTORIZADA}:
        return True, ""
    motivos = {
        NFCeHub.STATUS_REJEITADA: "NFCE_REJEITADA",
        NFCeHub.STATUS_ERRO_GERACAO: "NFCE_ERRO_GERACAO",
        NFCeHub.STATUS_PENDENTE_TRANSMISSAO: "NFCE_PENDENTE_TRANSMISSAO",
    }
    return False, motivos.get(nfce.status, "NFCE_NAO_IMPRIMIVEL")


def _protocolo(nfce):
    if not nfce.protocolo:
        return None
    return {
        "numero": nfce.protocolo,
        "autorizada_em": timezone.localtime(nfce.autorizada_em).isoformat() if nfce.autorizada_em else "",
        "codigo_retorno": nfce.codigo_retorno,
        "mensagem_retorno": nfce.mensagem_retorno,
    }


def _data_hora_local(valor):
    if not valor:
        return ""
    try:
        dt = timezone.datetime.fromisoformat(valor)
    except ValueError:
        return valor
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.isoformat()


def _texto(elemento, caminho):
    if elemento is None:
        return ""
    encontrado = elemento.find(caminho, NS)
    return (encontrado.text or "") if encontrado is not None else ""


def _inteiro(valor):
    return int(valor) if valor not in (None, "") else None


def _decimal(valor):
    return Decimal(valor or "0")
