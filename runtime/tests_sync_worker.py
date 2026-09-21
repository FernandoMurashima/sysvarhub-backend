import os
import unittest
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sysvarhub.settings")

import django

django.setup()

from integracao.services.retaguarda import RetaguardaError
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
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    def test_hub_ativado_envia_heartbeat(self, executar, hub_config):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        client = Mock()
        client.heartbeat.return_value = {"comando_sincronizacao": None}

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        client.heartbeat.assert_called_once()
        executar.assert_not_called()

    @patch("runtime.sync_worker.HubConfig")
    @patch("runtime.sync_worker.executar_sincronizacao_comando")
    def test_heartbeat_com_comando_chama_orquestrador(self, executar, hub_config):
        hub = self._hub()
        hub_config.objects.first.return_value = hub
        client = Mock()
        comando = {"id": 1}
        client.heartbeat.return_value = {"comando_sincronizacao": comando}

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        executar.assert_called_once_with(hub, comando, client=client)

    @patch("runtime.sync_worker.HubConfig")
    def test_erro_retaguarda_no_heartbeat_nao_morre(self, hub_config):
        hub_config.objects.first.return_value = self._hub()
        client = Mock()
        client.heartbeat.side_effect = RetaguardaError("offline")

        SyncWorker(client_factory=Mock(return_value=client)).executar_ciclo()

        client.heartbeat.assert_called_once()

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

    def test_worker_nao_importa_sincronizar_eventos_pendentes(self):
        import runtime.sync_worker as sync_worker

        self.assertFalse(hasattr(sync_worker, "sincronizar_eventos_pendentes"))


if __name__ == "__main__":
    unittest.main()
