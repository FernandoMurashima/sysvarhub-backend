from django.core.management.base import CommandError

from core.models import HubConfig


def obter_hub_unico_ativo():
    total = HubConfig.objects.count()
    if total == 0:
        raise CommandError("Configuracao local do Hub nao encontrada.")
    if total > 1:
        raise CommandError("Existe mais de uma configuracao local do Hub.")

    hub = HubConfig.objects.get()
    if not hub.ativo:
        raise CommandError("Hub local esta inativo.")
    return hub
