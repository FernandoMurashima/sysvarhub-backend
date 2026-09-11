import io
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import CaixaHub, HubConfig
from integracao.services.bootstrap import BootstrapValidationError, sincronizar_bootstrap
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RetaguardaClientTests(TestCase):
    def test_ativacao_envia_payload_esperado_sem_empresa_ou_loja(self):
        hub_uuid = uuid.uuid4()
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"ok": True})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test/", timeout=3).ativar_hub(
                codigo="AAAA-BBBB-CCCC",
                hub_uuid=hub_uuid,
                nome="Sysvar Hub",
                hostname="loja-01",
                versao="0.1.0",
            )

        self.assertEqual(captured["url"], "http://central.test/api/hub/ativar/")
        self.assertEqual(captured["timeout"], 3)
        self.assertEqual(
            captured["payload"],
            {
                "codigo": "AAAA-BBBB-CCCC",
                "hub_uuid": str(hub_uuid),
                "nome": "Sysvar Hub",
                "hostname": "loja-01",
                "versao": "0.1.0",
            },
        )
        self.assertNotIn("empresa_id", captured["payload"])
        self.assertNotIn("loja_id", captured["payload"])
        self.assertNotIn("token", captured["payload"])

    def test_heartbeat_usa_authorization_hub_token(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"ok": True})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").heartbeat(
                token="TOKEN-SECRETO",
                hostname="loja-01",
                versao="0.1.0",
            )

        self.assertEqual(captured["url"], "http://central.test/api/hub/heartbeat/")
        self.assertEqual(captured["payload"], {"hostname": "loja-01", "versao": "0.1.0"})
        self.assertEqual(captured["headers"]["Authorization"], "Hub TOKEN-SECRETO")

    def test_bootstrap_usa_get_authorization_hub_token_sem_payload(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"bootstrap_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").bootstrap(token="TOKEN-SECRETO")

        self.assertEqual(captured["url"], "http://central.test/api/hub/bootstrap/")
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["headers"]["Authorization"], "Hub TOKEN-SECRETO")
        self.assertNotIn("empresa_id", captured["url"])
        self.assertNotIn("loja_id", captured["url"])

    def test_indisponibilidade_gera_erro_controlado(self):
        with patch(
            "integracao.services.retaguarda.request.urlopen",
            side_effect=TimeoutError(),
        ):
            with self.assertRaises(RetaguardaError):
                RetaguardaClient("http://central.test").ativar_hub(
                    codigo="AAAA",
                    hub_uuid=uuid.uuid4(),
                    nome="Sysvar Hub",
                    hostname="loja-01",
                    versao="0.1.0",
                )


