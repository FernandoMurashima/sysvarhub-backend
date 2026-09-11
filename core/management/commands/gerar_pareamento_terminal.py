from django.core.management.base import BaseCommand, CommandError

from core.management.commands._hub import obter_hub_unico_ativo
from core.models import Terminal
from core.services.terminais import PareamentoTerminalError, gerar_pareamento_terminal


class Command(BaseCommand):
    help = "Gera codigo temporario de pareamento para um terminal local."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True)

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()

        try:
            terminal = Terminal.objects.get(hub=hub, codigo=options["codigo"])
        except Terminal.DoesNotExist as exc:
            raise CommandError("Terminal nao encontrado.") from exc

        try:
            _pareamento, codigo = gerar_pareamento_terminal(terminal)
        except PareamentoTerminalError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Codigo de pareamento gerado."))
        self.stdout.write(f"Terminal: {terminal.codigo} - {terminal.nome}")
        self.stdout.write(f"Codigo: {codigo}")
        self.stdout.write("Validade: 15 minutos")
