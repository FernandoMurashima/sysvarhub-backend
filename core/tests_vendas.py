from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from core.models import CatalogoItemHub, SessaoCaixaHub, VendaEventoHub, VendaHub, VendaItemHub
from core.services.caixa import CaixaConflictError, abrir_caixa, fechar_caixa
from core.services.operadores import encerrar_sessao
from core.services.terminais import configurar_terminal
from core.services.vendas import (
    adicionar_item,
    calcular_reserva_sku,
    calcular_total_item,
    obter_ou_criar_venda_aberta,
    validar_quantidade,
    validar_sku_id,
)
from core.tests_caixa import CaixaHubTestMixin


class VendaHubTestMixin(CaixaHubTestMixin):
    def setUp(self):
        super().setUp()
        self.sessao_caixa = abrir_caixa(
            self.terminal,
            self.operador,
            self.sessao_operador,
            valor_abertura="100.00",
        )
        self.catalogo_item = self.criar_catalogo_item(10825, estoque="4.000")
        self.autenticar()

    def criar_catalogo_item(self, sku_id, **overrides):
        dados = {
            "hub": self.hub,
            "retaguarda_produto_id": 2050,
            "retaguarda_sku_id": sku_id,
            "tipo_produto": "1",
            "referencia": "27-01-01001",
            "descricao": "Calça Jeans Reta Aurora",
            "descricao_reduzida": "Calça Jeans",
            "ean13": "7892701000013",
            "codigo_item_ref": "00001",
            "cor_descricao": "Jeans",
            "tamanho_descricao": "34",
            "unidade_codigo": "UN",
            "preco": Decimal("199.9000"),
            "preco_venda": Decimal("199.9000"),
            "estoque_fisico": Decimal(overrides.pop("estoque", "4.000")),
            "reserva": Decimal("0.000"),
            "estoque_disponivel": Decimal(overrides.pop("estoque_disponivel", "4.000")),
            "vendavel": True,
            "motivos_bloqueio": [],
            "fiscal": {},
            "ativo": True,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return CatalogoItemHub.objects.create(**dados)

    def post_item(self, body=None):
        payload = {"sku_id": self.catalogo_item.retaguarda_sku_id, "quantidade": 1}
        if body:
            payload.update(body)
        return self.client.post("/api/terminal/venda/item/", payload, format="json")


class VendaAtualApiTests(VendaHubTestMixin, TestCase):
    def test_nenhum_item_venda_atual_null(self):
        resposta = self.client.get("/api/terminal/venda/atual/")

        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.data["venda"])

    def test_primeira_inclusao_cria_venda_hub(self):
        resposta = self.post_item()

        self.assertEqual(resposta.status_code, 201)
        venda = VendaHub.objects.get()
        self.assertEqual(venda.hub, self.hub)
        self.assertEqual(venda.sessao_caixa, self.sessao_caixa)
        self.assertEqual(venda.terminal, self.terminal)
        self.assertEqual(venda.operador_criacao, self.operador)

    def test_uma_venda_aberta_por_terminal_e_refresh_recupera(self):
        primeira = self.post_item()
        segunda = self.client.get("/api/terminal/venda/atual/")

        self.assertEqual(VendaHub.objects.filter(status=VendaHub.STATUS_ABERTA).count(), 1)
        self.assertEqual(primeira.data["venda"]["uuid"], segunda.data["venda"]["uuid"])

    def test_troca_operador_recupera_mesma_venda(self):
        resposta = self.post_item()
        operador_b = self.criar_operador("operador.b", 91, "Operador B")
        encerrar_sessao(self.sessao_operador)
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, operador_b)
        self.autenticar()

        atual = self.client.get("/api/terminal/venda/atual/")

        self.assertEqual(atual.data["venda"]["uuid"], resposta.data["venda"]["uuid"])
        self.assertEqual(atual.data["venda"]["operador_criacao"]["codigo"], "caixa.barra")

    def test_outro_terminal_tem_venda_propria(self):
        self.post_item()
        outra_caixa = self.caixa
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", outra_caixa.retaguarda_id)
        outro_token_terminal = outro_terminal.gerar_token()
        outro_terminal.save()
        outra_sessao_operador, outro_token_operador = self.criar_sessao_operador(outro_terminal, self.operador)
        self.autenticar(outro_token_terminal, outro_token_operador)

        resposta = self.client.post(
            "/api/terminal/venda/item/",
            {"sku_id": self.catalogo_item.retaguarda_sku_id, "quantidade": 1},
            format="json",
        )

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(VendaHub.objects.filter(status=VendaHub.STATUS_ABERTA).count(), 2)
        self.assertEqual(outra_sessao_operador.operador, self.operador)


