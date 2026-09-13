from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import CaixaHub, HubConfig, OperadorHub, SessaoCaixaHub, SessaoOperadorHub
from core.services.caixa import (
    CaixaConflictError,
    CaixaError,
    ValorAberturaError,
    abrir_caixa,
    fechar_caixa,
    obter_caixa_terminal,
    serializar_sessao_caixa,
    validar_valor_abertura,
)
from core.services.operadores import encerrar_sessao
from core.services.terminais import configurar_terminal


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


class CaixaHubTestMixin:
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub()
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        self.token_terminal = self.terminal.gerar_token()
        self.terminal.save()
        self.operador = self.criar_operador("caixa.barra", 90, "Juliana Rocha")
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, self.operador)

    def criar_operador(self, codigo, usuario_id, nome):
        return OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=usuario_id,
            codigo=codigo,
            nome=nome,
            tipo="Caixa",
            perfil_retaguarda_id=5,
            perfil_nome="Operador de Caixa",
            credencial_hash=make_password("1234"),
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def criar_sessao_operador(self, terminal, operador):
        sessao = SessaoOperadorHub(
            terminal=terminal,
            operador=operador,
            ultima_atividade_em=timezone.now(),
        )
        token = sessao.gerar_token()
        sessao.save()
        return sessao, token

    def autenticar(self, token_terminal=None, token_operador=None):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {token_terminal or self.token_terminal}",
            HTTP_X_SYSVAR_OPERADOR_SESSION=token_operador or self.token_operador,
        )

    def abrir_api(self, valor="100.00", extra=None):
        self.autenticar()
        body = {"valor_abertura": valor}
        if extra:
            body.update(extra)
        return self.client.post("/api/terminal/caixa/abrir/", body, format="json")


