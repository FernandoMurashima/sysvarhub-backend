from datetime import datetime, timedelta
import base64
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from lxml import etree

from core.models import ConfiguracaoFiscalHub, EventoSyncHub, FormaPagamentoFiscalMapHub, NFCeHub, VendaHub, VendaItemHub
from core.services.nfce import (
    CANONICALIZATION_ALGORITHM,
    DIGEST_ALGORITHM,
    ENVELOPED_SIGNATURE_ALGORITHM,
    ConfigQRCodeDesenvolvimento,
    MaterialAssinatura,
    NFCeMaterialProviderA1,
    NFCeErroDominio,
    SIGNATURE_ALGORITHM,
    SefazNFCeClient,
    SefazNFCeClientDesenvolvimento,
    SefazNFCeClientReal,
    ResultadoSefazNFCe,
    calcular_dv_chave,
    gerar_chave_acesso,
    gerar_nfce_local,
    listar_nfces_pendentes_transmissao,
    preparar_nfce_para_venda_finalizada,
    processar_nfce_preparada,
    retransmitir_nfces_pendentes,
    verificar_assinatura_nfce,
)
from core.services.sync import _criar_ou_atualizar_evento, enfileirar_nfce_atualizada, enfileirar_venda_finalizada, sincronizar_eventos_pendentes
from core.services.danfe_nfce import montar_dados_danfe_nfce
from core.tests_caixa import criar_hub
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
        self.config.emite_nfce = False
        self.config.save(update_fields=["emite_nfce"])
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro, valor=valor_pagamento)
        self.finalizar(venda_uuid)
        self.config.emite_nfce = True
        self.config.save(update_fields=["emite_nfce"])
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

    def test_qr_code_offline_v3_tem_assinatura_valida_e_pendencia_listavel(self):
        venda = self.venda_finalizada()
        nfce = gerar_nfce_local(
            venda,
            material_assinatura=self.material,
            qr_config=self.qr,
            codigo_numerico="12345678",
            tipo_emissao="9",
            justificativa_contingencia="SEFAZ indisponivel em teste",
        )

        self.assertEqual(nfce.status, NFCeHub.STATUS_CONTINGENCIA)
        self.assertEqual(nfce.chave_acesso[34], "9")
        self.assertIn("<dhCont>", nfce.xml_assinado)
        self.assertIn("<xJust>SEFAZ indisponivel em teste</xJust>", nfce.xml_assinado)
        self.assertNotIn("cHashQRCode", nfce.qr_code_payload)
        self.assertIn(nfce, list(listar_nfces_pendentes_transmissao(self.hub)))
        partes = nfce.qr_code_payload.split("?p=", 1)[1].split("|")
        self.assertEqual(partes[:3], [nfce.chave_acesso, "3", "2"])
        self.assertEqual(partes[5], "")
        self.assertEqual(partes[6], "")
        self._verificar_assinatura_qr_offline(partes, self.material)

    def test_nfce_pendente_transmissao_eh_listavel_para_retomada(self):
        venda = self.venda_finalizada()

        nfce = preparar_nfce_para_venda_finalizada(venda, codigo_numerico="12345678")

        self.assertEqual(nfce.status, NFCeHub.STATUS_PENDENTE_TRANSMISSAO)
        self.assertIn(nfce, list(listar_nfces_pendentes_transmissao(self.hub)))

    def test_danfe_usa_xml_historico_emitente_itens_totais_pagamentos_e_qr(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)
        self.catalogo_item.descricao = "Descricao alterada depois da emissao"
        self.catalogo_item.preco_venda = Decimal("1.0000")
        self.catalogo_item.save(update_fields=["descricao", "preco_venda"])

        dados = montar_dados_danfe_nfce(nfce)

        self.assertEqual(dados["nfce_uuid"], str(nfce.nfce_uuid))
        self.assertTrue(dados["imprimivel"])
        self.assertEqual(dados["via"], "CONSUMIDOR")
        self.assertEqual(dados["emitente"]["razao_social"], "Empresa Teste Ltda")
        self.assertEqual(dados["emitente"]["cnpj"], "12345678000199")
        self.assertEqual(dados["documento"]["numero"], 10)
        self.assertEqual(dados["documento"]["serie"], 7)
        self.assertEqual(dados["documento"]["chave_acesso"], nfce.chave_acesso)
        self.assertEqual(len(dados["documento"]["chave_acesso_formatada"].split()), 11)
        self.assertFalse(dados["consumidor"]["identificado"])
        self.assertEqual(dados["itens"][0]["codigo"], str(self.catalogo_item.retaguarda_sku_id))
        self.assertEqual(dados["itens"][0]["descricao"], "Calça Jeans Reta Aurora")
        self.assertEqual(dados["itens"][0]["quantidade"], "1.0000")
        self.assertEqual(dados["itens"][0]["unidade"], "UN")
        self.assertEqual(dados["itens"][0]["valor_bruto"], "199.90")
        self.assertEqual(dados["itens"][0]["valor_liquido"], "199.90")
        self.assertEqual(dados["totais"]["vProd"], "199.90")
        self.assertEqual(dados["totais"]["vNF"], "199.90")
        self.assertEqual(dados["pagamentos"][0]["tPag"], "01")
        self.assertEqual(dados["pagamentos"][0]["descricao"], "Dinheiro")
        self.assertEqual(dados["troco"], "0.00")
        self.assertIn("SEM VALOR FISCAL", " ".join(dados["mensagens"]))
        self.assertEqual(dados["qr_code_payload"], nfce.qr_code_payload)
        self.assertTrue(dados["qr_code_data_uri"].startswith("data:image/svg+xml;base64,"))

    def test_danfe_consumidor_identificado_contingencia_via_e_status_nao_imprimiveis(self):
        venda = self.venda_finalizada()
        venda.cliente_documento = "12345678901"
        venda.cliente_nome = "Cliente NFCe"
        venda.cliente_padrao = False
        venda.save(update_fields=["cliente_documento", "cliente_nome", "cliente_padrao"])
        nfce = gerar_nfce_local(
            venda,
            material_assinatura=self.material,
            qr_config=self.qr,
            codigo_numerico="12345678",
            tipo_emissao="9",
            justificativa_contingencia="SEFAZ indisponivel em teste",
        )

        dados = montar_dados_danfe_nfce(nfce, via="ESTABELECIMENTO")
        self.assertTrue(dados["imprimivel"])
        self.assertEqual(dados["via_texto"], "Via do Estabelecimento")
        self.assertTrue(dados["consumidor"]["identificado"])
        self.assertEqual(dados["consumidor"]["tipo_documento"], "CPF")
        self.assertEqual(dados["consumidor"]["nome"], "Cliente NFCe")
        self.assertIn("EMITIDA EM CONTINGENCIA", dados["mensagens"])
        self.assertIn("Pendente de autorizacao", dados["mensagens"])
        self.assertIsNone(dados["protocolo"])

        nfce.status = NFCeHub.STATUS_REJEITADA
        nfce.save(update_fields=["status"])
        rejeitada = montar_dados_danfe_nfce(nfce)
        self.assertFalse(rejeitada["imprimivel"])
        self.assertEqual(rejeitada["motivo_nao_imprimivel"], "NFCE_REJEITADA")

        nfce.status = NFCeHub.STATUS_ERRO_GERACAO
        nfce.save(update_fields=["status"])
        erro = montar_dados_danfe_nfce(nfce)
        self.assertFalse(erro["imprimivel"])
        self.assertEqual(erro["motivo_nao_imprimivel"], "NFCE_ERRO_GERACAO")

    def test_endpoint_danfe_autenticado_isola_hub_e_nao_expoe_xml_ou_segredo(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)

        resposta = self.client.get(f"/api/terminal/venda/{venda.venda_uuid}/danfe-nfce/")

        self.assertEqual(resposta.status_code, 200)
        conteudo = str(resposta.data)
        self.assertEqual(resposta.data["nfce_uuid"], str(nfce.nfce_uuid))
        self.assertNotIn("xml_assinado", resposta.data)
        self.assertNotIn("<NFe", conteudo)
        self.assertNotIn("PRIVATE KEY", conteudo)
        self.assertNotIn("CERTIFICATE", conteudo)

        venda.hub = criar_hub(retaguarda_hub_id=999, empresa_id=99, loja_id=99)
        venda.save(update_fields=["hub"])
        negada = self.client.get(f"/api/terminal/venda/{venda.venda_uuid}/danfe-nfce/")
        self.assertEqual(negada.status_code, 404)

    def test_sync_enfileira_venda_e_nfce_sem_segredos_e_em_ordem(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)

        enfileirar_nfce_atualizada(nfce)

        eventos = list(EventoSyncHub.objects.order_by("criado_em", "id"))
        self.assertEqual([e.tipo for e in eventos], ["VENDA_FINALIZADA", "NFCE_ATUALIZADA"])
        self.assertEqual(eventos[1].payload["nfce_uuid"], str(nfce.nfce_uuid))
        self.assertEqual(eventos[1].payload["venda_uuid"], str(venda.venda_uuid))
        self.assertEqual(eventos[1].payload["versao_evento"], 1)
        self.assertNotIn("senha", str(eventos[1].payload).lower())
        self.assertNotIn("private", str(eventos[1].payload).lower())

    def test_sync_worker_retry_processado_duplicado_conflito_e_processando_antigo(self):
        venda = self.venda_finalizada()
        enfileirar_venda_finalizada(venda)
        evento = EventoSyncHub.objects.get()

        class ClientErro:
            def sync_push(self, **kwargs):
                raise Exception("nao deveria")

        class ClientRetaguardaIndisponivel:
            def sync_push(self, **kwargs):
                from integracao.services.retaguarda import RetaguardaError
                raise RetaguardaError("offline")

        resultado = sincronizar_eventos_pendentes(self.hub, client=ClientRetaguardaIndisponivel())
        evento.refresh_from_db()
        self.assertEqual(resultado["erros"], 1)
        self.assertEqual(evento.status, EventoSyncHub.STATUS_ERRO)

        evento.proxima_tentativa_em = timezone.now() - timedelta(minutes=10)
        evento.status = EventoSyncHub.STATUS_PROCESSANDO
        evento.save(update_fields=["proxima_tentativa_em", "status"])

        class ClientOk:
            def __init__(self, status):
                self.status = status

            def sync_push(self, **kwargs):
                return {"resultados": [{"chave_idempotencia": evento.chave_idempotencia, "status": self.status}]}

        sincronizar_eventos_pendentes(self.hub, client=ClientOk("PROCESSADO"))
        evento.refresh_from_db()
        self.assertEqual(evento.status, EventoSyncHub.STATUS_SINCRONIZADO)

        conflito = _criar_ou_atualizar_evento(
            hub=self.hub,
            tipo="VENDA_FINALIZADA",
            chave="VENDA:CONFLITO",
            payload={"venda_uuid": str(venda.venda_uuid)},
            evento_uuid=uuid.uuid4(),
        )

        class ClientConflito:
            def sync_push(self, **kwargs):
                return {"resultados": [{"chave_idempotencia": conflito.chave_idempotencia, "status": "CONFLITO"}]}

        sincronizar_eventos_pendentes(self.hub, client=ClientConflito())
        conflito.refresh_from_db()
        self.assertEqual(conflito.status, EventoSyncHub.STATUS_CONFLITO)

    def test_sync_evento_sincronizado_nao_volta_pendente_e_payload_divergente_bloqueia(self):
        venda = self.venda_finalizada()
        evento = enfileirar_venda_finalizada(venda)
        evento.status = EventoSyncHub.STATUS_SINCRONIZADO
        evento.sincronizado_em = timezone.now()
        evento.save(update_fields=["status", "sincronizado_em"])

        mesmo = enfileirar_venda_finalizada(venda)

        self.assertEqual(mesmo.pk, evento.pk)
        mesmo.refresh_from_db()
        self.assertEqual(mesmo.status, EventoSyncHub.STATUS_SINCRONIZADO)
        payload_diferente = dict(mesmo.payload)
        payload_diferente["total"] = "999.99"
        with self.assertRaises(ValueError):
            _criar_ou_atualizar_evento(
                hub=self.hub,
                tipo=mesmo.tipo,
                chave=mesmo.chave_idempotencia,
                payload=payload_diferente,
                evento_uuid=mesmo.evento_uuid,
            )

    def test_sync_worker_marca_processando_dentro_de_atomic_e_envia_fora_do_lock(self):
        venda = self.venda_finalizada()
        enfileirar_venda_finalizada(venda)
        estados = {}

        class ClientObservador:
            def sync_push(self, **kwargs):
                evento = EventoSyncHub.objects.get()
                estados["durante_http"] = evento.status
                return {"resultados": [{"chave_idempotencia": evento.chave_idempotencia, "status": "DUPLICADO"}]}

        sincronizar_eventos_pendentes(self.hub, client=ClientObservador())

        evento = EventoSyncHub.objects.get()
        self.assertEqual(estados["durante_http"], EventoSyncHub.STATUS_PROCESSANDO)
        self.assertEqual(evento.status, EventoSyncHub.STATUS_SINCRONIZADO)

    def test_sync_nfce_nova_versao_mantem_uuid_e_muda_chave(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)
        enfileirar_nfce_atualizada(nfce)
        primeira = EventoSyncHub.objects.get(tipo="NFCE_ATUALIZADA")
        nfce.status = NFCeHub.STATUS_CONTINGENCIA
        nfce.sync_versao = 2
        nfce.save(update_fields=["status", "sync_versao"])

        enfileirar_nfce_atualizada(nfce)

        eventos = EventoSyncHub.objects.filter(tipo="NFCE_ATUALIZADA").order_by("chave_idempotencia")
        self.assertEqual(eventos.count(), 2)
        self.assertEqual({e.payload["nfce_uuid"] for e in eventos}, {str(nfce.nfce_uuid)})
        self.assertNotEqual(eventos[0].chave_idempotencia, eventos[1].chave_idempotencia)
        self.assertIn(":V:2", eventos[1].chave_idempotencia)

    def test_dev_nao_autoriza_nfce_nem_gera_protocolo_ficticio(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)

        resultado = SefazNFCeClientDesenvolvimento(SefazNFCeClient.AUTORIZADA).transmitir(nfce)

        self.assertTrue(resultado.simulacao)
        self.assertEqual(resultado.status, NFCeHub.STATUS_GERADA)
        self.assertEqual(resultado.protocolo, "")
        self.assertEqual(resultado.codigo, "")
        self.assertEqual(nfce.status, NFCeHub.STATUS_GERADA)
        self.assertEqual(nfce.protocolo, "")
        self.assertEqual(nfce.codigo_retorno, "")
        self.assertIsNone(nfce.autorizada_em)
        self.assertIn("SIMULACAO", resultado.mensagem)

    def test_provider_a1_carrega_pfx_e_erros_controlados(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "A1 Teste")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "A1 Teste")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        data = pkcs12.serialize_key_and_certificates(
            b"sysvar",
            key,
            cert,
            None,
            serialization.BestAvailableEncryption(b"1234"),
        )
        path = Path("data") / "test-a1.p12"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            material = NFCeMaterialProviderA1(path=str(path), password="1234").obter()
            self.assertIn(b"PRIVATE KEY", material.private_key_pem)
            with self.assertRaises(NFCeErroDominio) as erro:
                NFCeMaterialProviderA1(path=str(path), password="errada").obter()
            self.assertEqual(erro.exception.codigo, "A1_SENHA_INVALIDA")
        finally:
            path.unlink(missing_ok=True)
        with self.assertRaises(NFCeErroDominio) as ausente:
            NFCeMaterialProviderA1(path="C:/nao/existe/cert.p12", password="1234").obter()
        self.assertEqual(ausente.exception.codigo, "A1_ARQUIVO_NAO_ENCONTRADO")

    def test_retransmissao_contingencia_usa_mesmo_documento_e_autoriza(self):
        venda = self.venda_finalizada()
        nfce = gerar_nfce_local(
            venda,
            material_assinatura=self.material,
            qr_config=self.qr,
            codigo_numerico="12345678",
            tipo_emissao="9",
            justificativa_contingencia="SEFAZ indisponivel em teste",
        )
        numero = nfce.numero

        class ClientAutorizado:
            def transmitir(self, nfce):
                return ResultadoSefazNFCe(
                    status=SefazNFCeClient.AUTORIZADA,
                    codigo="100",
                    mensagem="Autorizado",
                    protocolo="135",
                    autorizada_em=timezone.now(),
                    xml_protocolo="<procNFe />",
                )

        with self.captureOnCommitCallbacks(execute=True):
            retransmitir_nfces_pendentes(self.hub, sefaz_client=ClientAutorizado())

        nfce.refresh_from_db()
        self.assertEqual(nfce.status, NFCeHub.STATUS_AUTORIZADA)
        self.assertEqual(nfce.numero, numero)
        self.assertEqual(nfce.protocolo, "135")
        self.assertTrue(EventoSyncHub.objects.filter(tipo="NFCE_ATUALIZADA").exists())

    def test_adapter_real_interpreta_transporte_mockado(self):
        venda = self.venda_finalizada()
        nfce = self.gerar_nfce(venda)
        chamadas = []

        def transporte(url, xml, timeout, material):
            chamadas.append((url, xml, timeout, material))
            return {
                "status": "AUTORIZADA",
                "codigo": "100",
                "mensagem": "Autorizado",
                "protocolo": "135",
                "autorizada_em": timezone.now(),
                "xml_protocolo": "<procNFe />",
            }

        client = SefazNFCeClientReal(autorizacao_url="https://sefaz.test/autorizacao", transport=transporte)
        resultado = client.transmitir(nfce)
        self.assertEqual(resultado.status, SefazNFCeClient.AUTORIZADA)
        self.assertEqual(resultado.protocolo, "135")
        self.assertEqual(chamadas[0][0], "https://sefaz.test/autorizacao")

        rejeitado = SefazNFCeClientReal(
            autorizacao_url="https://sefaz.test/autorizacao",
            transport=lambda *args: {"status": "REJEITADA", "codigo": "204", "mensagem": "Rejeicao"},
        ).transmitir(nfce)
        self.assertEqual(rejeitado.status, SefazNFCeClient.REJEITADA)
        self.assertEqual(rejeitado.codigo, "204")

        timeout = SefazNFCeClientReal(
            autorizacao_url="https://sefaz.test/autorizacao",
            transport=lambda *args: (_ for _ in ()).throw(TimeoutError()),
        ).transmitir(nfce)
        self.assertEqual(timeout.status, SefazNFCeClient.TIMEOUT)

        with self.assertRaises(NFCeErroDominio):
            SefazNFCeClientReal(autorizacao_url="").transmitir(nfce)

    def _digest_inf_nfe(self, xml, chave):
        root = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(remove_blank_text=True))
        inf = root.xpath("//*[@Id=$id]", id=f"NFe{chave}")[0]
        canonico = etree.tostring(inf, method="c14n", exclusive=False, with_comments=False)
        return base64.b64encode(hashlib.sha1(canonico).digest()).decode("ascii")

    def _verificar_assinatura_qr_offline(self, partes, material):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        cert = x509.load_pem_x509_certificate(material.certificate_pem)
        cert.public_key().verify(
            base64.b64decode(partes[7]),
            "|".join(partes[:7]).encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
