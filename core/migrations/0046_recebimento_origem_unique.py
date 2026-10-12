from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0045_recebimento_movimento_status"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="recebimentomercadoriahub",
            name="uniq_rec_hub_ret",
        ),
        migrations.AddConstraint(
            model_name="recebimentomercadoriahub",
            constraint=models.UniqueConstraint(fields=("hub", "origem", "retaguarda_id"), name="uniq_rec_hub_orig_ret"),
        ),
        migrations.AddIndex(
            model_name="recebimentomercadoriahub",
            index=models.Index(fields=["hub", "origem"], name="idx_rec_hub_origem"),
        ),
    ]
