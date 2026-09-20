import socket

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import HubConfig
from core.services.sync import sincronizar_eventos_pendentes
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError
from sysvarhub.version import VERSION


class Command(BaseCommand):
    help = "Envia heartbeat do Sysvar Hub para a retaguarda."

    def handle(self, *args, **options):
        hub = self._obter_config_ativada()
        client = RetaguardaClient(hub.retaguarda_url)

        try:
            client.heartbeat(
                token=hub.retaguarda_token,
                hostname=socket.gethostname(),
                versao=VERSION,
            )
        except RetaguardaError as exc:
            raise CommandError(str(exc)) from exc

        hub.ultimo_heartbeat_em = timezone.now()
        hub.save(update_fields=["ultimo_heartbeat_em", "atualizado_em"])
        sincronizar_eventos_pendentes(hub, client=client)

        self.stdout.write(self.style.SUCCESS("Heartbeat enviado com sucesso."))

    def _obter_config_ativada(self):
        total = HubConfig.objects.count()
        if total == 0:
            raise CommandError("Configuração local do Hub não encontrada.")
        if total > 1:
            raise CommandError("Existe mais de uma configuração local do Hub.")

        hub = HubConfig.objects.get()
        if not hub.ativo:
            raise CommandError("Hub local ainda não está ativado.")
        if not hub.retaguarda_token:
            raise CommandError("Hub local não possui token da retaguarda.")
        if not hub.retaguarda_url:
            raise CommandError("Hub local não possui URL da retaguarda.")
        return hub
