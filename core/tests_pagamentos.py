import uuid
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import (
    CatalogoItemHub,
    CashbackConfigHub,
    CashbackMovimentoHub,
    ConfiguracaoFiscalHub,
    EventoSyncHub,
    EstoqueMovimentoHub,
    FormaPagamentoFiscalMapHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    SessaoCaixaHub,
    VendaEventoHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
    VendaPagamentoParcelaHub,
    VendedorHub,
    NFCeHub,
    PromocaoHub,
    ValeTrocaHub,
    VendaDevolucaoHub,
)
from core.services.caixa import fechar_caixa
from core.services.sync import enfileirar_venda_finalizada, sincronizar_eventos_pendentes
from core.services.terminais import configurar_terminal
from core.services.vendas import calcular_disponivel_local, listar_formas_pagamento
from core.tests_caixa import criar_hub
from core.tests_vendas import VendaHubTestMixin


FISCAL_ITEM_NFCE = {
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


class PagamentoHubTestMixin(VendaHubTestMixin):
    def setUp(self):
        super().setUp()
        self.dinheiro = self.criar_forma("DIN", "DINHEIRO")
        self.pix = self.criar_forma("PIX", "PIX")
        self.cartao = self.criar_forma("CAR", "CARTAO")
        self.tef = self.criar_forma("TEF", "CARTAO", tef_habilitado=True)

    def criar_forma(self, codigo, tipo, **overrides):
        dados = {
            "hub": self.hub,
            "retaguarda_id": overrides.pop("retaguarda_id", 1000 + FormaPagamentoHub.objects.count()),
            "codigo": codigo,
            "descricao": codigo,
            "tipo": tipo,
            "num_parcelas": 1,
            "ativo": True,
            "gera_recebivel_bancario": False,
            "prazo_credito_dias": 0,
            "taxa_percentual": Decimal("0.0000"),
            "taxa_fixa": Decimal("0.00"),
            "tef_habilitado": False,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        forma = FormaPagamentoHub.objects.create(**dados)
        FormaPagamentoParcelaHub.objects.create(
            forma=forma,
            ordem=1,
            dias=0,
            percentual=Decimal("1.000000"),
            valor_fixo=None,
            sincronizado_em=timezone.now(),
        )
        return forma

    def criar_venda_com_item(self, quantidade=1):
        resposta = self.post_item({"quantidade": quantidade})
        vendedor = self.criar_vendedor_pagamento()
        self.client.put(
            "/api/terminal/venda/vendedor/",
            {"vendedor_id": vendedor.retaguarda_id},
            format="json",
        )
        return resposta.data["venda"]["uuid"]

    def criar_vendedor_pagamento(self, retaguarda_id=9901):
        vendedor, _created = VendedorHub.objects.get_or_create(
            hub=self.hub,
            retaguarda_id=retaguarda_id,
            defaults={
                "matricula": "009901",
                "nome": "Vendedor Pagamento",
                "apelido": "Vend Pag",
                "cargo_retaguarda_id": 5,
                "cargo_codigo": "VENDEDOR",
                "cargo_descricao": "Vendedor",
                "comissionado": True,
                "comissao_percentual": Decimal("3.00"),
                "ativo": True,
                "situacao": "ATIVO",
                "participa_vendas": True,
                "presente_retaguarda": True,
                "sincronizado_em": timezone.now(),
            },
        )
        return vendedor

    def pagar(self, venda_uuid, forma=None, valor="199.90", operacao_uuid=None):
        return self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(operacao_uuid or uuid.uuid4()),
                "forma_pagamento_id": (forma or self.dinheiro).id,
                "valor": valor,
                "autorizacao": "",
            },
            format="json",
        )

    def finalizar(self, venda_uuid):
        return self.client.post(
            "/api/terminal/venda/finalizar/",
            {"venda_uuid": venda_uuid},
            format="json",
        )


