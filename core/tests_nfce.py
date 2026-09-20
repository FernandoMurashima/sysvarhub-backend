from datetime import datetime, timedelta
import base64
import hashlib
from decimal import Decimal
from xml.etree import ElementTree as ET

from django.test import TestCase
from django.utils import timezone
from lxml import etree

from core.models import ConfiguracaoFiscalHub, FormaPagamentoFiscalMapHub, NFCeHub, VendaHub, VendaItemHub
from core.services.nfce import (
    CANONICALIZATION_ALGORITHM,
    DIGEST_ALGORITHM,
    ENVELOPED_SIGNATURE_ALGORITHM,
    ConfigQRCodeDesenvolvimento,
    MaterialAssinatura,
    NFCeErroDominio,
    SIGNATURE_ALGORITHM,
    calcular_dv_chave,
    gerar_chave_acesso,
    gerar_nfce_local,
    verificar_assinatura_nfce,
)
from core.tests_pagamentos import PagamentoHubTestMixin


FISCAL_ITEM = {
    "ncm": "62046200",
    "origem_mercadoria": 0,
    "cfop_venda_dentro": "5102",
    "cfop_venda_fora": "6102",
    "csosn_ou_cst_icms": "102",
    "aliquota_icms": "0.00",
    "cst_pis": "01",
    "aliq_pis": "0.0000",
    "cst_cofins": "01",
    "aliq_cofins": "0.0000",
}


def material_teste():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sysvar Hub Teste")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=1))
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


