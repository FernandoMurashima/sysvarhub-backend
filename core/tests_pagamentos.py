import uuid
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    CatalogoItemHub,
    CashbackConfigHub,
    CashbackMovimentoHub,
    ConfiguracaoFiscalHub,
    EventoSyncHub,
    FaixaNumeracaoDevolucaoHub,
    EstoqueMovimentoHub,
    FaixaNumeracaoVendaHub,
    FormaPagamentoCondicaoHub,
    FormaPagamentoFiscalMapHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    PrazoPagamentoHub,
    PrazoPagamentoParcelaHub,
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
    ValeTrocaMovimentoHub,
    VendaDevolucaoHub,
)
from core.services.caixa import fechar_caixa
from core.services.devolucoes import finalizar_devolucao_manual_offline
from core.services.sync import enfileirar_venda_finalizada, sincronizar_eventos_pendentes
from core.services.terminais import configurar_terminal
from core.services.vendas import VendaConflictError, calcular_disponivel_local, listar_formas_pagamento
from core.tests_caixa import criar_hub
from core.tests_vendas import VendaHubTestMixin
from integracao.services.retaguarda import RetaguardaError


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
MENSAGEM_CASHBACK_VALE_TROCA = (
    "Cashback não pode ser usado junto com Vale-Troca. "
    "Realize a venda com Cashback separadamente da troca."
)
MENSAGEM_DEVOLUCAO_MANUAL_OFFLINE = (
    "Não é possível realizar esta troca offline. "
    "A venda original não está registrada nesta loja. "
    "Aguarde a conexão com a Central."
)


class PagamentoHubTestMixin(VendaHubTestMixin):
    def setUp(self):
        super().setUp()
        self.dinheiro = self.criar_forma("DIN", "DINHEIRO")
        self.pix = self.criar_forma("PIX", "PIX")
        self.cartao = self.criar_forma("CAR", "CARTAO")
        self.credito = self.criar_forma("CRE", "CREDITO")
        self.prazo_1x = self.criar_prazo("30D", 1, [30])
        self.prazo_2x = self.criar_prazo("30-60", 2, [30, 60])
        self.tef = self.criar_forma("TEF", "CARTAO", tef_habilitado=True)
        FaixaNumeracaoVendaHub.objects.create(hub=self.hub, inicio=1, fim=9999, proximo_numero=1)

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

    def criar_condicao(self, forma, prazo, **overrides):
        return FormaPagamentoCondicaoHub.objects.create(
            hub=self.hub,
            retaguarda_id=overrides.pop("retaguarda_id", 9000 + FormaPagamentoCondicaoHub.objects.count()),
            forma_pagamento=forma,
            prazo_pagamento=prazo,
            taxa_percentual=overrides.pop("taxa_percentual", Decimal("1.5000")),
            taxa_fixa=overrides.pop("taxa_fixa", Decimal("0.25")),
            ativo=overrides.pop("ativo", True),
            sincronizado_em=timezone.now(),
            **overrides,
        )

    def criar_prazo(self, codigo, num_parcelas, dias, **overrides):
        prazo = PrazoPagamentoHub.objects.create(
            hub=self.hub,
            retaguarda_id=overrides.pop("retaguarda_id", 7000 + PrazoPagamentoHub.objects.count()),
            codigo=codigo,
            descricao=overrides.pop("descricao", codigo),
            num_parcelas=num_parcelas,
            intervalo_dias=overrides.pop("intervalo_dias", 30),
            ativo=True,
            sincronizado_em=timezone.now(),
            **overrides,
        )
        percentual = Decimal("1.000000") / Decimal(num_parcelas)
        for indice, dia in enumerate(dias, start=1):
            PrazoPagamentoParcelaHub.objects.create(
                prazo=prazo,
                ordem=indice,
                dias=dia,
                percentual=percentual,
                valor_fixo=None,
                sincronizado_em=timezone.now(),
            )
        return prazo

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

    def pagar(self, venda_uuid, forma=None, valor="199.90", operacao_uuid=None, prazo=None):
        return self.client.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(operacao_uuid or uuid.uuid4()),
                "forma_pagamento_id": (forma or self.dinheiro).id,
                "prazo_pagamento_id": prazo.id if prazo else None,
                "valor": valor,
                "autorizacao": "",
            },
            format="json",
        )

    def pagar_cashback(self, venda_uuid, valor="30.00", operacao_uuid=None):
        return self.client.post(
            "/api/terminal/venda/pagamento/cashback/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(operacao_uuid or uuid.uuid4()),
                "valor": valor,
            },
            format="json",
        )

    def pagar_cashback_online(self, venda_uuid, valor="30.00", operacao_uuid=None):
        return self.client.post(
            "/api/terminal/venda/pagamento/cashback-online/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(operacao_uuid or uuid.uuid4()),
                "valor": valor,
            },
            format="json",
        )

    def pagar_vale_troca(self, venda_uuid, documento="VT-HUB-11111111111111111111", valor="40.00", operacao_uuid=None):
        return self.client.post(
            "/api/terminal/venda/pagamento/vale-troca/",
            {
                "venda_uuid": venda_uuid,
                "operacao_uuid": str(operacao_uuid or uuid.uuid4()),
                "documento": documento,
                "valor": valor,
            },
            format="json",
        )

    def criar_cashback_disponivel(self, cliente, saldo="200.00", **config_overrides):
        dados_config = {
            "hub": self.hub,
            "retaguarda_id": 8000 + CashbackConfigHub.objects.count(),
            "ativo": True,
            "percentual": Decimal("0.0000"),
            "sincronizado_em": timezone.now(),
        }
        dados_config.update(config_overrides)
        CashbackConfigHub.objects.create(**dados_config)
        return CashbackMovimentoHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            tipo=CashbackMovimentoHub.TIPO_CREDITO,
            valor=Decimal(saldo),
        )

    def criar_vale_troca(self, cliente, documento="VT-HUB-11111111111111111111", saldo="40.00", **overrides):
        dados = {
            "hub": self.hub,
            "cliente_uuid": cliente.cliente_uuid,
            "cliente_retaguarda_id": cliente.retaguarda_id,
            "documento": documento,
            "valor_original": Decimal(saldo),
            "saldo": Decimal(saldo),
            "status": ValeTrocaHub.STATUS_ABERTO,
            "provisorio": True,
        }
        dados.update(overrides)
        return ValeTrocaHub.objects.create(**dados)

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

    def test_listar_forma_com_condicoes_parcelamento_ativas(self):
        self.credito.permite_parcelamento = True
        self.credito.save(update_fields=["permite_parcelamento", "atualizado_em"])
        condicao = self.criar_condicao(self.credito, self.prazo_2x, retaguarda_id=9100)
        self.criar_condicao(self.credito, self.prazo_1x, retaguarda_id=9101, ativo=False)

        resposta = self.client.get("/api/terminal/formas-pagamento/")

        credito = next(forma for forma in resposta.data["formas"] if forma["codigo"] == "CRE")
        self.assertTrue(credito["permite_parcelamento"])
        self.assertEqual(len(credito["condicoes_parcelamento"]), 1)
        self.assertEqual(credito["condicoes_parcelamento"][0]["retaguarda_id"], condicao.retaguarda_id)
        self.assertEqual(credito["condicoes_parcelamento"][0]["prazo_retaguarda_id"], self.prazo_2x.retaguarda_id)

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

        self.assertLessEqual(len(contexto), 6)
        self.assertGreaterEqual(len(payload["formas"]), 3)