class FormasPagamentoApiTests(PagamentoHubTestMixin, TestCase):
    def test_listar_formas_ativas(self):
        resposta = self.client.get("/api/terminal/formas-pagamento/")

        self.assertEqual(resposta.status_code, 200)
        codigos = [forma["codigo"] for forma in resposta.data["formas"]]
        self.assertIn("DIN", codigos)
        self.assertEqual(resposta.data["formas"][0]["parcelas"][0]["percentual"], "1.000000")

    def test_nao_listar_forma_inativa(self):
        self.pix.ativo = False
        self.pix.save()

        resposta = self.client.get("/api/terminal/formas-pagamento/")

        self.assertNotIn("PIX", [forma["codigo"] for forma in resposta.data["formas"]])

    def test_forma_de_outro_hub_nao_aparece(self):
        outro_hub = criar_hub()
        self.criar_forma("OUT", "DINHEIRO", hub=outro_hub, retaguarda_id=9000)

        resposta = self.client.get("/api/terminal/formas-pagamento/")

        self.assertNotIn("OUT", [forma["codigo"] for forma in resposta.data["formas"]])

    def test_listagem_das_formas_nao_gera_n_mais_um_para_parcelas(self):
        for indice in range(3):
            forma = self.criar_forma(f"F{indice}", "PIX", retaguarda_id=8000 + indice)
            FormaPagamentoParcelaHub.objects.create(
                forma=forma,
                ordem=2,
                dias=30,
                percentual=Decimal("0.500000"),
                valor_fixo=None,
                sincronizado_em=timezone.now(),
            )

        with CaptureQueriesContext(connection) as contexto:
            payload = listar_formas_pagamento(self.terminal)

        self.assertLessEqual(len(contexto), 2)
        self.assertGreaterEqual(len(payload["formas"]), 3)


