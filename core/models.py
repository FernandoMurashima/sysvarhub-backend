import hashlib
import hmac
import secrets
import string
import uuid

from django.conf import settings
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

    catalogo_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    catalogo_gerado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    catalogo_sincronizado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    operadores_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    operadores_gerado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    operadores_sincronizado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    tabela_preco_retaguarda_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )

    tabela_preco_codigo = models.CharField(
        max_length=30,
        blank=True,
        default="",
    )

    tabela_preco_nome = models.CharField(
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


class CatalogoItemHub(models.Model):
    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="catalogo_itens",
    )

    retaguarda_produto_id = models.PositiveBigIntegerField()
    retaguarda_sku_id = models.PositiveBigIntegerField()

    tipo_produto = models.CharField(max_length=30)
    referencia = models.CharField(max_length=80, blank=True, default="")
    descricao = models.CharField(max_length=200)
    descricao_reduzida = models.CharField(max_length=120, blank=True, default="")
    ean13 = models.CharField(max_length=13, blank=True, default="", db_index=True)
    codigo_item_ref = models.CharField(max_length=80, blank=True, default="")

    cor_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    cor_descricao = models.CharField(max_length=80, blank=True, default="")

    tamanho_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    tamanho_descricao = models.CharField(max_length=80, blank=True, default="")

    unidade_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    unidade_codigo = models.CharField(max_length=20, blank=True, default="")
    unidade_descricao = models.CharField(max_length=80, blank=True, default="")

    preco = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    preco_promocional = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )
    preco_venda = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
    )

    estoque_fisico = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    reserva = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    estoque_disponivel = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    vendavel = models.BooleanField(default=False)
    motivos_bloqueio = models.JSONField(default=list, blank=True)
    fiscal = models.JSONField(default=dict, blank=True)
    ativo = models.BooleanField(default=True)
    sincronizado_em = models.DateTimeField()

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Item de catálogo do Hub"
        verbose_name_plural = "Itens de catálogo do Hub"
        ordering = ("descricao", "retaguarda_sku_id")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_sku_id"],
                name="uniq_catalogo_hub_sku",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "ativo"], name="idx_catalogo_hub_ativo"),
            models.Index(fields=["hub", "vendavel"], name="idx_catalogo_hub_vendavel"),
            models.Index(fields=["hub", "referencia"], name="idx_catalogo_hub_ref"),
        ]

    def __str__(self):
        return f"{self.retaguarda_sku_id} - {self.descricao}"


class OperadorHub(models.Model):
    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="operadores",
    )

    retaguarda_usuario_id = models.PositiveBigIntegerField()
    codigo = models.CharField(max_length=30, db_index=True)
    nome = models.CharField(max_length=150)
    tipo = models.CharField(max_length=30)
    perfil_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    perfil_nome = models.CharField(max_length=150, blank=True, default="")
    credencial_hash = models.CharField(max_length=128, blank=True, default="")
    ativo = models.BooleanField(default=True)
    sincronizado_em = models.DateTimeField()

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operador do Hub"
        verbose_name_plural = "Operadores do Hub"
        ordering = ("codigo", "nome")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_usuario_id"],
                name="uniq_operador_hub_usuario",
            ),
            models.UniqueConstraint(
                fields=["hub", "codigo"],
                name="uniq_operador_hub_codigo",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "ativo"], name="idx_operador_hub_ativo"),
            models.Index(fields=["hub", "codigo"], name="idx_operador_hub_codigo"),
        ]

    def __str__(self):
        return f"{self.codigo} - {self.nome}"


class Terminal(models.Model):
    TOKEN_BYTES = 32

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
    )

    caixa_retaguarda_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
    )

    token_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )

    token_prefixo = models.CharField(
        max_length=12,
        blank=True,
        default="",
    )

    pareado_em = models.DateTimeField(
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
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "codigo"],
                name="uniq_terminal_hub_codigo",
            ),
        ]

    def __str__(self):
        return f"{self.codigo} - {self.nome}"

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def gerar_token(self):
        token = secrets.token_urlsafe(self.TOKEN_BYTES)
        self.token_hash = self.hash_token(token)
        self.token_prefixo = token[:12]
        return token


