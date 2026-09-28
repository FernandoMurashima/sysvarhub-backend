from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.models import CaixaHub, ComandoAdministrativoRecebidoHub, HubConfig, Terminal
from integracao.services.comandos_administrativos import executar_comando_administrativo
from integracao.services.retaguarda import RetaguardaError


class ComandosAdministrativosTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            retaguarda_hub_id=7,
            empresa_id=11,
            loja_id=41,
            bootstrap_versao=1,
            retaguarda_token="TOKEN-SECRETO",
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-01",
            descricao="Caixa 01",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    @patch("integracao.services.comandos_administrativos.configurar_terminal")
    def test_configurar_terminal_usa_servico_existente_e_sanitiza_resultado(self, configurar):
        terminal = Terminal.objects.create(hub=self.hub, codigo="PDV-01", nome="PDV 01", caixa_retaguarda_id=29)
        configurar.return_value = terminal
        client = Mock()

        executar_comando_administrativo(
            self.hub,
            {"id": 10, "hub_id": self.hub.retaguarda_hub_id, "tipo": "CONFIGURAR_TERMINAL", "payload": {"codigo": "PDV-01", "nome": "PDV 01", "caixa_retaguarda_id": 29}},
            client=client,
        )

        configurar.assert_called_once()
        local = ComandoAdministrativoRecebidoHub.objects.get(retaguarda_comando_id=10)
        self.assertEqual(local.status, ComandoAdministrativoRecebidoHub.STATUS_CONCLUIDO)
        self.assertNotIn("token_hash", str(local.resultado))
        client.atualizar_resultado_comando_administrativo.assert_called_once()

    @patch("integracao.services.comandos_administrativos.gerar_pareamento_terminal")
    def test_replay_de_pareamento_reenvia_mesmo_resultado_sem_gerar_novo_codigo(self, gerar):
        terminal = Terminal.objects.create(hub=self.hub, codigo="PDV-01", nome="PDV 01", caixa_retaguarda_id=29)
        pareamento = Mock(expira_em=timezone.now())
        gerar.return_value = (pareamento, "AAAA-BBBB-CCCC")
        client = Mock()
        comando = {"id": 11, "hub_id": self.hub.retaguarda_hub_id, "tipo": "GERAR_PAREAMENTO", "payload": {"codigo": "PDV-01"}}

        executar_comando_administrativo(self.hub, comando, client=client)
        executar_comando_administrativo(self.hub, comando, client=client)

        gerar.assert_called_once_with(terminal)
        local = ComandoAdministrativoRecebidoHub.objects.get(retaguarda_comando_id=11)
        self.assertEqual(local.resultado["codigo"], "AAAA-BBBB-CCCC")
        self.assertEqual(client.atualizar_resultado_comando_administrativo.call_count, 2)

    def test_resultado_persistido_e_reenviado_apos_falha(self):
        terminal = Terminal.objects.create(hub=self.hub, codigo="PDV-01", nome="PDV 01", caixa_retaguarda_id=29)
        comando = {"id": 12, "hub_id": self.hub.retaguarda_hub_id, "tipo": "CONFIGURAR_TERMINAL", "payload": {"codigo": "PDV-01", "nome": "PDV 01", "caixa_retaguarda_id": 29}}
        client = Mock()
        client.atualizar_resultado_comando_administrativo.side_effect = RetaguardaError("offline")

        executar_comando_administrativo(self.hub, comando, client=client)

        local = ComandoAdministrativoRecebidoHub.objects.get(retaguarda_comando_id=12)
        self.assertEqual(local.status, ComandoAdministrativoRecebidoHub.STATUS_CONCLUIDO)
        client.atualizar_resultado_comando_administrativo.side_effect = None
        executar_comando_administrativo(self.hub, comando, client=client)
        self.assertEqual(Terminal.objects.filter(codigo="PDV-01").count(), 1)

    def test_comando_de_outro_hub_e_rejeitado(self):
        with self.assertRaises(RetaguardaError):
            executar_comando_administrativo(self.hub, {"id": 13, "hub_id": 999, "tipo": "CONFIGURAR_TERMINAL", "payload": {}}, client=Mock())
