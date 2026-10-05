from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_devolucao_offline_provisoria"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendahub",
            name="documento",
            field=models.CharField(blank=True, db_index=True, max_length=12, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="FaixaNumeracaoVendaHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inicio", models.PositiveIntegerField()),
                ("fim", models.PositiveIntegerField()),
                ("proximo_numero", models.PositiveIntegerField()),
                ("recebido_em", models.DateTimeField(auto_now_add=True)),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="faixas_numeracao_venda", to="core.hubconfig")),
            ],
            options={
                "verbose_name": "Faixa de numeracao de venda do Hub",
                "verbose_name_plural": "Faixas de numeracao de venda do Hub",
                "ordering": ("hub_id", "inicio"),
                "indexes": [models.Index(fields=["hub", "proximo_numero", "fim"], name="idx_faixa_venda_hub_disp")],
                "constraints": [
                    models.UniqueConstraint(fields=("hub", "inicio", "fim"), name="uniq_faixa_venda_hub_intervalo"),
                    models.CheckConstraint(check=models.Q(inicio__lte=models.F("fim")), name="ck_faixa_venda_hub_ordem"),
                    models.CheckConstraint(check=models.Q(proximo_numero__gte=models.F("inicio")), name="ck_faixa_venda_hub_prox_min"),
                ],
            },
        ),
    ]
