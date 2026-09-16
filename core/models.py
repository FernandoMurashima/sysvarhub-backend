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

    formas_pagamento_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    formas_pagamento_gerado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    formas_pagamento_sincronizado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    clientes_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    clientes_gerado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    clientes_sincronizado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    vendedores_versao = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
    )

    vendedores_gerado_em = models.DateTimeField(
        null=True,
        blank=True,
    )

    vendedores_sincronizado_em = models.DateTimeField(
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


class FormaPagamentoHub(models.Model):
    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="formas_pagamento",
    )

    retaguarda_id = models.PositiveBigIntegerField()
    codigo = models.CharField(max_length=10)
    descricao = models.CharField(max_length=120)
    tipo = models.CharField(max_length=24)
    num_parcelas = models.PositiveIntegerField()
    ativo = models.BooleanField(default=True)

    prazo_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    prazo_codigo = models.CharField(max_length=12, blank=True, default="")
    prazo_descricao = models.CharField(max_length=120, blank=True, default="")
    prazo_num_parcelas = models.PositiveIntegerField(null=True, blank=True)
    prazo_intervalo_dias = models.PositiveIntegerField(null=True, blank=True)

    adquirente = models.CharField(max_length=80, null=True, blank=True)
    conta_liquidacao_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    gera_recebivel_bancario = models.BooleanField(default=False)
    prazo_credito_dias = models.PositiveIntegerField(default=0)
    taxa_percentual = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    taxa_fixa = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    tef_habilitado = models.BooleanField(default=False)
    tef_modalidade = models.CharField(max_length=20, blank=True, default="")
    tef_adquirente_codigo = models.CharField(max_length=40, blank=True, default="")
    tef_terminal_logico = models.CharField(max_length=40, blank=True, default="")

    sincronizado_em = models.DateTimeField()
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Forma de pagamento do Hub"
        verbose_name_plural = "Formas de pagamento do Hub"
        ordering = ("codigo", "retaguarda_id")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_id"],
                name="uniq_fpg_hub_ret",
            ),
            models.UniqueConstraint(
                fields=["hub", "codigo"],
                name="uniq_fpg_hub_cod",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "ativo"], name="idx_fpg_hub_ativo"),
        ]

    def __str__(self):
        return f"{self.codigo} - {self.descricao}"


class FormaPagamentoParcelaHub(models.Model):
    forma = models.ForeignKey(
        FormaPagamentoHub,
        on_delete=models.CASCADE,
        related_name="parcelas",
    )
    ordem = models.PositiveIntegerField()
    dias = models.IntegerField()
    percentual = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    valor_fixo = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    sincronizado_em = models.DateTimeField()
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Parcela de forma de pagamento do Hub"
        verbose_name_plural = "Parcelas de formas de pagamento do Hub"
        ordering = ("ordem", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["forma", "ordem"],
                name="uniq_fpg_parc_ord",
            ),
        ]

    def __str__(self):
        return f"{self.forma.codigo} - {self.ordem}"


