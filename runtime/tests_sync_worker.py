import os
import unittest
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sysvarhub.settings")

import django

django.setup()

from integracao.services.retaguarda import RetaguardaCredencialInvalidaError, RetaguardaError
from runtime.sync_worker import SyncWorker


class SyncWorkerTests(unittest.TestCase):
    def _hub(self, ativo=True):
        hub = Mock()
        hub.ativo = ativo
        hub.retaguarda_token = "token" if ativo else ""
        hub.retaguarda_url = "http://central.test/" if ativo else ""
        hub.save = Mock()
        return hub

    @patch("runtime.sync_worker.HubConfig")
    def test_hub_nao_ativado_nao_chama_central(self, hub_config):
        hub_config.objects.first.return_value = self._hub(ativo=False)
        client_factory = Mock()

        SyncWorker(client_factory=client_factory).executar_ciclo()

        client_factory.assert_not_called()

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.reenviar_resultados_pendentes")
    @patch("runtime.sync_worker.sincronizar_eventos_pendentes")
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    def test_hub_ativado_envia_heartbeat(self, executar, sincronizar_eventos, reenviar, montar_snapshot, hub_config):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        client.heartbeat.return_value = {"comando_sincronizacao": None}

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        client.heartbeat.assert_called_once()
        self.assertEqual(client.heartbeat.call_args.kwargs["snapshot_operacional"], {"terminais": []})
        montar_snapshot.assert_called_once_with(hub)
        sincronizar_eventos.assert_called_once_with(hub, client=client)
        reenviar.assert_called_once_with(hub, client=client)
        executar.assert_not_called()

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.reenviar_resultados_pendentes")
    @patch("runtime.sync_worker.sincronizar_eventos_pendentes")
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    def test_heartbeat_com_comando_chama_orquestrador(self, executar, sincronizar_eventos, reenviar, montar_snapshot, hub_config):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        comando = {"id": 1}
        client.heartbeat.return_value = {"comando_sincronizacao": comando}

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        executar.assert_called_once_with(hub, comando, client=client)
        sincronizar_eventos.assert_called_once_with(hub, client=client)
        reenviar.assert_called_once_with(hub, client=client)

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.marcar_credencial_hub_revogada")
    def test_erro_retaguarda_no_heartbeat_nao_morre(self, marcar_revogada, montar_snapshot, hub_config):
        hub_config.objects.first.return_value = self._hub()
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        client.heartbeat.side_effect = RetaguardaError("offline")

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        client.heartbeat.assert_called_once()
        marcar_revogada.assert_not_called()

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.reenviar_resultados_pendentes")
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    @patch("runtime.sync_worker.sincronizar_eventos_pendentes")
    @patch("runtime.sync_worker.executar_comando_administrativo")
    @patch("runtime.sync_worker.marcar_credencial_hub_revogada")
    def test_credencial_revogada_no_heartbeat_remove_ativacao_local(
        self,
        marcar_revogada,
        executar_admin,
        sincronizar_eventos,
        executar_sync,
        reenviar,
        montar_snapshot,
        hub_config,
    ):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        client.heartbeat.side_effect = RetaguardaCredencialInvalidaError("revogada")

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        client.heartbeat.assert_called_once()
        marcar_revogada.assert_called_once_with(hub)
        hub.save.assert_not_called()
        executar_sync.assert_not_called()
        executar_admin.assert_not_called()
        sincronizar_eventos.assert_not_called()
        reenviar.assert_not_called()

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.reenviar_resultados_pendentes")
    @patch("runtime.sync_worker.sincronizar_eventos_pendentes")
    @patch("runtime.sync_worker.executar_comando_administrativo")
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    def test_ciclo_executa_comandos_eventos_e_reenvio_administrativo(
        self,
        executar_sync,
        executar_admin,
        sincronizar_eventos,
        reenviar,
        montar_snapshot,
        hub_config,
    ):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        comando_sync = {"id": 1}
        comando_admin = {"id": 2}
        client.heartbeat.return_value = {
            "comando_sincronizacao": comando_sync,
            "comando_administrativo": comando_admin,
        }

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        executar_sync.assert_called_once_with(hub, comando_sync, client=client)
        executar_admin.assert_called_once_with(hub, comando_admin, client=client)
        sincronizar_eventos.assert_called_once_with(hub, client=client)
        reenviar.assert_called_once_with(hub, client=client)

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.montar_snapshot_operacional")
    @patch("runtime.sync_worker.reenviar_resultados_pendentes")
    @patch("runtime.sync_worker.sincronizar_eventos_pendentes")
    def test_erro_inesperado_no_processamento_de_eventos_nao_interrompe_reenvio(
        self,
        sincronizar_eventos,
        reenviar,
        montar_snapshot,
        hub_config,
    ):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        montar_snapshot.return_value = {"terminais": []}
        client = Mock()
        client.heartbeat.return_value = {}
        sincronizar_eventos.side_effect = RuntimeError("falha inesperada")

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        sincronizar_eventos.assert_called_once_with(hub, client=client)
        reenviar.assert_called_once_with(hub, client=client)

    def test_excecao_no_ciclo_nao_encerra_loop(self):
        stop = Event()
        worker = SyncWorker(interval=0.01, stop_event=stop)
        chamadas = {"n": 0}

        def ciclo():
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                raise RuntimeError("falha")
            stop.set()

        worker.executar_ciclo = ciclo
        worker.run()

        self.assertGreaterEqual(chamadas["n"], 2)

    def test_stop_event_encerra_loop(self):
        stop = Event()
        stop.set()
        worker = SyncWorker(interval=0.01, stop_event=stop)
        worker.executar_ciclo = Mock()

        worker.run()

        worker.executar_ciclo.assert_not_called()

    def test_worker_importa_sincronizar_eventos_pendentes(self):
        import runtime.sync_worker as sync_worker

        self.assertTrue(hasattr(sync_worker, "sincronizar_eventos_pendentes"))


if __name__ == "__main__":
    unittest.main()
