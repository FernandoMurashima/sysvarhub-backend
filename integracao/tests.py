import io
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import HubConfig
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
