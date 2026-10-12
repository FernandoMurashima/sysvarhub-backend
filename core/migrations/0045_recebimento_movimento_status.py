from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0044_recebimento_mercadoria_hub"),
    ]

    operations = [
        migrations.AddField(
            model_name="recebimentomercadoriamovimentoestoquehub",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDENTE", "Pendente"),
                    ("APLICADO", "Aplicado"),
                    ("REJEITADO", "Rejeitado"),
                ],
                db_index=True,
                default="APLICADO",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="recebimentomercadoriamovimentoestoquehub",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDENTE", "Pendente"),
                    ("APLICADO", "Aplicado"),
                    ("REJEITADO", "Rejeitado"),
                ],
                db_index=True,
                default="PENDENTE",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="recebimentomercadoriamovimentoestoquehub",
            name="aplicado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recebimentomercadoriamovimentoestoquehub",
            name="rejeitado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recebimentomercadoriamovimentoestoquehub",
            name="mensagem_status",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriamovimentoestoquehub",
            index=models.Index(fields=["hub", "status"], name="idx_rec_mov_hub_status"),
        ),
    ]
