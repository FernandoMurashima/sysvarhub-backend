from django.core.management.base import BaseCommand, CommandError

from core.services.terminais import (
    TerminalValidationError,
    configurar_terminal,
    obter_caixa_terminal,
)
from core.management.commands._hub import obter_hub_unico_ativo


class Command(BaseCommand):
    help = "Cria ou atualiza um terminal local do Sysvar Hub."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--nome", required=True)
        parser.add_argument("--caixa-id", type=int, dest="caixa_id")
        parser.add_argument("--hostname")

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()

        try:
            terminal = configurar_terminal(
                hub=hub,
                codigo=options["codigo"],
                nome=options["nome"],
                caixa_retaguarda_id=options.get("caixa_id"),
                hostname=options.get("hostname"),
            )
        except TerminalValidationError as exc:
            raise CommandError(str(exc)) from exc

        caixa = obter_caixa_terminal(hub, terminal.caixa_retaguarda_id)

        self.stdout.write(self.style.SUCCESS("Terminal configurado com sucesso."))
        self.stdout.write(f"Codigo: {terminal.codigo}")
        self.stdout.write(f"Nome: {terminal.nome}")
        self.stdout.write(f"UUID: {terminal.terminal_uuid}")
        self.stdout.write(f"Caixa: {self._formatar_caixa(caixa)}")
        self.stdout.write(f"Ativo: {'Sim' if terminal.ativo else 'Nao'}")

    def _formatar_caixa(self, caixa):
        if caixa is None:
            return "nao configurado"
        return f"{caixa.codigo} - {caixa.descricao or caixa.retaguarda_id}"
