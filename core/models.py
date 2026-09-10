import uuid

from django.db import models


class HubConfig(models.Model):
    hub_uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    nome = models.CharField(
        max_length=100,
        default="Sysvar Hub",
    )

    empresa_id = models.PositiveBigIntegerField()
    loja_id = models.PositiveBigIntegerField()

    retaguarda_url = models.URLField(
        max_length=255,
    )

    ativo = models.BooleanField(
        default=True,
    )

    ultima_sincronizacao_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    criado_em = models.DateTimeField(
        auto_now_add=True,
    )

    atualizado_em = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        verbose_name = "Configuração do Hub"
        verbose_name_plural = "Configurações do Hub"

    def __str__(self):
        return f"{self.nome} - Loja {self.loja_id}"