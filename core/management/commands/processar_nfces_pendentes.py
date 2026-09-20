from django.core.management.base import BaseCommand

from core.management.commands._hub import obter_hub_unico_ativo
from core.services.nfce import retransmitir_nfces_pendentes


class Command(BaseCommand):
    help = "Processa e retransmite NFC-es pendentes/contingencia do Hub."

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()
        resultados = retransmitir_nfces_pendentes(hub)
        self.stdout.write(self.style.SUCCESS(f"NFC-es processadas: {len(resultados)}"))
