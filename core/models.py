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

    loja_apelido = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    loja_cnpj = models.CharField(
        max_length=20,
        blank=True,
        default="",
    )

    loja_estado = models.CharField(
        max_length=2,
        blank=True,
        default="",
    )

    bootstrap_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
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


class CaixaHub(models.Model):
    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="caixas",
    )

    retaguarda_id = models.PositiveBigIntegerField()

    codigo = models.CharField(
        max_length=30,
    )

    descricao = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    ativo = models.BooleanField(
        default=True,
    )

    sincronizado_em = models.DateTimeField()

    criado_em = models.DateTimeField(
        auto_now_add=True,
    )

    atualizado_em = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        verbose_name = "Caixa do Hub"
        verbose_name_plural = "Caixas do Hub"
        ordering = ("codigo", "retaguarda_id")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_id"],
                name="uniq_caixa_hub_por_retaguarda",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "ativo"], name="idx_caixa_hub_ativo"),
        ]

    def __str__(self):
        return f"{self.codigo} - {self.descricao or self.retaguarda_id}"

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
