import io
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import CaixaHub, HubConfig, OperadorHub, SessaoOperadorHub, TipoDespesaPdvHub
from core.services.terminais import configurar_terminal
from integracao.services.retaguarda import RetaguardaClient
from integracao.services.tipos_despesa_pdv import (
    TiposDespesaPdvValidationError,
    sincronizar_tipos_despesa_pdv,
)


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


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


def natureza_payload(**overrides):
    dados = {
        "id": 501,
        "codigo": "3301",
        "descricao": "Lanche",
        "categoria_principal": "Alimentacao",
        "subcategoria": "Equipe",
        "tipo": "DESPESA",
        "status": "ATIVO",
        "tipo_natureza": "DEBITO",
        "natureza_operacao": "DESPESA",
        "categoria_gerencial": "Loja",
        "movimenta_financeiro": True,
        "entra_dre": True,
    }
    dados.update(overrides)
    return dados


def tipo_payload(**overrides):
    dados = {
        "id": 101,
        "codigo": "LAN",
        "descricao": "Lanche de loja",
        "exige_documento": False,
        "ativo": True,
        "natureza": natureza_payload(),
    }
    dados.update(overrides)
    return dados


def payload_tipos(hub, tipos=None, **overrides):
    dados = {
        "tipos_despesa_pdv_versao": 1,
        "gerado_em": timezone.now().isoformat(),
        "hub": {"id": hub.retaguarda_hub_id, "hub_uuid": str(hub.hub_uuid)},
        "empresa": {"id": hub.empresa_id},
        "loja": {"id": hub.loja_id},
        "tipos_despesa_pdv": tipos if tipos is not None else [tipo_payload()],
    }
    dados.update(overrides)
    return dados


