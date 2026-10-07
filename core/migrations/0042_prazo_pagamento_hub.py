from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_devolucao_documento_faixa_numeracao"),
    ]

    operations = [
        migrations.CreateModel(
            name="PrazoPagamentoHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("retaguarda_id", models.PositiveBigIntegerField()),
                ("codigo", models.CharField(max_length=12)),
                ("descricao", models.CharField(max_length=120)),
                ("num_parcelas", models.PositiveIntegerField()),
                ("intervalo_dias", models.PositiveIntegerField(blank=True, null=True)),
                ("ativo", models.BooleanField(default=True)),
                ("sincronizado_em", models.DateTimeField()),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="prazos_pagamento", to="core.hubconfig")),
            ],
            options={
                "verbose_name": "Prazo de pagamento do Hub",
                "verbose_name_plural": "Prazos de pagamento do Hub",
                "ordering": ("num_parcelas", "codigo", "retaguarda_id"),
            },
        ),
        migrations.CreateModel(
            name="PrazoPagamentoParcelaHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ordem", models.PositiveIntegerField()),
                ("dias", models.IntegerField()),
                ("percentual", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
                ("valor_fixo", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("sincronizado_em", models.DateTimeField()),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("prazo", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="parcelas", to="core.prazopagamentohub")),
            ],
            options={
                "verbose_name": "Parcela de prazo de pagamento do Hub",
                "verbose_name_plural": "Parcelas de prazos de pagamento do Hub",
                "ordering": ("ordem", "id"),
            },
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="prazo_pagamento",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pagamentos_venda", to="core.prazopagamentohub"),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="prazo_codigo",
            field=models.CharField(blank=True, default="", max_length=12),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="prazo_descricao",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="retaguarda_prazo_pagamento_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="prazopagamentohub",
            constraint=models.UniqueConstraint(fields=("hub", "retaguarda_id"), name="uniq_prazo_hub_ret"),
        ),
        migrations.AddConstraint(
            model_name="prazopagamentohub",
            constraint=models.UniqueConstraint(fields=("hub", "codigo"), name="uniq_prazo_hub_cod"),
        ),
        migrations.AddIndex(
            model_name="prazopagamentohub",
            index=models.Index(fields=["hub", "ativo"], name="idx_prazo_hub_ativo"),
        ),
        migrations.AddConstraint(
            model_name="prazopagamentoparcelahub",
            constraint=models.UniqueConstraint(fields=("prazo", "ordem"), name="uniq_prazo_parc_ord"),
        ),
    ]
