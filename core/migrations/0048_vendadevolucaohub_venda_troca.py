from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0047_cashback_online_pagamento_reserva")]

    operations = [
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="venda_troca",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="devolucoes_troca", to="core.vendahub",
            ),
        ),
    ]
