from django.core.management.base import BaseCommand, CommandError

from core.management.commands._hub import obter_hub_unico_ativo
from core.services.terminais import TerminalValidationError, desativar_terminal


class Command(BaseCommand):
    help = "Desativa um terminal local sem apagar seu historico."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True)

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()

        try:
            terminal = desativar_terminal(hub, options["codigo"])
        except TerminalValidationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Terminal desativado com sucesso."))
        self.stdout.write(f"Codigo: {terminal.codigo}")
        self.stdout.write(f"UUID: {terminal.terminal_uuid}")
