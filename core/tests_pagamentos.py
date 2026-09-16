import uuid
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import (
    CatalogoItemHub,
    EstoqueMovimentoHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    SessaoCaixaHub,
    VendaEventoHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
    VendaPagamentoParcelaHub,
    VendedorHub,
)
from core.services.caixa import fechar_caixa
from core.services.terminais import configurar_terminal
from core.services.vendas import calcular_disponivel_local, listar_formas_pagamento
from core.tests_caixa import criar_hub
from core.tests_vendas import VendaHubTestMixin


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


class VendaFinalizacaoTests(PagamentoHubTestMixin, TestCase):
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
        fechar_caixa(self.terminal, self.operador, self.sessao_operador)
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