class ClienteHub(models.Model):
    ORIGEM_RETAGUARDA = "RETAGUARDA"
    ORIGEM_LOCAL = "LOCAL"
    ORIGEM_CHOICES = [
        (ORIGEM_RETAGUARDA, "Retaguarda"),
        (ORIGEM_LOCAL, "Local"),
    ]

    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="clientes",
    )
    cliente_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    origem = models.CharField(max_length=20, choices=ORIGEM_CHOICES, default=ORIGEM_RETAGUARDA)
    presente_retaguarda = models.BooleanField(default=False)

    tipo_pessoa = models.CharField(max_length=2)
    documento = models.CharField(max_length=20, null=True, blank=True)
    cliente_padrao = models.BooleanField(default=False)

    nome_cliente = models.CharField(max_length=150)
    apelido = models.CharField(max_length=100, blank=True, default="")

    endereco = models.CharField(max_length=150, blank=True, default="")
    numero = models.CharField(max_length=20, blank=True, default="")
    complemento = models.CharField(max_length=100, blank=True, default="")
    cep = models.CharField(max_length=12, blank=True, default="")
    bairro = models.CharField(max_length=100, blank=True, default="")
    cidade = models.CharField(max_length=100, blank=True, default="")
    estado = models.CharField(max_length=2, blank=True, default="")

    telefone1 = models.CharField(max_length=30, blank=True, default="")
    telefone2 = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(max_length=254, blank=True, default="")
    categoria = models.CharField(max_length=80, blank=True, default="")

    bloqueio = models.BooleanField(default=False)
    motivo_bloqueio = models.CharField(max_length=200, null=True, blank=True)

    aniversario = models.DateField(null=True, blank=True)

    mala_direta = models.BooleanField(default=False)
    aceita_email = models.BooleanField(default=False)
    aceita_whatsapp = models.BooleanField(default=False)
    aceita_sms = models.BooleanField(default=False)
    consentimento_em = models.DateTimeField(null=True, blank=True)
    origem_consentimento = models.CharField(max_length=80, blank=True, default="")

    ativo = models.BooleanField(default=True)

    sincronizado_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cliente do Hub"
        verbose_name_plural = "Clientes do Hub"
        ordering = ("nome_cliente", "retaguarda_id", "cliente_uuid")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_id"],
                name="uniq_cliente_hub_ret",
            ),
            models.UniqueConstraint(
                fields=["hub", "documento"],
                name="uniq_cliente_hub_doc",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "presente_retaguarda"], name="idx_cli_hub_pres"),
            models.Index(fields=["hub", "origem"], name="idx_cli_hub_origem"),
            models.Index(fields=["hub", "nome_cliente"], name="idx_cli_hub_nome"),
        ]

    def __str__(self):
        return f"{self.retaguarda_id or self.cliente_uuid} - {self.nome_cliente}"


class VendedorHub(models.Model):
    hub = models.ForeignKey(
        HubConfig,
        on_delete=models.PROTECT,
        related_name="vendedores",
    )
    retaguarda_id = models.PositiveBigIntegerField()
    matricula = models.CharField(max_length=6, blank=True, default="")
    nome = models.CharField(max_length=50)
    apelido = models.CharField(max_length=20, blank=True, default="")

    cargo_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    cargo_codigo = models.CharField(max_length=20, blank=True, default="")
    cargo_descricao = models.CharField(max_length=80, blank=True, default="")

    comissionado = models.BooleanField(default=False)
    comissao_percentual = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    ativo = models.BooleanField(default=True)
    situacao = models.CharField(max_length=12)
    participa_vendas = models.BooleanField(default=False)
    presente_retaguarda = models.BooleanField(default=False)

    sincronizado_em = models.DateTimeField()
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Vendedor do Hub"
        verbose_name_plural = "Vendedores do Hub"
        ordering = ("nome", "retaguarda_id")
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "retaguarda_id"],
                name="uniq_vendedor_hub_ret",
            ),
        ]
        indexes = [
            models.Index(fields=["hub", "presente_retaguarda"], name="idx_vend_hub_pres"),
            models.Index(fields=["hub", "nome"], name="idx_vend_hub_nome"),
            models.Index(fields=["hub", "matricula"], name="idx_vend_hub_mat"),
        ]

    def __str__(self):
        return f"{self.retaguarda_id} - {self.nome}"


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


