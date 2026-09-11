from django.core.management.base import BaseCommand

from core.management.commands._hub import obter_hub_unico_ativo
from core.models import CaixaHub, Terminal


class Command(BaseCommand):
    help = "Lista os terminais locais configurados no Sysvar Hub."

    def handle(self, *args, **options):
        hub = obter_hub_unico_ativo()
        caixas = {
            caixa.retaguarda_id: caixa
            for caixa in CaixaHub.objects.filter(hub=hub)
        }

        for terminal in Terminal.objects.filter(hub=hub).order_by("codigo", "nome"):
            self.stdout.write(f"Codigo: {terminal.codigo}")
            self.stdout.write(f"Nome: {terminal.nome}")
            self.stdout.write(f"UUID: {terminal.terminal_uuid}")
            self.stdout.write(
                f"Caixa: {self._formatar_caixa(caixas.get(terminal.caixa_retaguarda_id))}"
            )
            self.stdout.write(f"Hostname: {terminal.hostname or '-'}")
            self.stdout.write(f"Ativo: {'Sim' if terminal.ativo else 'Nao'}")
            self.stdout.write(
                f"Ultima conexao: {terminal.ultima_conexao_em or 'nunca'}"
            )
            self.stdout.write("")

    def _formatar_caixa(self, caixa):
        if caixa is None:
            return "nao configurado"

        texto = f"{caixa.codigo} - {caixa.descricao or caixa.retaguarda_id}"
        if not caixa.ativo:
            texto = f"{texto} (INATIVO)"
        return texto
