from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_vale_troca_online_pagamento_reserva"),
    ]

    operations = [
        migrations.AddField(
            model_name="valetrocahub",
            name="origem",
            field=models.CharField(default="CENTRAL", max_length=20),
        ),
        migrations.AddField(
            model_name="valetrocahub",
            name="provisorio",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="valetrocahub",
            name="conflito_mensagem",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="origem",
            field=models.CharField(default="VENDA_LOCAL", max_length=24),
        ),
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="dados_origem",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="conflito_mensagem",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
