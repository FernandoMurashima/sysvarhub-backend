from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_hubconfig_carga_central_sincronizacaorecebidahub"),
    ]

    operations = [
        migrations.AddField(
            model_name="formapagamentohub",
            name="adquirente_retaguarda_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="formapagamentohub",
            name="condicao_adquirente_retaguarda_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
