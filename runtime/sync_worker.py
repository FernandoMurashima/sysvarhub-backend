import logging
import socket
import threading

from django.utils import timezone

from core.models import HubConfig
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError
from integracao.services.sincronizacao import executar_sincronizacao_comando
from sysvarhub.version import VERSION


logger = logging.getLogger(__name__)


class SyncWorker:
    def __init__(self, interval=10, stop_event=None, client_factory=None):
        self.interval = interval
        self.stop_event = stop_event or threading.Event()
        self.client_factory = client_factory or RetaguardaClient

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.executar_ciclo()
            except Exception:
                logger.warning("Ciclo de sincronização do Hub falhou; nova tentativa no próximo ciclo.", exc_info=True)
            self.stop_event.wait(self.interval)

    def executar_ciclo(self):
        hub = HubConfig.objects.first()
        if not hub or not hub.ativo or not hub.retaguarda_token or not hub.retaguarda_url:
            return
        client = self.client_factory(hub.retaguarda_url)
        try:
            resposta = client.heartbeat(
                token=hub.retaguarda_token,
                hostname=socket.gethostname(),
                versao=VERSION,
            )
        except RetaguardaError:
            logger.warning("Heartbeat do Hub falhou.", exc_info=True)
            return
        hub.ultimo_heartbeat_em = timezone.now()
        hub.save(update_fields=["ultimo_heartbeat_em", "atualizado_em"])
        comando = resposta.get("comando_sincronizacao")
        if comando:
            executar_sincronizacao_comando(hub, comando, client=client)


def start_worker_thread(stop_event=None, interval=10):
    worker = SyncWorker(interval=interval, stop_event=stop_event)
    thread = threading.Thread(target=worker.run, name="SysvarHubSyncWorker", daemon=True)
    thread.start()
    return worker, thread
