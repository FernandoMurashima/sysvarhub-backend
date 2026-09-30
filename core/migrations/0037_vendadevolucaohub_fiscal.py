from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_vendadevolucaohub_confirmado_central_em_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendadevolucaohub",
            name="fiscal",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