class ContextoVendaTerminalHub(models.Model):
    terminal = models.OneToOneField(
        Terminal,
        on_delete=models.CASCADE,
        related_name="contexto_venda",
    )
    cliente_preselecionado = models.ForeignKey(
        ClienteHub,
        on_delete=models.SET_NULL,
        related_name="contextos_pre_venda",
        null=True,
        blank=True,
    )
    vendedor_preselecionado = models.ForeignKey(
        VendedorHub,
        on_delete=models.SET_NULL,
        related_name="contextos_pre_venda",
        null=True,
        blank=True,
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Contexto de venda do terminal Hub"
        verbose_name_plural = "Contextos de venda dos terminais Hub"

    def __str__(self):
        return f"{self.terminal.codigo} - contexto de venda"


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


class VendaHub(models.Model):
    STATUS_ABERTA = "ABERTA"
    STATUS_FINALIZADA = "FINALIZADA"
    STATUS_CANCELADA = "CANCELADA"
    STATUS_CHOICES = [
        (STATUS_ABERTA, "Aberta"),
        (STATUS_FINALIZADA, "Finalizada"),
        (STATUS_CANCELADA, "Cancelada"),
    ]

    venda_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    hub = models.ForeignKey(HubConfig, on_delete=models.PROTECT, related_name="vendas")
    sessao_caixa = models.ForeignKey(
        SessaoCaixaHub,
        on_delete=models.PROTECT,
        related_name="vendas",
    )
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="vendas")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_ABERTA)
    chave_venda_aberta_terminal = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        unique=True,
    )
    operador_criacao = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_criadas",
    )
    sessao_operador_criacao = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_criadas",
    )
    criada_em = models.DateTimeField(auto_now_add=True)
    atualizada_em = models.DateTimeField(auto_now=True)
    cancelada_em = models.DateTimeField(null=True, blank=True)
    operador_cancelamento = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_canceladas",
        null=True,
        blank=True,
    )
    sessao_operador_cancelamento = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_canceladas",
        null=True,
        blank=True,
    )
    terminal_cancelamento = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="vendas_canceladas",
        null=True,
        blank=True,
    )
    finalizada_em = models.DateTimeField(null=True, blank=True)
    terminal_finalizacao = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="vendas_finalizadas",
        null=True,
        blank=True,
    )
    operador_finalizacao = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_finalizadas",
        null=True,
        blank=True,
    )
    sessao_operador_finalizacao = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="vendas_finalizadas",
        null=True,
        blank=True,
    )
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    desconto_itens = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    desconto_geral = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    valor_recebido = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    troco = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cliente_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    cliente_uuid = models.UUIDField(null=True, blank=True)
    cliente_tipo_pessoa = models.CharField(max_length=2, blank=True, default="")
    cliente_documento = models.CharField(max_length=20, null=True, blank=True)
    cliente_padrao = models.BooleanField(default=False)
    cliente_nome = models.CharField(max_length=150, blank=True, default="")
    vendedor_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    vendedor_matricula = models.CharField(max_length=6, blank=True, default="")
    vendedor_nome = models.CharField(max_length=50, blank=True, default="")
    vendedor_apelido = models.CharField(max_length=20, blank=True, default="")
    vendedor_cargo_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    vendedor_cargo_codigo = models.CharField(max_length=20, blank=True, default="")
    vendedor_cargo_descricao = models.CharField(max_length=80, blank=True, default="")
    vendedor_comissionado = models.BooleanField(default=False)
    vendedor_comissao_percentual = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Venda do Hub"
        verbose_name_plural = "Vendas do Hub"
        ordering = ("-criada_em",)
        indexes = [
            models.Index(fields=["hub", "status"], name="idx_venda_hub_status"),
            models.Index(fields=["terminal", "status"], name="idx_venda_terminal_status"),
            models.Index(fields=["sessao_caixa", "status"], name="idx_venda_caixa_status"),
        ]

    def __str__(self):
        return f"{self.venda_uuid} - {self.status}"


class VendaItemHub(models.Model):
    item_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    venda = models.ForeignKey(VendaHub, on_delete=models.PROTECT, related_name="itens")
    catalogo_item = models.ForeignKey(
        CatalogoItemHub,
        on_delete=models.PROTECT,
        related_name="venda_itens",
    )
    retaguarda_produto_id = models.PositiveBigIntegerField()
    retaguarda_sku_id = models.PositiveBigIntegerField()
    ean13 = models.CharField(max_length=13, blank=True, default="")
    referencia = models.CharField(max_length=80, blank=True, default="")
    codigo_item_ref = models.CharField(max_length=80, blank=True, default="")
    descricao = models.CharField(max_length=200)
    descricao_reduzida = models.CharField(max_length=120, blank=True, default="")
    cor_descricao = models.CharField(max_length=80, blank=True, default="")
    tamanho_descricao = models.CharField(max_length=80, blank=True, default="")
    unidade_codigo = models.CharField(max_length=20, blank=True, default="")
    quantidade = models.PositiveIntegerField()
    preco_unitario = models.DecimalField(max_digits=18, decimal_places=4)
    desconto = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_item = models.DecimalField(max_digits=18, decimal_places=2)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    operador_inclusao = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="venda_itens_incluidos",
    )
    sessao_operador_inclusao = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="venda_itens_incluidos",
    )
    terminal_inclusao = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="venda_itens_incluidos",
    )

    class Meta:
        verbose_name = "Item de venda do Hub"
        verbose_name_plural = "Itens de venda do Hub"
        ordering = ("criado_em", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["venda", "retaguarda_sku_id"],
                name="uniq_venda_hub_sku",
            ),
        ]
        indexes = [
            models.Index(fields=["venda", "retaguarda_sku_id"], name="idx_venda_item_sku"),
            models.Index(fields=["retaguarda_sku_id"], name="idx_venda_item_sku_global"),
        ]

    def __str__(self):
        return f"{self.venda_id} - {self.retaguarda_sku_id}"


