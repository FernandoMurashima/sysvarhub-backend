import io
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import (
    CaixaHub,
    CatalogoItemHub,
    CashbackConfigHub,
    ClienteHub,
    ConfiguracaoFiscalHub,
    FormaPagamentoFiscalMapHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    HubConfig,
    ValeTrocaHub,
)
from integracao.services.bootstrap import BootstrapValidationError, sincronizar_bootstrap
from integracao.services.catalogo import CatalogoValidationError, sincronizar_catalogo
from integracao.services.formas_pagamento import (
    FormasPagamentoValidationError,
    sincronizar_formas_pagamento,
)
from integracao.services.clientes import ClientesValidationError, sincronizar_clientes
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


class _Headers:
    def __init__(self, content_type):
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type


class _BytesResponse:
    def __init__(self, payload, content_type="image/jpeg"):
        self.payload = payload
        self.headers = _Headers(content_type)
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        if self.offset >= len(self.payload):
            return b""
        if size is None or size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


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

    def test_sync_push_usa_endpoint_existente_e_authorization_hub_token(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"resultados": []})

        eventos = [{"evento_uuid": str(uuid.uuid4()), "chave_idempotencia": "x", "tipo": "VENDA_FINALIZADA", "payload": {}}]
        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").sync_push(token="TOKEN-SECRETO", eventos=eventos)

        self.assertEqual(captured["url"], "http://central.test/api/hub/sync/push/")
        self.assertEqual(captured["payload"], {"versao": 1, "eventos": eventos})
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

    def test_catalogo_chama_get_com_authorization_hub(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"catalogo_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").catalogo(token="TOKEN-SECRETO")

        self.assertEqual(captured["url"], "http://central.test/api/hub/catalogo/")
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["headers"]["Authorization"], "Hub TOKEN-SECRETO")

    def test_catalogo_sem_token_nao_expoe_token_em_erro(self):
        with self.assertRaises(RetaguardaError) as ctx:
            RetaguardaClient("http://central.test").catalogo(token="")

        self.assertNotIn("TOKEN-SECRETO", str(ctx.exception))

    def test_formas_pagamento_chama_get_com_authorization_hub(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"formas_pagamento_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").formas_pagamento(token="TOKEN-SECRETO")

        self.assertEqual(captured["url"], "http://central.test/api/hub/formas-pagamento/")
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["headers"]["Authorization"], "Hub TOKEN-SECRETO")

    def test_formas_pagamento_exige_token(self):
        with self.assertRaises(RetaguardaError) as ctx:
            RetaguardaClient("http://central.test").formas_pagamento(token="")

        self.assertNotIn("TOKEN-SECRETO", str(ctx.exception))

    def test_clientes_chama_get_com_authorization_hub(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            return _JsonResponse({"clientes_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", fake_urlopen):
            RetaguardaClient("http://central.test").clientes(token="TOKEN-SECRETO")

        self.assertEqual(captured["url"], "http://central.test/api/hub/clientes/")
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["headers"]["Authorization"], "Hub TOKEN-SECRETO")

    def test_clientes_exige_token(self):
        with self.assertRaises(RetaguardaError) as ctx:
            RetaguardaClient("http://central.test").clientes(token="")

        self.assertNotIn("TOKEN-SECRETO", str(ctx.exception))

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

    def test_baixar_catalogo_imagem_salva_extensao_por_content_type(self):
        casos = [
            ("image/jpeg", ".jpg"),
            ("image/png", ".png"),
            ("image/webp", ".webp"),
            ("image/gif", ".gif"),
        ]
        for content_type, extensao in casos:
            with self.subTest(content_type=content_type), tempfile.TemporaryDirectory() as tmp:
                destino_base = Path(tmp) / "foto"

                with patch(
                    "integracao.services.retaguarda.request.urlopen",
                    return_value=_BytesResponse(b"foto", content_type),
                ):
                    destino = RetaguardaClient("http://central.test").baixar_catalogo_imagem(
                        token="TOKEN-SECRETO",
                        imagem_id=55,
                        destino=destino_base,
                    )

                self.assertEqual(destino.suffix, extensao)
                self.assertEqual(destino.read_bytes(), b"foto")
                self.assertFalse(destino_base.exists())

    def test_baixar_catalogo_imagem_rejeita_content_type_invalido_sem_parcial(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino_base = Path(tmp) / "foto"

            with patch(
                "integracao.services.retaguarda.request.urlopen",
                return_value=_BytesResponse(b"foto", "application/octet-stream"),
            ):
                with self.assertRaises(RetaguardaError):
                    RetaguardaClient("http://central.test").baixar_catalogo_imagem(
                        token="TOKEN-SECRETO",
                        imagem_id=55,
                        destino=destino_base,
                    )

            self.assertEqual(list(Path(tmp).glob("*")), [])


class ClientesSyncTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            retaguarda_hub_id=7,
            empresa_id=11,
            loja_id=41,
        )

    def cliente(self, **overrides):
        dados = {
            "id": 123,
            "tipo_pessoa": "PF",
            "documento": "123.456.789-01",
            "cliente_padrao": False,
            "nome_cliente": "Cliente Teste",
            "apelido": "",
            "endereco": "",
            "numero": "",
            "complemento": "",
            "cep": "",
            "bairro": "",
            "cidade": "Rio de Janeiro",
            "estado": "RJ",
            "telefone1": "21999990000",
            "telefone2": "",
            "email": "cliente@example.com",
            "categoria": "",
            "bloqueio": False,
            "motivo_bloqueio": None,
            "aniversario": None,
            "mala_direta": False,
            "aceita_email": False,
            "aceita_whatsapp": False,
            "aceita_sms": False,
            "consentimento_em": None,
            "origem_consentimento": "",
            "ativo": True,
        }
        dados.update(overrides)
        return dados

    def resposta(self, **overrides):
        dados = {
            "clientes_versao": 1,
            "gerado_em": "2026-09-14T10:00:00-03:00",
            "hub": {"id": 7, "hub_uuid": str(self.hub.hub_uuid)},
            "empresa": {"id": 11},
            "loja": {"id": 41},
            "clientes": [self.cliente()],
        }
        dados.update(overrides)
        return dados

    def assert_rejeita(self, resposta):
        with self.assertRaises(ClientesValidationError):
            sincronizar_clientes(self.hub, resposta)

    def test_valida_identidade(self):
        self.assert_rejeita(self.resposta(empresa={"id": 99}))

    def test_rejeita_versao_nao_suportada(self):
        self.assert_rejeita(self.resposta(clientes_versao=2))

    def test_rejeita_payload_invalido(self):
        self.assert_rejeita(self.resposta(clientes=[self.cliente(nome_cliente="")]))

    def test_rejeita_campo_obrigatorio_nulo(self):
        with self.assertRaises(ClientesValidationError) as ctx:
            sincronizar_clientes(self.hub, self.resposta(clientes=[self.cliente(nome_cliente=None)]))

        self.assertIn("campo obrigatório nulo: nome_cliente", str(ctx.exception))

    def test_rejeita_id_duplicado(self):
        self.assert_rejeita(self.resposta(clientes=[self.cliente(id=1), self.cliente(id=1, documento="12345678902")]))

    def test_rejeita_documento_duplicado(self):
        self.assert_rejeita(self.resposta(clientes=[self.cliente(id=1), self.cliente(id=2)]))

    def test_cria_clientes_e_normaliza_documento(self):
        resultado = sincronizar_clientes(self.hub, self.resposta())

        cliente = ClienteHub.objects.get(hub=self.hub, retaguarda_id=123)
        self.assertEqual(cliente.documento, "12345678901")
        self.assertTrue(cliente.presente_retaguarda)
        self.assertEqual(cliente.origem, ClienteHub.ORIGEM_RETAGUARDA)
        self.assertEqual(resultado["clientes_recebidos"], 1)

    def test_aceita_nulos_em_campos_opcionais_do_contrato(self):
        payload = self.cliente(
            documento=None,
            apelido=None,
            endereco=None,
            numero=None,
            complemento=None,
            cep=None,
            bairro=None,
            cidade=None,
            estado=None,
            telefone1=None,
            telefone2=None,
            email=None,
            categoria=None,
            motivo_bloqueio=None,
            aniversario=None,
            consentimento_em=None,
            origem_consentimento=None,
        )

        sincronizar_clientes(self.hub, self.resposta(clientes=[payload]))

        cliente = ClienteHub.objects.get(hub=self.hub, retaguarda_id=123)
        self.assertIsNone(cliente.documento)
        self.assertEqual(cliente.apelido, "")
        self.assertEqual(cliente.endereco, "")
        self.assertEqual(cliente.numero, "")
        self.assertEqual(cliente.complemento, "")
        self.assertEqual(cliente.cep, "")
        self.assertEqual(cliente.bairro, "")
        self.assertEqual(cliente.cidade, "")
        self.assertEqual(cliente.estado, "")
        self.assertEqual(cliente.telefone1, "")
        self.assertEqual(cliente.telefone2, "")
        self.assertEqual(cliente.email, "")
        self.assertEqual(cliente.categoria, "")
        self.assertIsNone(cliente.motivo_bloqueio)
        self.assertIsNone(cliente.aniversario)
        self.assertIsNone(cliente.consentimento_em)
        self.assertEqual(cliente.origem_consentimento, "")

    def test_atualiza_cliente_existente(self):
        sincronizar_clientes(self.hub, self.resposta())
        sincronizar_clientes(self.hub, self.resposta(clientes=[self.cliente(nome_cliente="Cliente Atualizado")]))

        self.assertEqual(ClienteHub.objects.get(retaguarda_id=123).nome_cliente, "Cliente Atualizado")

    def test_preserva_cliente_padrao_ativo_inativo_e_bloqueado(self):
        sincronizar_clientes(
            self.hub,
            self.resposta(clientes=[self.cliente(cliente_padrao=True, ativo=False, bloqueio=True, motivo_bloqueio="Bloqueado")]),
        )

        cliente = ClienteHub.objects.get(retaguarda_id=123)
        self.assertTrue(cliente.cliente_padrao)
        self.assertFalse(cliente.ativo)
        self.assertTrue(cliente.bloqueio)

    def test_ausencia_marca_presente_retaguarda_false(self):
        sincronizar_clientes(self.hub, self.resposta())
        resultado = sincronizar_clientes(self.hub, self.resposta(clientes=[]))

        self.assertFalse(ClienteHub.objects.get(retaguarda_id=123).presente_retaguarda)
        self.assertEqual(resultado["clientes_ausentes_marcados"], 1)

    def test_local_sem_retaguarda_id_nao_e_afetado(self):
        local = ClienteHub.objects.create(
            hub=self.hub,
            origem=ClienteHub.ORIGEM_LOCAL,
            retaguarda_id=None,
            tipo_pessoa="PF",
            documento="12345678901",
            nome_cliente="Cliente Local",
            sincronizado_em=timezone.now(),
        )
        sincronizar_clientes(self.hub, self.resposta(clientes=[]))

        local.refresh_from_db()
        self.assertIsNone(local.retaguarda_id)
        self.assertEqual(local.origem, ClienteHub.ORIGEM_LOCAL)

    def test_reconcilia_cliente_local_por_documento(self):
        local = ClienteHub.objects.create(
            hub=self.hub,
            origem=ClienteHub.ORIGEM_LOCAL,
            retaguarda_id=None,
            tipo_pessoa="PF",
            documento="12345678901",
            nome_cliente="Cliente Local",
            sincronizado_em=timezone.now(),
        )
        resultado = sincronizar_clientes(self.hub, self.resposta())

        local.refresh_from_db()
        self.assertEqual(local.retaguarda_id, 123)
        self.assertEqual(local.origem, ClienteHub.ORIGEM_LOCAL)
        self.assertEqual(resultado["clientes_reconciliados_por_documento"], 1)
        self.assertEqual(ClienteHub.objects.count(), 1)

    def test_reconcilia_vale_local_por_documento_sem_duplicar(self):
        cliente_local = ClienteHub.objects.create(
            hub=self.hub,
            origem=ClienteHub.ORIGEM_LOCAL,
            retaguarda_id=None,
            tipo_pessoa="PF",
            documento="12345678901",
            nome_cliente="Cliente Local",
            sincronizado_em=timezone.now(),
        )
        vale = ValeTrocaHub.objects.create(
            hub=self.hub,
            cliente_uuid=cliente_local.cliente_uuid,
            documento="VT-100",
            valor_original="50.00",
            saldo="50.00",
        )

        sincronizar_clientes(
            self.hub,
            self.resposta(clientes=[
                self.cliente(
                    vales_troca=[
                        {
                            "id": 900,
                            "documento": "VT-100",
                            "valor_original": "50.00",
                            "saldo": "50.00",
                            "status": "ABERTO",
                        }
                    ]
                )
            ]),
        )

        vale.refresh_from_db()
        self.assertEqual(ValeTrocaHub.objects.count(), 1)
        self.assertEqual(vale.retaguarda_id, 900)
        self.assertIsNotNone(vale.sincronizado_em)

    def test_sincroniza_saldo_cashback_retaguarda_cliente(self):
        sincronizar_clientes(self.hub, self.resposta(clientes=[self.cliente(cashback_saldo_retaguarda="123.45")]))

        cliente = ClienteHub.objects.get(hub=self.hub, retaguarda_id=123)
        self.assertEqual(cliente.cashback_saldo_retaguarda, Decimal("123.45"))

    def test_atomicidade_em_erro_e_timestamps_apenas_apos_sucesso(self):
        ClienteHub.objects.create(
            hub=self.hub,
            retaguarda_id=123,
            tipo_pessoa="PF",
            documento="12345678901",
            nome_cliente="Cliente Original",
            sincronizado_em=timezone.now(),
        )

        self.assert_rejeita(self.resposta(clientes=[self.cliente(nome_cliente="Cliente Novo"), self.cliente(id=456, documento="bad")]))

        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.clientes_versao)
        self.assertEqual(ClienteHub.objects.get(retaguarda_id=123).nome_cliente, "Cliente Original")

    def test_comando_sincroniza_sem_imprimir_dados_pessoais(self):
        self.hub.retaguarda_token = "TOKEN-SECRETO"
        self.hub.bootstrap_versao = 1
        self.hub.save()
        out = io.StringIO()

        with patch(
            "integracao.management.commands.sincronizar_clientes_hub.RetaguardaClient.clientes",
            return_value=self.resposta(),
        ):
            call_command("sincronizar_clientes_hub", stdout=out)

        texto = out.getvalue()
        self.assertIn("Clientes recebidos: 1", texto)
        self.assertNotIn("TOKEN-SECRETO", texto)
        self.assertNotIn("12345678901", texto)


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

    def fiscal(self, **overrides):
        payload = {
            "emite_nfce": True,
            "ambiente_fiscal": "HOMOLOGACAO",
            "regime_tributario": "SIMPLES",
            "inscricao_estadual": "110042490114",
            "serie_nfce": 7,
            "proximo_numero_nfce": 10,
            "razao_social": "Empresa Teste Ltda",
            "nome_fantasia": "Empresa Teste",
            "cnpj": "12345678000199",
            "logradouro": "Rua",
            "endereco": "Rua Teste",
            "numero": "123",
            "complemento": "",
            "bairro": "Centro",
            "cidade": "Sao Paulo",
            "estado": "SP",
            "uf": "SP",
            "cep": "01001000",
            "codigo_municipio_ibge": "3550308",
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

    def test_fiscal_presente_persiste_snapshot(self):
        resposta = self.resposta()
        resposta["loja"]["fiscal"] = self.fiscal()

        resultado = sincronizar_bootstrap(self.hub, resposta)

        fiscal = ConfiguracaoFiscalHub.objects.get(hub=self.hub)
        self.assertTrue(resultado["configuracao_fiscal_atualizada"])
        self.assertTrue(fiscal.emite_nfce)
        self.assertEqual(fiscal.serie_nfce, 7)
        self.assertEqual(fiscal.proximo_numero_nfce, 10)
        self.assertEqual(fiscal.codigo_municipio_ibge, "3550308")

    def test_fiscal_nfce_desligada_aceita_codigo_municipio_ibge_vazio(self):
        resposta = self.resposta()
        resposta["loja"]["fiscal"] = self.fiscal(emite_nfce=False, codigo_municipio_ibge="")

        sincronizar_bootstrap(self.hub, resposta)

        fiscal = ConfiguracaoFiscalHub.objects.get(hub=self.hub)
        self.assertFalse(fiscal.emite_nfce)
        self.assertEqual(fiscal.codigo_municipio_ibge, "")

    def test_fiscal_nfce_ligada_rejeita_codigo_municipio_ibge_ausente_ou_invalido(self):
        casos = (
            ("", "Bootstrap retornou fiscal.codigo_municipio_ibge ausente para NFC-e."),
            (None, "Bootstrap retornou fiscal.codigo_municipio_ibge ausente para NFC-e."),
            ("355030", "Bootstrap retornou fiscal.codigo_municipio_ibge inválido."),
            ("35503088", "Bootstrap retornou fiscal.codigo_municipio_ibge inválido."),
            ("355A308", "Bootstrap retornou fiscal.codigo_municipio_ibge inválido."),
        )
        for codigo, mensagem in casos:
            resposta = self.resposta()
            resposta["loja"]["fiscal"] = self.fiscal(codigo_municipio_ibge=codigo)

            with self.assertRaises(BootstrapValidationError) as ctx:
                sincronizar_bootstrap(self.hub, resposta)
            self.assertEqual(str(ctx.exception), mensagem)

    def test_bootstrap_sem_fiscal_mantem_config_anterior(self):
        ConfiguracaoFiscalHub.objects.create(
            hub=self.hub,
            emite_nfce=True,
            ambiente_fiscal="HOMOLOGACAO",
            regime_tributario="SIMPLES",
            serie_nfce=1,
            proximo_numero_nfce=50,
            razao_social="Anterior",
            cnpj="12345678000199",
            sincronizado_em=timezone.now(),
        )

        resultado = sincronizar_bootstrap(self.hub, self.resposta())

        fiscal = ConfiguracaoFiscalHub.objects.get(hub=self.hub)
        self.assertFalse(resultado["configuracao_fiscal_atualizada"])
        self.assertEqual(fiscal.proximo_numero_nfce, 50)

    def test_fiscal_nao_retrocede_numero_e_mudanca_serie_usa_seed(self):
        resposta = self.resposta()
        resposta["loja"]["fiscal"] = self.fiscal(proximo_numero_nfce=20)
        sincronizar_bootstrap(self.hub, resposta)
        ConfiguracaoFiscalHub.objects.filter(hub=self.hub).update(proximo_numero_nfce=25)
        resposta["loja"]["fiscal"] = self.fiscal(proximo_numero_nfce=12)
        sincronizar_bootstrap(self.hub, resposta)
        self.assertEqual(ConfiguracaoFiscalHub.objects.get(hub=self.hub).proximo_numero_nfce, 25)

        resposta["loja"]["fiscal"] = self.fiscal(serie_nfce=8, proximo_numero_nfce=3)
        sincronizar_bootstrap(self.hub, resposta)
        fiscal = ConfiguracaoFiscalHub.objects.get(hub=self.hub)
        self.assertEqual(fiscal.serie_nfce, 8)
        self.assertEqual(fiscal.proximo_numero_nfce, 3)

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

    def test_cashback_config_recebida_desativa_config_antiga(self):
        CashbackConfigHub.objects.create(
            hub=self.hub,
            retaguarda_id=1,
            nome="Antiga",
            ativo=True,
            percentual=Decimal("1.0000"),
            sincronizado_em=timezone.now(),
        )

        sincronizar_bootstrap(
            self.hub,
            self.resposta(
                cashback_config={
                    "retaguarda_id": 2,
                    "nome": "Nova",
                    "ativo": True,
                    "percentual": "5.0000",
                    "validade_dias": 30,
                    "valor_minimo_geracao": "0.00",
                    "valor_minimo_uso": "0.00",
                    "limite_uso_percentual": "100.0000",
                    "consumidor_final_participa": False,
                }
            ),
        )

        self.assertFalse(CashbackConfigHub.objects.get(hub=self.hub, retaguarda_id=1).ativo)
        self.assertTrue(CashbackConfigHub.objects.get(hub=self.hub, retaguarda_id=2).ativo)

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


class CatalogoHubServiceTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
            retaguarda_hub_id=99,
            empresa_id=3,
            loja_id=7,
            bootstrap_versao=1,
        )

    def item(self, **overrides):
        payload = {
            "produto_id": 10,
            "sku_id": 100,
            "tipo_produto": "SIMPLES",
            "referencia": "REF-10",
            "descricao": "Produto Teste",
            "descricao_reduzida": "Produto",
            "ean13": "7890000000001",
            "codigo_item_ref": "REF-10-A",
            "cor": {"id": 1, "descricao": "Azul"},
            "tamanho": {"id": 2, "descricao": "M"},
            "unidade": {"id": 3, "codigo": "UN", "descricao": "Unidade"},
            "preco": "19.9000",
            "preco_promocional": None,
            "preco_venda": "19.9000",
            "estoque_fisico": "5.000",
            "reserva": "1.000",
            "estoque_disponivel": "4.000",
            "vendavel": True,
            "motivos_bloqueio": [],
            "fiscal": {"ncm": "61091000"},
        }
        payload.update(overrides)
        return payload

    def resposta(self, itens=None, **overrides):
        if itens is None:
            itens = [self.item()]
        payload = {
            "catalogo_versao": 1,
            "gerado_em": "2026-09-12T10:00:00-03:00",
            "hub": {"id": 99, "hub_uuid": str(self.hub.hub_uuid)},
            "empresa": {"id": 3, "nome": "Empresa Teste"},
            "loja": {"id": 7, "nome": "Filial 1", "apelido": "F1"},
            "tabela_preco": {"id": 5, "codigo": "PADRAO", "nome": "Tabela Padrão"},
            "total_itens": len(itens),
            "itens": itens,
        }
        payload.update(overrides)
        return payload

    def assert_rejeita(self, resposta):
        with self.assertRaises(CatalogoValidationError):
            sincronizar_catalogo(self.hub, resposta)

    def test_versao_1_e_aceita(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.catalogo_versao, 1)

    def test_versao_desconhecida_rejeita(self):
        self.assert_rejeita(self.resposta(catalogo_versao=3))

    def test_versao_2_com_imagem_persiste_metadados_e_baixa_foto(self):
        class FakeClient:
            chamadas = []

            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                self.chamadas.append((token, imagem_id, destino))
                final = destino.with_suffix(".jpg")
                final.parent.mkdir(parents=True, exist_ok=True)
                final.write_bytes(b"foto")
                return final

        item = self.item(imagem={"id": 55, "versao": "2026-09-19T10:00:00-03:00", "tipo": "reduzida"})
        with tempfile.TemporaryDirectory() as tmp, patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            client = FakeClient()
            sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=client)

            catalogo = CatalogoItemHub.objects.get()
            self.assertEqual(catalogo.imagem_retaguarda_id, 55)
            self.assertEqual(catalogo.imagem_versao, "2026-09-19T10:00:00-03:00")
            self.assertEqual(catalogo.imagem_tipo, "reduzida")
            self.assertTrue((Path(tmp) / catalogo.imagem_local).is_file())
            self.assertTrue(catalogo.imagem_local.endswith(".jpg"))
            self.assertEqual(len(client.chamadas), 1)

    def test_mesma_imagem_mesma_versao_nao_baixa_novamente(self):
        class FakeClient:
            chamadas = 0

            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                self.chamadas += 1
                final = destino.with_suffix(".png")
                final.parent.mkdir(parents=True, exist_ok=True)
                final.write_bytes(b"foto")
                return final

        item = self.item(imagem={"id": 55, "versao": "v1", "tipo": "original"})
        with tempfile.TemporaryDirectory() as tmp, patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            client = FakeClient()
            sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=client)
            sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=client)

            self.assertEqual(client.chamadas, 1)

    def test_erro_preparar_diretorio_nao_cancela_catalogo_comercial(self):
        class FakeClient:
            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                raise AssertionError("download nao deveria ser chamado")

        item = self.item(imagem={"id": 55, "versao": "v1", "tipo": "original"})
        with patch("integracao.services.catalogo._resolver_imagem_local", side_effect=OSError("mkdir falhou")):
            sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=FakeClient())

        catalogo = CatalogoItemHub.objects.get()
        self.assertEqual(catalogo.descricao, "Produto Teste")
        self.assertEqual(catalogo.imagem_local, "")

    def test_erro_escrita_nao_cancela_catalogo_comercial_e_nao_associa(self):
        class FakeClient:
            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                raise OSError("write falhou")

        item = self.item(imagem={"id": 55, "versao": "v1", "tipo": "original"})
        sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=FakeClient())

        catalogo = CatalogoItemHub.objects.get()
        self.assertEqual(catalogo.descricao, "Produto Teste")
        self.assertEqual(catalogo.imagem_local, "")

    def test_erro_replace_nao_cancela_catalogo_comercial_e_nao_deixa_parcial(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self.item(imagem={"id": 55, "versao": "v1", "tipo": "original"})
            with patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
                with patch("integracao.services.retaguarda.request.urlopen", return_value=_BytesResponse(b"foto", "image/jpeg")):
                    with patch("integracao.services.retaguarda.os.replace", side_effect=OSError("replace falhou")):
                        sincronizar_catalogo(
                            self.hub,
                            self.resposta([item], catalogo_versao=2),
                            client=RetaguardaClient("http://central.test"),
                        )

                catalogo = CatalogoItemHub.objects.get()
                self.assertEqual(catalogo.imagem_local, "")
                self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_content_type_invalido_nao_associa_foto(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = self.item(imagem={"id": 55, "versao": "v1", "tipo": "original"})
            with patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
                with patch(
                    "integracao.services.retaguarda.request.urlopen",
                    return_value=_BytesResponse(b"foto", "application/octet-stream"),
                ):
                    sincronizar_catalogo(
                        self.hub,
                        self.resposta([item], catalogo_versao=2),
                        client=RetaguardaClient("http://central.test"),
                    )

                catalogo = CatalogoItemHub.objects.get()
                self.assertEqual(catalogo.imagem_local, "")
                self.assertEqual(list(Path(tmp).rglob("*.*")), [])

    def test_erro_limpeza_orfao_nao_cancela_catalogo_comercial(self):
        class FakeClient:
            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                return None

        item = self.item(imagem=None)
        with tempfile.TemporaryDirectory() as tmp:
            orfao = Path(tmp) / "catalogo-imagens/produto-10/orfao.jpg"
            orfao.parent.mkdir(parents=True)
            orfao.write_bytes(b"orfao")
            with patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
                with patch("integracao.services.catalogo.Path.unlink", side_effect=OSError("unlink falhou")):
                    sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=FakeClient())

        self.assertEqual(CatalogoItemHub.objects.get().descricao, "Produto Teste")

    def test_falha_download_nao_cancela_catalogo_e_nao_mantem_foto_antiga(self):
        class FakeClient:
            def baixar_catalogo_imagem(self, *, token, imagem_id, destino):
                raise RetaguardaError("falha controlada")

        antigo = CatalogoItemHub.objects.create(
            hub=self.hub,
            retaguarda_produto_id=10,
            retaguarda_sku_id=100,
            tipo_produto="SIMPLES",
            referencia="REF-10",
            descricao="Produto antigo",
            sincronizado_em=timezone.now(),
            imagem_retaguarda_id=55,
            imagem_versao="v1",
            imagem_tipo="original",
            imagem_local="catalogo-imagens/produto-10/antiga.bin",
        )
        item = self.item(imagem={"id": 55, "versao": "v2", "tipo": "original"})
        with tempfile.TemporaryDirectory() as tmp, patch("integracao.services.catalogo.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            sincronizar_catalogo(self.hub, self.resposta([item], catalogo_versao=2), client=FakeClient())

            antigo.refresh_from_db()
            self.assertEqual(antigo.descricao, "Produto Teste")
            self.assertEqual(antigo.imagem_versao, "v2")
            self.assertEqual(antigo.imagem_local, "")

    def test_imagem_null_limpa_associacao(self):
        CatalogoItemHub.objects.create(
            hub=self.hub,
            retaguarda_produto_id=10,
            retaguarda_sku_id=100,
            tipo_produto="SIMPLES",
            referencia="REF-10",
            descricao="Produto antigo",
            sincronizado_em=timezone.now(),
            imagem_retaguarda_id=55,
            imagem_versao="v1",
            imagem_tipo="original",
            imagem_local="catalogo-imagens/produto-10/antiga.bin",
        )

        sincronizar_catalogo(self.hub, self.resposta([self.item(imagem=None)], catalogo_versao=2))

        item = CatalogoItemHub.objects.get()
        self.assertIsNone(item.imagem_retaguarda_id)
        self.assertEqual(item.imagem_local, "")

    def test_hub_id_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["hub"]["id"] = 100
        self.assert_rejeita(resposta)

    def test_hub_uuid_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["hub"]["hub_uuid"] = str(uuid.uuid4())
        self.assert_rejeita(resposta)

    def test_empresa_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["empresa"]["id"] = 4
        self.assert_rejeita(resposta)

    def test_loja_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["loja"]["id"] = 8
        self.assert_rejeita(resposta)

    def test_total_itens_divergente_rejeita(self):
        self.assert_rejeita(self.resposta(total_itens=2))

    def test_itens_precisa_ser_lista(self):
        self.assert_rejeita(self.resposta(itens={}, total_itens=1))

    def test_item_precisa_ser_dict(self):
        self.assert_rejeita(self.resposta(itens=["invalido"]))

    def test_sku_id_obrigatorio(self):
        item = self.item()
        del item["sku_id"]
        self.assert_rejeita(self.resposta([item]))

    def test_produto_id_obrigatorio(self):
        item = self.item()
        del item["produto_id"]
        self.assert_rejeita(self.resposta([item]))

    def test_sku_sem_ean_e_aceito(self):
        sincronizar_catalogo(self.hub, self.resposta([self.item(ean13=None)]))

        self.assertEqual(CatalogoItemHub.objects.get().ean13, "")

    def test_decimal_string_e_convertido(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.assertEqual(str(CatalogoItemHub.objects.get().preco_venda), "19.9000")

    def test_decimal_invalido_rejeita_snapshot(self):
        self.assert_rejeita(self.resposta([self.item(estoque_fisico="abc")]))

    def test_preco_null_e_aceito_quando_nao_vendavel(self):
        item = self.item(
            preco=None,
            preco_venda=None,
            estoque_disponivel="4.000",
            vendavel=False,
            motivos_bloqueio=["SEM_PRECO"],
        )
        sincronizar_catalogo(self.hub, self.resposta([item]))

        self.assertIsNone(CatalogoItemHub.objects.get().preco_venda)

    def test_tabela_padrao_e_persistida(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.tabela_preco_retaguarda_id, 5)
        self.assertEqual(self.hub.tabela_preco_codigo, "PADRAO")
        self.assertEqual(self.hub.tabela_preco_nome, "Tabela Padrão")

    def test_tabela_null_e_aceita(self):
        sincronizar_catalogo(self.hub, self.resposta(tabela_preco=None))

        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.tabela_preco_retaguarda_id)
        self.assertEqual(self.hub.tabela_preco_codigo, "")

    def test_codigo_tabela_incompativel_rejeita_v1(self):
        self.assert_rejeita(
            self.resposta(tabela_preco={"id": 5, "codigo": "ATACADO", "nome": "Atacado"})
        )

    def test_codigo_tabela_varejo_rejeita_v1(self):
        self.assert_rejeita(
            self.resposta(tabela_preco={"id": 5, "codigo": "VAREJO", "nome": "Varejo"})
        )

    def test_sku_id_duplicado_rejeita_snapshot(self):
        self.assert_rejeita(self.resposta([self.item(), self.item(descricao="Outro")]))

    def test_estoque_disponivel_incoerente_rejeita(self):
        self.assert_rejeita(self.resposta([self.item(estoque_disponivel="3.000")]))

    def test_vendavel_true_sem_preco_rejeita(self):
        self.assert_rejeita(self.resposta([self.item(preco_venda=None)]))

    def test_vendavel_true_sem_estoque_rejeita(self):
        self.assert_rejeita(
            self.resposta(
                [
                    self.item(
                        estoque_fisico="1.000",
                        reserva="1.000",
                        estoque_disponivel="0.000",
                    )
                ]
            )
        )

    def test_vendavel_true_com_motivo_rejeita(self):
        self.assert_rejeita(self.resposta([self.item(motivos_bloqueio=["SEM_PRECO"])]))

    def test_sem_preco_exige_nao_vendavel(self):
        self.assert_rejeita(self.resposta([self.item(motivos_bloqueio=["SEM_PRECO"])]))

    def test_sem_estoque_exige_nao_vendavel(self):
        self.assert_rejeita(self.resposta([self.item(motivos_bloqueio=["SEM_ESTOQUE"])]))

    def test_motivo_desconhecido_rejeita_v1(self):
        self.assert_rejeita(
            self.resposta([self.item(vendavel=False, motivos_bloqueio=["BLOQUEADO"])])
        )

    def test_upsert_cria_item(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.assertEqual(CatalogoItemHub.objects.count(), 1)

    def test_segundo_snapshot_atualiza_item_existente(self):
        sincronizar_catalogo(self.hub, self.resposta())
        sincronizar_catalogo(self.hub, self.resposta([self.item(descricao="Novo nome")]))

        self.assertEqual(CatalogoItemHub.objects.count(), 1)
        self.assertEqual(CatalogoItemHub.objects.get().descricao, "Novo nome")

    def test_identidade_local_do_hub_nao_muda(self):
        hub_uuid = self.hub.hub_uuid
        sincronizar_catalogo(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.hub_uuid, hub_uuid)
        self.assertEqual(self.hub.empresa_id, 3)
        self.assertEqual(self.hub.loja_id, 7)

    def test_sku_ausente_fica_inativo_sem_deletar_e_nao_vendavel(self):
        sincronizar_catalogo(self.hub, self.resposta([self.item(sku_id=100)]))
        sincronizar_catalogo(self.hub, self.resposta([self.item(sku_id=101)]))

        ausente = CatalogoItemHub.objects.get(retaguarda_sku_id=100)
        self.assertFalse(ausente.ativo)
        self.assertFalse(ausente.vendavel)
        self.assertEqual(CatalogoItemHub.objects.count(), 2)

    def test_snapshot_vazio_inativa_todos(self):
        sincronizar_catalogo(self.hub, self.resposta())
        sincronizar_catalogo(self.hub, self.resposta([]))

        self.assertFalse(CatalogoItemHub.objects.get().ativo)

    def test_sku_sem_preco_permanece_ativo_no_catalogo(self):
        item = self.item(preco_venda=None, vendavel=False, motivos_bloqueio=["SEM_PRECO"])
        sincronizar_catalogo(self.hub, self.resposta([item]))

        self.assertTrue(CatalogoItemHub.objects.get().ativo)

    def test_sku_sem_estoque_permanece_ativo_no_catalogo(self):
        item = self.item(
            estoque_fisico="0.000",
            reserva="0.000",
            estoque_disponivel="0.000",
            vendavel=False,
            motivos_bloqueio=["SEM_ESTOQUE"],
        )
        sincronizar_catalogo(self.hub, self.resposta([item]))

        self.assertTrue(CatalogoItemHub.objects.get().ativo)

    def test_hubconfig_recebe_datas_e_tabela(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertIsNotNone(self.hub.catalogo_gerado_em)
        self.assertIsNotNone(self.hub.catalogo_sincronizado_em)
        self.assertEqual(self.hub.tabela_preco_codigo, "PADRAO")

    def test_tabela_null_limpa_metadados_anteriores(self):
        sincronizar_catalogo(self.hub, self.resposta())
        sincronizar_catalogo(self.hub, self.resposta(tabela_preco=None))

        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.tabela_preco_retaguarda_id)
        self.assertEqual(self.hub.tabela_preco_codigo, "")
        self.assertEqual(self.hub.tabela_preco_nome, "")

    def test_token_empresa_e_loja_nao_mudam(self):
        sincronizar_catalogo(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.retaguarda_token, "TOKEN-SECRETO")
        self.assertEqual(self.hub.empresa_id, 3)
        self.assertEqual(self.hub.loja_id, 7)

    def test_rollback_preserva_catalogo_anterior_se_resposta_invalida(self):
        sincronizar_catalogo(self.hub, self.resposta())

        with self.assertRaises(CatalogoValidationError):
            sincronizar_catalogo(self.hub, self.resposta([self.item(descricao="Invalido")], total_itens=2))

        self.assertEqual(CatalogoItemHub.objects.get().descricao, "Produto Teste")


class SincronizarCatalogoHubCommandTests(TestCase):
    def hub_pronto(self, **overrides):
        payload = {
            "retaguarda_url": "http://central.test",
            "ativo": True,
            "retaguarda_token": "TOKEN-SECRETO",
            "retaguarda_hub_id": 99,
            "empresa_id": 3,
            "loja_id": 7,
            "bootstrap_versao": 1,
        }
        payload.update(overrides)
        return HubConfig.objects.create(**payload)

    def resposta(self, hub):
        return {
            "catalogo_versao": 1,
            "gerado_em": "2026-09-12T10:00:00-03:00",
            "hub": {"id": 99, "hub_uuid": str(hub.hub_uuid)},
            "empresa": {"id": 3, "nome": "Empresa Teste"},
            "loja": {"id": 7, "nome": "Filial 1"},
            "tabela_preco": None,
            "total_itens": 0,
            "itens": [],
        }

    def test_comando_sincroniza_sem_expor_token(self):
        hub = self.hub_pronto()
        out = io.StringIO()

        with patch(
            "integracao.management.commands.sincronizar_catalogo_hub.RetaguardaClient.catalogo",
            return_value=self.resposta(hub),
        ):
            call_command("sincronizar_catalogo_hub", stdout=out)

        self.assertIn("Catálogo sincronizado com sucesso.", out.getvalue())
        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_comando_exige_hub_ativo(self):
        self.hub_pronto(ativo=False)

        with self.assertRaises(CommandError):
            call_command("sincronizar_catalogo_hub", stdout=io.StringIO())

    def test_comando_exige_configuracao_completa(self):
        self.hub_pronto(retaguarda_token="")

        with self.assertRaises(CommandError):
            call_command("sincronizar_catalogo_hub", stdout=io.StringIO())


class FormasPagamentoHubServiceTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            ativo=True,
            retaguarda_token="TOKEN-SECRETO",
            retaguarda_hub_id=99,
            empresa_id=11,
            loja_id=41,
            bootstrap_versao=1,
        )

    def parcela(self, **overrides):
        payload = {
            "ordem": 1,
            "dias": 0,
            "percentual": "1.000000",
            "valor_fixo": None,
        }
        payload.update(overrides)
        return payload

    def prazo(self, **overrides):
        payload = {
            "id": 5,
            "codigo": "30D",
            "descricao": "30 dias",
            "num_parcelas": 1,
            "intervalo_dias": 30,
        }
        payload.update(overrides)
        return payload

    def forma(self, **overrides):
        payload = {
            "id": 10,
            "codigo": "DIN",
            "descricao": "Dinheiro",
            "tipo": "DINHEIRO",
            "num_parcelas": 1,
            "ativo": True,
            "prazo_pagamento": None,
            "adquirente": None,
            "conta_liquidacao_id": None,
            "gera_recebivel_bancario": False,
            "prazo_credito_dias": 0,
            "taxa_percentual": "0.0000",
            "taxa_fixa": "0.00",
            "tef_habilitado": False,
            "tef_modalidade": "",
            "tef_adquirente_codigo": "",
            "tef_terminal_logico": "",
            "parcelas": [self.parcela()],
        }
        payload.update(overrides)
        return payload

    def resposta(self, formas=None, **overrides):
        if formas is None:
            formas = [self.forma()]
        payload = {
            "formas_pagamento_versao": 1,
            "gerado_em": "2026-09-14T10:00:00-03:00",
            "hub": {"id": 99, "hub_uuid": str(self.hub.hub_uuid)},
            "empresa": {"id": 11},
            "loja": {"id": 41},
            "formas_pagamento": formas,
            "mapas_fiscais": [],
        }
        payload.update(overrides)
        return payload

    def assert_rejeita(self, resposta):
        with self.assertRaises(FormasPagamentoValidationError):
            sincronizar_formas_pagamento(self.hub, resposta)

    def test_snapshot_v1_valido_atualiza_hubconfig(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.formas_pagamento_versao, 1)
        self.assertIsNotNone(self.hub.formas_pagamento_gerado_em)
        self.assertIsNotNone(self.hub.formas_pagamento_sincronizado_em)

    def test_identidade_hub_correta_e_aceita(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())

        self.assertEqual(FormaPagamentoHub.objects.count(), 1)

    def test_hub_id_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["hub"]["id"] = 100
        self.assert_rejeita(resposta)

    def test_hub_uuid_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["hub"]["hub_uuid"] = str(uuid.uuid4())
        self.assert_rejeita(resposta)

    def test_empresa_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["empresa"]["id"] = 12
        self.assert_rejeita(resposta)

    def test_loja_divergente_rejeita(self):
        resposta = self.resposta()
        resposta["loja"]["id"] = 42
        self.assert_rejeita(resposta)

    def test_forma_valida_e_criada(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())

        forma = FormaPagamentoHub.objects.get()
        self.assertEqual(forma.codigo, "DIN")
        self.assertEqual(forma.tipo, "DINHEIRO")

    def test_segunda_sincronizacao_atualiza_sem_duplicar(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(descricao="Dinheiro novo")]))

        self.assertEqual(FormaPagamentoHub.objects.count(), 1)
        self.assertEqual(FormaPagamentoHub.objects.get().descricao, "Dinheiro novo")

    def test_forma_inativa_permanece_com_ativo_false(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(ativo=False)]))

        self.assertFalse(FormaPagamentoHub.objects.get().ativo)

    def test_forma_ausente_e_inativada_sem_deletar(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(id=10, codigo="DIN")]))
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(id=11, codigo="CAR")]))

        self.assertEqual(FormaPagamentoHub.objects.count(), 2)
        self.assertFalse(FormaPagamentoHub.objects.get(retaguarda_id=10).ativo)

    def test_codigo_duplicado_rejeita(self):
        self.assert_rejeita(
            self.resposta([self.forma(id=10, codigo="DIN"), self.forma(id=11, codigo="DIN")])
        )

    def test_id_duplicado_rejeita(self):
        self.assert_rejeita(
            self.resposta([self.forma(id=10, codigo="DIN"), self.forma(id=10, codigo="CAR")])
        )

    def test_prazo_null(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(prazo_pagamento=None)]))

        forma = FormaPagamentoHub.objects.get()
        self.assertIsNone(forma.prazo_retaguarda_id)
        self.assertEqual(forma.prazo_codigo, "")

    def test_prazo_preenchido(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(prazo_pagamento=self.prazo())]))

        forma = FormaPagamentoHub.objects.get()
        self.assertEqual(forma.prazo_retaguarda_id, 5)
        self.assertEqual(forma.prazo_codigo, "30D")

    def test_troca_prazo_preenchido_para_null_limpa_snapshot(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(prazo_pagamento=self.prazo())]))
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(prazo_pagamento=None)]))

        forma = FormaPagamentoHub.objects.get()
        self.assertIsNone(forma.prazo_retaguarda_id)
        self.assertEqual(forma.prazo_descricao, "")

    def test_parcelas_criadas(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())

        self.assertEqual(FormaPagamentoParcelaHub.objects.count(), 1)

    def test_parcelas_atualizadas(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(parcelas=[self.parcela(dias=30)])]))

        self.assertEqual(FormaPagamentoParcelaHub.objects.get().dias, 30)

    def test_parcela_ausente_e_removida(self):
        sincronizar_formas_pagamento(
            self.hub,
            self.resposta([self.forma(parcelas=[self.parcela(ordem=1), self.parcela(ordem=2)])]),
        )
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(parcelas=[self.parcela(ordem=1)])]))

        self.assertEqual(FormaPagamentoParcelaHub.objects.count(), 1)

    def test_ordem_duplicada_rejeita(self):
        self.assert_rejeita(
            self.resposta([self.forma(parcelas=[self.parcela(ordem=1), self.parcela(ordem=1)])])
        )

    def test_taxa_percentual_exige_string_4_casas(self):
        self.assert_rejeita(self.resposta([self.forma(taxa_percentual="2.500")]))

    def test_taxa_fixa_exige_string_2_casas(self):
        self.assert_rejeita(self.resposta([self.forma(taxa_fixa="1.200")]))

    def test_percentual_parcela_exige_6_casas(self):
        self.assert_rejeita(self.resposta([self.forma(parcelas=[self.parcela(percentual="0.5")])]))

    def test_valor_fixo_exige_2_casas(self):
        self.assert_rejeita(self.resposta([self.forma(parcelas=[self.parcela(valor_fixo="100.0")])]))

    def test_float_e_rejeitado(self):
        self.assert_rejeita(self.resposta([self.forma(taxa_percentual=2.5)]))

    def test_bool_nao_e_aceito_como_inteiro(self):
        self.assert_rejeita(self.resposta([self.forma(id=True)]))

    def test_tef_e_persistido(self):
        sincronizar_formas_pagamento(
            self.hub,
            self.resposta(
                [
                    self.forma(
                        tef_habilitado=True,
                        tef_modalidade="CREDITO",
                        tef_adquirente_codigo="001",
                        tef_terminal_logico="PDV01",
                    )
                ]
            ),
        )

        forma = FormaPagamentoHub.objects.get()
        self.assertTrue(forma.tef_habilitado)
        self.assertEqual(forma.tef_modalidade, "CREDITO")

    def test_conta_liquidacao_id_e_persistida_apenas_como_id(self):
        sincronizar_formas_pagamento(self.hub, self.resposta([self.forma(conta_liquidacao_id=77)]))

        self.assertEqual(FormaPagamentoHub.objects.get().conta_liquidacao_retaguarda_id, 77)

    def test_erro_de_validacao_nao_deixa_alteracao_parcial(self):
        sincronizar_formas_pagamento(self.hub, self.resposta())

        with self.assertRaises(FormasPagamentoValidationError):
            sincronizar_formas_pagamento(
                self.hub,
                self.resposta([self.forma(descricao="Alterado"), self.forma(id=11, codigo="DIN")]),
            )

        self.assertEqual(FormaPagamentoHub.objects.get().descricao, "Dinheiro")

    def test_hubconfig_so_atualiza_apos_sucesso(self):
        with self.assertRaises(FormasPagamentoValidationError):
            sincronizar_formas_pagamento(self.hub, self.resposta(formas_pagamento_versao=2))

        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.formas_pagamento_versao)

    def test_mapas_fiscais_sincroniza_preserva_multiplos_e_remove_ausentes(self):
        resposta = self.resposta(
            [self.forma(id=10, codigo="DIN")],
            mapas_fiscais=[
                {"forma_pagamento_id": 10, "codigo_tpag": "01", "descricao_fiscal": "Dinheiro"},
                {"forma_pagamento_id": 10, "codigo_tpag": "17", "descricao_fiscal": "PIX"},
            ],
        )
        sincronizar_formas_pagamento(self.hub, resposta)
        self.assertEqual(FormaPagamentoFiscalMapHub.objects.count(), 2)

        resposta["mapas_fiscais"] = [
            {"forma_pagamento_id": 10, "codigo_tpag": "01", "descricao_fiscal": "Dinheiro"},
        ]
        resultado = sincronizar_formas_pagamento(self.hub, resposta)

        self.assertEqual(resultado["mapas_fiscais_ausentes_removidos"], 1)
        self.assertEqual(list(FormaPagamentoFiscalMapHub.objects.values_list("codigo_tpag", flat=True)), ["01"])

    def test_mapa_fiscal_para_forma_desconhecida_rejeita(self):
        self.assert_rejeita(
            self.resposta(
                [self.forma(id=10, codigo="DIN")],
                mapas_fiscais=[
                    {"forma_pagamento_id": 99, "codigo_tpag": "01", "descricao_fiscal": "Dinheiro"},
                ],
            )
        )


