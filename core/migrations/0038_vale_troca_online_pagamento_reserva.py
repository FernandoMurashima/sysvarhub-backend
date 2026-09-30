from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0037_vendadevolucaohub_fiscal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendapagamentohub",
            name="forma_pagamento",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pagamentos_venda", to="core.formapagamentohub"),
        ),
        migrations.AlterField(
            model_name="vendapagamentohub",
            name="retaguarda_forma_pagamento_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="vale_troca_reserva_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendapagamentohub",
            name="vale_troca_valor_reservado",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
    ]
