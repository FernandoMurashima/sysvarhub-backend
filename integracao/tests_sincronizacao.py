from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.models import CatalogoItemHub, EventoSyncHub, HubConfig, SincronizacaoRecebidaHub
from integracao.services.catalogo import sincronizar_catalogo
from integracao.services.retaguarda import RetaguardaError
from integracao.services.sincronizacao import ETAPAS, executar_sincronizacao_comando


class SincronizacaoCentralHubTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            nome="Hub Teste",
            empresa_id=1,
            loja_id=1,
            retaguarda_hub_id=10,
            retaguarda_token="token",
            retaguarda_url="http://central.test/",
            ativo=True,
        )
        self.client = Mock()
        self.client.atualizar_status_sincronizacao.return_value = {}
        for _etapa, metodo, _funcao in ETAPAS:
            getattr(self.client, metodo).return_value = self._resposta_etapa()

    def test_comando_novo_cria_registro_e_executa_etapas_em_ordem(self):
        chamadas = []
        etapas = [(nome, metodo, self._funcao_etapa(nome, chamadas)) for nome, metodo, _funcao in ETAPAS]

        with patch("integracao.services.sincronizacao.ETAPAS", etapas):
            sync = executar_sincronizacao_comando(self.hub, self._comando(55), client=self.client)

        self.assertEqual(sync.status, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)
        self.assertEqual(chamadas, ["BOOTSTRAP", "CATALOGO", "OPERADORES", "VENDEDORES", "FORMAS_PAGAMENTO", "TIPOS_DESPESA", "CLIENTES"])
        self.assertEqual(sync.etapa_atual, "CLIENTES")
        self.hub.refresh_from_db()
        self.assertIsNotNone(self.hub.primeira_carga_concluida_em)
        self.assertIsNotNone(self.hub.ultima_carga_central_em)
        self.assertEqual(self.hub.ultima_carga_central_status, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)

    def test_erro_em_etapa_marca_erro_controlado(self):
        def falha(_hub, _resposta, client=None):
            raise ValueError("falha controlada\nstack")

        etapas = [("BOOTSTRAP", "bootstrap", self._funcao_etapa("BOOTSTRAP", [])), ("CATALOGO", "catalogo", falha)]

        with patch("integracao.services.sincronizacao.ETAPAS", etapas):
            sync = executar_sincronizacao_comando(self.hub, self._comando(56), client=self.client)

        self.assertEqual(sync.status, SincronizacaoRecebidaHub.STATUS_ERRO)
        self.assertEqual(sync.etapa_atual, "CATALOGO")
        self.assertEqual(sync.mensagem_erro, "falha controlada")

    def test_solicitacao_concluida_nao_reexecuta_e_reenvia_confirmacao(self):
        sync = SincronizacaoRecebidaHub.objects.create(
            hub=self.hub,
            retaguarda_solicitacao_id=57,
            tipo="COMPLETA",
            status=SincronizacaoRecebidaHub.STATUS_CONCLUIDA,
            etapa_atual="CLIENTES",
        )

        with patch("integracao.services.sincronizacao.ETAPAS", [("BOOTSTRAP", "bootstrap", Mock())]):
            resultado = executar_sincronizacao_comando(self.hub, self._comando(57), client=self.client)

        self.assertEqual(resultado.pk, sync.pk)
        self.client.bootstrap.assert_not_called()
        self.client.atualizar_status_sincronizacao.assert_called()

    def test_solicitacao_erro_nao_reexecuta_mesmo_id(self):
        SincronizacaoRecebidaHub.objects.create(
            hub=self.hub,
            retaguarda_solicitacao_id=58,
            tipo="COMPLETA",
            status=SincronizacaoRecebidaHub.STATUS_ERRO,
            mensagem_erro="falha",
        )

        executar_sincronizacao_comando(self.hub, self._comando(58), client=self.client)

        self.client.bootstrap.assert_not_called()

    def test_processando_apos_reinicio_pode_reexecutar(self):
        SincronizacaoRecebidaHub.objects.create(
            hub=self.hub,
            retaguarda_solicitacao_id=59,
            tipo="COMPLETA",
            status=SincronizacaoRecebidaHub.STATUS_PROCESSANDO,
        )
        chamadas = []

        with patch("integracao.services.sincronizacao.ETAPAS", [("BOOTSTRAP", "bootstrap", self._funcao_etapa("BOOTSTRAP", chamadas))]):
            executar_sincronizacao_comando(self.hub, self._comando(59), client=self.client)

        self.assertEqual(chamadas, ["BOOTSTRAP"])

    def test_falha_ao_informar_concluida_nao_transforma_em_erro(self):
        chamadas = []

        def informar(**kwargs):
            if kwargs["status"] == SincronizacaoRecebidaHub.STATUS_CONCLUIDA:
                raise RetaguardaError("sem internet")
            return {}

        self.client.atualizar_status_sincronizacao.side_effect = informar
        with patch("integracao.services.sincronizacao.ETAPAS", [("BOOTSTRAP", "bootstrap", self._funcao_etapa("BOOTSTRAP", chamadas))]):
            sync = executar_sincronizacao_comando(self.hub, self._comando(60), client=self.client)

        sync.refresh_from_db()
        self.hub.refresh_from_db()
        self.assertEqual(sync.status, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)
        self.assertEqual(self.hub.ultima_carga_central_status, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)
        self.assertEqual(chamadas, ["BOOTSTRAP"])

    def test_heartbeat_posterior_mesmo_id_reenvia_concluida_sem_reexecutar(self):
        SincronizacaoRecebidaHub.objects.create(
            hub=self.hub,
            retaguarda_solicitacao_id=61,
            tipo="COMPLETA",
            status=SincronizacaoRecebidaHub.STATUS_CONCLUIDA,
        )

        executar_sincronizacao_comando(self.hub, self._comando(61), client=self.client)

        self.client.bootstrap.assert_not_called()
        self.client.atualizar_status_sincronizacao.assert_called_once()

    def _comando(self, sync_id):
        return {"id": sync_id, "tipo": "COMPLETA", "hub_id": self.hub.retaguarda_hub_id}

    def _resposta_etapa(self):
        return {"ok": True}

    def _funcao_etapa(self, etapa, chamadas):
        def executar(_hub, _resposta, client=None):
            chamadas.append(etapa)

        return executar


class CatalogoProtecaoEstoqueTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            nome="Hub Cat",
            empresa_id=1,
            loja_id=1,
            retaguarda_hub_id=10,
            retaguarda_token="token",
            retaguarda_url="http://central.test/",
            ativo=True,
        )

    def test_venda_pendente_preserva_saldo_local_e_atualiza_cadastro(self):
        self._item_existente(sku=100, estoque=Decimal("9.000"))
        EventoSyncHub.objects.create(hub=self.hub, chave_idempotencia="v1", tipo="VENDA_FINALIZADA", payload={}, status=EventoSyncHub.STATUS_PENDENTE)

        sincronizar_catalogo(self.hub, self._catalogo(100, estoque="10.000", preco="22.00", ncm="2222"))
        item = CatalogoItemHub.objects.get(hub=self.hub, retaguarda_sku_id=100)

        self.assertEqual(item.estoque_fisico, Decimal("9.000"))
        self.assertEqual(item.preco_venda, Decimal("22.00"))
        self.assertEqual(item.fiscal["ncm"], "2222")
        self.assertTrue(item.vendavel)

    def test_devolucao_pendente_preserva_saldo_local(self):
        self._item_existente(sku=101, estoque=Decimal("4.000"))
        EventoSyncHub.objects.create(hub=self.hub, chave_idempotencia="d1", tipo="DEVOLUCAO_FINALIZADA", payload={}, status=EventoSyncHub.STATUS_ERRO)

        sincronizar_catalogo(self.hub, self._catalogo(101, estoque="8.000"))
        item = CatalogoItemHub.objects.get(hub=self.hub, retaguarda_sku_id=101)

        self.assertEqual(item.estoque_fisico, Decimal("4.000"))

    def test_sem_evento_pendente_usa_saldo_central(self):
        self._item_existente(sku=102, estoque=Decimal("1.000"))

        sincronizar_catalogo(self.hub, self._catalogo(102, estoque="7.000"))
        item = CatalogoItemHub.objects.get(hub=self.hub, retaguarda_sku_id=102)

        self.assertEqual(item.estoque_fisico, Decimal("7.000"))

    def test_sku_novo_usa_saldo_central_mesmo_com_pendencia(self):
        EventoSyncHub.objects.create(hub=self.hub, chave_idempotencia="v2", tipo="VENDA_FINALIZADA", payload={}, status=EventoSyncHub.STATUS_PROCESSANDO)

        sincronizar_catalogo(self.hub, self._catalogo(103, estoque="6.000"))
        item = CatalogoItemHub.objects.get(hub=self.hub, retaguarda_sku_id=103)

        self.assertEqual(item.estoque_fisico, Decimal("6.000"))

    def _item_existente(self, sku, estoque):
        return CatalogoItemHub.objects.create(
            hub=self.hub,
            retaguarda_produto_id=sku,
            retaguarda_sku_id=sku,
            tipo_produto="PRODUTO",
            descricao="Antigo",
            preco=Decimal("10.0000"),
            preco_venda=Decimal("10.0000"),
            estoque_fisico=estoque,
            reserva=Decimal("0.000"),
            estoque_disponivel=estoque,
            vendavel=estoque > 0,
            motivos_bloqueio=[] if estoque > 0 else ["SEM_ESTOQUE"],
            fiscal={"ncm": "1111"},
            sincronizado_em=timezone.now(),
        )

    def _catalogo(self, sku, estoque, preco="10.00", ncm="1111"):
        return {
            "catalogo_versao": 1,
            "gerado_em": timezone.now().isoformat(),
            "hub": {"id": self.hub.retaguarda_hub_id, "hub_uuid": str(self.hub.hub_uuid)},
            "empresa": {"id": self.hub.empresa_id, "nome": "Empresa"},
            "loja": {"id": self.hub.loja_id, "nome": "Loja"},
            "total_itens": 1,
            "itens": [{
                "produto_id": sku,
                "sku_id": sku,
                "tipo_produto": "PRODUTO",
                "descricao": "Novo nome",
                "vendavel": True,
                "motivos_bloqueio": [],
                "estoque_fisico": estoque,
                "reserva": "0.000",
                "estoque_disponivel": estoque,
                "preco": preco,
                "preco_venda": preco,
                "fiscal": {"ncm": ncm},
            }],
        }