class SessaoOperadorHub(models.Model):
    TOKEN_BYTES = 32

    sessao_uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="sessoes_operador",
    )
    operador = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="sessoes",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    token_prefixo = models.CharField(max_length=12, blank=True, default="")
    ativa = models.BooleanField(default=True, db_index=True)
    iniciada_em = models.DateTimeField(auto_now_add=True)
    ultima_atividade_em = models.DateTimeField()
    encerrada_em = models.DateTimeField(null=True, blank=True)
    motivo_encerramento = models.CharField(max_length=30, blank=True, default="")
    ultimo_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "Sessão de operador do Hub"
        verbose_name_plural = "Sessões de operador do Hub"
        ordering = ("-iniciada_em",)
        indexes = [
            models.Index(
                fields=["terminal", "ativa"],
                name="idx_sessao_oper_terminal",
            ),
            models.Index(
                fields=["operador", "ativa"],
                name="idx_sessao_oper_operador",
            ),
        ]

    def __str__(self):
        return f"{self.terminal.codigo} - {self.operador.codigo}"

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def gerar_token(self):
        token = secrets.token_urlsafe(self.TOKEN_BYTES)
        self.token_hash = self.hash_token(token)
        self.token_prefixo = token[:12]
        return token


class SessaoCaixaHub(models.Model):
    STATUS_ABERTO = "ABERTO"
    STATUS_FECHADO = "FECHADO"
    STATUS_CHOICES = [
        (STATUS_ABERTO, "Aberto"),
        (STATUS_FECHADO, "Fechado"),
    ]

    sessao_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    caixa = models.ForeignKey(
        CaixaHub,
        on_delete=models.PROTECT,
        related_name="sessoes",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ABERTO)
    chave_caixa_aberto = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    valor_abertura = models.DecimalField(max_digits=12, decimal_places=2)
    aberto_em = models.DateTimeField()
    terminal_abertura = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_abertura",
    )
    operador_abertura = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_abertura",
    )
    sessao_operador_abertura = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_abertura",
    )
    fechado_em = models.DateTimeField(null=True, blank=True)
    terminal_fechamento = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_fechamento",
        null=True,
        blank=True,
    )
    operador_fechamento = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_fechamento",
        null=True,
        blank=True,
    )
    sessao_operador_fechamento = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="sessoes_caixa_fechamento",
        null=True,
        blank=True,
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sessão de caixa do Hub"
        verbose_name_plural = "Sessões de caixa do Hub"
        ordering = ("-aberto_em",)
        indexes = [
            models.Index(fields=["caixa", "status"], name="idx_sessao_caixa_status"),
            models.Index(fields=["aberto_em"], name="idx_sessao_caixa_aberto"),
            models.Index(fields=["terminal_abertura"], name="idx_sessao_caixa_term_ab"),
            models.Index(fields=["operador_abertura"], name="idx_sessao_caixa_oper_ab"),
        ]

    def __str__(self):
        return f"{self.caixa.codigo} - {self.status}"


class PareamentoTerminal(models.Model):
    CODIGO_GRUPOS = 3
    CODIGO_TAMANHO_GRUPO = 4
    CODIGO_ALFABETO = string.ascii_uppercase + string.digits

    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.CASCADE,
        related_name="pareamentos",
    )

    codigo_hash = models.CharField(
        max_length=64,
        db_index=True,
    )

    codigo_prefixo = models.CharField(
        max_length=4,
    )

    criado_em = models.DateTimeField(
        auto_now_add=True,
    )

    expira_em = models.DateTimeField()

    usado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    revogado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "Pareamento de Terminal"
        verbose_name_plural = "Pareamentos de Terminal"
        ordering = ("-criado_em",)
        indexes = [
            models.Index(
                fields=["terminal", "usado_em", "revogado_em"],
                name="idx_pareamento_terminal_ativo",
            ),
        ]

    def __str__(self):
        return f"{self.terminal.codigo} - {self.codigo_prefixo}"

    @classmethod
    def gerar_codigo(cls):
        grupos = []
        for _indice in range(cls.CODIGO_GRUPOS):
            grupo = "".join(
                secrets.choice(cls.CODIGO_ALFABETO)
                for _posicao in range(cls.CODIGO_TAMANHO_GRUPO)
            )
            grupos.append(grupo)
        return "-".join(grupos)

    @staticmethod
    def normalizar_codigo(codigo):
        return (codigo or "").strip().upper()

    @classmethod
    def hash_codigo(cls, codigo):
        normalizado = cls.normalizar_codigo(codigo)
        return hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            normalizado.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