class VendaItemApiTests(VendaHubTestMixin, TestCase):
    def test_sku_valido_adiciona_snapshot_e_preco_do_catalogo(self):
        resposta = self.post_item(body={"preco": "1.00", "terminal_id": 999, "operador_id": 999})
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(item.retaguarda_produto_id, self.catalogo_item.retaguarda_produto_id)
        self.assertEqual(item.retaguarda_sku_id, self.catalogo_item.retaguarda_sku_id)
        self.assertEqual(item.descricao, self.catalogo_item.descricao)
        self.assertEqual(item.preco_unitario, Decimal("199.9000"))
        self.assertEqual(item.total_item, Decimal("199.90"))
        self.assertEqual(item.terminal_inclusao, self.terminal)
        self.assertEqual(item.operador_inclusao, self.operador)

    def test_segunda_bipagem_incrementa_mesma_linha(self):
        self.post_item()
        resposta = self.post_item()
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(item.quantidade, 2)
        self.assertEqual(VendaItemHub.objects.count(), 1)
        self.assertEqual(VendaHub.objects.get().total, Decimal("399.80"))

    def test_alterar_quantidade_recalcula(self):
        resposta = self.post_item()
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]

        alterada = self.client.patch(
            f"/api/terminal/venda/item/{item_uuid}/",
            {"quantidade": 2},
            format="json",
        )

        self.assertEqual(alterada.status_code, 200)
        self.assertEqual(alterada.data["venda"]["total"], "399.80")

    def test_remover_item_libera_e_mantem_venda_aberta(self):
        resposta = self.post_item()
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]

        removida = self.client.delete(f"/api/terminal/venda/item/{item_uuid}/")

        self.assertEqual(removida.status_code, 200)
        self.assertEqual(removida.data["venda"]["itens"], [])
        self.assertEqual(VendaHub.objects.get().status, VendaHub.STATUS_ABERTA)


class VendaReservaTests(VendaHubTestMixin, TestCase):
    def test_mesma_venda_nao_excede_estoque_ao_bipar(self):
        self.post_item({"quantidade": 3})

        resposta = self.post_item({"quantidade": 2})
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Saldo disponível insuficiente.")
        self.assertEqual(resposta.data["estoque_disponivel"], "1.000")
        self.assertEqual(item.quantidade, 3)
        self.assertEqual(calcular_reserva_sku(self.hub, self.catalogo_item.retaguarda_sku_id), Decimal("3"))

    def test_mesma_venda_nao_excede_estoque_por_patch(self):
        resposta = self.post_item({"quantidade": 3})
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]

        patch_resposta = self.client.patch(
            f"/api/terminal/venda/item/{item_uuid}/",
            {"quantidade": 5},
            format="json",
        )
        item = VendaItemHub.objects.get()

        self.assertEqual(patch_resposta.status_code, 409)
        self.assertEqual(patch_resposta.data["estoque_disponivel"], "1.000")
        self.assertEqual(item.quantidade, 3)

    def test_limite_exato_permitido_e_novo_incremento_bloqueado(self):
        self.post_item({"quantidade": 3})
        resposta_limite = self.post_item({"quantidade": 1})
        resposta_excesso = self.post_item({"quantidade": 1})
        item = VendaItemHub.objects.get()

        self.assertEqual(resposta_limite.status_code, 200)
        self.assertEqual(item.quantidade, 4)
        self.assertEqual(resposta_excesso.status_code, 409)
        self.assertEqual(resposta_excesso.data["estoque_disponivel"], "0.000")
        item.refresh_from_db()
        self.assertEqual(item.quantidade, 4)

    def test_reducao_libera_saldo_para_nova_inclusao(self):
        resposta = self.post_item({"quantidade": 3})
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]

        self.client.patch(f"/api/terminal/venda/item/{item_uuid}/", {"quantidade": 2}, format="json")
        nova = self.post_item({"quantidade": 2})
        item = VendaItemHub.objects.get()

        self.assertEqual(nova.status_code, 200)
        self.assertEqual(item.quantidade, 4)

    def test_reserva_total_nunca_fica_maior_que_estoque(self):
        self.post_item({"quantidade": 3})
        self.post_item({"quantidade": 2})

        self.assertLessEqual(
            calcular_reserva_sku(self.hub, self.catalogo_item.retaguarda_sku_id),
            self.catalogo_item.estoque_disponivel,
        )

    def test_reserva_considera_vendas_abertas_e_dois_terminais(self):
        self.post_item({"quantidade": 2})
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", self.caixa.retaguarda_id)
        outro_token_terminal = outro_terminal.gerar_token()
        outro_terminal.save()
        outra_sessao_operador, outro_token_operador = self.criar_sessao_operador(outro_terminal, self.operador)
        self.autenticar(outro_token_terminal, outro_token_operador)

        resposta = self.client.post(
            "/api/terminal/venda/item/",
            {"sku_id": self.catalogo_item.retaguarda_sku_id, "quantidade": 3},
            format="json",
        )

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Saldo disponível insuficiente.")
        self.assertEqual(resposta.data["estoque_disponivel"], "2.000")
        self.assertEqual(calcular_reserva_sku(self.hub, self.catalogo_item.retaguarda_sku_id), Decimal("2"))

    def test_reducao_remocao_e_cancelamento_liberam_reserva_sem_alterar_catalogo(self):
        resposta = self.post_item({"quantidade": 3})
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]
        self.client.patch(f"/api/terminal/venda/item/{item_uuid}/", {"quantidade": 2}, format="json")
        self.client.delete(f"/api/terminal/venda/item/{item_uuid}/")
        self.post_item({"quantidade": 1})
        self.client.post("/api/terminal/venda/cancelar/", {}, format="json")
        self.catalogo_item.refresh_from_db()

        self.assertEqual(calcular_reserva_sku(self.hub, self.catalogo_item.retaguarda_sku_id), Decimal("0"))
        self.assertEqual(self.catalogo_item.estoque_disponivel, Decimal("4.000"))
        self.assertEqual(VendaHub.objects.get().status, VendaHub.STATUS_CANCELADA)