class SincronizarFormasPagamentoHubCommandTests(TestCase):
    def hub_pronto(self, **overrides):
        payload = {
            "retaguarda_url": "http://central.test",
            "ativo": True,
            "retaguarda_token": "TOKEN-SECRETO",
            "retaguarda_hub_id": 99,
            "empresa_id": 11,
            "loja_id": 41,
            "bootstrap_versao": 1,
        }
        payload.update(overrides)
        return HubConfig.objects.create(**payload)

    def resposta(self, hub):
        return {
            "formas_pagamento_versao": 1,
            "gerado_em": "2026-09-14T10:00:00-03:00",
            "hub": {"id": 99, "hub_uuid": str(hub.hub_uuid)},
            "empresa": {"id": 11},
            "loja": {"id": 41},
            "formas_pagamento": [],
        }

    def test_comando_sincroniza_sem_expor_token(self):
        hub = self.hub_pronto()
        out = io.StringIO()

        with patch(
            "integracao.management.commands.sincronizar_formas_pagamento_hub.RetaguardaClient.formas_pagamento",
            return_value=self.resposta(hub),
        ):
            call_command("sincronizar_formas_pagamento_hub", stdout=out)

        self.assertIn("Formas de pagamento sincronizadas com sucesso.", out.getvalue())
        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_comando_exige_hub_ativo(self):
        self.hub_pronto(ativo=False)

        with self.assertRaises(CommandError):
            call_command("sincronizar_formas_pagamento_hub", stdout=io.StringIO())

    def test_comando_exige_configuracao_completa(self):
        self.hub_pronto(retaguarda_token="")

        with self.assertRaises(CommandError):
            call_command("sincronizar_formas_pagamento_hub", stdout=io.StringIO())
