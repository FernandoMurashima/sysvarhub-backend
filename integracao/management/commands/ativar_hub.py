from django.core.management.base import BaseCommand, CommandError

from integracao.services.ativacao import AtivacaoHubError, ativar_hub_local


class Command(BaseCommand):
    help = "Ativa o Sysvar Hub local na retaguarda."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--url", required=True, dest="retaguarda_url")

    def handle(self, *args, **options):
        try:
            hub = ativar_hub_local(options["codigo"], options["retaguarda_url"])
        except AtivacaoHubError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Sysvar Hub ativado com sucesso."))
        self.stdout.write(f"Empresa: {hub.empresa_nome or hub.empresa_id}")
        self.stdout.write(f"Loja: {hub.loja_nome or hub.loja_id}")
        self.stdout.write(f"Hub UUID: {hub.hub_uuid}")
