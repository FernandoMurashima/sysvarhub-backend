from django.core.management.base import BaseCommand, CommandError

from core.models import HubConfig
from integracao.services.catalogo import CatalogoValidationError, sincronizar_catalogo
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


class Command(BaseCommand):
    help = "Sincroniza o catálogo operacional do Sysvar Hub a partir da retaguarda."

    def handle(self, *args, **options):
        hub = self._obter_config_pronta()
        client = RetaguardaClient(hub.retaguarda_url)

        try:
            resposta = client.catalogo(token=hub.retaguarda_token)
            resultado = sincronizar_catalogo(hub, resposta)
        except (RetaguardaError, CatalogoValidationError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Catálogo sincronizado com sucesso."))
        self.stdout.write(f"Empresa: {resultado['empresa']}")
        self.stdout.write(f"Loja: {resultado['loja']}")
        self.stdout.write(f"Versão: {resultado['versao']}")
        self.stdout.write(f"Tabela: {resultado['tabela']}")
        self.stdout.write(f"Itens recebidos: {resultado['itens_recebidos']}")
        self.stdout.write(f"Itens ativos: {resultado['itens_ativos']}")
        self.stdout.write(f"Itens inativados: {resultado['itens_inativados']}")
        self.stdout.write(f"Vendáveis: {resultado['vendaveis']}")
        self.stdout.write(f"Não vendáveis: {resultado['nao_vendaveis']}")

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