class VendaEventoHub(models.Model):
    TIPO_VENDA_CRIADA = "VENDA_CRIADA"
    TIPO_ITEM_ADICIONADO = "ITEM_ADICIONADO"
    TIPO_ITEM_QUANTIDADE_ALTERADA = "ITEM_QUANTIDADE_ALTERADA"
    TIPO_ITEM_REMOVIDO = "ITEM_REMOVIDO"
    TIPO_VENDA_CANCELADA = "VENDA_CANCELADA"
    TIPO_PAGAMENTO_ADICIONADO = "PAGAMENTO_ADICIONADO"
    TIPO_PAGAMENTO_REMOVIDO = "PAGAMENTO_REMOVIDO"
    TIPO_VENDA_FINALIZADA = "VENDA_FINALIZADA"
    TIPO_CLIENTE_SELECIONADO = "CLIENTE_SELECIONADO"
    TIPO_CLIENTE_REMOVIDO = "CLIENTE_REMOVIDO"
    TIPO_VENDEDOR_SELECIONADO = "VENDEDOR_SELECIONADO"
    TIPO_VENDEDOR_REMOVIDO = "VENDEDOR_REMOVIDO"
    TIPO_CHOICES = [
        (TIPO_VENDA_CRIADA, "Venda criada"),
        (TIPO_ITEM_ADICIONADO, "Item adicionado"),
        (TIPO_ITEM_QUANTIDADE_ALTERADA, "Item alterado"),
        (TIPO_ITEM_REMOVIDO, "Item removido"),
        (TIPO_VENDA_CANCELADA, "Venda cancelada"),
        (TIPO_PAGAMENTO_ADICIONADO, "Pagamento adicionado"),
        (TIPO_PAGAMENTO_REMOVIDO, "Pagamento removido"),
        (TIPO_VENDA_FINALIZADA, "Venda finalizada"),
        (TIPO_CLIENTE_SELECIONADO, "Cliente selecionado"),
        (TIPO_CLIENTE_REMOVIDO, "Cliente removido"),
        (TIPO_VENDEDOR_SELECIONADO, "Vendedor selecionado"),
        (TIPO_VENDEDOR_REMOVIDO, "Vendedor removido"),
    ]

    venda = models.ForeignKey(VendaHub, on_delete=models.PROTECT, related_name="eventos")
    tipo = models.CharField(max_length=40, choices=TIPO_CHOICES)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="eventos_venda")
    operador = models.ForeignKey(OperadorHub, on_delete=models.PROTECT, related_name="eventos_venda")
    sessao_operador = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="eventos_venda",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    dados = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Evento de venda do Hub"
        verbose_name_plural = "Eventos de venda do Hub"
        ordering = ("criado_em", "id")
        indexes = [
            models.Index(fields=["venda", "tipo"], name="idx_venda_evento_tipo"),
            models.Index(fields=["criado_em"], name="idx_venda_evento_criado"),
        ]

    def __str__(self):
        return f"{self.venda_id} - {self.tipo}"


