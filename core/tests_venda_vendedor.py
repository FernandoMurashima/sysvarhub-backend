import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.models import (
    ContextoVendaTerminalHub,
    VendaEventoHub,
    VendaHub,
    VendaPagamentoHub,
    VendedorHub,
)
from core.services.caixa import fechar_caixa
from core.services.terminais import configurar_terminal
from core.tests_pagamentos import PagamentoHubTestMixin


class VendaVendedorTestMixin(PagamentoHubTestMixin):
    def criar_vendedor(self, retaguarda_id=501, **overrides):
        dados = {
            "hub": self.hub,
            "retaguarda_id": retaguarda_id,
            "matricula": f"{retaguarda_id:06d}"[-6:],
            "nome": f"Vendedor {retaguarda_id}",
            "apelido": f"Vend {retaguarda_id}",
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
        }
        dados.update(overrides)
        return VendedorHub.objects.create(**dados)

    def put_vendedor(self, vendedor_id):
        return self.client.put(
            "/api/terminal/venda/vendedor/",
            {"vendedor_id": vendedor_id},
            format="json",
        )

    def delete_vendedor(self):
        return self.client.delete("/api/terminal/venda/vendedor/", format="json")

    def selecionar_vendedor(self, vendedor=None):
        vendedor = vendedor or self.criar_vendedor()
        return self.put_vendedor(vendedor.retaguarda_id)


class VendaVendedorPreSelecaoTests(VendaVendedorTestMixin, TestCase):
    def test_selecao_sem_venda_grava_preselecao(self):
        vendedor = self.criar_vendedor()
        resposta = self.put_vendedor(vendedor.retaguarda_id)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["vendedor_preselecionado"]["id"], vendedor.retaguarda_id)

    def test_selecao_sem_venda_nao_cria_venda(self):
        self.selecionar_vendedor()
        self.assertFalse(VendaHub.objects.exists())

    def test_selecao_preserva_cliente_preselecionado(self):
        cliente = self.criar_cliente()
        self.client.put("/api/terminal/venda/cliente/", {"cliente_uuid": str(cliente.cliente_uuid)}, format="json")
        self.selecionar_vendedor()
        self.assertEqual(ContextoVendaTerminalHub.objects.get().cliente_preselecionado, cliente)

    def test_remocao_vendedor_pre_venda_preserva_cliente(self):
        cliente = self.criar_cliente()
        self.client.put("/api/terminal/venda/cliente/", {"cliente_uuid": str(cliente.cliente_uuid)}, format="json")
        self.selecionar_vendedor()
        self.delete_vendedor()
        contexto = ContextoVendaTerminalHub.objects.get()
        self.assertEqual(contexto.cliente_preselecionado, cliente)
        self.assertIsNone(contexto.vendedor_preselecionado)

    def test_vendedor_de_outro_hub_rejeitado(self):
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", self.caixa.retaguarda_id)
        outro = self.criar_vendedor(777)
        outro.hub = self.hub.__class__.objects.create(
            retaguarda_url="http://central2.test",
            retaguarda_hub_id=8,
            empresa_id=12,
            loja_id=42,
            bootstrap_versao=1,
            retaguarda_token="TOKEN",
        )
        outro.save()
        resposta = self.put_vendedor(outro.retaguarda_id)
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(VendaHub.objects.filter(terminal=outro_terminal).exists())

    def test_vendedor_ausente_rejeitado(self):
        vendedor = self.criar_vendedor(presente_retaguarda=False)
        self.assertEqual(self.put_vendedor(vendedor.retaguarda_id).status_code, 400)

    def test_vendedor_inativo_rejeitado(self):
        vendedor = self.criar_vendedor(ativo=False)
        self.assertEqual(self.put_vendedor(vendedor.retaguarda_id).status_code, 400)

    def test_vendedor_situacao_diferente_de_ativo_rejeitado(self):
        vendedor = self.criar_vendedor(situacao="INATIVO")
        self.assertEqual(self.put_vendedor(vendedor.retaguarda_id).status_code, 400)

    def test_vendedor_nao_participa_vendas_rejeitado(self):
        vendedor = self.criar_vendedor(participa_vendas=False)
        self.assertEqual(self.put_vendedor(vendedor.retaguarda_id).status_code, 400)

    def test_vendedor_id_bool_float_zero_rejeitado(self):
        for valor in (True, 1.5, 0):
            with self.subTest(valor=valor):
                self.assertEqual(self.put_vendedor(valor).status_code, 400)