class RetaguardaClientTiposDespesaPdvTests(TestCase):
    def test_tipos_despesa_pdv_chama_get_com_authorization_hub(self):
        def fake_urlopen(req, timeout):
            self.assertEqual(req.full_url, "http://central.test/api/hub/tipos-despesa-pdv/")
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.headers["Authorization"], "Hub TOKEN-SECRETO")
            return _JsonResponse({"tipos_despesa_pdv_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", side_effect=fake_urlopen):
            resposta = RetaguardaClient("http://central.test").tipos_despesa_pdv(token="TOKEN-SECRETO")

        self.assertEqual(resposta["tipos_despesa_pdv_versao"], 1)


class TiposDespesaPdvSincronizacaoTests(TestCase):
    def setUp(self):
        self.hub = criar_hub()

    def test_snapshot_valido_cria_cache(self):
        resultado = sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub))

        tipo = TipoDespesaPdvHub.objects.get()
        self.assertEqual(resultado["tipos_recebidos"], 1)
        self.assertEqual(tipo.retaguarda_id, 101)
        self.assertEqual(tipo.codigo, "LAN")
        self.assertEqual(tipo.natureza_retaguarda_id, 501)
        self.assertTrue(tipo.presente_retaguarda)

    def test_atualizacao_altera_snapshot_existente_sem_duplicar(self):
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub))
        sincronizar_tipos_despesa_pdv(
            self.hub,
            payload_tipos(self.hub, [tipo_payload(descricao="Lanche atualizado", exige_documento=True)]),
        )

        self.assertEqual(TipoDespesaPdvHub.objects.count(), 1)
        tipo = TipoDespesaPdvHub.objects.get()
        self.assertEqual(tipo.descricao, "Lanche atualizado")
        self.assertTrue(tipo.exige_documento)

    def test_id_ausente_marca_presente_retaguarda_false(self):
        sincronizar_tipos_despesa_pdv(
            self.hub,
            payload_tipos(self.hub, [tipo_payload(id=101), tipo_payload(id=102, codigo="TAX", natureza=natureza_payload(id=502))]),
        )

        resultado = sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101)]))

        ausente = TipoDespesaPdvHub.objects.get(retaguarda_id=102)
        self.assertEqual(resultado["tipos_ausentes_marcados"], 1)
        self.assertFalse(ausente.presente_retaguarda)

    def test_tipo_reaparecendo_volta_para_true(self):
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101)]))
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, []))
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101)]))

        self.assertTrue(TipoDespesaPdvHub.objects.get(retaguarda_id=101).presente_retaguarda)

    def test_snapshot_vazio_marca_anteriores_ausentes(self):
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101)]))

        resultado = sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, []))

        self.assertEqual(resultado["tipos_recebidos"], 0)
        self.assertFalse(TipoDespesaPdvHub.objects.get(retaguarda_id=101).presente_retaguarda)

    def test_versao_invalida_rejeitada(self):
        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, tipos_despesa_pdv_versao=2))

    def test_hub_id_divergente_rejeitado(self):
        payload = payload_tipos(self.hub)
        payload["hub"]["id"] = 999

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload)

    def test_hub_uuid_divergente_rejeitado(self):
        payload = payload_tipos(self.hub)
        payload["hub"]["hub_uuid"] = "00000000-0000-4000-8000-000000000000"

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload)

    def test_empresa_divergente_rejeitada(self):
        payload = payload_tipos(self.hub)
        payload["empresa"]["id"] = 999

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload)

    def test_loja_divergente_rejeitada(self):
        payload = payload_tipos(self.hub)
        payload["loja"]["id"] = 999

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload)

    def test_id_duplicado_rejeitado(self):
        tipos = [tipo_payload(id=101), tipo_payload(id=101, codigo="TAX")]

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, tipos))

    def test_tipo_invalido_rejeitado(self):
        for item in [tipo_payload(id=True), tipo_payload(codigo=""), tipo_payload(descricao="")]:
            with self.subTest(item=item), self.assertRaises(TiposDespesaPdvValidationError):
                sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [item]))

    def test_natureza_invalida_rejeitada(self):
        invalidos = [
            tipo_payload(natureza=None),
            tipo_payload(natureza=natureza_payload(id=True)),
            tipo_payload(natureza=natureza_payload(descricao="")),
        ]
        for item in invalidos:
            with self.subTest(item=item), self.assertRaises(TiposDespesaPdvValidationError):
                sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [item]))

    def test_bool_invalido_rejeitado(self):
        invalidos = [
            tipo_payload(exige_documento="nao"),
            tipo_payload(ativo="sim"),
            tipo_payload(natureza=natureza_payload(movimenta_financeiro="sim")),
            tipo_payload(natureza=natureza_payload(entra_dre="sim")),
        ]
        for item in invalidos:
            with self.subTest(item=item), self.assertRaises(TiposDespesaPdvValidationError):
                sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [item]))

    def test_gerado_em_invalido_rejeitado(self):
        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, gerado_em="invalido"))

    def test_falha_de_validacao_nao_altera_banco(self):
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101)]))

        with self.assertRaises(TiposDespesaPdvValidationError):
            sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub, [tipo_payload(id=101), tipo_payload(id=102, descricao="")]))

        self.assertEqual(TipoDespesaPdvHub.objects.count(), 1)
        self.assertTrue(TipoDespesaPdvHub.objects.get(retaguarda_id=101).presente_retaguarda)

    def test_atualiza_metadados_do_hub(self):
        sincronizar_tipos_despesa_pdv(self.hub, payload_tipos(self.hub))

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.tipos_despesa_pdv_versao, 1)
        self.assertIsNotNone(self.hub.tipos_despesa_pdv_gerado_em)
        self.assertIsNotNone(self.hub.tipos_despesa_pdv_sincronizado_em)


class TiposDespesaPdvLocalApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub(tipos_despesa_pdv_versao=1, tipos_despesa_pdv_sincronizado_em=timezone.now())
        self.outro_hub = criar_hub(
            retaguarda_url="http://central-2.test",
            retaguarda_hub_id=8,
            empresa_id=12,
            loja_id=42,
        )
        CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        self.terminal_token = self.terminal.gerar_token()
        self.terminal.save()
        self.operador = OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=10,
            codigo="001",
            nome="Operador",
            tipo="VENDEDOR",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.sessao = SessaoOperadorHub.objects.create(
            terminal=self.terminal,
            operador=self.operador,
            token_hash=SessaoOperadorHub.hash_token("SESSAO"),
            token_prefixo="SESSAO",
            ativa=True,
            ultima_atividade_em=timezone.now(),
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {self.terminal_token}",
            HTTP_X_SYSVAR_OPERADOR_SESSION="SESSAO",
        )

    def criar_tipo(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_id": 101,
            "codigo": "LAN",
            "descricao": "Lanche de loja",
            "exige_documento": False,
            "ativo": True,
            "presente_retaguarda": True,
            "natureza_retaguarda_id": 501,
            "natureza_codigo": "3301",
            "natureza_descricao": "Lanche",
            "natureza_categoria_principal": "Alimentacao",
            "natureza_subcategoria": "Equipe",
            "natureza_tipo": "DESPESA",
            "natureza_status": "ATIVO",
            "natureza_tipo_natureza": "DEBITO",
            "natureza_operacao": "DESPESA",
            "natureza_categoria_gerencial": "Loja",
            "natureza_movimenta_financeiro": True,
            "natureza_entra_dre": True,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return TipoDespesaPdvHub.objects.create(**dados)

    def test_exige_autenticacao_operador(self):
        self.client.credentials()

        resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_sem_dados_retorna_lista_vazia(self):
        resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["total"], 0)
        self.assertEqual(resposta.data["tipos_despesa_pdv"], [])

    def test_retorna_somente_tipos_do_hub_do_terminal(self):
        self.criar_tipo(descricao="Lanche de loja")
        self.criar_tipo(hub=self.outro_hub, retaguarda_id=202, codigo="OUT", descricao="Outro Hub")

        resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["tipos_despesa_pdv"][0]["descricao"], "Lanche de loja")

    def test_filtra_ausentes_e_inativos(self):
        self.criar_tipo(retaguarda_id=101, codigo="LAN", descricao="Lanche")
        self.criar_tipo(retaguarda_id=102, codigo="AUS", descricao="Ausente", presente_retaguarda=False)
        self.criar_tipo(retaguarda_id=103, codigo="INA", descricao="Inativo", ativo=False)

        resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        self.assertEqual([item["descricao"] for item in resposta.data["tipos_despesa_pdv"]], ["Lanche"])

    def test_contrato_usa_retaguarda_id_e_snapshot_da_natureza(self):
        tipo = self.criar_tipo(retaguarda_id=777, descricao="Lanche")

        resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        item = resposta.data["tipos_despesa_pdv"][0]
        self.assertEqual(item["id"], 777)
        self.assertNotEqual(item["id"], tipo.pk)
        self.assertNotIn("ativo", item)
        self.assertNotIn("presente_retaguarda", item)
        self.assertEqual(item["natureza"]["id"], 501)

    def test_endpoint_nao_chama_central(self):
        self.criar_tipo()

        with patch("integracao.services.retaguarda.RetaguardaClient.tipos_despesa_pdv") as tipos:
            resposta = self.client.get("/api/terminal/tipos-despesa-pdv/")

        self.assertEqual(resposta.status_code, 200)
        tipos.assert_not_called()


class SincronizarTiposDespesaPdvHubCommandTests(TestCase):
    def test_comando_chama_endpoint_e_servico_sem_imprimir_token(self):
        hub = criar_hub()
        resposta = payload_tipos(hub, [])
        resultado = {
            "empresa": hub.empresa_id,
            "loja": hub.loja_id,
            "versao": 1,
            "tipos_recebidos": 0,
            "tipos_ausentes_marcados": 0,
        }
        out = io.StringIO()

        with patch("integracao.management.commands.sincronizar_tipos_despesa_pdv_hub.RetaguardaClient.tipos_despesa_pdv", return_value=resposta) as tipos:
            with patch("integracao.management.commands.sincronizar_tipos_despesa_pdv_hub.sincronizar_tipos_despesa_pdv", return_value=resultado) as sincronizar:
                call_command("sincronizar_tipos_despesa_pdv_hub", stdout=out)

        tipos.assert_called_once_with(token="TOKEN-SECRETO")
        sincronizar.assert_called_once_with(hub, resposta)
        self.assertNotIn("TOKEN-SECRETO", out.getvalue())