class SessaoCaixaModelTests(CaixaHubTestMixin, TestCase):
    def test_uuid_unico(self):
        primeira = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="10.00")
        fechar_caixa(self.terminal, self.operador, self.sessao_operador)
        segunda = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="20.00")

        self.assertNotEqual(primeira.sessao_uuid, segunda.sessao_uuid)

    def test_valor_decimal(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        self.assertEqual(sessao.valor_abertura, Decimal("100.00"))

    def test_status_aberto_e_fechado(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")
        self.assertEqual(sessao.status, SessaoCaixaHub.STATUS_ABERTO)

        fechar_caixa(self.terminal, self.operador, self.sessao_operador)
        sessao.refresh_from_db()
        self.assertEqual(sessao.status, SessaoCaixaHub.STATUS_FECHADO)

    def test_historico_de_abertura(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        self.assertEqual(sessao.terminal_abertura, self.terminal)
        self.assertEqual(sessao.operador_abertura, self.operador)
        self.assertEqual(sessao.sessao_operador_abertura, self.sessao_operador)
        self.assertIsNotNone(sessao.aberto_em)

    def test_historico_de_fechamento(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")
        fechar_caixa(self.terminal, self.operador, self.sessao_operador)
        sessao.refresh_from_db()

        self.assertEqual(sessao.terminal_fechamento, self.terminal)
        self.assertEqual(sessao.operador_fechamento, self.operador)
        self.assertEqual(sessao.sessao_operador_fechamento, self.sessao_operador)
        self.assertIsNotNone(sessao.fechado_em)

    def test_chave_tecnica_nao_exposta(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")
        payload = serializar_sessao_caixa(sessao)

        self.assertNotIn("chave_caixa_aberto", payload)

    def test_chave_unica_impede_duas_abertas_no_mesmo_caixa(self):
        abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        with self.assertRaises(IntegrityError):
            SessaoCaixaHub.objects.create(
                caixa=self.caixa,
                status=SessaoCaixaHub.STATUS_ABERTO,
                chave_caixa_aberto=self.caixa.pk,
                valor_abertura="50.00",
                aberto_em=timezone.now(),
                terminal_abertura=self.terminal,
                operador_abertura=self.operador,
                sessao_operador_abertura=self.sessao_operador,
            )


class CaixaServiceTests(CaixaHubTestMixin, TestCase):
    def test_resolve_caixa_pelo_terminal(self):
        self.assertEqual(obter_caixa_terminal(self.terminal), self.caixa)

    def test_terminal_sem_caixa_falha(self):
        terminal = configurar_terminal(self.hub, "BALCAO-01", "Balcao 01")

        with self.assertRaises(CaixaError):
            obter_caixa_terminal(terminal)

    def test_caixa_inexistente_falha(self):
        self.terminal.caixa_retaguarda_id = 999
        self.terminal.save()

        with self.assertRaises(CaixaError):
            obter_caixa_terminal(self.terminal)

    def test_caixa_inativo_nao_abre(self):
        self.caixa.ativo = False
        self.caixa.save()

        with self.assertRaises(CaixaError):
            abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

    def test_valor_zero_permitido(self):
        self.assertEqual(validar_valor_abertura("0.00"), Decimal("0.00"))

    def test_valor_positivo_permitido(self):
        self.assertEqual(validar_valor_abertura("100.00"), Decimal("100.00"))

    def test_valor_negativo_rejeitado(self):
        with self.assertRaises(ValorAberturaError):
            validar_valor_abertura("-0.01")

    def test_valor_invalido_rejeitado(self):
        for valor in ("", "abc", "NaN", "Infinity", "1e2"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValorAberturaError):
                    validar_valor_abertura(valor)

    def test_boolean_rejeitado(self):
        with self.assertRaises(ValorAberturaError):
            validar_valor_abertura(True)

    def test_mais_de_duas_casas_rejeitado(self):
        with self.assertRaises(ValorAberturaError):
            validar_valor_abertura("10.001")

    def test_abre_e_registra_operador_sessao_terminal(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        self.assertEqual(sessao.operador_abertura, self.operador)
        self.assertEqual(sessao.sessao_operador_abertura, self.sessao_operador)
        self.assertEqual(sessao.terminal_abertura, self.terminal)

    def test_segunda_abertura_conflita(self):
        sessao = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        with self.assertRaises(CaixaConflictError) as contexto:
            abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="50.00")

        self.assertEqual(contexto.exception.sessao, sessao)

    def test_dois_terminais_mesmo_caixa_nao_duplicam(self):
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        outra_sessao_operador, _token = self.criar_sessao_operador(outro_terminal, self.operador)
        abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")

        with self.assertRaises(CaixaConflictError):
            abrir_caixa(outro_terminal, self.operador, outra_sessao_operador, valor_abertura="50.00")

        self.assertEqual(SessaoCaixaHub.objects.filter(caixa=self.caixa, status="ABERTO").count(), 1)

    def test_caixas_diferentes_podem_abrir(self):
        outra_caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=30,
            codigo="CX-02",
            descricao="Caixa 02",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", 30)
        outra_sessao_operador, _token = self.criar_sessao_operador(outro_terminal, self.operador)

        abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")
        segunda = abrir_caixa(outro_terminal, self.operador, outra_sessao_operador, valor_abertura="50.00")

        self.assertEqual(segunda.caixa, outra_caixa)
        self.assertEqual(SessaoCaixaHub.objects.filter(status="ABERTO").count(), 2)

    def test_fechamento_libera_chave_e_permite_nova_abertura(self):
        primeira = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="100.00")
        fechar_caixa(self.terminal, self.operador, self.sessao_operador)
        primeira.refresh_from_db()

        segunda = abrir_caixa(self.terminal, self.operador, self.sessao_operador, valor_abertura="20.00")

        self.assertIsNone(primeira.chave_caixa_aberto)
        self.assertEqual(segunda.status, SessaoCaixaHub.STATUS_ABERTO)

    def test_fechar_sem_abertura_conflita(self):
        with self.assertRaises(CaixaConflictError):
            fechar_caixa(self.terminal, self.operador, self.sessao_operador)


class CaixaApiTests(CaixaHubTestMixin, TestCase):
    def test_exige_terminal_e_operador(self):
        resposta = self.client.get("/api/terminal/caixa/status/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_status_fechado_retorna_aberto_false(self):
        self.autenticar()

        resposta = self.client.get("/api/terminal/caixa/status/")

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.data["aberto"])
        self.assertIsNone(resposta.data["sessao"])

    def test_status_aberto_retorna_sessao(self):
        self.abrir_api("100.00")

        resposta = self.client.get("/api/terminal/caixa/status/")

        self.assertTrue(resposta.data["aberto"])
        self.assertEqual(resposta.data["sessao"]["status"], "ABERTO")
        self.assertEqual(resposta.data["sessao"]["valor_abertura"], "100.00")

    def test_outro_terminal_mesmo_caixa_ve_sessao(self):
        self.abrir_api("100.00")
        outro_terminal = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        outro_token_terminal = outro_terminal.gerar_token()
        outro_terminal.save()
        outra_sessao_operador, outro_token_operador = self.criar_sessao_operador(outro_terminal, self.operador)
        self.autenticar(outro_token_terminal, outro_token_operador)

        resposta = self.client.get("/api/terminal/caixa/status/")

        self.assertTrue(resposta.data["aberto"])
        self.assertEqual(resposta.data["sessao"]["operador_abertura"]["codigo"], "caixa.barra")
        self.assertEqual(outra_sessao_operador.operador, self.operador)

    def test_troca_de_operador_nao_altera_abertura(self):
        self.abrir_api("100.00")
        operador_b = self.criar_operador("operador.b", 91, "Operador B")
        encerrar_sessao(self.sessao_operador)
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, operador_b)
        self.autenticar()

        resposta = self.client.get("/api/terminal/caixa/status/")

        self.assertTrue(resposta.data["aberto"])
        self.assertEqual(resposta.data["sessao"]["operador_abertura"]["codigo"], "caixa.barra")

    def test_abrir_retorna_201(self):
        resposta = self.abrir_api("100.00")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.data["status"], "ABERTO")

    def test_abrir_ignora_caixa_do_body(self):
        resposta = self.abrir_api("100.00", extra={"caixa_id": 999})

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.data["caixa"]["id"], 29)

    def test_abrir_ja_aberto_retorna_409_com_sessao(self):
        self.abrir_api("100.00")

        resposta = self.abrir_api("50.00")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Caixa já está aberto.")
        self.assertEqual(resposta.data["sessao"]["valor_abertura"], "100.00")

    def test_fechar_corretamente(self):
        self.abrir_api("100.00")

        resposta = self.client.post("/api/terminal/caixa/fechar/", {}, format="json")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["status"], "ok")
        self.assertEqual(resposta.data["sessao"]["status"], "FECHADO")
        self.assertIsNotNone(resposta.data["sessao"]["fechado_em"])

    def test_fechar_registra_operador_terminal_sessao(self):
        self.abrir_api("100.00")
        operador_b = self.criar_operador("operador.b", 91, "Operador B")
        encerrar_sessao(self.sessao_operador)
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, operador_b)
        self.autenticar()

        resposta = self.client.post("/api/terminal/caixa/fechar/", {}, format="json")
        sessao = SessaoCaixaHub.objects.get()

        self.assertEqual(resposta.data["sessao"]["operador_fechamento"]["codigo"], "operador.b")
        self.assertEqual(sessao.operador_fechamento, operador_b)
        self.assertEqual(sessao.terminal_fechamento, self.terminal)
        self.assertEqual(sessao.sessao_operador_fechamento, self.sessao_operador)

    def test_fechar_ja_fechado_retorna_409(self):
        resposta = self.client.post("/api/terminal/caixa/fechar/", {}, format="json")

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Caixa não está aberto.")

    def test_resposta_nao_contem_hashes_ou_tokens(self):
        resposta = self.abrir_api("100.00")
        conteudo = f"{resposta.data}"

        self.assertNotIn("token", conteudo.lower())
        self.assertNotIn("hash", conteudo.lower())
        self.assertNotIn("credencial", conteudo.lower())
        self.assertNotIn("chave_caixa_aberto", conteudo)

    def test_nao_chama_sysvar_central(self):
        with patch("integracao.services.retaguarda.RetaguardaClient.catalogo") as catalogo:
            resposta = self.abrir_api("100.00")

        self.assertEqual(resposta.status_code, 201)
        catalogo.assert_not_called()
