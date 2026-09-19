from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_fechamentodiahub_fechamentodiaformahub_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='catalogoitemhub',
            name='imagem_local',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='catalogoitemhub',
            name='imagem_retaguarda_id',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='catalogoitemhub',
            name='imagem_tipo',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='catalogoitemhub',
            name='imagem_versao',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
        migrations.AddIndex(
            model_name='catalogoitemhub',
            index=models.Index(fields=['hub', 'retaguarda_produto_id'], name='idx_catalogo_hub_prod'),
        ),
    ]
