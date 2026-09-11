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

    empresa_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )
    loja_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )

    retaguarda_hub_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )

    retaguarda_token = models.TextField(
        blank=True,
        default="",
    )

    empresa_nome = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    loja_nome = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

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

    ativado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    ultimo_heartbeat_em = models.DateTimeField(
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
        loja = self.loja_nome or self.loja_id or "não ativada"
        return f"{self.nome} - Loja {loja}"

class Terminal(models.Model):
    terminal_uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="terminais",
    )

    nome = models.CharField(
        max_length=100,
    )

    codigo = models.CharField(
        max_length=30,
        unique=True,
    )

    caixa_retaguarda_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )

    hostname = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    ultimo_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    ativo = models.BooleanField(
        default=True,
    )

    ultima_conexao_em = models.DateTimeField(
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
        verbose_name = "Terminal"
        verbose_name_plural = "Terminais"
        ordering = ("codigo", "nome")

    def __str__(self):
        return f"{self.codigo} - {self.nome}"
