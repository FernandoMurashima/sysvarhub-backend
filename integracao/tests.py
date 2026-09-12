import io
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import CaixaHub, CatalogoItemHub, HubConfig
from integracao.services.bootstrap import BootstrapValidationError, sincronizar_bootstrap
from integracao.services.catalogo import CatalogoValidationError, sincronizar_catalogo
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
        self.assert_rejeita(self.resposta(catalogo_versao=2))

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