class VendaSegurancaAuditoriaTests(VendaHubTestMixin, TestCase):
    def test_caixa_fechado_bloqueia(self):
        fechar_caixa(self.terminal, self.operador, self.sessao_operador)

        resposta = self.post_item()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Caixa não está aberto.")

    def test_terminal_sem_caixa_bloqueia(self):
        self.terminal.caixa_retaguarda_id = None
        self.terminal.save()

        resposta = self.post_item()

        self.assertEqual(resposta.status_code, 400)

    def test_sku_de_outro_hub_inativo_nao_vendavel_e_sem_preco_bloqueiam(self):
        for sku_id, overrides in (
            (10826, {"ativo": False}),
            (10827, {"vendavel": False, "motivos_bloqueio": ["SEM_ESTOQUE"]}),
            (10828, {"preco_venda": None}),
        ):
            with self.subTest(overrides=overrides):
                item = self.criar_catalogo_item(sku_id, **overrides)
                resposta = self.post_item({"sku_id": item.retaguarda_sku_id})
                self.assertEqual(resposta.status_code, 409)
                self.assertEqual(VendaHub.objects.count(), 0)

    def test_resposta_nao_contem_tokens_hashes_ou_chaves(self):
        resposta = self.post_item()
        conteudo = f"{resposta.data}"

        self.assertNotIn("token", conteudo.lower())
        self.assertNotIn("hash", conteudo.lower())
        self.assertNotIn("chave_venda_aberta_terminal", conteudo)

    def test_nao_chama_central(self):
        with patch("integracao.services.retaguarda.RetaguardaClient.catalogo") as catalogo:
            resposta = self.post_item()

        self.assertEqual(resposta.status_code, 201)
        catalogo.assert_not_called()

    def test_auditoria_eventos_e_operador_atual(self):
        resposta = self.post_item()
        item_uuid = resposta.data["venda"]["itens"][0]["uuid"]
        operador_b = self.criar_operador("operador.b", 91, "Operador B")
        encerrar_sessao(self.sessao_operador)
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, operador_b)
        self.autenticar()
        self.client.patch(f"/api/terminal/venda/item/{item_uuid}/", {"quantidade": 2}, format="json")
        self.client.delete(f"/api/terminal/venda/item/{item_uuid}/")
        self.client.post("/api/terminal/venda/cancelar/", {}, format="json")

        tipos = list(VendaEventoHub.objects.values_list("tipo", flat=True))
        self.assertIn(VendaEventoHub.TIPO_VENDA_CRIADA, tipos)
        self.assertIn(VendaEventoHub.TIPO_ITEM_ADICIONADO, tipos)
        self.assertIn(VendaEventoHub.TIPO_ITEM_QUANTIDADE_ALTERADA, tipos)
        self.assertIn(VendaEventoHub.TIPO_ITEM_REMOVIDO, tipos)
        self.assertIn(VendaEventoHub.TIPO_VENDA_CANCELADA, tipos)
        self.assertEqual(VendaEventoHub.objects.filter(operador=operador_b).count(), 3)