class AtivarHubCommandTests(TestCase):
    def test_ativacao_salva_dados_sem_imprimir_token(self):
        hub = HubConfig.objects.create(retaguarda_url="http://old.test", ativo=False)
        response = {
            "token": "TOKEN-SECRETO",
            "hub_uuid": str(hub.hub_uuid),
            "hub_id": 99,
            "loja_id": 7,
            "loja_nome": "Filial 1",
            "empresa_id": 3,
            "empresa_nome": "Empresa Teste",
        }
        out = io.StringIO()

        with patch(
            "integracao.management.commands.ativar_hub.RetaguardaClient.ativar_hub",
            return_value=response,
        ):
            call_command(
                "ativar_hub",
                "--codigo",
                "AAAA-BBBB-CCCC",
                "--url",
                "http://central.test",
                stdout=out,
            )

        hub.refresh_from_db()
        self.assertEqual(hub.retaguarda_token, "TOKEN-SECRETO")
        self.assertEqual(hub.retaguarda_hub_id, 99)
        self.assertEqual(hub.empresa_id, 3)
        self.assertEqual(hub.empresa_nome, "Empresa Teste")
        self.assertEqual(hub.loja_id, 7)
        self.assertEqual(hub.loja_nome, "Filial 1")
        self.assertIsNotNone(hub.ativado_em)
        self.assertTrue(hub.ativo)
        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_hubconfig_aceita_empresa_e_loja_vazias_antes_da_ativacao(self):
        hub = HubConfig.objects.create(retaguarda_url="http://central.test", ativo=False)

        self.assertIsNone(hub.empresa_id)
        self.assertIsNone(hub.loja_id)

    def test_reativacao_reutiliza_mesmo_hub_uuid(self):
        hub = HubConfig.objects.create(retaguarda_url="http://old.test", ativo=False)
        original_uuid = hub.hub_uuid
        captured = {}

        def fake_ativar(self, **payload):
            captured.update(payload)
            return {
                "token": "TOKEN-NOVO",
                "hub_uuid": str(original_uuid),
                "hub_id": 100,
                "loja_id": 8,
                "empresa_id": 4,
            }

        with patch(
            "integracao.management.commands.ativar_hub.RetaguardaClient.ativar_hub",
            fake_ativar,
        ):
            call_command(
                "ativar_hub",
                "--codigo",
                "ZZZZ",
                "--url",
                "http://central.test",
                stdout=io.StringIO(),
            )

        hub.refresh_from_db()
        self.assertEqual(hub.hub_uuid, original_uuid)
        self.assertEqual(captured["hub_uuid"], original_uuid)

    def test_hub_uuid_diferente_e_rejeitado(self):
        hub = HubConfig.objects.create(
            retaguarda_url="http://old.test",
            ativo=True,
            retaguarda_token="TOKEN-VALIDO",
        )
        response = {
            "token": "TOKEN-NOVO",
            "hub_uuid": str(uuid.uuid4()),
            "hub_id": 99,
            "loja_id": 7,
            "empresa_id": 3,
        }

        with patch(
            "integracao.management.commands.ativar_hub.RetaguardaClient.ativar_hub",
            return_value=response,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "ativar_hub",
                    "--codigo",
                    "AAAA",
                    "--url",
                    "http://central.test",
                    stdout=io.StringIO(),
                )

        hub.refresh_from_db()
        self.assertEqual(hub.retaguarda_token, "TOKEN-VALIDO")

    def test_resposta_incompleta_nao_deixa_configuracao_parcialmente_ativada(self):
        hub = HubConfig.objects.create(
            retaguarda_url="http://old.test",
            ativo=True,
            retaguarda_token="TOKEN-VALIDO",
            retaguarda_hub_id=10,
        )

        with patch(
            "integracao.management.commands.ativar_hub.RetaguardaClient.ativar_hub",
            return_value={"token": "TOKEN-NOVO", "hub_uuid": str(hub.hub_uuid)},
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "ativar_hub",
                    "--codigo",
                    "AAAA",
                    "--url",
                    "http://central.test",
                    stdout=io.StringIO(),
                )

        hub.refresh_from_db()
        self.assertEqual(hub.retaguarda_token, "TOKEN-VALIDO")
        self.assertEqual(hub.retaguarda_hub_id, 10)

    def test_mais_de_um_hubconfig_e_erro(self):
        HubConfig.objects.create(retaguarda_url="http://one.test")
        HubConfig.objects.create(retaguarda_url="http://two.test")

        with self.assertRaises(CommandError):
            call_command(
                "ativar_hub",
                "--codigo",
                "AAAA",
                "--url",
                "http://central.test",
                stdout=io.StringIO(),
            )


class HeartbeatHubCommandTests(TestCase):
    def test_heartbeat_sem_token_falha(self):
        HubConfig.objects.create(retaguarda_url="http://central.test", ativo=True)

        with self.assertRaises(CommandError):
            call_command("heartbeat_hub", stdout=io.StringIO())

    def test_heartbeat_atualiza_ultimo_heartbeat(self):
        hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
        )

        with patch(
            "integracao.management.commands.heartbeat_hub.RetaguardaClient.heartbeat",
            return_value={"ok": True},
        ):
            call_command("heartbeat_hub", stdout=io.StringIO())

        hub.refresh_from_db()
        self.assertIsNotNone(hub.ultimo_heartbeat_em)

    def test_heartbeat_indisponivel_nao_apaga_token(self):
        hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
        )

        with patch(
            "integracao.management.commands.heartbeat_hub.RetaguardaClient.heartbeat",
            side_effect=RetaguardaError("Não foi possível conectar à retaguarda."),
        ):
            with self.assertRaises(CommandError):
                call_command("heartbeat_hub", stdout=io.StringIO())

        hub.refresh_from_db()
        self.assertEqual(hub.retaguarda_token, "TOKEN-SECRETO")
        self.assertIsNone(hub.ultimo_heartbeat_em)


class BootstrapHubServiceTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
            retaguarda_hub_id=99,
            empresa_id=3,
            loja_id=7,
        )

    def resposta(self, **overrides):
        payload = {
            "bootstrap_versao": 1,
            "hub": {"id": 99, "hub_uuid": str(self.hub.hub_uuid), "versao": "0.1.0"},
            "empresa": {"id": 3, "nome": "Empresa Teste"},
            "loja": {
                "id": 7,
                "nome_loja": "Filial 1",
                "apelido_loja": "F1",
                "cnpj": "12345678000199",
                "estado": "SP",
            },
            "caixas": [
                {"id": 10, "codigo": "CX01", "descricao": "Caixa 1", "ativo": True},
            ],
        }
        payload.update(overrides)
        return payload

    def test_bootstrap_versao_1_e_aceito_e_atualiza_config(self):
        sincronizar_bootstrap(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.bootstrap_versao, 1)
        self.assertEqual(self.hub.empresa_nome, "Empresa Teste")
        self.assertEqual(self.hub.loja_nome, "Filial 1")
        self.assertEqual(self.hub.loja_apelido, "F1")
        self.assertEqual(self.hub.loja_cnpj, "12345678000199")
        self.assertEqual(self.hub.loja_estado, "SP")
        self.assertIsNotNone(self.hub.ultima_sincronizacao_em)

    def test_versao_nao_suportada_e_rejeitada(self):
        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, self.resposta(bootstrap_versao=2))

    def test_hub_uuid_divergente_e_rejeitado(self):
        resposta = self.resposta()
        resposta["hub"]["hub_uuid"] = str(uuid.uuid4())

        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, resposta)

    def test_hub_id_divergente_e_rejeitado(self):
        resposta = self.resposta()
        resposta["hub"]["id"] = 100

        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, resposta)

    def test_empresa_id_divergente_e_rejeitado(self):
        resposta = self.resposta()
        resposta["empresa"]["id"] = 4

        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, resposta)

    def test_loja_id_divergente_e_rejeitado(self):
        resposta = self.resposta()
        resposta["loja"]["id"] = 8

        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, resposta)

    def test_resposta_invalida_nao_altera_configuracao(self):
        self.hub.empresa_nome = "Empresa Antiga"
        self.hub.save(update_fields=["empresa_nome"])

        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, {"bootstrap_versao": 1})

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.empresa_nome, "Empresa Antiga")
        self.assertFalse(CaixaHub.objects.exists())

    def test_caixa_novo_e_criado(self):
        sincronizar_bootstrap(self.hub, self.resposta())

        caixa = CaixaHub.objects.get(hub=self.hub, retaguarda_id=10)
        self.assertEqual(caixa.codigo, "CX01")
        self.assertEqual(caixa.descricao, "Caixa 1")
        self.assertTrue(caixa.ativo)

    def test_caixa_existente_e_atualizado(self):
        CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=10,
            codigo="OLD",
            descricao="Antigo",
            ativo=False,
            sincronizado_em=timezone.now(),
        )

        sincronizar_bootstrap(self.hub, self.resposta())

        caixa = CaixaHub.objects.get(hub=self.hub, retaguarda_id=10)
        self.assertEqual(caixa.codigo, "CX01")
        self.assertEqual(caixa.descricao, "Caixa 1")
        self.assertTrue(caixa.ativo)

    def test_caixa_ausente_e_inativado_sem_deletar(self):
        caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=11,
            codigo="CX02",
            descricao="Caixa 2",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

        sincronizar_bootstrap(self.hub, self.resposta())

        caixa.refresh_from_db()
        self.assertFalse(caixa.ativo)

    def test_duas_sincronizacoes_nao_duplicam_caixas(self):
        sincronizar_bootstrap(self.hub, self.resposta())
        sincronizar_bootstrap(self.hub, self.resposta())

        self.assertEqual(CaixaHub.objects.filter(hub=self.hub, retaguarda_id=10).count(), 1)

    def test_ultima_sincronizacao_em_atualiza_somente_com_sucesso(self):
        with self.assertRaises(BootstrapValidationError):
            sincronizar_bootstrap(self.hub, self.resposta(bootstrap_versao=2))

        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.ultima_sincronizacao_em)


class SincronizarBootstrapHubCommandTests(TestCase):
    def test_token_nao_aparece_na_saida_do_command(self):
        hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
            retaguarda_hub_id=99,
            empresa_id=3,
            loja_id=7,
        )
        resposta = {
            "bootstrap_versao": 1,
            "hub": {"id": 99, "hub_uuid": str(hub.hub_uuid)},
            "empresa": {"id": 3, "nome": "Empresa Teste"},
            "loja": {"id": 7, "nome_loja": "Filial 1"},
            "caixas": [],
        }
        out = io.StringIO()

        with patch(
            "integracao.management.commands.sincronizar_bootstrap_hub.RetaguardaClient.bootstrap",
            return_value=resposta,
        ):
            call_command("sincronizar_bootstrap_hub", stdout=out)

        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_mais_de_um_hubconfig_gera_erro(self):
        HubConfig.objects.create(retaguarda_url="http://one.test")
        HubConfig.objects.create(retaguarda_url="http://two.test")

        with self.assertRaises(CommandError):
            call_command("sincronizar_bootstrap_hub", stdout=io.StringIO())
