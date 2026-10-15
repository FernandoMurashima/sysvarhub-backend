import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core import tests_pagamentos as pagamentos_tests
from core.models import (
    CashbackMovimentoHub, EventoSyncHub, FaixaNumeracaoDevolucaoHub, NFCeHub,
    ValeTrocaHub, ValeTrocaMovimentoHub, VendaDevolucaoHub, VendaDevolucaoItemHub,
    VendaHub, VendaItemHub, VendaPagamentoHub,
)
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


class CreditoLiquidoCashbackDevolucaoTests(pagamentos_tests.PagamentoHubTestMixin, TestCase):
    aplicar_credito = pagamentos_tests.CreditoProvisorioTrocaTests.aplicar_credito
    _client_sync = pagamentos_tests.BeneficiosHubTests._client_sync
    _sincronizar_devolucao_confirmada = pagamentos_tests.BeneficiosHubTests._sincronizar_devolucao_confirmada

    def setUp(self):
        super().setUp()
        self.cliente = self.criar_cliente()
        FaixaNumeracaoDevolucaoHub.objects.create(hub=self.hub, inicio=1, fim=100, proximo_numero=1)

    def vender(self, preco="100.00", quantidade=1, cashback="30.00"):
        self.catalogo_item.preco_venda = Decimal(preco)
        self.catalogo_item.save()
        venda_uuid = self.criar_venda_com_item(quantidade)
        self.assertEqual(self.put_cliente(self.cliente).status_code, 200)
        if Decimal(cashback):
            self.criar_cashback_disponivel(self.cliente, saldo="0.00")
            with patch("core.services.cashback_online.RetaguardaClient") as central:
                central.return_value.cashback_reservar_venda.side_effect = self.reserva_central()
                resposta = self.pagar_cashback_online(venda_uuid, cashback)
            self.assertEqual(resposta.status_code, 201, resposta.data)
        restante = Decimal(preco) * quantidade - Decimal(cashback)
        if restante:
            self.assertEqual(self.pagar(venda_uuid, self.pix, str(restante)).status_code, 201)
        with self.captureOnCommitCallbacks(execute=True):
            resposta = self.finalizar(venda_uuid)
        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        self.item = self.venda.itens.get()

    def devolver(self, quantidade=1, devolucao_uuid=None):
        devolucao_uuid = devolucao_uuid or uuid.uuid4()
        with self.captureOnCommitCallbacks(execute=True):
            resposta = self.client.post("/api/terminal/devolucoes/finalizar/", {
                "venda_uuid": str(self.venda.venda_uuid),
                "devolucao_uuid": str(devolucao_uuid),
                "itens": [{"item_uuid": str(self.item.item_uuid), "quantidade": quantidade}],
            }, format="json")
        self.assertEqual(resposta.status_code, 201, resposta.data)
        return VendaDevolucaoHub.objects.get(devolucao_uuid=devolucao_uuid)

    def confirmar(self, devolucao, liquido, *, retry=False, **consumo):
        EventoSyncHub.objects.filter(
            chave_idempotencia=f"VENDA:{self.venda.venda_uuid}:FINALIZADA",
        ).update(status=EventoSyncHub.STATUS_SINCRONIZADO)
        evento = EventoSyncHub.objects.get(
            chave_idempotencia=f"DEVOLUCAO:{devolucao.devolucao_uuid}:FINALIZADA",
        )
        if retry:
            evento.status = EventoSyncHub.STATUS_PENDENTE
            evento.sincronizado_em = None
            evento.save(update_fields=["status", "sincronizado_em"])
        resultado = {
            "devolucao_retaguarda_id": 500 + devolucao.pk,
            "documento": devolucao.documento,
            "valor_total": liquido,
        }
        if Decimal(liquido):
            resultado.update(vale_documento=f"VT{devolucao.pk:07}", vale_retaguarda_id=900 + devolucao.pk)
        resultado.update(consumo)
        self._sincronizar_devolucao_confirmada(evento, status="DUPLICADO" if retry else "PROCESSADO", **resultado)
        evento.refresh_from_db()
        self.assertEqual(evento.status, EventoSyncHub.STATUS_SINCRONIZADO, evento.ultimo_erro)
        devolucao.refresh_from_db()
        self.assertIsNotNone(devolucao.confirmado_central_em)
        return evento

    def test_integral_emite_somente_setenta_sem_restituicao_local(self):
        self.vender()
        movimentos_cashback = CashbackMovimentoHub.objects.count()
        devolucao = self.devolver()
        vale = devolucao.vale_troca
        self.assertEqual(devolucao.valor_total, Decimal("100.00"))
        self.assertEqual((vale.valor_original, vale.saldo), (Decimal("70.00"), Decimal("70.00")))
        self.assertEqual(devolucao.dados_origem["cashback_restituicao_prevista"], "30.00")
        self.assertEqual(list(vale.movimentos.values_list("tipo", "valor")), [("CREDITO", Decimal("70.00"))])
        self.assertEqual(CashbackMovimentoHub.objects.count(), movimentos_cashback)
        evento = EventoSyncHub.objects.get(chave_idempotencia=f"DEVOLUCAO:{devolucao.devolucao_uuid}:FINALIZADA")
        self.assertEqual(evento.payload["valor_total"], "100.00")
        self.assertEqual(evento.payload["vale_troca"]["valor"], "70.00")

    def test_parcial_restitui_cashback_proporcional(self):
        self.vender(preco="50.00", quantidade=2)
        devolucao = self.devolver()
        self.assertEqual(devolucao.valor_total, Decimal("50.00"))
        self.assertEqual(devolucao.vale_troca.saldo, Decimal("35.00"))
        self.assertEqual(devolucao.dados_origem["cashback_restituicao_prevista"], "15.00")

    def test_parciais_sucessivas_preservam_total_e_retry(self):
        self.vender(preco="33.33", quantidade=3)
        devolucoes = [self.devolver() for _ in range(3)]
        self.assertEqual(sum((d.vale_troca.valor_original for d in devolucoes), Decimal("0.00")), Decimal("69.99"))
        self.assertEqual(sum((Decimal(d.dados_origem["cashback_restituicao_prevista"]) for d in devolucoes), Decimal("0.00")), Decimal("30.00"))
        self.catalogo_item.refresh_from_db()
        estoque = self.catalogo_item.estoque_fisico
        repetida = self.devolver(devolucao_uuid=devolucoes[-1].devolucao_uuid)
        self.assertEqual(repetida.pk, devolucoes[-1].pk)
        self.assertEqual(ValeTrocaHub.objects.count(), 3)
        self.assertEqual(ValeTrocaMovimentoHub.objects.filter(tipo="CREDITO").count(), 3)
        self.catalogo_item.refresh_from_db()
        self.assertEqual(self.catalogo_item.estoque_fisico, estoque)

    def test_arredondamento_acumulado_igual_ao_da_central(self):
        self.vender(preco="0.01", quantidade=4, cashback="0.01")
        devolucoes = [self.devolver() for _ in range(4)]
        self.assertEqual([d.dados_origem["cashback_restituicao_prevista"] for d in devolucoes], ["0.00", "0.00", "0.01", "0.00"])
        self.assertEqual([d.dados_origem["credito_liquido_previsto"] for d in devolucoes], ["0.01", "0.01", "0.00", "0.01"])
        self.assertEqual(sum(ValeTrocaHub.objects.values_list("valor_original", flat=True)), Decimal("0.03"))

    def test_desconto_fracionado_por_unidade_compativel_com_central(self):
        self.vender(preco="10.00", quantidade=3, cashback="3.00")
        # Snapshot de venda com desconto de R$ 1 distribuído por três unidades.
        self.item.desconto = Decimal("1.00")
        self.item.total_item = Decimal("29.00")
        self.item.save(update_fields=["desconto", "total_item"])
        self.venda.total = Decimal("29.00")
        self.venda.save(update_fields=["total"])
        self.venda.pagamentos.filter(tipo="PIX").update(valor=Decimal("26.00"))
        devolucao = self.devolver(2)
        self.assertEqual(devolucao.valor_total, Decimal("19.33"))
        self.assertEqual(devolucao.dados_origem["cashback_restituicao_prevista"], "2.00")
        self.assertEqual(devolucao.vale_troca.saldo, Decimal("17.34"))
        ultima = self.devolver()
        self.assertEqual(ultima.vale_troca.saldo, Decimal("8.67"))

    def test_confirmacao_anterior_considera_credito_oficial_original(self):
        self.vender(preco="50.00", quantidade=2)
        primeira = self.devolver()
        self.confirmar(primeira, "34.99")
        # O saldo restante não representa o crédito original nem a restituição.
        ValeTrocaHub.objects.filter(devolucao=primeira).update(saldo=Decimal("4.99"))
        segunda = self.devolver()
        self.assertEqual(segunda.dados_origem["cashback_restituicao_prevista"], "14.99")
        self.assertEqual(segunda.vale_troca.saldo, Decimal("35.01"))

    def test_cashback_integral_nao_disponibiliza_vale_e_confirma_zero(self):
        self.vender(cashback="100.00")
        devolucao = self.devolver()
        self.assertFalse(ValeTrocaHub.objects.filter(devolucao=devolucao).exists())
        self.assertFalse(ValeTrocaMovimentoHub.objects.exists())
        evento = self.confirmar(devolucao, "0.00")
        self.assertIsNone(evento.payload["vale_troca"])
        self.confirmar(devolucao, "0.00", retry=True)
        self.assertEqual(VendaDevolucaoHub.objects.count(), 1)
        self.assertFalse(ValeTrocaHub.objects.exists())

    def test_consumo_provisorio_limitado_ao_liquido_e_idempotente(self):
        self.vender()
        self.devolucao = self.devolver()
        self.vale = self.devolucao.vale_troca
        troca = self.criar_venda_com_item()
        self.assertEqual(self.put_cliente(self.cliente).status_code, 200)
        for tipo in (None, "TROCA", "VALE_TROCA"):
            with self.subTest(tipo=tipo):
                self.assertEqual(self.aplicar_credito(troca, "70.01", tipo=tipo).status_code, 409)
        self.assertFalse(VendaPagamentoHub.objects.filter(venda__venda_uuid=troca).exists())
        operacao = uuid.uuid4()
        self.assertEqual(self.aplicar_credito(troca, "70.00", operacao=operacao).status_code, 201)
        self.assertEqual(self.aplicar_credito(troca, "70.00", operacao=operacao).status_code, 200)
        self.assertEqual(self.pagar(troca, self.pix, "30.00").status_code, 201)
        self.assertEqual(self.finalizar(troca).status_code, 200)
        self.assertEqual(self.finalizar(troca).status_code, 200)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("0.00"))
        self.assertEqual(list(self.vale.movimentos.filter(tipo="USO").values_list("valor", flat=True)), [Decimal("70.00")])
        consumo = {
            "venda_troca_uuid": troca, "operacao_uuid": str(operacao),
            "valor_consumido_offline": "70.00", "vale_reserva_id": 801,
        }
        self.confirmar(self.devolucao, "70.00", **consumo)
        self.confirmar(self.devolucao, "70.00", retry=True, **consumo)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("0.00"))
        self.assertFalse(self.vale.provisorio)
        self.assertEqual(self.vale.movimentos.count(), 2)
        pagamento = VendaPagamentoHub.objects.get(venda__venda_uuid=troca, tipo="VALE_TROCA")
        self.assertEqual(pagamento.vale_troca_reserva_id, 801)

    def test_confirmacao_central_e_retry_nao_duplicam_credito(self):
        self.vender()
        devolucao = self.devolver()
        evento = self.confirmar(devolucao, "70.00")
        snapshot = (evento.evento_uuid, evento.chave_idempotencia, evento.payload)
        self.confirmar(devolucao, "70.00", retry=True)
        evento.refresh_from_db()
        self.assertEqual((evento.evento_uuid, evento.chave_idempotencia, evento.payload), snapshot)
        vale = ValeTrocaHub.objects.get(devolucao=devolucao)
        self.assertEqual((vale.valor_original, vale.saldo), (Decimal("70.00"), Decimal("70.00")))
        self.assertFalse(vale.provisorio)
        self.assertEqual(vale.movimentos.count(), 1)

    def test_sem_cashback_preserva_credito_integral(self):
        self.vender(cashback="0.00")
        devolucao = self.devolver()
        self.assertEqual(devolucao.vale_troca.saldo, Decimal("100.00"))
        self.assertEqual(devolucao.dados_origem, {})
        self.confirmar(devolucao, "100.00")
        self.assertEqual(ValeTrocaHub.objects.get(devolucao=devolucao).saldo, Decimal("100.00"))

    def test_historico_local_conta_sem_incluir_pagamentos_removidos(self):
        self.vender()
        self.venda.pagamentos.filter(tipo="CASHBACK").update(cashback_origem=VendaPagamentoHub.CASHBACK_ORIGEM_LOCAL)
        removido = self.criar_cashback_local_historico(str(self.venda.venda_uuid), "20.00")
        removido.status = VendaPagamentoHub.STATUS_REMOVIDO
        removido.save(update_fields=["status"])
        devolucao = self.devolver()
        self.assertEqual(devolucao.vale_troca.saldo, Decimal("70.00"))
        self.assertEqual(self.venda.pagamentos.filter(tipo="CASHBACK", cashback_origem=VendaPagamentoHub.CASHBACK_ORIGEM_LOCAL).count(), 2)