class VendaPagamentoHub(models.Model):
    ORIGEM_MANUAL = "MANUAL"
    ORIGEM_CHOICES = [
        (ORIGEM_MANUAL, "Manual"),
    ]
    STATUS_ATIVO = "ATIVO"
    STATUS_REMOVIDO = "REMOVIDO"
    STATUS_CHOICES = [
        (STATUS_ATIVO, "Ativo"),
        (STATUS_REMOVIDO, "Removido"),
    ]

    pagamento_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    operacao_uuid = models.UUIDField(editable=False)
    venda = models.ForeignKey(VendaHub, on_delete=models.PROTECT, related_name="pagamentos")
    forma_pagamento = models.ForeignKey(
        FormaPagamentoHub,
        on_delete=models.PROTECT,
        related_name="pagamentos_venda",
    )
    retaguarda_forma_pagamento_id = models.PositiveBigIntegerField()
    codigo = models.CharField(max_length=10)
    descricao = models.CharField(max_length=120)
    tipo = models.CharField(max_length=24)
    num_parcelas = models.PositiveIntegerField()
    adquirente = models.CharField(max_length=80, null=True, blank=True)
    conta_liquidacao_retaguarda_id = models.PositiveBigIntegerField(null=True, blank=True)
    gera_recebivel_bancario = models.BooleanField(default=False)
    prazo_credito_dias = models.PositiveIntegerField(default=0)
    taxa_percentual = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    taxa_fixa = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    valor = models.DecimalField(max_digits=18, decimal_places=2)
    autorizacao = models.CharField(max_length=120, blank=True, default="")
    origem_captura = models.CharField(max_length=10, choices=ORIGEM_CHOICES, default=ORIGEM_MANUAL)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ATIVO)
    terminal_inclusao = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="pagamentos_incluidos")
    operador_inclusao = models.ForeignKey(OperadorHub, on_delete=models.PROTECT, related_name="pagamentos_incluidos")
    sessao_operador_inclusao = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="pagamentos_incluidos",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    removido_em = models.DateTimeField(null=True, blank=True)
    terminal_remocao = models.ForeignKey(
        Terminal,
        on_delete=models.PROTECT,
        related_name="pagamentos_removidos",
        null=True,
        blank=True,
    )
    operador_remocao = models.ForeignKey(
        OperadorHub,
        on_delete=models.PROTECT,
        related_name="pagamentos_removidos",
        null=True,
        blank=True,
    )
    sessao_operador_remocao = models.ForeignKey(
        SessaoOperadorHub,
        on_delete=models.PROTECT,
        related_name="pagamentos_removidos",
        null=True,
        blank=True,
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Pagamento de venda do Hub"
        verbose_name_plural = "Pagamentos de venda do Hub"
        ordering = ("criado_em", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["venda", "operacao_uuid"],
                name="uniq_vpag_oper",
            ),
        ]

    def __str__(self):
        return f"{self.venda_id} - {self.codigo} - {self.valor}"


class VendaPagamentoParcelaHub(models.Model):
    pagamento = models.ForeignKey(
        VendaPagamentoHub,
        on_delete=models.CASCADE,
        related_name="parcelas_snapshot",
    )
    ordem = models.PositiveIntegerField()
    dias = models.IntegerField()
    percentual = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    valor_fixo = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "Parcela snapshot de pagamento do Hub"
        verbose_name_plural = "Parcelas snapshot de pagamento do Hub"
        ordering = ("ordem", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["pagamento", "ordem"],
                name="uniq_vpag_parc_ord",
            ),
        ]

    def __str__(self):
        return f"{self.pagamento_id} - {self.ordem}"


class EstoqueMovimentoHub(models.Model):
    TIPO_SAIDA_VENDA = "SAIDA_VENDA"
    TIPO_CHOICES = [
        (TIPO_SAIDA_VENDA, "Saída por venda"),
    ]

    movimento_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    hub = models.ForeignKey(HubConfig, on_delete=models.PROTECT, related_name="movimentos_estoque")
    venda = models.ForeignKey(VendaHub, on_delete=models.PROTECT, related_name="movimentos_estoque")
    venda_item = models.ForeignKey(
        VendaItemHub,
        on_delete=models.PROTECT,
        related_name="movimentos_estoque",
    )
    catalogo_item = models.ForeignKey(
        CatalogoItemHub,
        on_delete=models.PROTECT,
        related_name="movimentos_estoque",
    )
    retaguarda_produto_id = models.PositiveBigIntegerField()
    retaguarda_sku_id = models.PositiveBigIntegerField()
    ean13 = models.CharField(max_length=13, blank=True, default="")
    referencia = models.CharField(max_length=80, blank=True, default="")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_SAIDA_VENDA)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3)
    sincronizado_central_em = models.DateTimeField(null=True, blank=True)
    reconciliado_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Movimento de estoque do Hub"
        verbose_name_plural = "Movimentos de estoque do Hub"
        ordering = ("criado_em", "id")
        indexes = [
            models.Index(fields=["hub", "retaguarda_sku_id"], name="idx_est_mov_hub_sku"),
            models.Index(fields=["reconciliado_em"], name="idx_est_mov_reconc"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["venda_item"], name="uniq_est_mov_item"),
        ]

    def __str__(self):
        return f"{self.tipo} - {self.retaguarda_sku_id} - {self.quantidade}"


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
