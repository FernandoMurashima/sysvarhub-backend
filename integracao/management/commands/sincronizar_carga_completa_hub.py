from django.core.management.base import BaseCommand, CommandError

from core.models import HubConfig
from integracao.services.retaguarda import RetaguardaClient
from integracao.services.sincronizacao import executar_carga_completa_tecnica


class Command(BaseCommand):
    help = "Executa carga completa Central -> Hub para diagnóstico/manutenção."

    def handle(self, *args, **options):
        if HubConfig.objects.count() != 1:
            raise CommandError("É necessário existir exatamente uma configuração local do Hub.")
        hub = HubConfig.objects.get()
        if not hub.ativo or not hub.retaguarda_token or not hub.retaguarda_url:
            raise CommandError("Hub precisa estar ativado e possuir URL/token da Central.")
        client = RetaguardaClient(hub.retaguarda_url)
        sincronizacao = executar_carga_completa_tecnica(hub, client=client)
        self.stdout.write(
            self.style.SUCCESS(
                f"Carga completa finalizada com status {sincronizacao.status} na etapa {sincronizacao.etapa_atual or '-'}."
            )
        )