class VendaCaixaConcorrenciaTests(VendaHubTestMixin, TestCase):
    def test_fechar_caixa_com_venda_aberta_retorna_409(self):
        self.post_item()

        resposta = self.client.post("/api/terminal/caixa/fechar/", {}, format="json")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Existe venda em andamento neste caixa.")
        self.sessao_caixa.refresh_from_db()
        self.assertEqual(self.sessao_caixa.status, SessaoCaixaHub.STATUS_ABERTO)

    def test_cancelar_venda_permite_fechamento(self):
        self.post_item()
        self.client.post("/api/terminal/venda/cancelar/", {}, format="json")

        resposta = self.client.post("/api/terminal/caixa/fechar/", {}, format="json")

        self.assertEqual(resposta.status_code, 200)

    def test_unique_tecnico_impede_duas_vendas_abertas_no_terminal(self):
        self.post_item()

        with self.assertRaises(IntegrityError):
            VendaHub.objects.create(
                hub=self.hub,
                sessao_caixa=self.sessao_caixa,
                terminal=self.terminal,
                status=VendaHub.STATUS_ABERTA,
                chave_venda_aberta_terminal=self.terminal.pk,
                operador_criacao=self.operador,
                sessao_operador_criacao=self.sessao_operador,
            )

    def test_integrity_error_recupera_venda_aberta_com_savepoint(self):
        venda_existente = VendaHub.objects.create(
            hub=self.hub,
            sessao_caixa=self.sessao_caixa,
            terminal=self.terminal,
            status=VendaHub.STATUS_ABERTA,
            chave_venda_aberta_terminal=self.terminal.pk,
            operador_criacao=self.operador,
            sessao_operador_criacao=self.sessao_operador,
        )

        with patch("core.services.vendas.VendaHub.objects.select_for_update") as select_for_update:
            queryset_vazio = VendaHub.objects.none()
            queryset_existente = VendaHub.objects.filter(pk=venda_existente.pk)
            select_for_update.return_value.filter.side_effect = [queryset_vazio, queryset_existente]
            with patch.object(VendaHub, "save", side_effect=IntegrityError("duplicado")):
                venda, criada = obter_ou_criar_venda_aberta(
                    self.terminal,
                    self.operador,
                    self.sessao_operador,
                    self.sessao_caixa,
                )

        self.assertEqual(venda, venda_existente)
        self.assertFalse(criada)

    def test_integrity_error_sem_venda_existente_repropaga(self):
        with patch("core.services.vendas.VendaHub.objects.select_for_update") as select_for_update:
            select_for_update.return_value.filter.return_value = VendaHub.objects.none()
            with patch.object(VendaHub, "save", side_effect=IntegrityError("duplicado")):
                with self.assertRaises(IntegrityError):
                    obter_ou_criar_venda_aberta(
                        self.terminal,
                        self.operador,
                        self.sessao_operador,
                        self.sessao_caixa,
                    )

    def test_lock_catalogo_item_usado_na_inclusao(self):
        with patch("core.services.vendas.CatalogoItemHub.objects") as manager:
            manager.select_for_update.side_effect = RuntimeError("lock chamado")
            with self.assertRaises(RuntimeError):
                adicionar_item(
                    self.terminal,
                    self.operador,
                    self.sessao_operador,
                    sku_id=self.catalogo_item.retaguarda_sku_id,
                    quantidade=1,
                )


class VendaValidacaoPuraTests(TestCase):
    def test_quantidade_rejeita_bool_float_decimal_string_null_zero_e_negativo(self):
        for valor in (True, False, 1.1, 1.9, 0.5, "1.5", None, 0, -1):
            with self.subTest(valor=valor):
                with self.assertRaises(Exception):
                    validar_quantidade(valor)

    def test_quantidade_aceita_inteiros_e_string_inteira(self):
        for valor in (1, 2, 10, "1", "2"):
            with self.subTest(valor=valor):
                self.assertEqual(validar_quantidade(valor), int(valor))

    def test_sku_id_rejeita_bool_float_decimal_string_null_zero_e_negativo(self):
        for valor in (True, False, 1.5, "1.5", None, 0, -1):
            with self.subTest(valor=valor):
                with self.assertRaises(Exception):
                    validar_sku_id(valor)

    def test_sku_id_aceita_inteiros_positivos(self):
        for valor in (1, 2, 10825, "10825"):
            with self.subTest(valor=valor):
                self.assertEqual(validar_sku_id(valor), int(valor))

    def test_total_item_usa_round_half_up(self):
        self.assertEqual(calcular_total_item(1, Decimal("1.0050"), Decimal("0.00")), Decimal("1.01"))