class BeneficiosHubTests(PagamentoHubTestMixin, TestCase):
    def test_promocao_local_aplica_desconto_e_preserva_snapshot(self):
        PromocaoHub.objects.create(
            hub=self.hub,
            retaguarda_id=77,
            nome="Promo 10",
            tipo=PromocaoHub.TIPO_PERCENTUAL,
            valor=Decimal("10.0000"),
            sku_retaguarda_id=self.catalogo_item.retaguarda_sku_id,
            ativo=True,
            sincronizado_em=timezone.now(),
        )

        resposta = self.post_item()
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(item.desconto, Decimal("19.99"))
        self.assertEqual(item.total_item, Decimal("179.91"))
        self.assertEqual(resposta.data["venda"]["itens"][0]["promocao"]["id"], 77)

    def test_promocao_recalcula_ao_incrementar_mesmo_sku(self):
        PromocaoHub.objects.create(
            hub=self.hub,
            retaguarda_id=78,
            nome="Promo 10",
            tipo=PromocaoHub.TIPO_PERCENTUAL,
            valor=Decimal("10.0000"),
            sku_retaguarda_id=self.catalogo_item.retaguarda_sku_id,
            ativo=True,
            sincronizado_em=timezone.now(),
        )

        self.post_item()
        resposta = self.post_item()
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(item.quantidade, 2)
        self.assertEqual(item.desconto, Decimal("39.98"))
        self.assertEqual(item.total_item, Decimal("359.82"))

    def test_cashback_e_vale_validam_saldo_e_geram_movimentos(self):
        cliente = self.criar_cliente()
        CashbackConfigHub.objects.create(
            hub=self.hub,
            retaguarda_id=1,
            ativo=True,
            percentual=Decimal("5.0000"),
            valor_minimo_uso=Decimal("1.00"),
            sincronizado_em=timezone.now(),
        )
        CashbackMovimentoHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            tipo=CashbackMovimentoHub.TIPO_CREDITO,
            valor=Decimal("30.00"),
        )
        vale = ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-1",
            valor_original=Decimal("40.00"),
            saldo=Decimal("40.00"),
            status=ValeTrocaHub.STATUS_ABERTO,
        )
        cashback = self.criar_forma("CBK", "CASHBACK")
        troca = self.criar_forma("TRO", "TROCA")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        self.assertEqual(self.pagar(venda_uuid, cashback, "30.00").status_code, 201)
        resposta_troca = self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(uuid.uuid4()),
                "forma_pagamento_id": troca.id,
                "valor": "40.00",
                "autorizacao": "VT-1",
            },
            format="json",
        )
        self.assertEqual(resposta_troca.status_code, 201)
        self.assertEqual(self.pagar(venda_uuid, self.dinheiro, "129.90").status_code, 201)
        self.assertEqual(self.finalizar(venda_uuid).status_code, 200)

        vale.refresh_from_db()
        self.assertEqual(vale.saldo, Decimal("0.00"))
        self.assertTrue(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_DEBITO).exists())
        self.assertTrue(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_CREDITO).exists())

    def test_cashback_respeita_limite_percentual_e_nao_gera_troco(self):
        cliente = self.criar_cliente()
        CashbackConfigHub.objects.create(
            hub=self.hub,
            retaguarda_id=2,
            ativo=True,
            percentual=Decimal("0.0000"),
            limite_uso_percentual=Decimal("50.0000"),
            sincronizado_em=timezone.now(),
        )
        CashbackMovimentoHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            tipo=CashbackMovimentoHub.TIPO_CREDITO,
            valor=Decimal("500.00"),
        )
        cashback = self.criar_forma("CB2", "CASHBACK")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        limite = self.pagar(venda_uuid, cashback, "120.00")
        self.assertEqual(limite.status_code, 409)
        permitido = self.pagar(venda_uuid, cashback, "99.95")
        self.assertEqual(permitido.status_code, 201)
        troco = self.pagar(venda_uuid, cashback, "100.00")
        self.assertEqual(troco.status_code, 409)

    def test_cashback_credito_usa_base_parcial_de_itens_que_acumulam(self):
        cliente = self.criar_cliente()
        CashbackConfigHub.objects.create(
            hub=self.hub,
            retaguarda_id=3,
            ativo=True,
            percentual=Decimal("10.0000"),
            sincronizado_em=timezone.now(),
        )
        PromocaoHub.objects.create(
            hub=self.hub,
            retaguarda_id=88,
            nome="Sem cashback",
            tipo=PromocaoHub.TIPO_VALOR_FIXO,
            valor=Decimal("0.0000"),
            escopo=PromocaoHub.ESCOPO_PRODUTO,
            produto_ids=[self.catalogo_item.retaguarda_produto_id],
            acumula_cashback=False,
            sincronizado_em=timezone.now(),
        )
        outro = self.criar_catalogo_item(10826, retaguarda_produto_id=2051, ean13="7892701000014")
        venda_uuid = self.criar_venda_com_item()
        self.post_item({"sku_id": outro.retaguarda_sku_id})
        self.put_cliente(cliente)
        self.assertEqual(self.pagar(venda_uuid, self.dinheiro, "399.80").status_code, 201)
        self.assertEqual(self.finalizar(venda_uuid).status_code, 200)

        credito = CashbackMovimentoHub.objects.get(tipo=CashbackMovimentoHub.TIPO_CREDITO)
        self.assertEqual(credito.valor, Decimal("19.99"))

    def test_promocao_aplica_escopos_todos_produto_colecao_grupo_subgrupo(self):
        casos = [
            (PromocaoHub.ESCOPO_TODOS, {}),
            (PromocaoHub.ESCOPO_PRODUTO, {"produto_ids": [self.catalogo_item.retaguarda_produto_id]}),
            (PromocaoHub.ESCOPO_COLECAO, {"colecao_ids": [10], "catalogo": {"colecao_retaguarda_id": 10}}),
            (PromocaoHub.ESCOPO_GRUPO, {"grupo_ids": [20], "catalogo": {"grupo_retaguarda_id": 20}}),
            (PromocaoHub.ESCOPO_SUBGRUPO, {"subgrupo_ids": [30], "catalogo": {"subgrupo_retaguarda_id": 30}}),
        ]
        for indice, (escopo, extra) in enumerate(casos, start=1):
            VendaEventoHub.objects.all().delete()
            VendaItemHub.objects.all().delete()
            VendaHub.objects.all().delete()
            PromocaoHub.objects.all().delete()
            for campo, valor in extra.get("catalogo", {}).items():
                setattr(self.catalogo_item, campo, valor)
            self.catalogo_item.save()
            PromocaoHub.objects.create(
                hub=self.hub,
                retaguarda_id=200 + indice,
                nome=f"Promo {escopo}",
                tipo=PromocaoHub.TIPO_PERCENTUAL,
                valor=Decimal("10.0000"),
                escopo=escopo,
                produto_ids=extra.get("produto_ids", []),
                colecao_ids=extra.get("colecao_ids", []),
                grupo_ids=extra.get("grupo_ids", []),
                subgrupo_ids=extra.get("subgrupo_ids", []),
                sincronizado_em=timezone.now(),
            )
            resposta = self.post_item()
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(VendaItemHub.objects.get().desconto, Decimal("19.99"))

    def test_devolucao_gera_vale_local_e_evento_sync(self):
        cliente = self.criar_cliente()
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.assertEqual(self.pagar(venda_uuid, self.dinheiro, "199.90").status_code, 201)
        self.assertEqual(self.finalizar(venda_uuid).status_code, 200)
        item = VendaItemHub.objects.get()

        with self.captureOnCommitCallbacks(execute=True):
            resposta = self.client.post(
                "/api/terminal/devolucoes/finalizar/",
                {
                    "venda_uuid": venda_uuid,
                    "motivo": "Troca de tamanho",
                    "itens": [{"item_uuid": str(item.item_uuid), "quantidade": 1}],
                },
                format="json",
            )

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(VendaDevolucaoHub.objects.count(), 1)
        self.assertEqual(ValeTrocaHub.objects.filter(documento__startswith="VT-HUB-").count(), 1)
        self.assertEqual(EventoSyncHub.objects.filter(tipo="DEVOLUCAO_FINALIZADA").count(), 1)

    def test_consulta_beneficios_nao_bloqueia_e_separa_saldos_offline_retaguarda(self):
        cliente = self.criar_cliente(cashback_saldo_retaguarda=Decimal("80.00"))
        CashbackMovimentoHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            tipo=CashbackMovimentoHub.TIPO_CREDITO,
            valor=Decimal("30.00"),
        )
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-LOCAL",
            valor_original=Decimal("10.00"),
            saldo=Decimal("10.00"),
        )
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-RET",
            valor_original=Decimal("20.00"),
            saldo=Decimal("20.00"),
            sincronizado_em=timezone.now(),
        )

        resposta = self.client.get(f"/api/terminal/clientes/{cliente.cliente_uuid}/beneficios/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["cashback"]["saldo_retaguarda"], "80.00")
        self.assertEqual(resposta.data["cashback"]["saldo_offline_utilizavel"], "30.00")
        vales = {vale["documento"]: vale for vale in resposta.data["vales_troca"]}
        self.assertTrue(vales["VT-LOCAL"]["utilizavel_offline"])
        self.assertFalse(vales["VT-RET"]["utilizavel_offline"])

    def test_vale_retaguarda_nao_pode_ser_consumido_offline(self):
        cliente = self.criar_cliente()
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-RET",
            valor_original=Decimal("40.00"),
            saldo=Decimal("40.00"),
            sincronizado_em=timezone.now(),
        )
        troca = self.criar_forma("TRO", "TROCA")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        resposta = self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(uuid.uuid4()),
                "forma_pagamento_id": troca.id,
                "valor": "10.00",
                "autorizacao": "VT-RET",
            },
            format="json",
        )

        self.assertEqual(resposta.status_code, 409)

    def test_sync_confirmado_centraliza_cashback_e_vale_local(self):
        cliente = self.criar_cliente()
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        movimento = CashbackMovimentoHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            venda=VendaHub.objects.get(venda_uuid=venda_uuid),
            tipo=CashbackMovimentoHub.TIPO_CREDITO,
            valor=Decimal("5.00"),
        )
        evento_venda = enfileirar_venda_finalizada(VendaHub.objects.get(venda_uuid=venda_uuid))
        vale = ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-LOCAL",
            valor_original=Decimal("15.00"),
            saldo=Decimal("15.00"),
        )
        evento_dev = EventoSyncHub.objects.create(
            hub=self.hub,
            tipo="DEVOLUCAO_FINALIZADA",
            chave_idempotencia="DEVOLUCAO:1:FINALIZADA",
            payload={"devolucao_uuid": str(uuid.uuid4()), "vale_troca": {"documento": vale.documento}},
        )

        class ClientOk:
            def sync_push(self, **kwargs):
                return {
                    "resultados": [
                        {"chave_idempotencia": evento_venda.chave_idempotencia, "status": "PROCESSADO"},
                        {"chave_idempotencia": evento_dev.chave_idempotencia, "status": "PROCESSADO"},
                    ]
                }

        sincronizar_eventos_pendentes(self.hub, client=ClientOk())
        movimento.refresh_from_db()
        vale.refresh_from_db()

        self.assertIsNotNone(movimento.centralizado_em)
        self.assertIsNotNone(vale.sincronizado_em)

    def test_consulta_devolucao_por_numero_nfce(self):
        cliente = self.criar_cliente()
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.assertEqual(self.pagar(venda_uuid, self.dinheiro, "199.90").status_code, 201)
        self.assertEqual(self.finalizar(venda_uuid).status_code, 200)
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        NFCeHub.objects.create(
            hub=self.hub,
            venda=venda,
            ambiente="HOMOLOGACAO",
            serie=1,
            numero=123,
            codigo_numerico="00000001",
            digito_verificador="1",
            chave_acesso="35260900000000000123650010000001231000000011",
            status=NFCeHub.STATUS_AUTORIZADA,
            emitida_em=timezone.now(),
        )

        resposta = self.client.get("/api/terminal/devolucoes/vendas/?documento=123")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["venda"]["uuid"], str(venda.venda_uuid))


