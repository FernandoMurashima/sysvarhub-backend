from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0042_prazo_pagamento_hub"),
    ]

    operations = [
        migrations.AddField(
            model_name="formapagamentohub",
            name="permite_parcelamento",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="FormaPagamentoCondicaoHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("retaguarda_id", models.PositiveBigIntegerField()),
                ("taxa_percentual", models.DecimalField(decimal_places=4, default=0, max_digits=7)),
                ("taxa_fixa", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("ativo", models.BooleanField(default=True)),
                ("sincronizado_em", models.DateTimeField()),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("forma_pagamento", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="condicoes_parcelamento", to="core.formapagamentohub")),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="formas_pagamento_condicoes", to="core.hubconfig")),
                ("prazo_pagamento", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="formas_pagamento_condicoes", to="core.prazopagamentohub")),
            ],
            options={
                "verbose_name": "Condição de parcelamento de forma de pagamento do Hub",
                "verbose_name_plural": "Condições de parcelamento de formas de pagamento do Hub",
                "ordering": ("forma_pagamento__codigo", "prazo_pagamento__num_parcelas", "retaguarda_id"),
            },
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="retaguarda_forma_pagamento_condicao_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="formapagamentocondicaohub",
            constraint=models.UniqueConstraint(fields=("hub", "retaguarda_id"), name="uniq_fpg_cond_hub_ret"),
        ),
        migrations.AddConstraint(
            model_name="formapagamentocondicaohub",
            constraint=models.UniqueConstraint(fields=("forma_pagamento", "prazo_pagamento"), name="uniq_fpg_cond_forma_prazo"),
        ),
        migrations.AddIndex(
            model_name="formapagamentocondicaohub",
            index=models.Index(fields=["hub", "ativo"], name="idx_fpg_cond_hub_ativo"),
        ),
        migrations.AddIndex(
            model_name="formapagamentocondicaohub",
            index=models.Index(fields=["forma_pagamento", "ativo"], name="idx_fpg_cond_forma_ativo"),
        ),
    ]
