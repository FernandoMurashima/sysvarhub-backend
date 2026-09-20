from datetime import datetime, timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET

from django.test import TestCase
from django.utils import timezone

from core.models import ConfiguracaoFiscalHub, FormaPagamentoFiscalMapHub, NFCeHub, VendaHub, VendaItemHub
from core.services.nfce import (
    ConfigQRCodeDesenvolvimento,
    MaterialAssinatura,
    NFCeErroDominio,
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
            url_consulta="https://sefaz.test/qrcode",
            csc_id="000001",
            csc="CSC-DE-TESTE",
        )

    def venda_finalizada(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        self.finalizar(venda_uuid)
        return VendaHub.objects.get(venda_uuid=venda_uuid)

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

        nfce = gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="12345678")
        segunda = gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="87654321")

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
        self.assertEqual(root.find("ds:Signature/ds:SignedInfo/ds:Reference", ns).attrib["URI"], f"#NFe{nfce.chave_acesso}")
        self.assertNotIn("PRIVATE KEY", nfce.xml_assinado)
        self.assertIn(nfce.chave_acesso, nfce.qr_code_payload)
        self.assertIn("tpAmb=2", nfce.qr_code_payload)
        self.assertTrue(verificar_assinatura_nfce(nfce.xml_assinado))

    def test_segunda_venda_usa_proximo_numero(self):
        primeira = self.venda_finalizada()
        gerar_nfce_local(primeira, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="11111111")
        segunda = self.venda_finalizada()
        nfce = gerar_nfce_local(segunda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="22222222")

        self.assertEqual(nfce.numero, 11)

    def test_erro_apos_reserva_nao_reutiliza_numero(self):
        venda = self.venda_finalizada()
        FormaPagamentoFiscalMapHub.objects.filter(hub=self.hub).delete()

        with self.assertRaises(NFCeErroDominio) as ctx:
            gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="33333333")

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
            gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="44444444")
        self.assertEqual(ctx.exception.codigo, "NFCE_DESABILITADA")

        self.config.emite_nfce = True
        self.config.save(update_fields=["emite_nfce"])
        item = VendaItemHub.objects.get(venda=venda)
        item.fiscal = {}
        item.save(update_fields=["fiscal"])
        with self.assertRaises(NFCeErroDominio) as ctx:
            gerar_nfce_local(venda, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="55555555")
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
            gerar_nfce_local(outra, material_assinatura=self.material, qr_config=self.qr, codigo_numerico="66666666")
        self.assertEqual(ctx.exception.codigo, "PAGAMENTO_TPAG_AMBIGUO")
