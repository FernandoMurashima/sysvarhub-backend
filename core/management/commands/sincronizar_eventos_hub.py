from django.core.management.base import BaseCommand, CommandError

from core.management.commands._hub import obter_hub_unico_ativo
from core.services.sync import sincronizar_eventos_pendentes


class Command(BaseCommand):
    help = "Sincroniza eventos pendentes do Hub com a retaguarda."

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()
        if not hub.retaguarda_token:
            raise CommandError("Hub local não possui token da retaguarda.")
        resultado = sincronizar_eventos_pendentes(hub)
        self.stdout.write(self.style.SUCCESS(f"Eventos sync: {resultado}"))
