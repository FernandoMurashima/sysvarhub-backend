from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_terminal_token_criptografado"),
    ]

    operations = [
        migrations.AddField(
            model_name="hubconfig",
            name="ultima_tentativa_central_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
