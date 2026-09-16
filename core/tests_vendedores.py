import io
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import CaixaHub, HubConfig, OperadorHub, SessaoOperadorHub, VendedorHub
from core.services.terminais import configurar_terminal
from integracao.services.retaguarda import RetaguardaClient
from integracao.services.vendedores import (
    VendedoresValidationError,
    sincronizar_vendedores,
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


def payload_vendedores(hub, vendedores=None, **overrides):
    dados = {
        "vendedores_versao": 1,
        "gerado_em": timezone.now().isoformat(),
        "hub": {"id": hub.retaguarda_hub_id, "hub_uuid": str(hub.hub_uuid)},
        "empresa": {"id": hub.empresa_id},
        "loja": {"id": hub.loja_id},
        "vendedores": vendedores if vendedores is not None else [vendedor_payload()],
    }
    dados.update(overrides)
    return dados


def vendedor_payload(**overrides):
    dados = {
        "id": 101,
        "matricula": "000101",
        "nome": "Ana Vendedora",
        "apelido": "Ana",
        "cargo": {"id": 5, "codigo": "VENDEDOR", "descricao": "Vendedor"},
        "comissionado": True,
        "comissao_percentual": "3.00",
        "ativo": True,
        "situacao": "ATIVO",
        "participa_vendas": True,
    }
    dados.update(overrides)
    return dados


class RetaguardaClientVendedoresTests(TestCase):
    def test_vendedores_chama_get_com_authorization_hub(self):
        def fake_urlopen(req, timeout):
            self.assertEqual(req.full_url, "http://central.test/api/hub/vendedores/")
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.headers["Authorization"], "Hub TOKEN-SECRETO")
            return _JsonResponse({"vendedores_versao": 1})

        with patch("integracao.services.retaguarda.request.urlopen", side_effect=fake_urlopen):
            resposta = RetaguardaClient("http://central.test").vendedores(token="TOKEN-SECRETO")

        self.assertEqual(resposta["vendedores_versao"], 1)


class VendedoresSincronizacaoTests(TestCase):
    def setUp(self):
        self.hub = criar_hub()

    def test_versao_nao_suportada_rejeitada(self):
        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload_vendedores(self.hub, vendedores_versao=2))

    def test_identidade_hub_divergente_rejeitada(self):
        payload = payload_vendedores(self.hub)
        payload["hub"]["id"] = 999

        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload)

    def test_empresa_divergente_rejeitada(self):
        payload = payload_vendedores(self.hub)
        payload["empresa"]["id"] = 999

        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload)

    def test_loja_divergente_rejeitada(self):
        payload = payload_vendedores(self.hub)
        payload["loja"]["id"] = 999

        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload)

    def test_lista_invalida_rejeitada(self):
        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload_vendedores(self.hub, vendedores={}))

    def test_id_duplicado_rejeitado(self):
        vendedores = [vendedor_payload(id=101), vendedor_payload(id=101, nome="Bruno")]

        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(self.hub, payload_vendedores(self.hub, vendedores))

    def test_tipos_invalidos_rejeitados(self):
        invalidos = [
            vendedor_payload(id=True),
            vendedor_payload(comissionado="sim"),
            vendedor_payload(participa_vendas="sim"),
            vendedor_payload(comissao_percentual="abc"),
        ]
        for item in invalidos:
            with self.subTest(item=item), self.assertRaises(VendedoresValidationError):
                sincronizar_vendedores(self.hub, payload_vendedores(self.hub, [item]))

    def test_cria_vendedor_novo(self):
        resultado = sincronizar_vendedores(self.hub, payload_vendedores(self.hub))

        vendedor = VendedorHub.objects.get()
        self.assertEqual(resultado["vendedores_recebidos"], 1)
        self.assertEqual(vendedor.retaguarda_id, 101)
        self.assertEqual(vendedor.nome, "Ana Vendedora")
        self.assertEqual(vendedor.cargo_retaguarda_id, 5)
        self.assertEqual(vendedor.comissao_percentual, Decimal("3.00"))
        self.assertTrue(vendedor.presente_retaguarda)

    def test_atualiza_vendedor_existente(self):
        sincronizar_vendedores(self.hub, payload_vendedores(self.hub))
        sincronizar_vendedores(
            self.hub,
            payload_vendedores(self.hub, [vendedor_payload(nome="Ana Atualizada")]),
        )

        self.assertEqual(VendedorHub.objects.get().nome, "Ana Atualizada")

    def test_snapshot_completo_marca_ausente_sem_deletar(self):
        sincronizar_vendedores(
            self.hub,
            payload_vendedores(
                self.hub,
                [vendedor_payload(id=101), vendedor_payload(id=102, nome="Bruno", matricula="000102")],
            ),
        )

        resultado = sincronizar_vendedores(self.hub, payload_vendedores(self.hub, [vendedor_payload(id=101)]))

        ausente = VendedorHub.objects.get(retaguarda_id=102)
        self.assertEqual(resultado["vendedores_ausentes_marcados"], 1)
        self.assertFalse(ausente.presente_retaguarda)
        self.assertEqual(VendedorHub.objects.count(), 2)

    def test_sincronizacao_invalida_nao_persiste_parcialmente(self):
        with self.assertRaises(VendedoresValidationError):
            sincronizar_vendedores(
                self.hub,
                payload_vendedores(
                    self.hub,
                    [vendedor_payload(id=101), vendedor_payload(id=102, nome="")],
                ),
            )

        self.assertEqual(VendedorHub.objects.count(), 0)

    def test_atualiza_metadados_do_hub(self):
        sincronizar_vendedores(self.hub, payload_vendedores(self.hub))

        self.hub.refresh_from_db()
        self.assertEqual(self.hub.vendedores_versao, 1)
        self.assertIsNotNone(self.hub.vendedores_gerado_em)
        self.assertIsNotNone(self.hub.vendedores_sincronizado_em)


class VendedoresLocalApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub(vendedores_versao=1, vendedores_sincronizado_em=timezone.now())
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

    def criar_vendedor(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_id": 101,
            "matricula": "000101",
            "nome": "Ana Vendedora",
            "apelido": "Ana",
            "cargo_retaguarda_id": 5,
            "cargo_codigo": "VENDEDOR",
            "cargo_descricao": "Vendedor",
            "comissionado": True,
            "comissao_percentual": Decimal("3.00"),
            "ativo": True,
            "situacao": "ATIVO",
            "participa_vendas": True,
            "presente_retaguarda": True,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return VendedorHub.objects.create(**dados)

    def test_exige_autenticacao_operador(self):
        self.client.credentials()

        resposta = self.client.get("/api/terminal/vendedores/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_retorna_somente_vendedores_do_hub_do_terminal(self):
        self.criar_vendedor(nome="Ana Vendedora")
        self.criar_vendedor(hub=self.outro_hub, retaguarda_id=202, nome="Outro Hub")

        resposta = self.client.get("/api/terminal/vendedores/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["vendedores"][0]["nome"], "Ana Vendedora")

    def test_filtra_vendedores_inelegiveis_localmente(self):
        self.criar_vendedor(retaguarda_id=101, nome="Ana")
        self.criar_vendedor(retaguarda_id=102, nome="Ausente", presente_retaguarda=False)
        self.criar_vendedor(retaguarda_id=103, nome="Inativo", ativo=False)
        self.criar_vendedor(retaguarda_id=104, nome="Afastado", situacao="AFASTADO")
        self.criar_vendedor(retaguarda_id=105, nome="Nao Participa", participa_vendas=False)

        resposta = self.client.get("/api/terminal/vendedores/")

        self.assertEqual([item["nome"] for item in resposta.data["vendedores"]], ["Ana"])

    def test_busca_por_matricula_nome_e_apelido(self):
        self.criar_vendedor(matricula="000777", nome="Ana Vendedora", apelido="Aninha")

        self.assertEqual(self.client.get("/api/terminal/vendedores/", {"q": "000777"}).data["total"], 1)
        self.assertEqual(self.client.get("/api/terminal/vendedores/", {"q": "Vendedora"}).data["total"], 1)
        self.assertEqual(self.client.get("/api/terminal/vendedores/", {"q": "Aninha"}).data["total"], 1)

    def test_total_limit_e_decimal_string(self):
        for idx in range(55):
            self.criar_vendedor(
                retaguarda_id=1000 + idx,
                matricula=str(idx).zfill(6),
                nome=f"Vendedor {idx:02d}",
            )

        resposta = self.client.get("/api/terminal/vendedores/")

        self.assertEqual(resposta.data["total"], 55)
        self.assertEqual(resposta.data["limit"], 50)
        self.assertEqual(len(resposta.data["vendedores"]), 50)
        self.assertEqual(resposta.data["vendedores"][0]["comissao_percentual"], "3.00")

    def test_endpoint_nao_chama_central(self):
        self.criar_vendedor()

        with patch("integracao.services.retaguarda.RetaguardaClient.vendedores") as vendedores:
            resposta = self.client.get("/api/terminal/vendedores/")

        self.assertEqual(resposta.status_code, 200)
        vendedores.assert_not_called()


class SincronizarVendedoresHubCommandTests(TestCase):
    def test_comando_chama_endpoint_e_servico_sem_imprimir_token(self):
        hub = criar_hub()
        resposta = payload_vendedores(hub, [])
        resultado = {
            "empresa": hub.empresa_id,
            "loja": hub.loja_id,
            "versao": 1,
            "vendedores_recebidos": 0,
            "vendedores_ausentes_marcados": 0,
        }
        out = io.StringIO()

        with patch("integracao.management.commands.sincronizar_vendedores_hub.RetaguardaClient.vendedores", return_value=resposta) as vendedores:
            with patch("integracao.management.commands.sincronizar_vendedores_hub.sincronizar_vendedores", return_value=resultado) as sincronizar:
                call_command("sincronizar_vendedores_hub", stdout=out)

        vendedores.assert_called_once_with(token="TOKEN-SECRETO")
        sincronizar.assert_called_once_with(hub, resposta)
        self.assertNotIn("TOKEN-SECRETO", out.getvalue())
