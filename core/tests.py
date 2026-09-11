from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import resolve
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from core.models import CaixaHub, HubConfig, PareamentoTerminal, Terminal
from core.api import ParearTerminalView
from core.services.terminais import (
    PareamentoTerminalError,
    TerminalValidationError,
    configurar_terminal,
    desativar_terminal,
    gerar_pareamento_terminal,
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


class TerminalPareamentoApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
            empresa_nome="Sysvar Moda Comercio e Confeccoes Ltda",
            loja_nome="Loja Barra",
            loja_apelido="Barra",
            loja_estado="RJ",
            retaguarda_token="TOKEN-SECRETO",
            bootstrap_versao=1,
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

    def gerar_codigo(self, terminal=None):
        return gerar_pareamento_terminal(terminal or self.terminal)

    def parear(self, codigo, hostname="PDV-BARRA-01"):
        return self.client.post(
            "/api/terminal/parear/",
            {"codigo": codigo, "hostname": hostname},
            format="json",
            REMOTE_ADDR="10.0.0.10",
        )

    def autenticar_terminal(self):
        _pareamento, codigo = self.gerar_codigo()
        resposta = self.parear(codigo)
        token = resposta.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {token}")
        self.terminal.refresh_from_db()
        return token

    def test_codigo_de_pareamento_e_gerado(self):
        _pareamento, codigo = self.gerar_codigo()

        self.assertRegex(codigo, r"^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")

    def test_codigo_puro_nao_e_armazenado(self):
        pareamento, codigo = self.gerar_codigo()

        self.assertNotEqual(pareamento.codigo_hash, codigo)
        self.assertNotIn(codigo, pareamento.codigo_hash)

    def test_codigo_expira_em_15_minutos(self):
        pareamento, _codigo = self.gerar_codigo()

        delta = pareamento.expira_em - pareamento.criado_em
        self.assertGreater(delta.total_seconds(), 14 * 60)
        self.assertLessEqual(delta.total_seconds(), 15 * 60)

    def test_codigo_e_uso_unico(self):
        _pareamento, codigo = self.gerar_codigo()

        primeira = self.parear(codigo)
        segunda = self.parear(codigo)

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 400)

    def test_codigo_revogado_e_rejeitado(self):
        pareamento, codigo = self.gerar_codigo()
        pareamento.revogado_em = timezone.now()
        pareamento.save()

        resposta = self.parear(codigo)

        self.assertEqual(resposta.status_code, 400)

    def test_novo_codigo_revoga_anterior_nao_usado(self):
        primeiro, _codigo = self.gerar_codigo()

        self.gerar_codigo()

        primeiro.refresh_from_db()
        self.assertIsNotNone(primeiro.revogado_em)

    def test_terminal_inativo_nao_gera_pareamento(self):
        self.terminal.ativo = False
        self.terminal.save()

        with self.assertRaises(PareamentoTerminalError):
            self.gerar_codigo()

    def test_pareamento_valido_retorna_token(self):
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("token", resposta.data)

    def test_token_puro_nao_fica_no_banco(self):
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertNotEqual(self.terminal.token_hash, resposta.data["token"])
        self.assertNotIn(resposta.data["token"], self.terminal.token_hash)

    def test_pareamento_atualiza_pareado_em(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertIsNotNone(self.terminal.pareado_em)

    def test_pareamento_atualiza_hostname(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo, hostname="PDV-BARRA-99")
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.hostname, "PDV-BARRA-99")

    def test_pareamento_atualiza_ultimo_ip(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.ultimo_ip, "10.0.0.10")

    def test_pareamento_atualiza_ultima_conexao_em(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertIsNotNone(self.terminal.ultima_conexao_em)

    def test_codigo_nao_permite_escolher_outro_terminal(self):
        outro = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        outro.refresh_from_db()

        self.assertEqual(outro.token_hash, "")

    def test_token_valido_autentica_contexto(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.status_code, 200)

    def test_token_invalido_e_rejeitado(self):
        self.client.credentials(HTTP_AUTHORIZATION="Terminal invalido")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_terminal_inativo_com_token_valido_e_rejeitado(self):
        self.autenticar_terminal()
        self.terminal.ativo = False
        self.terminal.save()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_contexto_retorna_apenas_terminal_autenticado(self):
        configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["terminal"]["codigo"], "PDV-01")

    def test_contexto_retorna_caixa_correto(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["caixa"]["id"], 29)
        self.assertEqual(resposta.data["caixa"]["codigo"], "CX-BARRA")

    def test_contexto_sem_caixa_retorna_null(self):
        terminal_sem_caixa = configurar_terminal(self.hub, "BALCAO-01", "Balcao 01")
        _pareamento, codigo = self.gerar_codigo(terminal_sem_caixa)
        resposta_pareamento = self.parear(codigo)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {resposta_pareamento.data['token']}"
        )

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIsNone(resposta.data["caixa"])

    def test_contexto_retorna_loja_empresa_do_hub(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["loja"]["id"], 41)
        self.assertEqual(resposta.data["empresa"]["id"], 11)

    def test_heartbeat_atualiza_ultima_conexao_em(self):
        self.autenticar_terminal()
        anterior = self.terminal.ultima_conexao_em

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertGreaterEqual(self.terminal.ultima_conexao_em, anterior)

    def test_heartbeat_atualiza_ultimo_ip(self):
        self.autenticar_terminal()

        self.client.post(
            "/api/terminal/heartbeat/",
            {"hostname": "PDV-BARRA-01"},
            REMOTE_ADDR="10.0.0.11",
        )
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.ultimo_ip, "10.0.0.11")

    def test_heartbeat_nao_altera_terminal_uuid(self):
        self.autenticar_terminal()
        terminal_uuid = self.terminal.terminal_uuid

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.terminal_uuid, terminal_uuid)

    def test_heartbeat_nao_altera_caixa(self):
        self.autenticar_terminal()

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.caixa_retaguarda_id, 29)

    def test_repareamento_rotaciona_token(self):
        token_antigo = self.autenticar_terminal()
        self.client.credentials()
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)

        self.assertNotEqual(resposta.data["token"], token_antigo)

    def test_token_anterior_falha_apos_repareamento(self):
        token_antigo = self.autenticar_terminal()
        self.client.credentials()
        _pareamento, codigo = self.gerar_codigo()
        self.parear(codigo)
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {token_antigo}")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_nenhum_endpoint_retorna_retaguarda_token(self):
        _pareamento, codigo = self.gerar_codigo()
        resposta_pareamento = self.parear(codigo)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {resposta_pareamento.data['token']}"
        )
        contexto = self.client.get("/api/terminal/contexto/")
        heartbeat = self.client.post("/api/terminal/heartbeat/", {})

        conteudo = f"{resposta_pareamento.data}{contexto.data}{heartbeat.data}"
        self.assertNotIn("TOKEN-SECRETO", conteudo)
        self.assertNotIn("retaguarda_token", conteudo)

    def test_endpoint_de_pareamento_possui_throttle(self):
        view = resolve("/api/terminal/parear/").func.view_class

        self.assertIs(view, ParearTerminalView)
        self.assertIn(ScopedRateThrottle, view.throttle_classes)
        self.assertEqual(view.throttle_scope, "terminal_pareamento")

    def test_authorization_hub_nao_autentica_como_terminal(self):
        self.client.credentials(HTTP_AUTHORIZATION="Hub TOKEN-SECRETO")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))
