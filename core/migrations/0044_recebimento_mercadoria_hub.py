from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0043_forma_pagamento_condicao_hub"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecebimentoMercadoriaHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("recebimento_uuid", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("retaguarda_id", models.PositiveBigIntegerField()),
                ("origem", models.CharField(blank=True, default="", max_length=30)),
                ("status_retaguarda", models.CharField(blank=True, default="", max_length=30)),
                ("status_local", models.CharField(choices=[("PENDENTE", "Pendente"), ("CONFERIDO", "Conferido"), ("SINCRONIZADO", "Sincronizado"), ("CONFLITO", "Conflito")], db_index=True, default="PENDENTE", max_length=20)),
                ("empresa_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("loja_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("loja_nome", models.CharField(blank=True, default="", max_length=150)),
                ("fornecedor_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("fornecedor_nome", models.CharField(blank=True, default="", max_length=150)),
                ("fornecedor_documento", models.CharField(blank=True, default="", max_length=20)),
                ("documento_fiscal", models.JSONField(blank=True, default=dict)),
                ("pedidos", models.JSONField(blank=True, default=list)),
                ("resumo", models.JSONField(blank=True, default=dict)),
                ("contrato_retorno", models.JSONField(blank=True, default=dict)),
                ("observacao_divergencia", models.TextField(blank=True, default="")),
                ("mensagem_sync", models.CharField(blank=True, default="", max_length=255)),
                ("resposta_central", models.JSONField(blank=True, default=dict)),
                ("recebido_central_em", models.DateTimeField(blank=True, null=True)),
                ("conferido_em", models.DateTimeField(blank=True, null=True)),
                ("estoque_efetivado_em", models.DateTimeField(blank=True, null=True)),
                ("confirmado_central_em", models.DateTimeField(blank=True, null=True)),
                ("sincronizado_em", models.DateTimeField(blank=True, null=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("evento_sync", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recebimentos_mercadoria", to="core.eventosynchub")),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recebimentos_mercadoria", to="core.hubconfig")),
                ("operador_conferencia", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recebimentos_conferidos", to="core.operadorhub")),
                ("sessao_operador_conferencia", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recebimentos_conferidos", to="core.sessaooperadorhub")),
                ("terminal_conferencia", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recebimentos_conferidos", to="core.terminal")),
            ],
            options={
                "verbose_name": "Recebimento de mercadoria do Hub",
                "verbose_name_plural": "Recebimentos de mercadoria do Hub",
                "ordering": ("-recebido_central_em", "-id"),
            },
        ),
        migrations.CreateModel(
            name="RecebimentoMercadoriaItemHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("conferencia_item_id", models.PositiveBigIntegerField()),
                ("pedido_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("pedido_item_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("retaguarda_produto_id", models.PositiveBigIntegerField()),
                ("retaguarda_sku_id", models.PositiveBigIntegerField()),
                ("ean13", models.CharField(blank=True, default="", max_length=13)),
                ("referencia", models.CharField(blank=True, default="", max_length=80)),
                ("descricao", models.CharField(max_length=200)),
                ("cor_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("cor_descricao", models.CharField(blank=True, default="", max_length=80)),
                ("tamanho_retaguarda_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("tamanho_descricao", models.CharField(blank=True, default="", max_length=80)),
                ("quantidade_esperada", models.DecimalField(decimal_places=3, default=0, max_digits=14)),
                ("quantidade_recebida", models.DecimalField(decimal_places=3, default=0, max_digits=14)),
                ("diferenca", models.DecimalField(decimal_places=3, default=0, max_digits=14)),
                ("situacao", models.CharField(blank=True, default="", max_length=10)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("catalogo_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recebimentos_mercadoria", to="core.catalogoitemhub")),
                ("recebimento", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="itens", to="core.recebimentomercadoriahub")),
            ],
            options={
                "verbose_name": "Item de recebimento de mercadoria do Hub",
                "verbose_name_plural": "Itens de recebimento de mercadoria do Hub",
                "ordering": ("id",),
            },
        ),
        migrations.CreateModel(
            name="RecebimentoMercadoriaMovimentoEstoqueHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("movimento_uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("retaguarda_produto_id", models.PositiveBigIntegerField()),
                ("retaguarda_sku_id", models.PositiveBigIntegerField()),
                ("ean13", models.CharField(blank=True, default="", max_length=13)),
                ("referencia", models.CharField(blank=True, default="", max_length=80)),
                ("tipo", models.CharField(default="ENTRADA_RECEBIMENTO", max_length=30)),
                ("quantidade", models.DecimalField(decimal_places=3, max_digits=14)),
                ("saldo_anterior", models.DecimalField(decimal_places=3, default=0, max_digits=14)),
                ("saldo_posterior", models.DecimalField(decimal_places=3, default=0, max_digits=14)),
                ("sincronizado_central_em", models.DateTimeField(blank=True, null=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("catalogo_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="movimentos_recebimento", to="core.catalogoitemhub")),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="movimentos_recebimento", to="core.hubconfig")),
                ("item", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="movimento_estoque", to="core.recebimentomercadoriaitemhub")),
                ("recebimento", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="movimentos_estoque", to="core.recebimentomercadoriahub")),
            ],
            options={
                "verbose_name": "Movimento de entrada de recebimento do Hub",
                "verbose_name_plural": "Movimentos de entrada de recebimento do Hub",
                "ordering": ("criado_em", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="recebimentomercadoriahub",
            constraint=models.UniqueConstraint(fields=("hub", "retaguarda_id"), name="uniq_rec_hub_ret"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriahub",
            index=models.Index(fields=["hub", "status_local"], name="idx_rec_hub_status"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriahub",
            index=models.Index(fields=["hub", "status_retaguarda"], name="idx_rec_hub_status_ret"),
        ),
        migrations.AddConstraint(
            model_name="recebimentomercadoriaitemhub",
            constraint=models.UniqueConstraint(fields=("recebimento", "conferencia_item_id"), name="uniq_rec_item_hub_conf"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriaitemhub",
            index=models.Index(fields=["retaguarda_sku_id"], name="idx_rec_item_hub_sku"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriamovimentoestoquehub",
            index=models.Index(fields=["hub", "retaguarda_sku_id"], name="idx_rec_mov_hub_sku"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriamovimentoestoquehub",
            index=models.Index(fields=["sincronizado_central_em"], name="idx_rec_mov_hub_sync"),
        ),
    ]
