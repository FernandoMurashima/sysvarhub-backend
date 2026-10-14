import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core.models import NFCeHub, VendaDevolucaoHub, VendaDevolucaoItemHub, VendaHub, VendaItemHub
from core.services.devolucoes import consultar_venda_para_devolucao_por_documento_local
from core.services.vendas import VendaNotFoundError
from core.tests_caixa import criar_hub
from core.tests_vendas import VendaHubTestMixin


class ConsultaVendaDevolucaoLocalTests(VendaHubTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.venda = VendaHub.objects.create(
            hub=self.hub,
            sessao_caixa=self.sessao_caixa,
            terminal=self.terminal,
            operador_criacao=self.operador,
            sessao_operador_criacao=self.sessao_operador,
            status=VendaHub.STATUS_FINALIZADA,
            documento="VE0110000001",
            finalizada_em=timezone.now(),
            cliente_retaguarda_id=123,
            cliente_uuid=uuid.uuid4(),
            cliente_nome="Cliente Teste",
            subtotal=Decimal("60.00"),
            total=Decimal("60.00"),
        )
        self.item = VendaItemHub.objects.create(
            venda=self.venda,
            catalogo_item=self.catalogo_item,
            retaguarda_produto_id=self.catalogo_item.retaguarda_produto_id,
            retaguarda_sku_id=self.catalogo_item.retaguarda_sku_id,
            descricao=self.catalogo_item.descricao,
            quantidade=3,
            preco_unitario=Decimal("20.0000"),
            total_item=Decimal("60.00"),
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
            terminal_inclusao=self.terminal,
        )

    def consultar(self, documento):
        return consultar_venda_para_devolucao_por_documento_local(self.terminal, documento)

    def test_busca_ve_sem_espacos(self):
        self.assertEqual(self.consultar("VE0110000001")["uuid"], str(self.venda.venda_uuid))

    def test_busca_ve_com_espacos(self):
        self.assertEqual(self.consultar("VE 011 0000001")["uuid"], str(self.venda.venda_uuid))

    def test_venda_de_outro_hub_bloqueada(self):
        self.terminal.hub = criar_hub(retaguarda_hub_id=8, loja_id=42)
        for documento in ("VE0110000001", "VE 011 0000001", str(self.venda.venda_uuid)):
            with self.subTest(documento=documento), self.assertRaises(VendaNotFoundError):
                self.consultar(documento)

    def test_venda_nao_finalizada_bloqueada(self):
        for status in (VendaHub.STATUS_ABERTA, VendaHub.STATUS_CANCELADA):
            VendaHub.objects.filter(pk=self.venda.pk).update(status=status)
            for documento in ("VE0110000001", "VE 011 0000001", str(self.venda.venda_uuid)):
                with self.subTest(status=status, documento=documento), self.assertRaises(VendaNotFoundError):
                    self.consultar(documento)

    def test_busca_uuid_e_identificadores_nfce_preservada_sem_documento_comercial(self):
        VendaHub.objects.filter(pk=self.venda.pk).update(documento=None)
        nfce = NFCeHub.objects.create(
            hub=self.hub,
            venda=self.venda,
            ambiente="2",
            serie=1,
            numero=123,
            codigo_numerico="12345678",
            digito_verificador="0",
            chave_acesso="1" * 44,
            protocolo="123456789012345",
            emitida_em=timezone.now(),
        )
        for documento in (str(self.venda.venda_uuid), str(nfce.numero), nfce.chave_acesso, nfce.protocolo):
            with self.subTest(documento=documento):
                resposta = self.consultar(documento)
                self.assertEqual(resposta["uuid"], str(self.venda.venda_uuid))
                self.assertIsNone(resposta["documento"])

    def test_resposta_preserva_documento_cliente_total_itens_e_quantidades(self):
        devolucao = VendaDevolucaoHub.objects.create(
            hub=self.hub,
            venda_origem=self.venda,
            operador=self.operador,
            terminal=self.terminal,
            valor_total=Decimal("20.00"),
            finalizada_em=timezone.now(),
        )
        VendaDevolucaoItemHub.objects.create(
            devolucao=devolucao,
            venda_item=self.item,
            catalogo_item=self.catalogo_item,
            retaguarda_produto_id=self.item.retaguarda_produto_id,
            retaguarda_sku_id=self.item.retaguarda_sku_id,
            descricao=self.item.descricao,
            quantidade=1,
            preco_unitario=self.item.preco_unitario,
            total_item=Decimal("20.00"),
        )
        self.assertEqual(self.consultar("VE0110000001"), {
            "uuid": str(self.venda.venda_uuid),
            "documento": "VE0110000001",
            "cliente": {"id": 123, "uuid": str(self.venda.cliente_uuid), "nome": "Cliente Teste"},
            "total": "60.00",
            "itens": [{
                "item_uuid": str(self.item.item_uuid),
                "sku_id": self.item.retaguarda_sku_id,
                "descricao": self.item.descricao,
                "quantidade": 3,
                "quantidade_devolvida": 1,
                "quantidade_disponivel": 2,
                "preco_unitario": "20.0000",
                "total_item": "60.00",
            }],
        })

    def test_endpoint_consulta_ve_com_central_offline(self):
        with (
            patch("core.services.devolucoes.calcular_status_central", return_value={"status": "OFFLINE"}),
            patch("core.services.devolucoes.RetaguardaClient") as client_cls,
        ):
            resposta = self.client.get("/api/terminal/devolucoes/vendas/", {"documento": "VE 011 0000001"})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["venda"]["documento"], "VE0110000001")
        self.assertEqual(resposta.data["venda"]["uuid"], str(self.venda.venda_uuid))
        client_cls.return_value.devolucao_venda.assert_not_called()
