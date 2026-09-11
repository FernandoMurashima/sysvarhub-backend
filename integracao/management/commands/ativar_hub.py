import socket

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import HubConfig
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError
from sysvarhub.version import VERSION


class Command(BaseCommand):
    help = "Ativa o Sysvar Hub local na retaguarda."

    def add_arguments(self, parser):
        parser.add_argument("--codigo", required=True)
        parser.add_argument("--url", required=True, dest="retaguarda_url")

    def handle(self, *args, **options):
        hub = self._obter_config(options["retaguarda_url"])
        hostname = socket.gethostname()

        client = RetaguardaClient(options["retaguarda_url"])
        try:
            resposta = client.ativar_hub(
                codigo=options["codigo"],
                hub_uuid=hub.hub_uuid,
                nome=hub.nome,
                hostname=hostname,
                versao=VERSION,
            )
            self._validar_resposta(resposta, hub)
        except RetaguardaError as exc:
            raise CommandError(str(exc)) from exc

        with transaction.atomic():
            hub.retaguarda_token = resposta["token"]
            hub.retaguarda_hub_id = resposta["hub_id"]
            hub.empresa_id = resposta["empresa_id"]
            hub.empresa_nome = resposta.get("empresa_nome", "")
            hub.loja_id = resposta["loja_id"]
            hub.loja_nome = resposta.get("loja_nome", "")
            hub.ativado_em = timezone.now()
            hub.ativo = True
            hub.retaguarda_url = options["retaguarda_url"]
            hub.save(
                update_fields=[
                    "retaguarda_token",
                    "retaguarda_hub_id",
                    "empresa_id",
                    "empresa_nome",
                    "loja_id",
                    "loja_nome",
                    "ativado_em",
                    "ativo",
                    "retaguarda_url",
                    "atualizado_em",
                ]
            )

        self.stdout.write(self.style.SUCCESS("Sysvar Hub ativado com sucesso."))
        self.stdout.write(f"Empresa: {hub.empresa_nome or hub.empresa_id}")
        self.stdout.write(f"Loja: {hub.loja_nome or hub.loja_id}")
        self.stdout.write(f"Hub UUID: {hub.hub_uuid}")

    def _obter_config(self, retaguarda_url):
        total = HubConfig.objects.count()
        if total > 1:
            raise CommandError("Existe mais de uma configuração local do Hub.")
        if total == 1:
            return HubConfig.objects.get()
        return HubConfig.objects.create(retaguarda_url=retaguarda_url, ativo=False)

    def _validar_resposta(self, resposta, hub):
        obrigatorios = ("token", "hub_uuid", "hub_id", "loja_id", "empresa_id")
        faltando = [campo for campo in obrigatorios if not resposta.get(campo)]
        if faltando:
            raise CommandError("Resposta de ativação incompleta da retaguarda.")
        if str(resposta["hub_uuid"]) != str(hub.hub_uuid):
            raise CommandError("Retaguarda retornou hub_uuid diferente do Hub local.")
