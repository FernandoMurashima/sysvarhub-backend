from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.utils import timezone

from core.models import CaixaHub, HubConfig, OperadorHub, SessaoCaixaHub, SessaoOperadorHub
from core.services.snapshot_operacional import montar_snapshot_operacional
from core.services.terminais import configurar_terminal


class SnapshotOperacionalTests(TestCase):
    def setUp(self):
        self.agora = timezone.now()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            retaguarda_hub_id=7,
            empresa_id=11,
            loja_id=41,
            bootstrap_versao=1,
            retaguarda_token="TOKEN-SECRETO",
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-01",
            descricao="Caixa 01",
            ativo=True,
            sincronizado_em=self.agora,
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", self.caixa.retaguarda_id)
        self.terminal.pareado_em = self.agora
        self.terminal.ultima_conexao_em = self.agora
        self.terminal.hostname = "pdv-local"
        self.terminal.ultimo_ip = "127.0.0.1"
        self.terminal.gerar_token()
        self.terminal.save()
        self.operador = OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=90,
            codigo="op.caixa",
            nome="Operador Caixa",
            tipo="Caixa",
            credencial_hash=make_password("1234"),
            sincronizado_em=self.agora,
        )
        self.sessao_operador = SessaoOperadorHub(
            terminal=self.terminal,
            operador=self.operador,
            ultima_atividade_em=self.agora,
        )
        self.sessao_operador.gerar_token()
        self.sessao_operador.save()

    def test_snapshot_serializa_status_operacional_sem_campos_sensiveis(self):
        SessaoCaixaHub.objects.create(
            caixa=self.caixa,
            status=SessaoCaixaHub.STATUS_ABERTO,
            chave_caixa_aberto=self.caixa.pk,
            valor_abertura="100.00",
            aberto_em=self.agora,
            terminal_abertura=self.terminal,
            operador_abertura=self.operador,
            sessao_operador_abertura=self.sessao_operador,
        )

        snapshot = montar_snapshot_operacional(self.hub)

        terminal = snapshot["terminais"][0]
        self.assertEqual(terminal["terminal_uuid"], str(self.terminal.terminal_uuid))
        self.assertEqual(terminal["codigo"], "PDV-01")
        self.assertTrue(terminal["online"])
        self.assertTrue(terminal["pareado"])
        self.assertEqual(terminal["caixa_status"], "ABERTO")
        self.assertEqual(terminal["caixa"]["id"], 29)
        self.assertEqual(terminal["sessao_caixa"]["operador"]["codigo"], "op.caixa")
        self.assertNotIn("token_hash", terminal)
        self.assertNotIn("token_prefixo", terminal)
        self.assertNotIn("valor_abertura", terminal["sessao_caixa"])

    def test_snapshot_distingue_terminal_sem_caixa_e_caixa_nao_encontrado(self):
        sem_caixa = configurar_terminal(self.hub, "BALCAO", "Balcao")
        caixa_inexistente = configurar_terminal(self.hub, "PDV-02", "PDV 02")
        caixa_inexistente.caixa_retaguarda_id = 999
        caixa_inexistente.save(update_fields=["caixa_retaguarda_id", "atualizado_em"])

        snapshot = montar_snapshot_operacional(self.hub)

        status_por_codigo = {terminal["codigo"]: terminal["caixa_status"] for terminal in snapshot["terminais"]}
        self.assertEqual(status_por_codigo["BALCAO"], "SEM_CAIXA")
        self.assertEqual(status_por_codigo["PDV-02"], "CAIXA_NAO_ENCONTRADO")
