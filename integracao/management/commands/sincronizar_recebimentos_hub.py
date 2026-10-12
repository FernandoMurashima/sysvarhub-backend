from django.core.management.base import BaseCommand, CommandError

from core.management.commands._hub import obter_hub_unico_ativo
from integracao.services.recebimentos import RecebimentosValidationError, sincronizar_recebimentos
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


class Command(BaseCommand):
    help = "Sincroniza recebimentos de mercadoria da Central para o Sysvar Hub."

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()
        if not hub.retaguarda_url or not hub.retaguarda_token:
            raise CommandError("Hub local nao possui retaguarda configurada.")
        client = RetaguardaClient(hub.retaguarda_url)
        try:
            resposta = client.recebimentos(token=hub.retaguarda_token)
            resultado = sincronizar_recebimentos(hub, resposta, client=client)
        except (RetaguardaError, RecebimentosValidationError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Recebimentos sincronizados com sucesso."))
        self.stdout.write(f"Versao: {resultado['versao']}")
        self.stdout.write(f"Recebimentos: {resultado['recebimentos']}")
        self.stdout.write(f"Pendentes locais: {resultado['pendentes_locais']}")
