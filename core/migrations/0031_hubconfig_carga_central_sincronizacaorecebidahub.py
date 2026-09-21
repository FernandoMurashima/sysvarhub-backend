# Generated manually because the local venv points to an unavailable Python and mysqlclient cannot load under the fallback interpreter.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0030_cashbackmovimentohub_centralizado_em_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="hubconfig",
            name="primeira_carga_concluida_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hubconfig",
            name="ultima_carga_central_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hubconfig",
            name="ultima_carga_central_status",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="hubconfig",
            name="ultima_carga_central_erro",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="SincronizacaoRecebidaHub",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("retaguarda_solicitacao_id", models.PositiveBigIntegerField()),
                ("tipo", models.CharField(max_length=20)),
                ("status", models.CharField(choices=[("PENDENTE", "Pendente"), ("PROCESSANDO", "Processando"), ("CONCLUIDA", "Concluída"), ("ERRO", "Erro")], db_index=True, default="PENDENTE", max_length=20)),
                ("etapa_atual", models.CharField(blank=True, default="", max_length=80)),
                ("mensagem_erro", models.TextField(blank=True, default="")),
                ("recebido_em", models.DateTimeField(auto_now_add=True)),
                ("iniciado_em", models.DateTimeField(blank=True, null=True)),
                ("concluido_em", models.DateTimeField(blank=True, null=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("hub", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sincronizacoes_recebidas", to="core.hubconfig")),
            ],
            options={
                "ordering": ("-recebido_em", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="sincronizacaorecebidahub",
            constraint=models.UniqueConstraint(fields=("hub", "retaguarda_solicitacao_id"), name="uniq_sync_recebida_hub_ret"),
        ),
        migrations.AddIndex(
            model_name="sincronizacaorecebidahub",
            index=models.Index(fields=["hub", "status"], name="idx_sync_rec_hub_status"),
        ),
    ]
