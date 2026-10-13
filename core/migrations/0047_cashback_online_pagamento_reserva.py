from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0046_recebimento_origem_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendapagamentohub",
            name="cashback_origem",
            field=models.CharField(
                choices=[("LOCAL", "Local"), ("CENTRAL", "Central")],
                default="LOCAL",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="cashback_reserva_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="cashback_valor_reservado",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
    ]
