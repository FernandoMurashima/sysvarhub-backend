from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_venda_documento_faixa_numeracao"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="documento",
            field=models.CharField(blank=True, db_index=True, max_length=11, null=True),
        ),
        migrations.CreateModel(
            name="FaixaNumeracaoDevolucaoHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inicio", models.PositiveIntegerField()),
                ("fim", models.PositiveIntegerField()),
                ("proximo_numero", models.PositiveIntegerField()),
                ("recebido_em", models.DateTimeField(auto_now_add=True)),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="faixas_numeracao_devolucao", to="core.hubconfig")),
            ],
            options={
                "verbose_name": "Faixa de numeracao de devolucao do Hub",
                "verbose_name_plural": "Faixas de numeracao de devolucao do Hub",
                "ordering": ("hub_id", "inicio"),
                "indexes": [models.Index(fields=["hub", "proximo_numero", "fim"], name="idx_faixa_dev_hub_disp")],
                "constraints": [
                    models.UniqueConstraint(fields=("hub", "inicio", "fim"), name="uniq_faixa_dev_hub_intervalo"),
                    models.CheckConstraint(check=models.Q(inicio__lte=models.F("fim")), name="ck_faixa_dev_hub_ordem"),
                    models.CheckConstraint(check=models.Q(proximo_numero__gte=models.F("inicio")), name="ck_faixa_dev_hub_prox_min"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="vendadevolucaohub",
            constraint=models.UniqueConstraint(fields=("hub", "documento"), name="uniq_dev_hub_documento"),
        ),
    ]
