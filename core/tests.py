from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import CaixaHub, HubConfig, Terminal
from core.services.terminais import (
    TerminalValidationError,
    configurar_terminal,
    desativar_terminal,
)

class HubConfigTests(TestCase):
    def test_preserva_hub_uuid_ao_salvar(self):
        hub = HubConfig.objects.create(retaguarda_url="http://central.test")
        original_uuid = hub.hub_uuid

        hub.nome = "Sysvar Hub Loja 1"
        hub.save()

        hub.refresh_from_db()
        self.assertEqual(hub.hub_uuid, original_uuid)

    def test_str_nao_expoe_token(self):
        hub = HubConfig.objects.create(
            nome="Sysvar Hub",
            loja_id=1,
            retaguarda_url="http://central.test",
            retaguarda_token="TOKEN-SECRETO",
        )

        self.assertNotIn("TOKEN-SECRETO", str(hub))


class TerminalServiceTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def test_cria_terminal_sem_caixa(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")

        self.assertEqual(terminal.caixa_retaguarda_id, None)

    def test_cria_terminal_com_caixahub_ativo(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        self.assertEqual(terminal.caixa_retaguarda_id, 29)

    def test_rejeita_caixahub_inexistente(self):
        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 999)

    def test_rejeita_caixahub_inativo(self):
        self.caixa.ativo = False
        self.caixa.save()

        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

    def test_rejeita_caixahub_de_outro_hub(self):
        outro_hub = HubConfig.objects.create(retaguarda_url="http://central-2.test")
        CaixaHub.objects.create(
            hub=outro_hub,
            retaguarda_id=30,
            codigo="CX-OUTRA",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 30)

    def test_mesmo_hub_codigo_nao_duplica_terminal(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01")
        configurar_terminal(self.hub, "PDV-01", "PDV 01 atualizado")

        self.assertEqual(Terminal.objects.filter(hub=self.hub, codigo="PDV-01").count(), 1)

    def test_configuracao_existente_preserva_terminal_uuid(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        terminal_uuid = terminal.terminal_uuid

        atualizado = configurar_terminal(self.hub, "PDV-01", "PDV 01 atualizado")

        self.assertEqual(atualizado.terminal_uuid, terminal_uuid)

    def test_atualizacao_altera_nome_corretamente(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01")

        terminal = configurar_terminal(self.hub, "PDV-01", "PDV Principal")

        self.assertEqual(terminal.nome, "PDV Principal")

    def test_configuracao_existente_reativa_terminal_inativo(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        terminal.ativo = False
        terminal.save()

        atualizado = configurar_terminal(self.hub, "PDV-01", "PDV 01")

        self.assertTrue(atualizado.ativo)

    def test_caixa_pode_ser_alterado_posteriormente(self):
        outra_caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=31,
            codigo="CX-02",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        terminal = configurar_terminal(
            self.hub,
            "PDV-01",
            "PDV 01",
            outra_caixa.retaguarda_id,
        )

        self.assertEqual(terminal.caixa_retaguarda_id, 31)

    def test_desativacao_nao_deleta_terminal(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(Terminal.objects.filter(hub=self.hub, codigo="PDV-01").count(), 1)

    def test_desativacao_preserva_uuid(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativado = desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(desativado.terminal_uuid, terminal.terminal_uuid)

    def test_desativacao_preserva_caixa_retaguarda_id(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativado = desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(desativado.caixa_retaguarda_id, 29)

    def test_listagem_nao_altera_dados(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        antes = Terminal.objects.get(pk=terminal.pk).atualizado_em
        out = StringIO()

        call_command("listar_terminais", stdout=out)

        terminal.refresh_from_db()
        self.assertEqual(terminal.atualizado_em, antes)

    def test_comando_nao_exibe_retaguarda_token(self):
        self.hub.retaguarda_token = "TOKEN-SECRETO"
        self.hub.save()
        out = StringIO()

        call_command(
            "configurar_terminal",
            "--codigo",
            "PDV-01",
            "--nome",
            "PDV 01",
            stdout=out,
        )

        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_mais_de_um_hubconfig_gera_erro(self):
        HubConfig.objects.create(retaguarda_url="http://central-2.test")

        with self.assertRaises(CommandError):
            call_command(
                "configurar_terminal",
                "--codigo",
                "PDV-01",
                "--nome",
                "PDV 01",
                stdout=StringIO(),
            )

    def test_hub_inativo_impede_configuracao(self):
        self.hub.ativo = False
        self.hub.save()

        with self.assertRaises(CommandError):
            call_command(
                "configurar_terminal",
                "--codigo",
                "PDV-01",
                "--nome",
                "PDV 01",
                stdout=StringIO(),
            )

    def test_dois_terminais_diferentes_podem_usar_mesmo_caixahub(self):
        primeiro = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        segundo = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)

        self.assertEqual(primeiro.caixa_retaguarda_id, segundo.caixa_retaguarda_id)
