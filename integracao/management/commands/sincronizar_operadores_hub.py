from django.core.management.base import BaseCommand, CommandError

from core.models import HubConfig
from integracao.services.operadores import (
    OperadoresValidationError,
    sincronizar_operadores,
)
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


class Command(BaseCommand):
    help = "Sincroniza operadores operacionais do Sysvar Hub a partir da retaguarda."

    def handle(self, *args, **options):
        hub = self._obter_config_pronta()
        client = RetaguardaClient(hub.retaguarda_url)

        try:
            resposta = client.operadores(token=hub.retaguarda_token)
            resultado = sincronizar_operadores(hub, resposta)
        except (RetaguardaError, OperadoresValidationError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Operadores sincronizados com sucesso."))
        self.stdout.write(f"Empresa: {resultado['empresa']}")
        self.stdout.write(f"Loja: {resultado['loja']}")
        self.stdout.write(f"Versão: {resultado['versao']}")
        self.stdout.write(f"Operadores recebidos: {resultado['operadores_recebidos']}")
        self.stdout.write(f"Operadores ativos: {resultado['operadores_ativos']}")
        self.stdout.write(f"Operadores inativados: {resultado['operadores_inativados']}")
        self.stdout.write(f"Sessões revogadas: {resultado['sessoes_revogadas']}")

    def _obter_config_pronta(self):
        total = HubConfig.objects.count()
        if total == 0:
            raise CommandError("Configuração local do Hub não encontrada.")
        if total > 1:
            raise CommandError("Existe mais de uma configuração local do Hub.")

        hub = HubConfig.objects.get()
        if not hub.ativo:
            raise CommandError("Hub local está inativo.")
        if not hub.retaguarda_url:
            raise CommandError("Hub local não possui URL da retaguarda.")
        if not hub.retaguarda_token:
            raise CommandError("Hub local não possui token da retaguarda.")
        if not hub.retaguarda_hub_id:
            raise CommandError("Hub local não possui ID da retaguarda.")
        if not hub.empresa_id:
            raise CommandError("Hub local não possui empresa da retaguarda.")
        if not hub.loja_id:
            raise CommandError("Hub local não possui loja da retaguarda.")
        if not hub.bootstrap_versao:
            raise CommandError("Hub local ainda não possui bootstrap sincronizado.")
        return hub
