import logging
import socket
import threading

from django.utils import timezone

from core.models import HubConfig
from core.services.numeracao_devolucoes import tentar_repor_faixa_devolucao
from core.services.numeracao_vendas import tentar_repor_faixa_venda
from core.services.snapshot_operacional import montar_snapshot_operacional
from core.services.sync import sincronizar_eventos_pendentes
from integracao.services.comandos_administrativos import executar_comando_administrativo, reenviar_resultados_pendentes
from integracao.services.ativacao import marcar_credencial_hub_revogada
from integracao.services.retaguarda import RetaguardaClient, RetaguardaCredencialInvalidaError, RetaguardaError
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
        hub.ultima_tentativa_central_em = timezone.now()
        hub.save(update_fields=["ultima_tentativa_central_em", "atualizado_em"])
        try:
            resposta = client.heartbeat(
                token=hub.retaguarda_token,
                hostname=socket.gethostname(),
                versao=VERSION,
                snapshot_operacional=montar_snapshot_operacional(hub),
            )
        except RetaguardaCredencialInvalidaError:
            logger.warning("Credencial do Hub foi revogada na retaguarda; ativacao local sera removida.", exc_info=True)
            marcar_credencial_hub_revogada(hub)
            return
        except RetaguardaError:
            logger.warning("Heartbeat do Hub falhou.", exc_info=True)
            return
        hub.ultimo_heartbeat_em = timezone.now()
        hub.save(update_fields=["ultimo_heartbeat_em", "atualizado_em"])
        comando = resposta.get("comando_sincronizacao")
        if comando:
            executar_sincronizacao_comando(hub, comando, client=client)
        comando_administrativo = resposta.get("comando_administrativo")
        if comando_administrativo:
            executar_comando_administrativo(hub, comando_administrativo, client=client)
        try:
            sincronizar_eventos_pendentes(hub, client=client)
        except Exception:
            logger.warning("Falha ao sincronizar eventos operacionais pendentes do Hub.", exc_info=True)
        try:
            reenviar_resultados_pendentes(hub, client=client)
        except RetaguardaError:
            logger.warning("Falha ao reenviar resultados administrativos pendentes.", exc_info=True)
        tentar_repor_faixa_venda(hub, client, logger=logger)
        tentar_repor_faixa_devolucao(hub, client, logger=logger)


def start_worker_thread(stop_event=None, interval=10):
    worker = SyncWorker(interval=interval, stop_event=stop_event)
    thread = threading.Thread(target=worker.run, name="SysvarHubSyncWorker", daemon=True)
    thread.start()
    return worker, thread