class VendaVendedorInicioTests(VendaVendedorTestMixin, TestCase):
    def test_inicio_materializa_vendedor_preselecionado(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        resposta = self.post_iniciar()
        self.assertEqual(resposta.data["venda"]["vendedor"]["id"], vendedor.retaguarda_id)

    def test_materializacao_grava_snapshot_completo(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        self.post_iniciar()
        venda = VendaHub.objects.get()
        self.assertEqual(venda.vendedor_matricula, vendedor.matricula)
        self.assertEqual(venda.vendedor_cargo_descricao, vendedor.cargo_descricao)
        self.assertEqual(venda.vendedor_comissao_percentual, Decimal("3.00"))

    def test_materializacao_nao_cria_evento_artificial(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        self.post_iniciar()
        self.assertFalse(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDEDOR_SELECIONADO).exists())

    def test_retry_iniciar_venda_nao_troca_vendedor(self):
        vendedor_a = self.criar_vendedor(501)
        vendedor_b = self.criar_vendedor(502)
        self.put_vendedor(vendedor_a.retaguarda_id)
        self.post_iniciar()
        self.put_vendedor(vendedor_b.retaguarda_id)
        self.post_iniciar()
        self.assertEqual(VendaHub.objects.get().vendedor_retaguarda_id, vendedor_b.retaguarda_id)

    def test_preselecionado_inelegivel_antes_inicio_rejeita_sem_parcial(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        vendedor.presente_retaguarda = False
        vendedor.save()
        resposta = self.post_iniciar()
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(VendaHub.objects.exists())


class VendaVendedorVendaAbertaTests(VendaVendedorTestMixin, TestCase):
    def test_selecao_com_venda_aberta_grava_snapshot(self):
        self.post_iniciar()
        vendedor = self.criar_vendedor()
        resposta = self.put_vendedor(vendedor.retaguarda_id)
        self.assertEqual(resposta.data["venda"]["vendedor"]["nome"], vendedor.nome)

    def test_troca_vendedor_mantem_mesma_venda(self):
        self.post_iniciar()
        vendedor_a = self.criar_vendedor(501)
        vendedor_b = self.criar_vendedor(502)
        self.put_vendedor(vendedor_a.retaguarda_id)
        uuid_venda = VendaHub.objects.get().venda_uuid
        self.put_vendedor(vendedor_b.retaguarda_id)
        self.assertEqual(VendaHub.objects.get().venda_uuid, uuid_venda)

    def test_troca_registra_evento_com_anterior_atual(self):
        self.post_iniciar()
        vendedor_a = self.criar_vendedor(501)
        vendedor_b = self.criar_vendedor(502)
        self.put_vendedor(vendedor_a.retaguarda_id)
        self.put_vendedor(vendedor_b.retaguarda_id)
        evento = VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDEDOR_SELECIONADO).last()
        self.assertEqual(evento.dados["vendedor_anterior"]["id"], vendedor_a.retaguarda_id)
        self.assertEqual(evento.dados["vendedor_atual"]["id"], vendedor_b.retaguarda_id)

    def test_mesmo_vendedor_idempotente_nao_duplica_evento(self):
        self.post_iniciar()
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        self.put_vendedor(vendedor.retaguarda_id)
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDEDOR_SELECIONADO).count(), 1)

    def test_remover_vendedor_limpa_snapshot(self):
        self.post_iniciar()
        self.selecionar_vendedor()
        self.delete_vendedor()
        self.assertIsNone(VendaHub.objects.get().vendedor_retaguarda_id)

    def test_remover_registra_evento(self):
        self.post_iniciar()
        self.selecionar_vendedor()
        self.delete_vendedor()
        self.assertTrue(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDEDOR_REMOVIDO).exists())

    def test_remover_sem_vendedor_idempotente(self):
        self.post_iniciar()
        self.delete_vendedor()
        self.delete_vendedor()
        self.assertEqual(VendaEventoHub.objects.filter(tipo=VendaEventoHub.TIPO_VENDEDOR_REMOVIDO).count(), 0)


