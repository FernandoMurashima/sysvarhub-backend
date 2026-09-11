from django.core.management.base import BaseCommand, CommandError

from core.models import HubConfig
from integracao.services.bootstrap import BootstrapValidationError, sincronizar_bootstrap
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


class Command(BaseCommand):
    help = "Sincroniza identidade local e caixas do Sysvar Hub a partir da retaguarda."

    def handle(self, *args, **options):
        hub = self._obter_config_ativada()
        client = RetaguardaClient(hub.retaguarda_url)

        try:
            resposta = client.bootstrap(token=hub.retaguarda_token)
            resultado = sincronizar_bootstrap(hub, resposta)
        except (RetaguardaError, BootstrapValidationError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Bootstrap sincronizado com sucesso."))
        self.stdout.write(f"Empresa: {resultado['empresa']}")
        self.stdout.write(f"Loja: {resultado['loja']}")
        self.stdout.write(f"Caixas ativos: {resultado['caixas_ativos']}")
        self.stdout.write(f"Caixas inativados: {resultado['caixas_inativados']}")

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
        if not hub.empresa_id:
            raise CommandError("Hub local não possui empresa da retaguarda.")
        if not hub.loja_id:
            raise CommandError("Hub local não possui loja da retaguarda.")
        if not hub.retaguarda_hub_id:
            raise CommandError("Hub local não possui ID da retaguarda.")
        return hub