class CreditoProvisorioTrocaTests(PagamentoHubTestMixin, TestCase):
    def preparar_troca(self, preco="95.00", cliente_retaguarda_id=123):
        self.cliente_troca = self.criar_cliente(retaguarda_id=cliente_retaguarda_id)
        self.catalogo_item.preco_venda = Decimal("100.00")
        self.catalogo_item.save()
        origem = self.criar_venda_com_item()
        self.put_cliente(self.cliente_troca)
        self.assertEqual(self.pagar(origem, self.dinheiro, "100.00").status_code, 201)
        self.assertEqual(self.finalizar(origem).status_code, 200)
        item = VendaItemHub.objects.get(venda__venda_uuid=origem)
        FaixaNumeracaoDevolucaoHub.objects.create(hub=self.hub, inicio=1, fim=100, proximo_numero=1)
        resposta = self.client.post("/api/terminal/devolucoes/finalizar/", {
            "venda_uuid": origem,
            "itens": [{"item_uuid": str(item.item_uuid), "quantidade": 1}],
        }, format="json")
        self.assertEqual(resposta.status_code, 201, resposta.data)
        self.devolucao = VendaDevolucaoHub.objects.get()
        self.vale = self.devolucao.vale_troca
        self.catalogo_item.preco_venda = Decimal(preco)
        self.catalogo_item.save()
        venda = self.criar_venda_com_item()
        self.put_cliente(self.cliente_troca)
        return venda

    def aplicar_credito(self, venda, valor="95.00", *, operacao=None, devolucao=True, tipo=None):
        payload = {
            "venda_uuid": venda, "operacao_uuid": str(operacao or uuid.uuid4()),
            "documento": self.vale.documento, "valor": valor,
        }
        if devolucao:
            payload["devolucao_uuid"] = str(self.devolucao.devolucao_uuid)
        if tipo:
            forma = FormaPagamentoHub.objects.filter(hub=self.hub, codigo=tipo).first() or self.criar_forma(tipo, tipo)
            payload["forma_pagamento_id"] = forma.pk
            payload["autorizacao"] = payload.pop("documento")
        endpoint = "/api/terminal/venda/pagamento/" if tipo else "/api/terminal/venda/pagamento/vale-troca/"
        return self.client.post(endpoint, payload, format="json")

    def test_consumo_parcial_saldo_restrito_e_retry(self):
        venda = self.preparar_troca()
        self.assertTrue(self.vale.provisorio)
        self.assertEqual(self.vale.valor_original, Decimal("100.00"))
        self.assertIsNone(self.devolucao.venda_troca_id)
        operacao = uuid.uuid4()
        self.assertEqual(self.aplicar_credito(venda, operacao=operacao).status_code, 201)
        self.assertEqual(self.aplicar_credito(venda, operacao=operacao).status_code, 200)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("5.00"))
        self.assertEqual(self.vale.movimentos.filter(tipo="USO").count(), 1)
        outra = self.criar_venda_com_item()
        self.put_cliente(self.cliente_troca)
        self.assertEqual(self.aplicar_credito(outra, "5.00").status_code, 409)
        for tipo in ("TROCA", "VALE_TROCA"):
            self.assertEqual(self.aplicar_credito(outra, "5.00", tipo=tipo).status_code, 409)
        self.assertEqual(self.vale.movimentos.get(tipo="USO").valor, Decimal("95.00"))

    def test_consumo_integral(self):
        venda = self.preparar_troca("100.00")
        self.assertEqual(self.aplicar_credito(venda, "100.00").status_code, 201)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("0.00"))

    def test_cliente_local_identificado_por_uuid(self):
        venda = self.preparar_troca(cliente_retaguarda_id=None)
        self.assertEqual(self.aplicar_credito(venda).status_code, 201)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("5.00"))

    def test_forma_legada_vale_troca_vinculada(self):
        venda = self.preparar_troca()
        self.assertEqual(self.aplicar_credito(venda, tipo="VALE_TROCA").status_code, 201)
        self.assertEqual(self.aplicar_credito(venda).status_code, 409)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.assertEqual(self.vale.movimentos.filter(tipo="USO").count(), 1)

    def test_pagamento_complementar_pix(self):
        venda = self.preparar_troca("105.00")
        self.assertEqual(self.aplicar_credito(venda, "100.00").status_code, 201)
        self.assertEqual(self.pagar(venda, self.pix, "5.00").status_code, 201)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("0.00"))

    def test_uuid_obrigatorio_e_legados_protegidos(self):
        venda = self.preparar_troca()
        for tipo in (None, "TROCA", "VALE_TROCA"):
            with self.subTest(tipo=tipo):
                self.assertEqual(self.aplicar_credito(venda, devolucao=False, tipo=tipo).status_code, 409)
        self.assertEqual(self.aplicar_credito(venda, tipo="TROCA").status_code, 201)
        self.assertEqual(self.finalizar(venda).status_code, 200)

    def test_cancelamento_nao_transfere_vinculo(self):
        venda = self.preparar_troca("105.00")
        self.assertEqual(self.aplicar_credito(venda, "100.00").status_code, 201)
        pagamento = VendaPagamentoHub.objects.get(venda__venda_uuid=venda)
        resposta = self.client.delete(f"/api/terminal/venda/pagamento/{pagamento.pagamento_uuid}/")
        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(self.aplicar_credito(venda, "95.00").status_code, 201)
        self.assertEqual(self.pagar(venda, self.dinheiro, "10.00").status_code, 201)
        self.assertEqual(self.finalizar(venda).status_code, 200)
        outra = self.criar_venda_com_item()
        self.put_cliente(self.cliente_troca)
        self.assertEqual(self.aplicar_credito(outra, "5.00").status_code, 409)

    def test_cliente_e_hub_incorretos(self):
        venda = self.preparar_troca()
        outro = self.criar_cliente(retaguarda_id=999, documento="98765432100")
        self.put_cliente(outro)
        self.assertEqual(self.aplicar_credito(venda).status_code, 409)
        self.put_cliente(self.cliente_troca)
        outro_hub = criar_hub(loja_id=99)
        self.vale.hub = outro_hub
        self.vale.save()
        self.assertEqual(self.aplicar_credito(venda).status_code, 409)
        self.assertFalse(self.vale.movimentos.filter(tipo="USO").exists())

    def test_provisorio_nao_exposto_como_saldo_geral(self):
        venda = self.preparar_troca()
        resposta = self.client.get(f"/api/terminal/clientes/{self.cliente_troca.cliente_uuid}/beneficios/")
        self.assertEqual(resposta.data["vales_troca"], [])
        with patch("core.services.vales_troca.RetaguardaClient") as client_cls:
            client_cls.return_value.vale_troca_disponiveis.side_effect = RetaguardaError("offline")
            resposta = self.client.get(f"/api/terminal/vale-troca/disponiveis/?venda_uuid={venda}")
        self.assertEqual(resposta.status_code, 409)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("100.00"))

    def test_vale_oficial_exige_central_e_preserva_reserva(self):
        venda = self.preparar_troca()
        with patch("core.services.vales_troca.RetaguardaClient") as client_cls:
            client = client_cls.return_value
            client.vale_troca_consultar.return_value = {"vale_troca": {
                "id": 77, "documento": "VT0000077", "cliente": {"id": 123},
                "saldo_disponivel": "100.00", "status": "ABERTO",
            }}
            operacao = uuid.uuid4()
            resposta = self.client.post("/api/terminal/venda/pagamento/vale-troca/", {
                "venda_uuid": venda, "operacao_uuid": str(operacao),
                "documento": "VT0000077", "valor": "95.00",
            }, format="json")
            self.assertEqual(resposta.status_code, 201, resposta.data)
            client.vale_troca_reservar_venda.return_value = {"reservas": [{
                "operacao_uuid": str(operacao), "id": 800, "vale_id": 77,
            }]}
            self.assertEqual(self.finalizar(venda).status_code, 200)
            client.vale_troca_reservar_venda.assert_called_once()
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("100.00"))

    def test_finalizacao_rejeita_pagamento_sem_vinculo(self):
        venda = self.preparar_troca()
        self.assertEqual(self.aplicar_credito(venda).status_code, 201)
        VendaDevolucaoHub.objects.filter(pk=self.devolucao.pk).update(venda_troca=None)
        self.assertEqual(self.finalizar(venda).status_code, 409)
        self.vale.refresh_from_db()
        self.assertEqual(self.vale.saldo, Decimal("100.00"))
        self.assertFalse(self.vale.movimentos.filter(tipo="USO").exists())

    def test_uuid_invalido_e_retry_divergente(self):
        venda = self.preparar_troca()
        operacao = uuid.uuid4()
        resposta = self.aplicar_credito(venda, operacao=operacao)
        self.assertEqual(resposta.status_code, 201)
        pagamento = next(p for p in resposta.data["venda"]["pagamentos"] if p["tipo"] == "VALE_TROCA")
        self.assertEqual(pagamento["devolucao_uuid"], str(self.devolucao.devolucao_uuid))
        for identificador, esperado in (("invalido", 400), (str(uuid.uuid4()), 409)):
            resposta = self.client.post("/api/terminal/venda/pagamento/vale-troca/", {
                "venda_uuid": venda, "operacao_uuid": str(operacao),
                "documento": self.vale.documento, "valor": "95.00",
                "devolucao_uuid": identificador,
            }, format="json")
            self.assertEqual(resposta.status_code, esperado)
        self.assertEqual(VendaPagamentoHub.objects.filter(venda__venda_uuid=venda).count(), 1)

    def test_espelho_oficial_sem_sync_nao_autoriza_offline(self):
        venda = self.preparar_troca()
        self.vale.provisorio = False
        self.vale.documento = "VT0000077"
        self.vale.save()
        self.assertEqual(self.aplicar_credito(venda, tipo="TROCA").status_code, 409)
        with patch("core.services.vales_troca.RetaguardaClient") as client_cls:
            client_cls.return_value.vale_troca_consultar.side_effect = RetaguardaError("offline")
            self.assertEqual(self.aplicar_credito(venda).status_code, 409)
        self.assertIsNone(VendaDevolucaoHub.objects.get(pk=self.devolucao.pk).venda_troca_id)


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

    def test_cashback_local_e_vale_troca_comercial_nao_podem_ser_usados_juntos(self):
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
        vale = self.criar_vale_troca(
            cliente,
            documento="VT-1",
            saldo="40.00",
            provisorio=False,
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

        vale.refresh_from_db()
        self.assertEqual(resposta_troca.status_code, 409)
        self.assertEqual(resposta_troca.data["detail"], MENSAGEM_CASHBACK_VALE_TROCA)
        self.assertEqual(vale.saldo, Decimal("40.00"))
        self.assertFalse(ValeTrocaMovimentoHub.objects.exists())

    def test_vale_troca_antes_do_cashback_local_bloqueia_cashback(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="30.00")
        self.criar_vale_troca(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        self.assertEqual(self.pagar_vale_troca(venda_uuid).status_code, 201)
        resposta = self.pagar_cashback(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], MENSAGEM_CASHBACK_VALE_TROCA)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)

    def test_cashback_antes_do_vale_troca_dedicado_bloqueia_vale(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="30.00")
        vale = self.criar_vale_troca(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        self.assertEqual(self.pagar_cashback(venda_uuid, "30.00").status_code, 201)
        resposta = self.pagar_vale_troca(venda_uuid)

        vale.refresh_from_db()
        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], MENSAGEM_CASHBACK_VALE_TROCA)
        self.assertEqual(vale.saldo, Decimal("40.00"))

    def test_cashback_central_com_vale_troca_bloqueado_sem_reservar_na_central(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        self.criar_vale_troca(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.assertEqual(self.pagar_vale_troca(venda_uuid).status_code, 201)

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            resposta = self.pagar_cashback_online(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], MENSAGEM_CASHBACK_VALE_TROCA)
        client_cls.return_value.cashback_reservar_venda.assert_not_called()

    def test_venda_com_vale_troca_nao_gera_cashback_novo(self):
        cliente = self.criar_cliente()
        CashbackConfigHub.objects.create(
            hub=self.hub,
            retaguarda_id=4,
            ativo=True,
            percentual=Decimal("10.0000"),
            sincronizado_em=timezone.now(),
        )
        vale = self.criar_vale_troca(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        self.assertEqual(self.pagar_vale_troca(venda_uuid).status_code, 201)
        self.assertEqual(self.pagar(venda_uuid, self.dinheiro, "159.90").status_code, 201)
        finalizacao = self.finalizar(venda_uuid)
        self.assertEqual(finalizacao.status_code, 200, finalizacao.data)

        vale.refresh_from_db()
        self.assertEqual(vale.saldo, Decimal("0.00"))
        self.assertFalse(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_CREDITO).exists())

    def test_vale_troca_exige_autorizacao_documento(self):
        cliente = self.criar_cliente()
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-OBRIGATORIO",
            valor_original=Decimal("40.00"),
            saldo=Decimal("40.00"),
            status=ValeTrocaHub.STATUS_ABERTO,
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
                "autorizacao": "",
            },
            format="json",
        )

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Selecione um cupom de troca válido para o pagamento.")

    def test_vale_troca_nao_consumo_automatico_de_outro_cupom(self):
        cliente = self.criar_cliente()
        vale = ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-CORRETO",
            valor_original=Decimal("40.00"),
            saldo=Decimal("40.00"),
            status=ValeTrocaHub.STATUS_ABERTO,
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
                "autorizacao": "VT-INEXISTENTE",
            },
            format="json",
        )
        vale.refresh_from_db()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(vale.saldo, Decimal("40.00"))
        self.assertFalse(ValeTrocaMovimentoHub.objects.exists())

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
        FaixaNumeracaoDevolucaoHub.objects.create(hub=self.hub, inicio=1, fim=9999, proximo_numero=1)
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

    def test_devolucao_manual_offline_sem_venda_local_bloqueada_sem_efeitos(self):
        cliente = self.criar_cliente()
        estoque_fisico = self.catalogo_item.estoque_fisico
        estoque_disponivel = self.catalogo_item.estoque_disponivel

        resposta = self.client.post(
            "/api/terminal/devolucoes/finalizar/",
            {
                "documento_venda": "VENDA-OUTRA-LOJA",
                "motivo": "Troca manual offline",
                "itens": [
                    {
                        "sku_retaguarda_id": self.catalogo_item.retaguarda_sku_id,
                        "ean": self.catalogo_item.ean13,
                        "descricao": self.catalogo_item.descricao,
                        "quantidade": 1,
                        "valor_liquido": "199.90",
                        "cliente_uuid": str(cliente.cliente_uuid),
                        "cliente_documento": cliente.documento,
                    }
                ],
            },
            format="json",
        )

        self.catalogo_item.refresh_from_db()
        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], MENSAGEM_DEVOLUCAO_MANUAL_OFFLINE)
        self.assertEqual(VendaDevolucaoHub.objects.count(), 0)
        self.assertEqual(ValeTrocaHub.objects.count(), 0)
        self.assertEqual(ValeTrocaMovimentoHub.objects.count(), 0)
        self.assertEqual(EventoSyncHub.objects.filter(tipo="DEVOLUCAO_FINALIZADA").count(), 0)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 0)
        self.assertEqual(self.catalogo_item.estoque_fisico, estoque_fisico)
        self.assertEqual(self.catalogo_item.estoque_disponivel, estoque_disponivel)

    def test_finalizar_devolucao_manual_offline_direto_bloqueado_sem_efeitos(self):
        cliente = self.criar_cliente()
        estoque_fisico = self.catalogo_item.estoque_fisico
        estoque_disponivel = self.catalogo_item.estoque_disponivel

        with self.assertRaisesMessage(VendaConflictError, MENSAGEM_DEVOLUCAO_MANUAL_OFFLINE):
            finalizar_devolucao_manual_offline(
                self.terminal,
                self.operador,
                documento_venda="MANUAL-SEM-VENDA",
                motivo="Troca manual offline",
                itens=[
                    {
                        "sku_retaguarda_id": self.catalogo_item.retaguarda_sku_id,
                        "ean": self.catalogo_item.ean13,
                        "descricao": self.catalogo_item.descricao,
                        "quantidade": 1,
                        "valor_liquido": "199.90",
                        "cliente_uuid": str(cliente.cliente_uuid),
                    }
                ],
            )

        self.catalogo_item.refresh_from_db()
        self.assertEqual(VendaDevolucaoHub.objects.count(), 0)
        self.assertEqual(ValeTrocaHub.objects.count(), 0)
        self.assertEqual(ValeTrocaMovimentoHub.objects.count(), 0)
        self.assertEqual(EventoSyncHub.objects.filter(tipo="DEVOLUCAO_FINALIZADA").count(), 0)
        self.assertEqual(EstoqueMovimentoHub.objects.count(), 0)
        self.assertEqual(self.catalogo_item.estoque_fisico, estoque_fisico)
        self.assertEqual(self.catalogo_item.estoque_disponivel, estoque_disponivel)

    def test_devolucao_online_preservada_para_venda_validada_na_central(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        payload_central = {
            "devolucao": {
                "id": 901,
                "documento": "DEVONLINE01",
                "valor_total": "50.00",
                "motivo": "Troca online",
                "cliente": {"id": cliente.retaguarda_id},
                "venda_origem": {
                    "id": 501,
                    "documento": "VENDA-501",
                    "cliente": {"id": cliente.retaguarda_id},
                    "loja_origem": {"id": 99, "nome": "Outra Loja"},
                },
                "itens": [
                    {
                        "sku": self.catalogo_item.retaguarda_sku_id,
                        "produto": self.catalogo_item.retaguarda_produto_id,
                        "ean": self.catalogo_item.ean13,
                        "descricao": self.catalogo_item.descricao,
                        "quantidade": 1,
                        "preco_unitario": "50.00",
                        "total_item": "50.00",
                    }
                ],
                "vale_troca": {
                    "id": 777,
                    "documento": "VT0000777",
                    "valor_original": "50.00",
                    "saldo": "50.00",
                    "status": ValeTrocaHub.STATUS_ABERTO,
                },
            }
        }

        with (
            patch("core.services.devolucoes.calcular_status_central", return_value={"status": "ONLINE"}),
            patch("core.services.devolucoes.RetaguardaClient") as client_cls,
        ):
            client_cls.return_value.devolucao_finalizar.return_value = payload_central
            resposta = self.client.post(
                "/api/terminal/devolucoes/finalizar/",
                {
                    "venda_id": 501,
                    "documento_venda": "VENDA-501",
                    "motivo": "Troca online",
                    "itens": [{"sku_id": self.catalogo_item.retaguarda_sku_id, "quantidade": 1}],
                },
                format="json",
            )

        self.assertEqual(resposta.status_code, 201, resposta.data)
        client_cls.return_value.devolucao_finalizar.assert_called_once()
        devolucao = VendaDevolucaoHub.objects.get()
        self.assertEqual(devolucao.retaguarda_id, 901)
        self.assertEqual(devolucao.venda_origem_retaguarda_id, 501)
        vale = ValeTrocaHub.objects.get()
        self.assertEqual(vale.retaguarda_id, 777)
        self.assertEqual(vale.documento, "VT0000777")
        self.assertEqual(vale.saldo, Decimal("50.00"))

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
            documento="VT0000001",
            valor_original=Decimal("10.00"),
            saldo=Decimal("10.00"),
        )
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT0000002",
            valor_original=Decimal("20.00"),
            saldo=Decimal("20.00"),
            sincronizado_em=timezone.now(),
        )

        resposta = self.client.get(f"/api/terminal/clientes/{cliente.cliente_uuid}/beneficios/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["cashback"]["saldo_retaguarda"], "80.00")
        self.assertEqual(resposta.data["cashback"]["saldo_offline_utilizavel"], "30.00")
        vales = {vale["documento"]: vale for vale in resposta.data["vales_troca"]}
        self.assertTrue(vales["VT0000001"]["utilizavel_offline"])
        self.assertFalse(vales["VT0000002"]["utilizavel_offline"])

    def test_endpoint_vales_troca_disponiveis_consulta_central_e_canonicaliza_espelho(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        ValeTrocaHub.objects.create(
            hub=self.hub,
            retaguarda_id=45,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-HUB-DEV-antigo",
            valor_original=Decimal("219.90"),
            saldo=Decimal("219.90"),
            sincronizado_em=timezone.now(),
        )

        resposta_central = {
            "vales_troca": [
                {
                    "id": 45,
                    "documento": "VT0000001",
                    "cliente": {"id": 123, "nome": "Fernanda Oliveira Lima", "documento": "12345678901"},
                    "valor_original": "219.90",
                    "saldo_contabil": "219.90",
                    "saldo_reservado": "0.00",
                    "saldo_disponivel": "219.90",
                    "status": "ABERTO",
                    "validade": None,
                    "loja_origem": {"id": 1, "nome": "Loja"},
                    "devolucao_origem": None,
                },
                {
                    "id": 46,
                    "documento": "VT0000002",
                    "cliente": {"id": 123, "nome": "Fernanda Oliveira Lima", "documento": "12345678901"},
                    "valor_original": "219.90",
                    "saldo_contabil": "219.90",
                    "saldo_reservado": "0.00",
                    "saldo_disponivel": "219.90",
                    "status": "ABERTO",
                    "validade": None,
                    "loja_origem": {"id": 1, "nome": "Loja"},
                    "devolucao_origem": None,
                },
            ]
        }

        with patch("core.services.vales_troca.RetaguardaClient") as client_cls:
            client_cls.return_value.vale_troca_disponiveis.return_value = resposta_central
            resposta = self.client.get(f"/api/terminal/vale-troca/disponiveis/?venda_uuid={venda_uuid}")

        self.assertEqual(resposta.status_code, 200)
        client_cls.return_value.vale_troca_disponiveis.assert_called_once_with(token=self.hub.retaguarda_token, cliente_id=123)
        self.assertEqual([vale["documento"] for vale in resposta.data["vales_troca"]], ["VT0000001", "VT0000002"])
        self.assertEqual(ValeTrocaHub.objects.count(), 1)
        self.assertEqual(ValeTrocaHub.objects.get(retaguarda_id=45).documento, "VT0000001")

    def test_endpoint_vales_troca_disponiveis_central_offline_retorna_erro_sem_espelho(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        ValeTrocaHub.objects.create(
            hub=self.hub,
            retaguarda_id=45,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT-HUB-DEV-antigo",
            valor_original=Decimal("219.90"),
            saldo=Decimal("219.90"),
            sincronizado_em=timezone.now(),
        )

        with patch("core.services.vales_troca.RetaguardaClient") as client_cls:
            client_cls.return_value.vale_troca_disponiveis.side_effect = RetaguardaError("offline")
            resposta = self.client.get(f"/api/terminal/vale-troca/disponiveis/?venda_uuid={venda_uuid}")

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("Central offline. Consulta de Vale-Troca indisponível", resposta.data["detail"])
        self.assertEqual(ValeTrocaHub.objects.get(retaguarda_id=45).documento, "VT-HUB-DEV-antigo")

    def test_vale_retaguarda_nao_pode_ser_consumido_offline(self):
        cliente = self.criar_cliente()
        ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento="VT0000003",
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
                "autorizacao": "VT0000003",
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
            documento="VT0000004",
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

    def test_sync_devolucao_concilia_vale_provisorio_com_credito_liquido(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")

        self._sincronizar_devolucao_confirmada(
            evento,
            devolucao_retaguarda_id=501,
            documento=_devolucao.documento,
            vale_documento="VT0000501",
            vale_retaguarda_id=901,
            valor_total="70.00",
        )
        vale.refresh_from_db()

        self.assertEqual(vale.documento, "VT0000501")
        self.assertEqual(vale.retaguarda_id, 901)
        self.assertEqual(vale.valor_original, Decimal("70.00"))
        self.assertEqual(vale.saldo, Decimal("70.00"))
        self.assertFalse(vale.provisorio)
        self.assertEqual(vale.origem, ValeTrocaHub.ORIGEM_CENTRAL)
        self.assertEqual(
            list(vale.movimentos.filter(tipo=ValeTrocaMovimentoHub.TIPO_ESTORNO).values_list("valor", "saldo_apos")),
            [(Decimal("30.00"), Decimal("70.00"))],
        )

    def test_sync_devolucao_preserva_vale_sem_cashback_quando_valor_confere(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")

        self._sincronizar_devolucao_confirmada(
            evento,
            devolucao_retaguarda_id=502,
            documento=_devolucao.documento,
            vale_documento="VT0000502",
            vale_retaguarda_id=902,
            valor_total="100.00",
        )
        vale.refresh_from_db()

        self.assertEqual(vale.valor_original, Decimal("100.00"))
        self.assertEqual(vale.saldo, Decimal("100.00"))
        self.assertFalse(vale.provisorio)
        self.assertFalse(vale.movimentos.filter(tipo=ValeTrocaMovimentoHub.TIPO_ESTORNO).exists())

    def test_sync_devolucao_credito_liquido_zero_neutraliza_provisorio_sem_documento_oficial(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")
        documento_provisorio = vale.documento

        self._sincronizar_devolucao_confirmada(
            evento,
            devolucao_retaguarda_id=503,
            documento=_devolucao.documento,
            valor_total="0.00",
        )
        vale.refresh_from_db()

        self.assertEqual(vale.documento, documento_provisorio)
        self.assertIsNone(vale.retaguarda_id)
        self.assertEqual(vale.valor_original, Decimal("0.00"))
        self.assertEqual(vale.saldo, Decimal("0.00"))
        self.assertEqual(vale.status, ValeTrocaHub.STATUS_CANCELADO)
        self.assertFalse(vale.provisorio)
        self.assertEqual(
            list(vale.movimentos.filter(tipo=ValeTrocaMovimentoHub.TIPO_ESTORNO).values_list("valor", "saldo_apos")),
            [(Decimal("100.00"), Decimal("0.00"))],
        )

    def test_sync_devolucao_retry_nao_duplica_ajuste_de_conciliacao(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")
        resposta = {
            "devolucao_retaguarda_id": 504,
            "documento": _devolucao.documento,
            "vale_documento": "VT0000504",
            "vale_retaguarda_id": 904,
            "valor_total": "70.00",
        }

        self._sincronizar_devolucao_confirmada(evento, **resposta)
        evento.status = EventoSyncHub.STATUS_PENDENTE
        evento.sincronizado_em = None
        evento.save(update_fields=["status", "sincronizado_em", "atualizado_em"])
        self._sincronizar_devolucao_confirmada(evento, status="DUPLICADO", **resposta)
        vale.refresh_from_db()

        self.assertEqual(vale.saldo, Decimal("70.00"))
        self.assertEqual(vale.movimentos.filter(tipo=ValeTrocaMovimentoHub.TIPO_ESTORNO).count(), 1)

    def test_sync_devolucao_falha_local_preserva_resposta_sem_sincronizar_evento(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")

        with patch("core.services.sync._conciliar_vale_provisorio_confirmado", side_effect=RuntimeError("falha local")):
            self._sincronizar_devolucao_confirmada(
                evento,
                devolucao_retaguarda_id=505,
                documento=_devolucao.documento,
                vale_documento="VT0000505",
                vale_retaguarda_id=905,
                valor_total="70.00",
            )
        evento.refresh_from_db()
        vale.refresh_from_db()

        self.assertEqual(evento.status, EventoSyncHub.STATUS_ERRO)
        self.assertEqual(evento.resposta["mapeamento"]["valor_total"], "70.00")
        self.assertIsNone(evento.sincronizado_em)
        self.assertTrue(vale.provisorio)
        self.assertEqual(vale.saldo, Decimal("100.00"))

        evento.proxima_tentativa_em = None
        evento.save(update_fields=["proxima_tentativa_em", "atualizado_em"])
        self._sincronizar_devolucao_confirmada(
            evento,
            status="DUPLICADO",
            devolucao_retaguarda_id=505,
            documento=_devolucao.documento,
            vale_documento="VT0000505",
            vale_retaguarda_id=905,
            valor_total="70.00",
        )
        evento.refresh_from_db()
        vale.refresh_from_db()

        self.assertEqual(evento.status, EventoSyncHub.STATUS_SINCRONIZADO)
        self.assertEqual(vale.documento, "VT0000505")
        self.assertEqual(vale.saldo, Decimal("70.00"))

    def test_sync_devolucao_mapeamento_ausente_ou_invalido_nao_usa_raiz_da_resposta(self):
        _devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")
        resposta_raiz = {
            "status": "PROCESSADO",
            "devolucao_retaguarda_id": 507,
            "documento": _devolucao.documento,
            "vale_documento": "VT0000507",
            "vale_retaguarda_id": 907,
            "valor_total": "70.00",
        }

        sincronizar_eventos_pendentes(self.hub, client=self._client_sync(evento, resposta_raiz), evento_ids=[evento.pk])
        evento.refresh_from_db()
        vale.refresh_from_db()

        self.assertEqual(evento.status, EventoSyncHub.STATUS_ERRO)
        self.assertIn("mapeamento valido", evento.ultimo_erro)
        self.assertEqual(evento.resposta["valor_total"], "70.00")
        self.assertTrue(vale.provisorio)
        self.assertEqual(vale.saldo, Decimal("100.00"))

        evento.proxima_tentativa_em = None
        evento.save(update_fields=["proxima_tentativa_em", "atualizado_em"])
        sincronizar_eventos_pendentes(
            self.hub,
            client=self._client_sync(evento, {"status": "DUPLICADO", "mapeamento": []}),
            evento_ids=[evento.pk],
        )
        evento.refresh_from_db()

        self.assertEqual(evento.status, EventoSyncHub.STATUS_ERRO)
        self.assertIn("mapeamento valido", evento.ultimo_erro)

    def test_sync_devolucao_provisorio_ja_utilizado_entra_em_conflito_e_bloqueia_dependente(self):
        devolucao, vale, evento = self._criar_devolucao_provisoria("100.00")
        venda_uuid = self.criar_venda_com_item()
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        ValeTrocaMovimentoHub.objects.create(
            vale=vale,
            venda=venda,
            tipo=ValeTrocaMovimentoHub.TIPO_USO,
            valor=Decimal("30.00"),
            saldo_apos=Decimal("70.00"),
            observacao="Uso local antes da confirmação",
        )
        vale.saldo = Decimal("70.00")
        vale.save(update_fields=["saldo", "atualizado_em"])
        evento_venda = EventoSyncHub.objects.create(
            hub=self.hub,
            tipo="VENDA_FINALIZADA",
            chave_idempotencia="VENDA:DEPENDENTE:FINALIZADA",
            payload={"venda_uuid": str(venda.venda_uuid), "dependencias": {"devolucoes_uuid": [str(devolucao.devolucao_uuid)]}},
        )

        self._sincronizar_devolucao_confirmada(
            evento,
            devolucao_retaguarda_id=506,
            documento=devolucao.documento,
            vale_documento="VT0000506",
            vale_retaguarda_id=906,
            valor_total="70.00",
        )
        evento.refresh_from_db()
        vale.refresh_from_db()
        sincronizar_eventos_pendentes(self.hub, client=self._client_sync(evento_venda, {"status": "PROCESSADO"}), evento_ids=[evento_venda.pk])
        evento_venda.refresh_from_db()

        self.assertEqual(evento.status, EventoSyncHub.STATUS_CONFLITO)
        self.assertTrue(vale.provisorio)
        self.assertEqual(vale.status, ValeTrocaHub.STATUS_CANCELADO)
        self.assertEqual(vale.saldo, Decimal("70.00"))
        self.assertEqual(evento_venda.status, EventoSyncHub.STATUS_PENDENTE)
        self.assertIn("Aguardando devolução provisória", evento_venda.ultimo_erro)

    def _criar_devolucao_provisoria(self, valor):
        cliente = self.criar_cliente()
        valor = Decimal(valor)
        devolucao = VendaDevolucaoHub.objects.create(
            hub=self.hub,
            documento="00000012345",
            operador=self.operador,
            terminal=self.terminal,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            motivo="Troca",
            valor_total=valor,
            origem=VendaDevolucaoHub.ORIGEM_VENDA_LOCAL,
            finalizada_em=timezone.now(),
        )
        vale = ValeTrocaHub.objects.create(
            hub=self.hub,
            devolucao=devolucao,
            cliente_uuid=cliente.cliente_uuid,
            cliente_retaguarda_id=cliente.retaguarda_id,
            documento=f"VT-HUB-{devolucao.devolucao_uuid.hex[:20]}",
            valor_original=valor,
            saldo=valor,
            status=ValeTrocaHub.STATUS_ABERTO,
            origem_devolucao_uuid=devolucao.devolucao_uuid,
            origem=ValeTrocaHub.ORIGEM_HUB_PROVISORIO,
            provisorio=True,
        )
        ValeTrocaMovimentoHub.objects.create(
            vale=vale,
            tipo=ValeTrocaMovimentoHub.TIPO_CREDITO,
            valor=valor,
            saldo_apos=valor,
            observacao="Crédito provisório",
        )
        evento = EventoSyncHub.objects.create(
            hub=self.hub,
            tipo="DEVOLUCAO_FINALIZADA",
            chave_idempotencia=f"DEVOLUCAO:{devolucao.devolucao_uuid}:FINALIZADA",
            payload={"devolucao_uuid": str(devolucao.devolucao_uuid), "documento": devolucao.documento, "vale_troca": {"documento": vale.documento}},
        )
        return devolucao, vale, evento

    def _sincronizar_devolucao_confirmada(self, evento, status="PROCESSADO", **resultado):
        payload = {"status": status, "mapeamento": resultado}
        return sincronizar_eventos_pendentes(self.hub, client=self._client_sync(evento, payload), evento_ids=[evento.pk])

    def _client_sync(self, evento, resultado):
        class ClientOk:
            def sync_push(self, **kwargs):
                return {"resultados": [{"chave_idempotencia": evento.chave_idempotencia, **resultado}]}

        return ClientOk()

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

    def test_adicionar_cashback_sem_forma_comercial_cadastrada(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        resposta = self.pagar_cashback(venda_uuid, "30.00")

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(resposta.status_code, 201)
        self.assertIsNone(pagamento.forma_pagamento)
        self.assertEqual(pagamento.codigo, "CASHBACK")
        self.assertEqual(pagamento.tipo, "CASHBACK")
        self.assertFalse(pagamento.gera_recebivel_bancario)

    def test_cashback_sem_forma_comercial_valida_saldo_minimo_limite_e_troco(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(
            cliente,
            saldo="500.00",
            valor_minimo_uso=Decimal("10.00"),
            limite_uso_percentual=Decimal("50.0000"),
        )
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        minimo = self.pagar_cashback(venda_uuid, "5.00")
        limite = self.pagar_cashback(venda_uuid, "120.00")
        saldo = self.pagar_cashback(venda_uuid, "600.00")
        permitido = self.pagar_cashback(venda_uuid, "50.00")
        segundo_cashback = self.pagar_cashback(venda_uuid, "40.00")
        acumulado_limite = self.pagar_cashback(venda_uuid, "10.00")

        self.assertEqual(minimo.status_code, 409)
        self.assertEqual(minimo.data["detail"], "O valor de cashback usado é menor que o mínimo configurado.")
        self.assertEqual(limite.status_code, 409)
        self.assertEqual(limite.data["detail"], "O cashback usado ultrapassa o limite permitido para a venda.")
        self.assertEqual(saldo.status_code, 409)
        self.assertEqual(saldo.data["detail"], "O cashback informado é maior que o saldo disponível do cliente.")
        self.assertEqual(permitido.status_code, 201)
        self.assertEqual(segundo_cashback.status_code, 201)
        self.assertEqual(acumulado_limite.status_code, 409)
        self.assertEqual(acumulado_limite.data["detail"], "O cashback usado ultrapassa o limite permitido para a venda.")

    def test_cashback_sem_forma_comercial_rejeita_soma_acima_do_saldo(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="80.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        primeiro = self.pagar_cashback(venda_uuid, "50.00")
        segundo = self.pagar_cashback(venda_uuid, "31.00")

        self.assertEqual(primeiro.status_code, 201)
        self.assertEqual(segundo.status_code, 409)
        self.assertEqual(segundo.data["detail"], "O cashback informado é maior que o saldo disponível do cliente.")

    def test_cashback_sem_forma_comercial_nao_gera_troco(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="500.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.pagar(venda_uuid, self.pix, "150.00")

        resposta = self.pagar_cashback(venda_uuid, "60.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Cashback não pode gerar troco; use apenas o saldo pendente da venda.")

    def test_retry_cashback_sem_forma_comercial_nao_duplica(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()

        primeira = self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid)
        segunda = self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid)
        divergente = self.pagar_cashback(venda_uuid, "31.00", operacao_uuid=operacao_uuid)

        self.assertEqual(primeira.status_code, 201)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(divergente.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(
            VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO).count(),
            1,
        )

    def test_venda_mista_com_cashback_sem_forma_comercial_finaliza(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="50.00", percentual=Decimal("5.0000"))
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        cashback = self.pagar_cashback(venda_uuid, "30.00")
        pix = self.pagar(venda_uuid, self.pix, "169.90")
        VendaHub.objects.filter(venda_uuid=venda_uuid).update(documento="VMISTA01")
        finalizacao = self.finalizar(venda_uuid)

        self.assertEqual(cashback.status_code, 201)
        self.assertEqual(pix.status_code, 201)
        self.assertEqual(finalizacao.status_code, 200, finalizacao.data)
        self.assertTrue(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_DEBITO, valor=Decimal("30.00")).exists())
        self.assertTrue(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_CREDITO, valor=Decimal("8.50")).exists())

    def test_finalizacao_rejeita_combinacao_invalida_persistida_previamente(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="30.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=None,
            codigo="CASHBACK",
            descricao="Cashback",
            tipo="CASHBACK",
            num_parcelas=1,
            valor=Decimal("30.00"),
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=None,
            codigo="VT",
            descricao="Vale-Troca",
            tipo="VALE_TROCA",
            num_parcelas=1,
            valor=Decimal("40.00"),
            autorizacao="VT-HUB-11111111111111111111",
            vale_troca_documento="VT-HUB-11111111111111111111",
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=self.dinheiro,
            retaguarda_forma_pagamento_id=self.dinheiro.retaguarda_id,
            codigo=self.dinheiro.codigo,
            descricao=self.dinheiro.descricao,
            tipo=self.dinheiro.tipo,
            num_parcelas=1,
            valor=Decimal("129.90"),
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )

        resposta = self.finalizar(venda_uuid)

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], MENSAGEM_CASHBACK_VALE_TROCA)
        self.assertFalse(CashbackMovimentoHub.objects.filter(tipo=CashbackMovimentoHub.TIPO_DEBITO).exists())
        self.assertFalse(ValeTrocaMovimentoHub.objects.exists())

    def test_retry_cashback_existente_preservado_mesmo_com_venda_antiga_invalida(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="30.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()
        self.assertEqual(self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid).status_code, 201)
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=None,
            codigo="VT",
            descricao="Vale-Troca",
            tipo="VALE_TROCA",
            num_parcelas=1,
            valor=Decimal("40.00"),
            autorizacao="VT-HUB-11111111111111111111",
            vale_troca_documento="VT-HUB-11111111111111111111",
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )

        retry = self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid)

        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(VendaPagamentoHub.objects.filter(tipo="CASHBACK").count(), 1)

    def test_retry_vale_troca_existente_preservado_mesmo_com_venda_antiga_invalida(self):
        cliente = self.criar_cliente()
        self.criar_vale_troca(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()
        self.assertEqual(self.pagar_vale_troca(venda_uuid, operacao_uuid=operacao_uuid).status_code, 201)
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=None,
            codigo="CASHBACK",
            descricao="Cashback",
            tipo="CASHBACK",
            num_parcelas=1,
            valor=Decimal("30.00"),
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )

        retry = self.pagar_vale_troca(venda_uuid, operacao_uuid=operacao_uuid)

        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(VendaPagamentoHub.objects.filter(tipo="VALE_TROCA").count(), 1)

    def test_multiplos_cashbacks_geram_debito_unico_total_e_finalizacao_idempotente(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="100.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.pagar_cashback(venda_uuid, "30.00")
        self.pagar_cashback(venda_uuid, "20.00")
        self.pagar(venda_uuid, self.pix, "149.90")
        VendaHub.objects.filter(venda_uuid=venda_uuid).update(documento="VCASH01")

        primeira = self.finalizar(venda_uuid)
        segunda = self.finalizar(venda_uuid)

        debitos = CashbackMovimentoHub.objects.filter(venda__venda_uuid=venda_uuid, tipo=CashbackMovimentoHub.TIPO_DEBITO)
        self.assertEqual(primeira.status_code, 200, primeira.data)
        self.assertEqual(segunda.status_code, 200, segunda.data)
        self.assertEqual(debitos.count(), 1)
        self.assertEqual(debitos.get().valor, Decimal("50.00"))

    def test_cashback_revalida_saldo_na_finalizacao_em_disputa_de_terminais(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente, saldo="80.00")
        venda_uuid_1 = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.pagar_cashback(venda_uuid_1, "80.00")
        self.pagar(venda_uuid_1, self.pix, "119.90")
        VendaHub.objects.filter(venda_uuid=venda_uuid_1).update(documento="VCASH02")

        terminal_2 = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        token_terminal_2 = terminal_2.gerar_token()
        terminal_2.save()
        sessao_operador_2, token_operador_2 = self.criar_sessao_operador(terminal_2, self.operador)
        cliente_2 = APIClient()
        cliente_2.credentials(
            HTTP_AUTHORIZATION=f"Terminal {token_terminal_2}",
            HTTP_X_SYSVAR_OPERADOR_SESSION=token_operador_2,
        )
        cliente_2.post("/api/terminal/venda/iniciar/", {}, format="json")
        venda_2 = cliente_2.post(
            "/api/terminal/venda/item/",
            {"sku_id": self.catalogo_item.retaguarda_sku_id, "quantidade": 1},
            format="json",
        ).data["venda"]["uuid"]
        cliente_2.put("/api/terminal/venda/cliente/", {"cliente_uuid": str(cliente.cliente_uuid)}, format="json")
        vendedor = self.criar_vendedor_pagamento(retaguarda_id=9902)
        cliente_2.put("/api/terminal/venda/vendedor/", {"vendedor_id": vendedor.retaguarda_id}, format="json")
        pagamento_cashback_2 = cliente_2.post(
            "/api/terminal/venda/pagamento/cashback/",
            {"venda_uuid": venda_2, "operacao_uuid": str(uuid.uuid4()), "valor": "80.00"},
            format="json",
        )
        pagamento_pix_2 = cliente_2.post(
            "/api/terminal/venda/pagamento/",
            {
                "venda_uuid": venda_2,
                "operacao_uuid": str(uuid.uuid4()),
                "forma_pagamento_id": self.pix.id,
                "valor": "119.90",
            },
            format="json",
        )
        VendaHub.objects.filter(venda_uuid=venda_2).update(documento="VCASH03")

        primeira = self.finalizar(venda_uuid_1)
        segunda = cliente_2.post("/api/terminal/venda/finalizar/", {"venda_uuid": venda_2}, format="json")

        self.assertEqual(pagamento_cashback_2.status_code, 201)
        self.assertEqual(pagamento_pix_2.status_code, 201)
        self.assertEqual(primeira.status_code, 200, primeira.data)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(segunda.data["detail"], "O cashback informado é maior que o saldo disponível do cliente.")
        self.assertEqual(
            CashbackMovimentoHub.objects.filter(cliente_retaguarda_id=cliente.retaguarda_id, tipo=CashbackMovimentoHub.TIPO_DEBITO).count(),
            1,
        )
        saldo = sum(
            mov.valor if mov.tipo == CashbackMovimentoHub.TIPO_CREDITO else -mov.valor
            for mov in CashbackMovimentoHub.objects.filter(cliente_retaguarda_id=cliente.retaguarda_id)
        )
        self.assertEqual(saldo, Decimal("0.00"))

    def test_payload_sync_cashback_sem_forma_comercial_mantem_tipo_reconhecivel(self):
        cliente = self.criar_cliente()
        self.criar_cashback_disponivel(cliente)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        self.pagar_cashback(venda_uuid, "30.00")
        self.pagar(venda_uuid, self.pix, "169.90")

        evento = enfileirar_venda_finalizada(VendaHub.objects.get(venda_uuid=venda_uuid))
        cashback_payload = next(pagamento for pagamento in evento.payload["pagamentos"] if pagamento["tipo"] == "CASHBACK")

        self.assertEqual(cashback_payload["codigo"], "CASHBACK")
        self.assertEqual(cashback_payload["descricao"], "Cashback")
        self.assertIsNone(cashback_payload["forma_pagamento_id"])
        self.assertEqual(cashback_payload["valor"], "30.00")
        self.assertNotIn("cashback_reserva_id", cashback_payload)

    def test_cashback_online_consulta_saldo_oficial(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        resposta_central = {
            "cashback": {
                "saldo_oficial": "100.00",
                "valor_reservado": "25.00",
                "saldo_disponivel": "75.00",
            }
        }

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_saldo.return_value = resposta_central
            resposta = self.client.get(f"/api/terminal/venda/cashback-online/saldo/?venda_uuid={venda_uuid}")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(resposta.data["cashback"]["saldo_disponivel"], "75.00")
        client_cls.return_value.cashback_saldo.assert_called_once_with(token=self.hub.retaguarda_token, cliente_id=123)

    def test_cashback_online_reserva_pagamento_e_retry_sem_duplicar(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="40.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {
                    "id": 55,
                    "cliente_id": 123,
                    "valor": "30.00",
                    "status": "RESERVADA",
                }
            }
            primeira = self.pagar_cashback_online(venda_uuid, "30.00", operacao_uuid=operacao_uuid)
            segunda = self.pagar_cashback_online(venda_uuid, "30.00", operacao_uuid=operacao_uuid)
            conflito = self.pagar_cashback_online(venda_uuid, "31.00", operacao_uuid=operacao_uuid)

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(primeira.status_code, 201, primeira.data)
        self.assertEqual(segunda.status_code, 200, segunda.data)
        self.assertEqual(conflito.status_code, 409)
        self.assertEqual(client_cls.return_value.cashback_reservar_venda.call_count, 1)
        self.assertEqual(pagamento.cashback_origem, VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL)
        self.assertEqual(pagamento.cashback_reserva_id, 55)
        self.assertEqual(pagamento.cashback_valor_reservado, Decimal("30.00"))

    def test_mesma_operacao_nao_troca_cashback_local_por_central(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="100.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()
        local = self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid)

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            central = self.pagar_cashback_online(venda_uuid, "30.00", operacao_uuid=operacao_uuid)

        self.assertEqual(local.status_code, 201, local.data)
        self.assertEqual(central.status_code, 409)
        client_cls.return_value.cashback_reservar_venda.assert_not_called()
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(VendaPagamentoHub.objects.get().cashback_origem, VendaPagamentoHub.CASHBACK_ORIGEM_LOCAL)

    def test_mesma_operacao_nao_troca_cashback_central_por_local(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="100.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        operacao_uuid = uuid.uuid4()
        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 56, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            central = self.pagar_cashback_online(venda_uuid, "30.00", operacao_uuid=operacao_uuid)
        local = self.pagar_cashback(venda_uuid, "30.00", operacao_uuid=operacao_uuid)

        self.assertEqual(central.status_code, 201, central.data)
        self.assertEqual(local.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(VendaPagamentoHub.objects.get().cashback_origem, VendaPagamentoHub.CASHBACK_ORIGEM_CENTRAL)

    def test_cashback_online_central_indisponivel_nao_vira_offline(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="100.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.side_effect = RetaguardaError("offline")
            resposta = self.pagar_cashback_online(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("Central ONLINE obrigatória", resposta.data["detail"])
        self.assertFalse(VendaPagamentoHub.objects.exists())
        self.assertEqual(self.pagar_cashback(venda_uuid, "30.00").status_code, 201)

    def test_cashback_online_rejeita_reserva_de_outro_cliente(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 66, "cliente_id": 456, "valor": "30.00", "status": "RESERVADA"}
            }
            resposta = self.pagar_cashback_online(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("outro cliente", resposta.data["detail"])
        self.assertFalse(VendaPagamentoHub.objects.exists())

    def test_cashback_online_falha_local_apos_reserva_cancela_reserva(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        with patch("core.services.cashback_online.RetaguardaClient") as client_cls, patch("core.services.cashback_online.VendaPagamentoHub.objects.create") as create:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 90, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            create.side_effect = RuntimeError("falha local")
            resposta = self.pagar_cashback_online(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("foi cancelada na Central", resposta.data["detail"])
        self.assertIn("falha local", resposta.data["detail"])
        client_cls.return_value.cashback_cancelar_reserva.assert_called_once()
        self.assertFalse(VendaPagamentoHub.objects.exists())

    def test_cashback_online_falha_local_e_falha_compensacao_informa_reserva(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        with patch("core.services.cashback_online.RetaguardaClient") as client_cls, patch("core.services.cashback_online.VendaPagamentoHub.objects.create") as create:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 91, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            client_cls.return_value.cashback_cancelar_reserva.side_effect = RetaguardaError("central offline")
            create.side_effect = RuntimeError("falha local")
            resposta = self.pagar_cashback_online(venda_uuid, "30.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("Reserva 91", resposta.data["detail"])
        self.assertIn("não foi cancelada automaticamente", resposta.data["detail"])
        self.assertIn("falha local", resposta.data["detail"])
        client_cls.return_value.cashback_cancelar_reserva.assert_called_once()
        self.assertFalse(VendaPagamentoHub.objects.exists())

    def test_venda_mista_cashback_local_central_pix_debita_apenas_local_e_sync_reserva_online(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="40.00", percentual=Decimal("5.0000"))
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)

        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 77, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            online = self.pagar_cashback_online(venda_uuid, "30.00", operacao_uuid=uuid.UUID("11111111-1111-4111-8111-111111111111"))
        local = self.pagar_cashback(venda_uuid, "20.00", operacao_uuid=uuid.UUID("22222222-2222-4222-8222-222222222222"))
        pix = self.pagar(venda_uuid, self.pix, "149.90")
        VendaHub.objects.filter(venda_uuid=venda_uuid).update(documento="VCBKON1")

        finalizacao = self.finalizar(venda_uuid)
        evento = enfileirar_venda_finalizada(VendaHub.objects.get(venda_uuid=venda_uuid))
        pagamentos = sorted(evento.payload["pagamentos"], key=lambda item: item["valor"])

        self.assertEqual(online.status_code, 201, online.data)
        self.assertEqual(local.status_code, 201, local.data)
        self.assertEqual(pix.status_code, 201, pix.data)
        self.assertEqual(finalizacao.status_code, 200, finalizacao.data)
        debitos = CashbackMovimentoHub.objects.filter(venda__venda_uuid=venda_uuid, tipo=CashbackMovimentoHub.TIPO_DEBITO)
        self.assertEqual(debitos.count(), 1)
        self.assertEqual(debitos.get().valor, Decimal("20.00"))
        cashback_online_payload = next(p for p in pagamentos if p["tipo"] == "CASHBACK" and p["valor"] == "30.00")
        cashback_local_payload = next(p for p in pagamentos if p["tipo"] == "CASHBACK" and p["valor"] == "20.00")
        self.assertEqual(cashback_online_payload["cashback_reserva_id"], 77)
        self.assertNotIn("cashback_reserva_id", cashback_local_payload)

    def test_remover_cashback_online_cancela_reserva(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 88, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            pagamento = self.pagar_cashback_online(venda_uuid, "30.00").data["venda"]["pagamentos"][0]
            resposta = self.client.delete(f"/api/terminal/venda/pagamento/{pagamento['uuid']}/")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        client_cls.return_value.cashback_cancelar_reserva.assert_called_once()
        self.assertEqual(VendaPagamentoHub.objects.get().status, VendaPagamentoHub.STATUS_REMOVIDO)

    def test_falha_cancelamento_cashback_online_nao_remove_pagamento(self):
        cliente = self.criar_cliente(retaguarda_id=123)
        self.criar_cashback_disponivel(cliente, saldo="0.00")
        venda_uuid = self.criar_venda_com_item()
        self.put_cliente(cliente)
        with patch("core.services.cashback_online.RetaguardaClient") as client_cls:
            client_cls.return_value.cashback_reservar_venda.return_value = {
                "reserva": {"id": 89, "cliente_id": 123, "valor": "30.00", "status": "RESERVADA"}
            }
            pagamento = self.pagar_cashback_online(venda_uuid, "30.00").data["venda"]["pagamentos"][0]
            client_cls.return_value.cashback_cancelar_reserva.side_effect = RetaguardaError("offline")
            resposta = self.client.delete(f"/api/terminal/venda/pagamento/{pagamento['uuid']}/")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.get().status, VendaPagamentoHub.STATUS_ATIVO)

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

    def test_credito_sem_permite_parcelamento_nao_exige_prazo(self):
        venda_uuid = self.criar_venda_com_item()

        resposta = self.pagar(venda_uuid, self.credito)

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(pagamento.codigo, "CRE")
        self.assertIsNone(pagamento.prazo_pagamento)
        self.assertIsNone(pagamento.retaguarda_forma_pagamento_condicao_id)
        self.assertEqual(pagamento.num_parcelas, self.credito.num_parcelas)
        self.assertEqual(list(pagamento.parcelas_snapshot.order_by("ordem").values_list("dias", flat=True)), [0])

    def test_forma_com_permite_parcelamento_exige_condicao_ativa_e_usa_taxas(self):
        self.credito.permite_parcelamento = True
        self.credito.save(update_fields=["permite_parcelamento", "atualizado_em"])
        condicao = self.criar_condicao(
            self.credito,
            self.prazo_2x,
            retaguarda_id=9200,
            taxa_percentual=Decimal("2.5000"),
            taxa_fixa=Decimal("0.50"),
        )
        venda_uuid = self.criar_venda_com_item()

        sem_prazo = self.pagar(venda_uuid, self.credito)
        prazo_nao_vinculado = self.pagar(venda_uuid, self.credito, prazo=self.prazo_1x)
        resposta = self.pagar(venda_uuid, self.credito, prazo=self.prazo_2x)

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(sem_prazo.status_code, 400)
        self.assertEqual(prazo_nao_vinculado.status_code, 400)
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(pagamento.retaguarda_forma_pagamento_condicao_id, condicao.retaguarda_id)
        self.assertEqual(pagamento.taxa_percentual, Decimal("2.5000"))
        self.assertEqual(pagamento.taxa_fixa, Decimal("0.50"))
        self.assertEqual(list(pagamento.parcelas_snapshot.order_by("ordem").values_list("dias", flat=True)), [30, 60])

    def test_pix_com_permite_parcelamento_exige_condicao_ativa(self):
        self.pix.permite_parcelamento = True
        self.pix.save(update_fields=["permite_parcelamento", "atualizado_em"])
        self.criar_condicao(self.pix, self.prazo_1x, retaguarda_id=9250)
        venda_uuid = self.criar_venda_com_item()

        sem_prazo = self.pagar(venda_uuid, self.pix)
        resposta = self.pagar(venda_uuid, self.pix, prazo=self.prazo_1x)

        pagamento = VendaPagamentoHub.objects.get()
        self.assertEqual(sem_prazo.status_code, 400)
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(pagamento.tipo, "PIX")
        self.assertEqual(pagamento.prazo_pagamento, self.prazo_1x)

    def test_payload_sync_pagamento_preserva_condicao_prazo_taxas_e_parcelas(self):
        self.credito.permite_parcelamento = True
        self.credito.save(update_fields=["permite_parcelamento", "atualizado_em"])
        condicao = self.criar_condicao(
            self.credito,
            self.prazo_2x,
            retaguarda_id=9300,
            taxa_percentual=Decimal("2.7500"),
            taxa_fixa=Decimal("1.10"),
        )
        venda_uuid = self.criar_venda_com_item()
        self.pagar(venda_uuid, self.credito, prazo=self.prazo_2x)

        evento = enfileirar_venda_finalizada(VendaHub.objects.get(venda_uuid=venda_uuid))
        pagamento_payload = evento.payload["pagamentos"][0]

        self.assertEqual(pagamento_payload["forma_pagamento_condicao_id"], condicao.retaguarda_id)
        self.assertEqual(pagamento_payload["prazo_pagamento_id"], self.prazo_2x.retaguarda_id)
        self.assertEqual(pagamento_payload["taxa_percentual"], "2.7500")
        self.assertEqual(pagamento_payload["taxa_fixa"], "1.10")
        self.assertEqual([parcela["dias"] for parcela in pagamento_payload["parcelas"]], [30, 60])

    def test_condicoes_oficiais_deb_e_credito_preservam_snapshot_e_payload(self):
        self.credito.permite_parcelamento = True
        self.credito.save(update_fields=["permite_parcelamento", "atualizado_em"])
        debito = self.criar_forma("DEB", "DEBITO", permite_parcelamento=True)
        prazo_av = self.criar_prazo("AV", 1, [0], retaguarda_id=7010, intervalo_dias=0)
        prazo_3x = self.criar_prazo("30-60-90", 3, [30, 60, 90], retaguarda_id=7011)
        cenarios = [
            (debito, prazo_av, self.criar_condicao(debito, prazo_av, retaguarda_id=9500, taxa_percentual=Decimal("0.0000"), taxa_fixa=Decimal("0.00")), [0], Decimal("0.0000"), "49.00"),
            (self.credito, self.prazo_1x, self.criar_condicao(self.credito, self.prazo_1x, retaguarda_id=9501, taxa_percentual=Decimal("2.0000"), taxa_fixa=Decimal("0.00")), [30], Decimal("2.0000"), "50.00"),
            (self.credito, self.prazo_2x, self.criar_condicao(self.credito, self.prazo_2x, retaguarda_id=9502, taxa_percentual=Decimal("2.5000"), taxa_fixa=Decimal("0.00")), [30, 60], Decimal("2.5000"), "50.00"),
            (self.credito, prazo_3x, self.criar_condicao(self.credito, prazo_3x, retaguarda_id=9503, taxa_percentual=Decimal("2.5000"), taxa_fixa=Decimal("0.00")), [30, 60, 90], Decimal("2.5000"), "50.90"),
        ]
        venda_uuid = self.criar_venda_com_item()

        for forma, prazo, condicao, dias, taxa, valor in cenarios:
            with self.subTest(forma=forma.codigo, prazo=prazo.codigo):
                resposta = self.pagar(venda_uuid, forma, valor=valor, prazo=prazo)
                pagamento = VendaPagamentoHub.objects.get(venda__venda_uuid=venda_uuid, forma_pagamento=forma, prazo_pagamento=prazo)

                self.assertEqual(resposta.status_code, 201)
                self.assertEqual(pagamento.retaguarda_forma_pagamento_condicao_id, condicao.retaguarda_id)
                self.assertEqual(pagamento.retaguarda_prazo_pagamento_id, prazo.retaguarda_id)
                self.assertEqual(pagamento.num_parcelas, prazo.num_parcelas)
                self.assertEqual(pagamento.taxa_percentual, taxa)
                self.assertEqual(pagamento.taxa_fixa, Decimal("0.00"))
                self.assertEqual(list(pagamento.parcelas_snapshot.order_by("ordem").values_list("dias", flat=True)), dias)

                condicao.taxa_percentual = Decimal("9.0000")
                condicao.save(update_fields=["taxa_percentual", "atualizado_em"])
                pagamento.refresh_from_db()
                self.assertEqual(pagamento.taxa_percentual, taxa)
                self.assertEqual(list(pagamento.parcelas_snapshot.order_by("ordem").values_list("dias", flat=True)), dias)

        payloads = {
            pagamento["forma_pagamento_condicao_id"]: pagamento
            for pagamento in enfileirar_venda_finalizada(VendaHub.objects.get(venda_uuid=venda_uuid)).payload["pagamentos"]
        }
        for _forma, prazo, condicao, dias, taxa, _valor in cenarios:
            with self.subTest(payload=condicao.retaguarda_id):
                payload = payloads[condicao.retaguarda_id]
                self.assertEqual(payload["forma_pagamento_condicao_id"], condicao.retaguarda_id)
                self.assertEqual(payload["prazo_pagamento_id"], prazo.retaguarda_id)
                self.assertEqual(payload["prazo_codigo"], prazo.codigo)
                self.assertEqual(payload["num_parcelas"], prazo.num_parcelas)
                self.assertEqual(payload["taxa_percentual"], f"{taxa:.4f}")
                self.assertEqual([parcela["dias"] for parcela in payload["parcelas"]], dias)

    def test_retry_com_prazo_diferente_retorna_409_quando_parcelado(self):
        self.credito.permite_parcelamento = True
        self.credito.save(update_fields=["permite_parcelamento", "atualizado_em"])
        self.criar_condicao(self.credito, self.prazo_1x, retaguarda_id=9400)
        self.criar_condicao(self.credito, self.prazo_2x, retaguarda_id=9401)
        venda_uuid = self.criar_venda_com_item()
        operacao_uuid = uuid.uuid4()
        self.pagar(venda_uuid, self.credito, operacao_uuid=operacao_uuid, prazo=self.prazo_1x)

        resposta = self.pagar(venda_uuid, self.credito, operacao_uuid=operacao_uuid, prazo=self.prazo_2x)

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(VendaPagamentoHub.objects.count(), 1)
        self.assertEqual(VendaPagamentoHub.objects.get().prazo_pagamento, self.prazo_1x)

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