class VendaVendedorPagamentosTests(VendaVendedorTestMixin, TestCase):
    def criar_venda_com_vendedor_item(self):
        vendedor = self.criar_vendedor()
        self.post_iniciar()
        self.put_vendedor(vendedor.retaguarda_id)
        resposta = self.post_item({"quantidade": 1}, iniciar=False)
        return resposta.data["venda"]["uuid"]

    def test_selecao_com_pagamento_ativo_bloqueada(self):
        venda_uuid = self.criar_venda_com_vendedor_item()
        self.pagar(venda_uuid)
        vendedor = self.criar_vendedor(502)
        self.assertEqual(self.put_vendedor(vendedor.retaguarda_id).status_code, 409)

    def test_remocao_com_pagamento_ativo_bloqueada(self):
        venda_uuid = self.criar_venda_com_vendedor_item()
        self.pagar(venda_uuid)
        self.assertEqual(self.delete_vendedor().status_code, 409)

    def test_primeiro_pagamento_sem_vendedor_bloqueado(self):
        venda_uuid = self.post_item({"quantidade": 1}).data["venda"]["uuid"]
        resposta = self.pagar(venda_uuid)
        self.assertEqual(resposta.status_code, 409)

    def test_pagamento_com_vendedor_continua_funcionando(self):
        venda_uuid = self.criar_venda_com_vendedor_item()
        self.assertEqual(self.pagar(venda_uuid).status_code, 201)

    def test_finalizacao_sem_vendedor_bloqueada(self):
        venda_uuid = self.post_item({"quantidade": 1}).data["venda"]["uuid"]
        venda = VendaHub.objects.get(venda_uuid=venda_uuid)
        VendaPagamentoHub.objects.create(
            operacao_uuid=uuid.uuid4(),
            venda=venda,
            forma_pagamento=self.dinheiro,
            retaguarda_forma_pagamento_id=self.dinheiro.retaguarda_id,
            codigo=self.dinheiro.codigo,
            descricao=self.dinheiro.descricao,
            tipo=self.dinheiro.tipo,
            num_parcelas=1,
            valor=Decimal("199.90"),
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )
        self.assertEqual(self.finalizar(venda_uuid).status_code, 409)

    def test_finalizacao_com_vendedor_mantem_fluxo(self):
        venda_uuid = self.criar_venda_com_vendedor_item()
        self.pagar(venda_uuid)
        self.assertEqual(self.finalizar(venda_uuid).status_code, 200)


class VendaVendedorSerializacaoTests(VendaVendedorTestMixin, TestCase):
    def test_get_venda_atual_sem_venda_retorna_vendedor_preselecionado(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        resposta = self.client.get("/api/terminal/venda/atual/")
        self.assertEqual(resposta.data["vendedor_preselecionado"]["id"], vendedor.retaguarda_id)

    def test_get_venda_atual_com_venda_retorna_snapshot(self):
        vendedor = self.criar_vendedor()
        self.post_iniciar()
        self.put_vendedor(vendedor.retaguarda_id)
        resposta = self.client.get("/api/terminal/venda/atual/")
        self.assertEqual(resposta.data["venda"]["vendedor"]["id"], vendedor.retaguarda_id)

    def test_nova_requisicao_mantem_vendedor(self):
        vendedor = self.criar_vendedor()
        self.put_vendedor(vendedor.retaguarda_id)
        outra = self.client.get("/api/terminal/venda/atual/")
        self.assertEqual(outra.data["vendedor_preselecionado"]["id"], vendedor.retaguarda_id)

    def test_troca_operador_nao_altera_vendedor_da_venda(self):
        vendedor = self.criar_vendedor()
        self.post_iniciar()
        self.put_vendedor(vendedor.retaguarda_id)
        operador_b = self.criar_operador("op.b", 91, "Operador B")
        sessao_b, token_b = self.criar_sessao_operador(self.terminal, operador_b)
        self.autenticar(token_operador=token_b)
        resposta = self.client.get("/api/terminal/venda/atual/")
        self.assertEqual(resposta.data["venda"]["vendedor"]["id"], vendedor.retaguarda_id)
        self.assertEqual(sessao_b.operador, operador_b)

    def test_fechamento_caixa_limpa_vendedor_preselecionado(self):
        self.selecionar_vendedor()
        fechar_caixa(self.terminal, self.operador, self.sessao_operador, valor_contado="100.00")
        self.assertFalse(ContextoVendaTerminalHub.objects.exists())

    def test_cliente_preselecionado_continua_preservado(self):
        cliente = self.criar_cliente()
        vendedor = self.criar_vendedor()
        self.client.put("/api/terminal/venda/cliente/", {"cliente_uuid": str(cliente.cliente_uuid)}, format="json")
        self.put_vendedor(vendedor.retaguarda_id)
        self.delete_vendedor()
        self.assertEqual(ContextoVendaTerminalHub.objects.get().cliente_preselecionado, cliente)

    def test_cliente_continua_materializando_sem_regressao(self):
        cliente = self.criar_cliente()
        self.client.put("/api/terminal/venda/cliente/", {"cliente_uuid": str(cliente.cliente_uuid)}, format="json")
        resposta = self.post_iniciar()
        self.assertEqual(resposta.data["venda"]["cliente"]["cliente_uuid"], str(cliente.cliente_uuid))