class NFCeHubTests(PagamentoHubTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.catalogo_item.fiscal = FISCAL_ITEM.copy()
        self.catalogo_item.save(update_fields=["fiscal"])
        self.config = ConfiguracaoFiscalHub.objects.create(
            hub=self.hub,
            emite_nfce=True,
            ambiente_fiscal="HOMOLOGACAO",
            regime_tributario="SIMPLES",
            inscricao_estadual="110042490114",
            serie_nfce=7,
            proximo_numero_nfce=10,
            razao_social="Empresa Teste Ltda",
            nome_fantasia="Empresa Teste",
            cnpj="12345678000199",
            endereco="Rua Teste",
            numero="123",
            bairro="Centro",
            cidade="Sao Paulo",
            uf="SP",
            cep="01001000",
            codigo_municipio_ibge="3550308",
            sincronizado_em=timezone.now(),
        )
        FormaPagamentoFiscalMapHub.objects.create(
            hub=self.hub,
            forma_pagamento_retaguarda_id=self.dinheiro.retaguarda_id,
            codigo_tpag="01",
            descricao_fiscal="Dinheiro",
            sincronizado_em=timezone.now(),
        )
        self.material = material_teste()
        self.qr = ConfigQRCodeDesenvolvimento(
            url_qrcode="https://sefaz.test/qrcode",
            url_chave="https://sefaz.test/consulta",
        )

    def venda_finalizada(self, valor_pagamento="199.90"):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro, valor=valor_pagamento)
        self.finalizar(venda_uuid)
        return VendaHub.objects.get(venda_uuid=venda_uuid)

    def gerar_nfce(self, venda, codigo_numerico="12345678"):
        return gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico=codigo_numerico)

    def ajustar_venda_item(self, venda, *, preco="100.0000", desconto="0.00", fiscal=None):
        item = VendaItemHub.objects.get(venda=venda)
        item.preco_unitario = Decimal(preco)
        item.desconto = Decimal(desconto)
        item.total_item = Decimal(preco) * item.quantidade - Decimal(desconto)
        if fiscal is not None:
            item.fiscal = fiscal
        item.save(update_fields=["preco_unitario", "desconto", "total_item", "fiscal"])
        venda.subtotal = item.total_item
        venda.desconto_itens = item.desconto
        venda.total = item.total_item
        venda.valor_recebido = item.total_item
        venda.troco = Decimal("0.00")
        venda.save(update_fields=["subtotal", "desconto_itens", "total", "valor_recebido", "troco"])
        pagamento = venda.pagamentos.get(status="ATIVO")
        pagamento.valor = item.total_item
        pagamento.save(update_fields=["valor"])
        return item

    def test_item_copia_snapshot_fiscal_do_catalogo(self):
        self.post_item()
        item = VendaItemHub.objects.get()
        self.catalogo_item.fiscal = {"ncm": "99999999"}
        self.catalogo_item.save(update_fields=["fiscal"])
        item.refresh_from_db()

        self.assertEqual(item.fiscal["ncm"], "62046200")

    def test_chave_44_digitos_componentes_e_dv(self):
        emissao = timezone.datetime(2026, 9, 20, tzinfo=timezone.get_current_timezone())
        chave, dv = gerar_chave_acesso(
            uf="SP",
            emissao=emissao,
            cnpj="12345678000199",
            serie=7,
            numero=10,
            tipo_emissao="1",
            codigo_numerico="12345678",
        )

        self.assertEqual(len(chave), 44)
        self.assertEqual(chave[:2], "35")
        self.assertEqual(chave[2:6], "2609")
        self.assertEqual(chave[6:20], "12345678000199")
        self.assertEqual(chave[20:22], "65")
        self.assertEqual(dv, calcular_dv_chave(chave[:43]))

    def test_gera_xml_assina_qr_persiste_e_idempotente(self):
        venda = self.venda_finalizada()

        nfce = self.gerar_nfce(venda, codigo_numerico="12345678")
        segunda = self.gerar_nfce(venda, codigo_numerico="87654321")

        self.assertEqual(nfce.pk, segunda.pk)
        self.assertEqual(nfce.status, NFCeHub.STATUS_GERADA)
        self.assertEqual(nfce.serie, 7)
        self.assertEqual(nfce.numero, 10)
        self.config.refresh_from_db()
        self.assertEqual(self.config.proximo_numero_nfce, 11)
        root = ET.fromstring(nfce.xml_assinado)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe", "ds": "http://www.w3.org/2000/09/xmldsig#"}
        self.assertEqual(root.find("n:infNFe/n:ide/n:mod", ns).text, "65")
        self.assertEqual(root.find("n:infNFe", ns).attrib["versao"], "4.00")
        self.assertEqual(root.find("n:infNFe/n:pag/n:detPag/n:tPag", ns).text, "01")
        self.assertEqual([child.tag.split("}", 1)[-1] for child in root], ["infNFe", "infNFeSupl", "Signature"])
        self.assertIn("<infNFeSupl>", nfce.xml_sem_assinatura)
        self.assertNotIn("<ds:Signature", nfce.xml_sem_assinatura)
        self.assertEqual(root.find("n:infNFeSupl/n:qrCode", ns).text, f"https://sefaz.test/qrcode?p={nfce.chave_acesso}|3|2")
        self.assertEqual(root.find("n:infNFeSupl/n:urlChave", ns).text, "https://sefaz.test/consulta")
        self.assertEqual(nfce.qr_code_payload, f"https://sefaz.test/qrcode?p={nfce.chave_acesso}|3|2")
        self.assertNotIn("cHashQRCode", nfce.qr_code_payload)
        self.assertNotIn("nVersao=100", nfce.qr_code_payload)
        self.assertNotIn("cIdToken", nfce.qr_code_payload)
        signed_info = root.find("ds:Signature/ds:SignedInfo", ns)
        reference = signed_info.find("ds:Reference", ns)
        self.assertEqual(reference.attrib["URI"], f"#NFe{nfce.chave_acesso}")
        self.assertEqual(signed_info.find("ds:SignatureMethod", ns).attrib["Algorithm"], SIGNATURE_ALGORITHM)
        self.assertEqual(reference.find("ds:DigestMethod", ns).attrib["Algorithm"], DIGEST_ALGORITHM)
        self.assertEqual(
            [node.attrib["Algorithm"] for node in reference.findall("ds:Transforms/ds:Transform", ns)],
            [ENVELOPED_SIGNATURE_ALGORITHM, CANONICALIZATION_ALGORITHM],
        )
        self.assertEqual(reference.find("ds:DigestValue", ns).text, self._digest_inf_nfe(nfce.xml_assinado, nfce.chave_acesso))
        self.assertNotIn("PRIVATE KEY", nfce.xml_assinado)
        self.assertTrue(verificar_assinatura_nfce(nfce.xml_assinado))

    def test_segunda_venda_usa_proximo_numero(self):
        primeira = self.venda_finalizada()
        self.gerar_nfce(primeira, codigo_numerico="11111111")
        segunda = self.venda_finalizada()
        nfce = self.gerar_nfce(segunda, codigo_numerico="22222222")

        self.assertEqual(nfce.numero, 11)

    def test_erro_apos_reserva_nao_reutiliza_numero(self):
        venda = self.venda_finalizada()
        FormaPagamentoFiscalMapHub.objects.filter(hub=self.hub).delete()

        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(venda, codigo_numerico="33333333")

        self.assertEqual(ctx.exception.codigo, "PAGAMENTO_SEM_TPAG")
        nfce = NFCeHub.objects.get(venda=venda)
        self.assertEqual(nfce.status, NFCeHub.STATUS_ERRO_GERACAO)
        self.config.refresh_from_db()
        self.assertEqual(self.config.proximo_numero_nfce, 11)

    def test_erros_controlados_config_desabilitada_produto_incompleto_e_tpag_ambiguo(self):
        venda = self.venda_finalizada()
        self.config.emite_nfce = False
        self.config.save(update_fields=["emite_nfce"])
        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(venda, codigo_numerico="44444444")
        self.assertEqual(ctx.exception.codigo, "NFCE_DESABILITADA")

        self.config.emite_nfce = True
        self.config.save(update_fields=["emite_nfce"])
        item = VendaItemHub.objects.get(venda=venda)
        item.fiscal = {}
        item.save(update_fields=["fiscal"])
        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(venda, codigo_numerico="55555555")
        self.assertEqual(ctx.exception.codigo, "PRODUTO_SEM_NCM")

        outra = self.venda_finalizada()
        FormaPagamentoFiscalMapHub.objects.create(
            hub=self.hub,
            forma_pagamento_retaguarda_id=self.dinheiro.retaguarda_id,
            codigo_tpag="17",
            descricao_fiscal="PIX",
            sincronizado_em=timezone.now(),
        )
        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(outra, codigo_numerico="66666666")
        self.assertEqual(ctx.exception.codigo, "PAGAMENTO_TPAG_AMBIGUO")

    def test_assinatura_falha_quando_inf_nfe_e_alterado(self):
        nfce = self.gerar_nfce(self.venda_finalizada())
        xml = nfce.xml_assinado.replace("<vNF>199.90</vNF>", "<vNF>198.90</vNF>", 1)

        self.assertFalse(verificar_assinatura_nfce(xml))

    def test_simples_usa_icmssn102_e_rejeita_csosn_incompativel(self):
        nfce = self.gerar_nfce(self.venda_finalizada())
        root = ET.fromstring(nfce.xml_assinado)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:ICMS/n:ICMSSN102/n:CSOSN", ns).text, "102")

        venda = self.venda_finalizada()
        item = VendaItemHub.objects.get(venda=venda)
        fiscal = item.fiscal.copy()
        fiscal["csosn_ou_cst_icms"] = "500"
        item.fiscal = fiscal
        item.save(update_fields=["fiscal"])
        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(venda, codigo_numerico="87654321")
        self.assertEqual(ctx.exception.codigo, "TRIBUTACAO_ICMS_NAO_SUPORTADA")

    def test_lucro_real_calcula_icms_pis_cofins_e_totais(self):
        venda = self.venda_finalizada()
        self.config.regime_tributario = "LUCRO_REAL"
        self.config.save(update_fields=["regime_tributario"])
        fiscal = FISCAL_ITEM.copy()
        fiscal.update({
            "csosn_ou_cst_icms": "000",
            "aliquota_icms": "18.00",
            "aliq_pis": "1.6500",
            "aliq_cofins": "7.6000",
        })
        self.ajustar_venda_item(venda, fiscal=fiscal)

        nfce = self.gerar_nfce(venda)
        root = ET.fromstring(nfce.xml_assinado)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe"}

        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:ICMS/n:ICMS00/n:CST", ns).text, "00")
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:ICMS/n:ICMS00/n:vBC", ns).text, "100.00")
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:ICMS/n:ICMS00/n:pICMS", ns).text, "18.0000")
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:ICMS/n:ICMS00/n:vICMS", ns).text, "18.00")
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:PIS/n:PISAliq/n:vPIS", ns).text, "1.65")
        self.assertEqual(root.find("n:infNFe/n:det/n:imposto/n:COFINS/n:COFINSAliq/n:vCOFINS", ns).text, "7.60")
        self.assertEqual(root.find("n:infNFe/n:total/n:ICMSTot/n:vICMS", ns).text, "18.00")
        self.assertEqual(root.find("n:infNFe/n:total/n:ICMSTot/n:vPIS", ns).text, "1.65")
        self.assertEqual(root.find("n:infNFe/n:total/n:ICMSTot/n:vCOFINS", ns).text, "7.60")
        self.assertTrue(verificar_assinatura_nfce(nfce.xml_assinado))

    def test_desconto_item_usa_vprod_bruto_e_desconto_geral_eh_bloqueado(self):
        venda = self.venda_finalizada()
        self.ajustar_venda_item(venda, preco="100.0000", desconto="10.00")

        nfce = self.gerar_nfce(venda)
        root = ET.fromstring(nfce.xml_assinado)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
        self.assertEqual(root.find("n:infNFe/n:det/n:prod/n:vProd", ns).text, "100.00")
        self.assertEqual(root.find("n:infNFe/n:det/n:prod/n:vDesc", ns).text, "10.00")
        self.assertEqual(root.find("n:infNFe/n:total/n:ICMSTot/n:vDesc", ns).text, "10.00")
        self.assertEqual(root.find("n:infNFe/n:total/n:ICMSTot/n:vNF", ns).text, "90.00")

        outra = self.venda_finalizada()
        outra.desconto_geral = Decimal("1.00")
        outra.save(update_fields=["desconto_geral"])
        with self.assertRaises(NFCeErroDominio) as ctx:
            self.gerar_nfce(outra, codigo_numerico="87654321")
        self.assertEqual(ctx.exception.codigo, "DESCONTO_GERAL_FISCAL_NAO_SUPORTADO")

    def test_troco_eh_emitido_no_grupo_pag(self):
        venda = self.venda_finalizada(valor_pagamento="200.00")

        nfce = self.gerar_nfce(venda)
        root = ET.fromstring(nfce.xml_assinado)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
        self.assertEqual(root.find("n:infNFe/n:pag/n:vTroco", ns).text, "0.10")

    def _digest_inf_nfe(self, xml, chave):
        root = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(remove_blank_text=True))
        inf = root.xpath("//*[@Id=$id]", id=f"NFe{chave}")[0]
        canonico = etree.tostring(inf, method="c14n", exclusive=False, with_comments=False)
        return base64.b64encode(hashlib.sha1(canonico).digest()).decode("ascii")