class VendaPagamentoApiTests(PagamentoHubTestMixin, TestCase):
    def test_adicionar_dinheiro(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.dinheiro)

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(resposta.data["venda"]["total_pago"], "199.90")

    def test_adicionar_pix_manual(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.pix)

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(VendaPagamentoHub.objects.get().tipo, "PIX")

    def test_forma_tef_bloqueia_captura_manual(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.tef)

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Forma de pagamento exige integração TEF.")

    def test_pagamento_exige_venda_uuid(self):
        resposta = self.pagar("", self.dinheiro)

        self.assertEqual(resposta.status_code, 400)

    def test_pagamento_exige_operacao_uuid(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "forma_pagamento_id": self.dinheiro.id,
                "valor": "199.90",
            },
            format="json",
        )

        self.assertEqual(resposta.status_code, 400)

    def test_retry_mesmo_operacao_uuid_nao_duplica(self):
        venda_uuid = self.criar_venda_com_item()
        operacao_uuid = uuid.uuid4()
        primeira = self.pagar(venda_uuid, self.dinheiro, operacao_uuid=operacao_uuid)
        segunda = self.pagar(venda_uuid, self.dinheiro, operacao_uuid=operacao_uuid)

        self.assertEqual(primeira.status_code, 201)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(
            VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO).count(),
            1,
        )

    def test_mesmo_operacao_uuid_forma_diferente_retorna_409_e_preserva_original(self):
        venda_uuid = self.criar_venda_com_item()
        operacao_uuid = uuid.uuid4()
        self.pagar(venda_uuid, self.dinheiro, operacao_uuid=operacao_uuid)
        resposta = self.pagar(venda_uuid, self.pix, operacao_uuid=operacao_uuid)

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Operação de pagamento já utilizada com dados diferentes.")
        self.assertEqual(pagamento.forma_pagamento, self.dinheiro)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)

    def test_mesmo_operacao_uuid_valor_diferente_retorna_409(self):
        venda_uuid = self.criar_venda_com_item()
        operacao_uuid = uuid.uuid4()
        self.pagar(venda_uuid, self.dinheiro, valor="199.90", operacao_uuid=operacao_uuid)
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="200.00", operacao_uuid=operacao_uuid)

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.get().valor, Decimal("199.90"))

    def test_mesmo_operacao_uuid_autorizacao_diferente_retorna_409(self):
        venda_uuid = self.criar_venda_com_item()
        operacao_uuid = uuid.uuid4()
        payload = {
            "venda_uuid": venda_uuid,
            "operacao_uuid": str(operacao_uuid),
            "forma_pagamento_id": self.dinheiro.id,
            "valor": "199.90",
            "autorizacao": "A1",
        }
        self.client.post("/api/terminal/venda/pagamento/", payload, format="json")
        payload["autorizacao"] = "A2"
        resposta = self.client.post("/api/terminal/venda/pagamento/", payload, format="json")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.get().autorizacao, "A1")

    def test_valor_float_rejeitado(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(uuid.uuid4()),
                "forma_pagamento_id": self.dinheiro.id,
                "valor": 199.90,
            },
            format="json",
        )

        self.assertEqual(resposta.status_code, 400)

    def test_valor_com_escala_errada_rejeitado(self):
        venda_uuid = self.criar_venda_com_item()

        for valor in ("199.9", "199", "NaN", "Infinity", "-1.00", "0.00"):
            with self.subTest(valor=valor):
                resposta = self.pagar(venda_uuid, self.dinheiro, valor=valor)
                self.assertEqual(resposta.status_code, 400)

    def test_valor_maximo_monetario_e_aceito(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="9999999999999999.99")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(VendaPagamentoHub.objects.get().valor, Decimal("9999999999999999.99"))

    def test_valor_acima_do_limite_monetario_retorna_400(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="10000000000000000.00")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Valor do pagamento inválido.")

    def test_pagamento_nao_dinheiro_maior_que_pendente_bloqueado(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.pix, valor="200.00")

        self.assertEqual(resposta.status_code, 409)

    def test_dinheiro_maior_que_pendente_permitido_e_troco_calculado(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="200.00")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.data["venda"]["troco"], "0.10")

    def test_multiplos_pagamentos(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.pix, valor="100.00")
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="99.90")

        self.assertEqual(resposta.data["venda"]["total_pago"], "199.90")
        self.assertEqual(VendaPagamentoHub.objects.count(), 2)

    def test_pagamento_parcela_snapshot(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)

        self.assertEqual(VendaPagamentoParcelaHub.objects.count(), 1)

    def test_remover_pagamento_nao_deleta_linha_e_libera_carrinho(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        pagamento_uuid = VendaPagamentoHub.objects.get().pagamento_uuid
        resposta = self.client.delete(f"/api/terminal/venda/pagamento/{pagamento_uuid}/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(VendaPagamentoHub.objects.get().status, VendaPagamentoHub.STATUS_REMOVIDO)
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]
        alteracao = self.client.patch(
            f"/api/terminal/venda/item/{item_uuid}/",
            {"quantidade": 2},
            format="json",
        )
        self.assertEqual(alteracao.status_code, 200)

    def test_segunda_remocao_idempotente(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        pagamento_uuid = VendaPagamentoHub.objects.get().pagamento_uuid
        self.client.delete(f"/api/terminal/venda/pagamento/{pagamento_uuid}/")
        self.client.delete(f"/api/terminal/venda/pagamento/{pagamento_uuid}/")

        self.assertEqual(
            VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_PAGAMENTO_REMOVIDO).count(),
            1,
        )

    def test_pagamento_ativo_bloqueia_adicionar_alterar_remover_e_cancelar(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        item_uuid = VendaItemHub.objects.get().item_uuid

        adicionar = self.post_item()
        alterar = self.client.patch(f"/api/terminal/venda/item/{item_uuid}/", {"quantidade": 2}, format="json")
        remover = self.client.delete(f"/api/terminal/venda/item/{item_uuid}/")
        cancelar = self.client.post("/api/terminal/venda/cancelar/", {}, format="json")

        self.assertEqual(adicionar.status_code, 409)
        self.assertEqual(alterar.status_code, 409)
        self.assertEqual(remover.status_code, 409)
        self.assertEqual(cancelar.status_code, 409)


class VendaFinalizacaoTests(PagamentoHubTestMixin, TransactionTestCase):
    def habilitar_nfce(self):
        self.catalogo_item.fiscal = FISCAL_ITEM_NFCE.copy()
        self.catalogo_item.save(update_fields=["fiscal"])
        config = ConfiguracaoFiscalHub.objects.create(
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
        return config

    def test_venda_sem_item_nao_finaliza(self):
        venda = VendaHub.objects.create(
            hub=self.hub,
            sessao_caixa=self.sessao_caixa,
            terminal=self.terminal,
            status=VendaHub.STATUS_ABERTA,
            chave_venda_aberta_terminal=self.terminal.pk,
            operador_criacao=self.operador,
            sessao_operador_criacao=self.sessao_operador,
        )

        resposta = self.finalizar(str(venda.venda_uuid))

        self.assertEqual(resposta.status_code, 409)

    def test_venda_sem_pagamento_nao_finaliza(self):
        venda_uuid = self.criar_venda_com_item()
        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 409)

    def test_pagamento_insuficiente_nao_finaliza(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro, valor="100.00")
        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Pagamento insuficiente.")

    def test_finalizacao_completa_funciona_status_chave_auditoria_e_movimento(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        resposta = self.finalizar(venda_uuid)
        venda = VendaHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(venda.status, VendaHub.STATUS_FINALIZADA)
        self.assertIsNone(venda.chave_venda_aberta_terminal)
        self.assertEqual(venda.operador_finalizacao, self.operador)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 1)
        self.assertEqual(resposta.data["venda"]["fiscal"], {"emite_nfce": False})
        self.assertEqual(NFCeHub.objects.count(), 0)

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="AUTORIZADA",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_finalizacao_com_nfce_cria_documento_e_payload_fiscal(self):
        self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        chamadas = []

        def transmitir_fora_de_atomic(client, nfce):
            chamadas.append(transaction.get_connection().in_atomic_block)
            return original_transmitir(client, nfce)

        from core.services.nfce import SefazNFCeClientDesenvolvimento

        original_transmitir = SefazNFCeClientDesenvolvimento.transmitir
        with patch.object(SefazNFCeClientDesenvolvimento, "transmitir", transmitir_fora_de_atomic):
            resposta = self.finalizar(venda_uuid)
        nfce = NFCeHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(chamadas, [False])
        self.assertEqual(nfce.status, NFCeHub.STATUS_GERADA)
        self.assertEqual(nfce.tipo_emissao, "1")
        self.assertEqual(nfce.protocolo, "")
        self.assertIn(f"?p={nfce.chave_acesso}|3|2", nfce.qr_code_payload)
        self.assertIn("SIMULACAO_SEFAZ_AUTORIZADA_SEM_VALOR_FISCAL", nfce.mensagem_retorno)
        self.assertEqual(resposta.data["venda"]["fiscal"]["emite_nfce"], True)
        self.assertEqual(resposta.data["venda"]["fiscal"]["nfce_uuid"], str(nfce.nfce_uuid))
        self.assertEqual(resposta.data["venda"]["fiscal"]["tipo_emissao"], "1")
        self.assertNotIn("PRIVATE KEY", str(resposta.data))
        self.assertNotIn("CERTIFICATE", str(resposta.data))

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="AUTORIZADA",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_erro_fiscal_pre_validacao_nao_finaliza_nem_consume_numero(self):
        config = self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        item = VendaItemHub.objects.get()
        item.fiscal = {}
        item.save(update_fields=["fiscal"])
        self.pagar(venda_uuid, self.dinheiro)

        resposta = self.finalizar(venda_uuid)
        venda = VendaHub.objects.get()
        config.refresh_from_db()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "PRODUTO_SEM_NCM")
        self.assertEqual(venda.status, VendaHub.STATUS_ABERTA)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 0)
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).count(), 0)
        self.assertEqual(NFCeHub.objects.count(), 0)
        self.assertEqual(config.proximo_numero_nfce, 10)

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="AUTORIZADA",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_retry_finalizar_com_nfce_nao_duplica_documento_numero_movimento_nem_evento(self):
        config = self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)

        primeira = self.finalizar(venda_uuid)
        segunda = self.finalizar(venda_uuid)
        config.refresh_from_db()

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(NFCeHub.objects.count(), 1)
        self.assertEqual(NFCeHub.objects.get().numero, 10)
        self.assertEqual(config.proximo_numero_nfce, 11)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 1)
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).count(), 1)

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="TIMEOUT",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_finalizacao_com_timeout_gera_contingencia_offline(self):
        self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)

        resposta = self.finalizar(venda_uuid)
        nfce = NFCeHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(nfce.status, NFCeHub.STATUS_CONTINGENCIA)
        self.assertEqual(nfce.tipo_emissao, "9")
        self.assertEqual(nfce.chave_acesso[34], "9")
        self.assertIn("<tpEmis>9</tpEmis>", nfce.xml_assinado)
        self.assertIn("<dhCont>", nfce.xml_assinado)
        self.assertIn("<xJust>SIMULACAO_SEFAZ_TIMEOUT</xJust>", nfce.xml_assinado)
        self.assertIn(f"?p={nfce.chave_acesso}|3|2|", nfce.qr_code_payload)
        self.assertNotIn("cHashQRCode", nfce.qr_code_payload)
        self.assertEqual(resposta.data["venda"]["fiscal"]["contingencia"], True)

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="REJEITADA",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_rejeicao_pos_commit_mantem_venda_finalizada_e_nao_duplica_retry(self):
        config = self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)

        resposta = self.finalizar(venda_uuid)
        retry = self.finalizar(venda_uuid)
        venda = VendaHub.objects.get()
        nfce = NFCeHub.objects.get()
        config.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(venda.status, VendaHub.STATUS_FINALIZADA)
        self.assertEqual(nfce.status, NFCeHub.STATUS_REJEITADA)
        self.assertIn("SIMULACAO_SEFAZ_REJEITADA", nfce.mensagem_retorno)
        self.assertEqual(resposta.data["venda"]["fiscal"]["status"], NFCeHub.STATUS_REJEITADA)
        self.assertEqual(NFCeHub.objects.count(), 1)
        self.assertEqual(config.proximo_numero_nfce, 11)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 1)
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).count(), 1)

    @override_settings(
        SYSVARHUB_NFCE_MATERIAL_MODE="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_CLIENT="DESENVOLVIMENTO",
        SYSVARHUB_NFCE_SEFAZ_DESENVOLVIMENTO_RESULTADO="AUTORIZADA",
        SYSVARHUB_NFCE_QRCODE_URL="https://sefaz.test/qrcode",
        SYSVARHUB_NFCE_URL_CHAVE="https://sefaz.test/consulta",
    )
    def test_erro_pos_reserva_mantem_nfce_rastreavel_e_retry_nao_duplica(self):
        config = self.habilitar_nfce()
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)

        with patch("core.services.nfce.assinar_xml_nfce", side_effect=RuntimeError("falha controlada")):
            resposta = self.finalizar(venda_uuid)
        retry = self.finalizar(venda_uuid)
        venda = VendaHub.objects.get()
        nfce = NFCeHub.objects.get()
        config.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(venda.status, VendaHub.STATUS_FINALIZADA)
        self.assertEqual(nfce.status, NFCeHub.STATUS_ERRO_GERACAO)
        self.assertEqual(nfce.mensagem_retorno, "DADOS_FISCAIS_INSUFICIENTES")
        self.assertEqual(resposta.data["venda"]["fiscal"]["status"], NFCeHub.STATUS_ERRO_GERACAO)
        self.assertEqual(NFCeHub.objects.count(), 1)
        self.assertEqual(config.proximo_numero_nfce, 11)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 1)
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).count(), 1)

    def test_um_movimento_por_item_e_catalogo_bruto_nao_alterado(self):
        venda_uuid = self.criar_venda_com_item()
        estoque = self.catalogo_item.estoque_disponivel
        self.pagar(venda_uuid, self.dinheiro)
        self.finalizar(venda_uuid)
        self.catalogo_item.refresh_from_db()

        self.assertEqual(EstoqueMovimentoHub.objects.count(), VendaItemHub.objects.count())
        self.assertEqual(self.catalogo_item.estoque_disponivel, estoque)

    def test_disponibilidade_local_cai_e_transicao_reserva_movimento_nao_duplica(self):
        venda_uuid = self.criar_venda_com_item()
        self.assertEqual(calcular_disponivel_local(self.catalogo_item), Decimal("3.000"))
        self.pagar(venda_uuid, self.dinheiro)
        self.finalizar(venda_uuid)

        self.assertEqual(calcular_disponivel_local(self.catalogo_item), Decimal("3.000"))

    def test_movimento_interfere_na_proxima_venda(self):
        venda_uuid = self.criar_venda_com_item(quantidade=4)
        self.pagar(venda_uuid, self.dinheiro, valor="799.60")
        self.finalizar(venda_uuid)
        resposta = self.post_item()

        self.assertEqual(resposta.status_code, 409)

    def test_retry_finalizar_nao_duplica_movimentos_nem_evento(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        self.finalizar(venda_uuid)
        self.finalizar(venda_uuid)

        self.assertEqual(EstoqueMovimentoHub.objects.count(), 1)
        self.assertEqual(
            VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).count(),
            1,
        )

    def test_venda_cancelada_nao_finaliza(self):
        venda_uuid = self.criar_venda_com_item()
        self.client.post("/api/terminal/venda/cancelar/", {}, format="json")
        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 409)

    def test_caixa_fechado_nao_finaliza(self):
        venda_uuid = self.criar_venda_com_item()
        VendaHub.objects.update(status=VendaHub.STATUS_CANCELADA, chave_venda_aberta_terminal=None)
        fechar_caixa(self.terminal, self.operador, self.sessao_operador, valor_contado="100.00")
        VendaHub.objects.update(status=VendaHub.STATUS_ABERTA, chave_venda_aberta_terminal=self.terminal.pk)
        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 409)

    def test_venda_de_outro_terminal_nao_pode_finalizar(self):
        venda_uuid = self.criar_venda_com_item()
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", self.caixa.retaguarda_id)
        outro_token_terminal = outro_terminal.gerar_token()
        outro_terminal.save()
        outra_sessao_operador, outro_token_operador = self.criar_sessao_operador(outro_terminal, self.operador)
        self.autenticar(outro_token_terminal, outro_token_operador)
        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(outra_sessao_operador.operador, self.operador)

    def test_dinheiro_mais_outra_forma_calcula_troco_e_excesso_sem_dinheiro_bloqueia(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.pix, valor="100.00")
        resposta = self.pagar(venda_uuid, self.dinheiro, valor="100.00")
        self.assertEqual(resposta.data["venda"]["troco"], "0.10")

        outra_venda_uuid = self.criar_venda_com_item()
        bloqueado = self.pagar(outra_venda_uuid, self.pix, valor="200.00")
        self.assertEqual(bloqueado.status_code, 409)

    def test_nova_venda_pode_iniciar_apos_finalizacao(self):
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.dinheiro)
        self.finalizar(venda_uuid)
        atual = self.client.get("/api/terminal/venda/atual/")
        nova = self.post_iniciar()

        self.assertIsNone(atual.data["venda"])
        self.assertEqual(nova.status_code, 201)
