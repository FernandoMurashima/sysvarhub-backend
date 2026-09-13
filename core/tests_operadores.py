from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.urls import resolve
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from core.api import OperadorLoginView
from core.models import HubConfig, OperadorHub, SessaoOperadorHub, Terminal
from core.services.terminais import configurar_terminal
from integracao.services.operadores import (
    OperadoresValidationError,
    sincronizar_operadores,
)


def criar_hub(**overrides):
    dados = {
        "retaguarda_url": "http://central.test",
        "retaguarda_hub_id": 7,
        "empresa_id": 11,
        "loja_id": 41,
        "bootstrap_versao": 1,
        "retaguarda_token": "TOKEN-SECRETO",
    }
    dados.update(overrides)
    return HubConfig.objects.create(**dados)


def payload_operadores(hub, operadores=None, **overrides):
    dados = {
        "operadores_versao": 1,
        "gerado_em": timezone.now().isoformat(),
        "hub": {"id": hub.retaguarda_hub_id, "hub_uuid": str(hub.hub_uuid)},
        "empresa": {"id": hub.empresa_id},
        "loja": {"id": hub.loja_id},
        "operadores": operadores if operadores is not None else [operador_payload()],
    }
    dados.update(overrides)
    return dados


def operador_payload(**overrides):
    dados = {
        "usuario_id": 101,
        "codigo": "101",
        "nome": "Operador Caixa",
        "tipo": "OPERADOR",
        "perfil": {"id": 5, "nome": "Caixa"},
        "credencial_hash": make_password("1234"),
        "ativo": True,
    }
    dados.update(overrides)
    return dados


class OperadoresSincronizacaoTests(TestCase):
    def setUp(self):
        self.hub = criar_hub()

    def test_versao_1_aceita(self):
        resultado = sincronizar_operadores(self.hub, payload_operadores(self.hub))

        self.assertEqual(resultado["versao"], 1)

    def test_versao_desconhecida_rejeitada(self):
        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(
                self.hub,
                payload_operadores(self.hub, operadores_versao=99),
            )

    def test_hub_id_divergente_rejeitado(self):
        payload = payload_operadores(self.hub)
        payload["hub"]["id"] = 999

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload)

    def test_hub_uuid_divergente_rejeitado(self):
        payload = payload_operadores(self.hub)
        payload["hub"]["hub_uuid"] = "00000000-0000-0000-0000-000000000000"

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload)

    def test_empresa_divergente_rejeitada(self):
        payload = payload_operadores(self.hub)
        payload["empresa"]["id"] = 999

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload)

    def test_loja_divergente_rejeitada(self):
        payload = payload_operadores(self.hub)
        payload["loja"]["id"] = 999

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload)

    def test_lista_obrigatoria(self):
        payload = payload_operadores(self.hub)
        del payload["operadores"]

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload)

    def test_usuario_id_duplicado_rejeitado(self):
        operadores = [operador_payload(usuario_id=1), operador_payload(usuario_id=1, codigo="102")]

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload_operadores(self.hub, operadores))

    def test_codigo_duplicado_rejeitado(self):
        operadores = [operador_payload(usuario_id=1, codigo="101"), operador_payload(usuario_id=2, codigo="101")]

        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(self.hub, payload_operadores(self.hub, operadores))

    def test_hash_invalido_rejeitado(self):
        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(
                self.hub,
                payload_operadores(self.hub, [operador_payload(credencial_hash="invalido")]),
            )

    def test_snapshot_cria_operadorhub(self):
        sincronizar_operadores(self.hub, payload_operadores(self.hub))

        self.assertTrue(OperadorHub.objects.filter(hub=self.hub, codigo="101").exists())

    def test_segundo_snapshot_atualiza(self):
        sincronizar_operadores(self.hub, payload_operadores(self.hub))
        sincronizar_operadores(
            self.hub,
            payload_operadores(self.hub, [operador_payload(nome="Operador Atualizado")]),
        )

        self.assertEqual(OperadorHub.objects.get(codigo="101").nome, "Operador Atualizado")

    def test_ausente_e_inativado_e_tem_hash_limpo(self):
        sincronizar_operadores(self.hub, payload_operadores(self.hub))
        sincronizar_operadores(self.hub, payload_operadores(self.hub, []))

        operador = OperadorHub.objects.get(codigo="101")
        self.assertFalse(operador.ativo)
        self.assertEqual(operador.credencial_hash, "")

    def test_alteracao_de_hash_encerra_sessao(self):
        sincronizar_operadores(self.hub, payload_operadores(self.hub))
        operador = OperadorHub.objects.get(codigo="101")
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        sessao = SessaoOperadorHub.objects.create(
            terminal=terminal,
            operador=operador,
            token_hash=SessaoOperadorHub.hash_token("TOKEN"),
            ultima_atividade_em=timezone.now(),
        )

        sincronizar_operadores(
            self.hub,
            payload_operadores(self.hub, [operador_payload(credencial_hash=make_password("5678"))]),
        )

        sessao.refresh_from_db()
        self.assertFalse(sessao.ativa)

    def test_inativacao_encerra_sessao(self):
        sincronizar_operadores(self.hub, payload_operadores(self.hub))
        operador = OperadorHub.objects.get(codigo="101")
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        sessao = SessaoOperadorHub.objects.create(
            terminal=terminal,
            operador=operador,
            token_hash=SessaoOperadorHub.hash_token("TOKEN"),
            ultima_atividade_em=timezone.now(),
        )

        sincronizar_operadores(self.hub, payload_operadores(self.hub, [operador_payload(ativo=False)]))

        sessao.refresh_from_db()
        self.assertFalse(sessao.ativa)

    def test_transacao_nao_fica_parcial_em_erro(self):
        with self.assertRaises(OperadoresValidationError):
            sincronizar_operadores(
                self.hub,
                payload_operadores(self.hub, [operador_payload(), operador_payload(codigo="101")]),
            )

        self.assertEqual(OperadorHub.objects.count(), 0)


class OperadorApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub()
        self.outro_hub = criar_hub(retaguarda_url="http://central-2.test", retaguarda_hub_id=8, empresa_id=12, loja_id=42)
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        self.outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02")
        self.token_terminal = self.terminal.gerar_token()
        self.terminal.save()
        self.outro_token_terminal = self.outro_terminal.gerar_token()
        self.outro_terminal.save()
        self.operador = OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=101,
            codigo="101",
            nome="Operador Caixa",
            tipo="OPERADOR",
            perfil_retaguarda_id=5,
            perfil_nome="Caixa",
            credencial_hash=make_password("1234"),
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        OperadorHub.objects.create(
            hub=self.outro_hub,
            retaguarda_usuario_id=101,
            codigo="OUTRO",
            nome="Outro Hub",
            tipo="OPERADOR",
            credencial_hash=make_password("1234"),
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def autenticar_terminal(self, token=None):
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {token or self.token_terminal}")

    def login(self, codigo="101", senha="1234"):
        self.autenticar_terminal()
        return self.client.post("/api/terminal/operador/login/", {"codigo": codigo, "senha": senha}, format="json")

    def test_login_exige_terminal_valido(self):
        resposta = self.client.post("/api/terminal/operador/login/", {"codigo": "101", "senha": "1234"})

        self.assertIn(resposta.status_code, (401, 403))

    def test_operador_de_outro_hub_nao_autentica(self):
        resposta = self.login(codigo="OUTRO")

        self.assertEqual(resposta.status_code, 400)

    def test_operador_inativo_nao_autentica(self):
        self.operador.ativo = False
        self.operador.save()

        resposta = self.login()

        self.assertEqual(resposta.status_code, 400)

    def test_senha_correta_autentica(self):
        resposta = self.login()

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("sessao_token", resposta.data)

    def test_senha_errada_falha_com_mensagem_generica(self):
        resposta = self.login(senha="errada")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Operador ou credencial inválidos.")

    def test_mensagem_generica_para_inexistente_e_senha_errada(self):
        inexistente = self.login(codigo="999")
        senha_errada = self.login(senha="errada")

        self.assertEqual(inexistente.data["detail"], senha_errada.data["detail"])

    def test_login_nao_chama_retaguarda(self):
        with patch("integracao.services.retaguarda.RetaguardaClient.operadores") as operadores:
            resposta = self.login()

        self.assertEqual(resposta.status_code, 200)
        operadores.assert_not_called()

    def test_token_puro_nao_e_armazenado_e_hash_corresponde(self):
        resposta = self.login()
        token = resposta.data["sessao_token"]
        sessao = SessaoOperadorHub.objects.get()

        self.assertNotEqual(sessao.token_hash, token)
        self.assertEqual(sessao.token_hash, SessaoOperadorHub.hash_token(token))

    def test_novo_login_encerra_sessao_anterior_do_mesmo_terminal(self):
        primeira = self.login()
        segunda = self.login()

        primeira_sessao = SessaoOperadorHub.objects.get(token_hash=SessaoOperadorHub.hash_token(primeira.data["sessao_token"]))
        self.assertFalse(primeira_sessao.ativa)
        self.assertEqual(segunda.status_code, 200)

    def test_outro_terminal_pode_manter_sua_sessao(self):
        primeira = self.login()
        self.autenticar_terminal(self.outro_token_terminal)
        segunda = self.client.post("/api/terminal/operador/login/", {"codigo": "101", "senha": "1234"}, format="json")

        primeira_sessao = SessaoOperadorHub.objects.get(token_hash=SessaoOperadorHub.hash_token(primeira.data["sessao_token"]))
        self.assertTrue(primeira_sessao.ativa)
        self.assertEqual(segunda.status_code, 200)

    def test_contexto_exige_terminal_e_token_operador(self):
        self.autenticar_terminal()
        resposta = self.client.get("/api/terminal/operador/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_sessao_de_outro_terminal_e_recusada(self):
        resposta_login = self.login()
        self.autenticar_terminal(self.outro_token_terminal)
        resposta = self.client.get(
            "/api/terminal/operador/contexto/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        self.assertIn(resposta.status_code, (401, 403))

    def test_sessao_encerrada_e_recusada(self):
        resposta_login = self.login()
        SessaoOperadorHub.objects.update(ativa=False, encerrada_em=timezone.now())

        resposta = self.client.get(
            "/api/terminal/operador/contexto/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        self.assertIn(resposta.status_code, (401, 403))

    def test_operador_inativo_e_recusado_no_contexto(self):
        resposta_login = self.login()
        self.operador.ativo = False
        self.operador.save()

        resposta = self.client.get(
            "/api/terminal/operador/contexto/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        self.assertIn(resposta.status_code, (401, 403))

    def test_contexto_atualiza_ultima_atividade(self):
        resposta_login = self.login()
        sessao = SessaoOperadorHub.objects.get()
        anterior = sessao.ultima_atividade_em

        resposta = self.client.get(
            "/api/terminal/operador/contexto/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        sessao.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertGreaterEqual(sessao.ultima_atividade_em, anterior)

    def test_logout_encerra_sessao_e_nao_altera_terminal(self):
        resposta_login = self.login()
        terminal_uuid = self.terminal.terminal_uuid

        resposta = self.client.post(
            "/api/terminal/operador/logout/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        self.terminal.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.terminal.terminal_uuid, terminal_uuid)
        self.assertFalse(SessaoOperadorHub.objects.get().ativa)

    def test_respostas_nao_expoem_segredos(self):
        resposta_login = self.login()
        contexto = self.client.get(
            "/api/terminal/operador/contexto/",
            HTTP_X_SYSVAR_OPERADOR_SESSION=resposta_login.data["sessao_token"],
        )

        conteudo = f"{resposta_login.data}{contexto.data}"
        self.assertNotIn("credencial_hash", conteudo)
        self.assertNotIn("token_hash", conteudo)

    def test_login_possui_throttle_scope(self):
        view = resolve("/api/terminal/operador/login/").func.view_class

        self.assertIs(view, OperadorLoginView)
        self.assertIn(ScopedRateThrottle, view.throttle_classes)
        self.assertEqual(view.throttle_scope, "operador_login")
