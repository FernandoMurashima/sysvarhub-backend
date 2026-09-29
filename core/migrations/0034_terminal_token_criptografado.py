from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_comandoadministrativorecebidohub_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="terminal",
            name="token_criptografado",
            field=models.TextField(blank=True, default=""),
        ),
    ]
