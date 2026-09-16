from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    CaixaHub,
    HubConfig,
    MovimentacaoCaixaHub,
    OperadorHub,
    SessaoCaixaHub,
    SessaoOperadorHub,
    TipoDespesaPdvHub,
)
from core.services.movimentacoes_caixa import (
    MovimentacaoCaixaValidationError,
    registrar_movimentacao_caixa,
)
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


class MovimentacoesCaixaApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub()
        self.outro_hub = criar_hub(
            retaguarda_url="http://central-2.test",
            retaguarda_hub_id=8,
            empresa_id=12,
            loja_id=42,
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-01",
            descricao="Caixa Loja",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.outro_caixa = CaixaHub.objects.create(
            hub=self.outro_hub,
            retaguarda_id=30,
            codigo="CX-02",
            descricao="Caixa Outro Hub",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", self.caixa.retaguarda_id)
        self.terminal_token = self.terminal.gerar_token()
        self.terminal.save()
        self.outro_terminal = configurar_terminal(self.outro_hub, "PDV-02", "PDV 02", self.outro_caixa.retaguarda_id)
        self.outro_terminal_token = self.outro_terminal.gerar_token()
        self.outro_terminal.save()
        self.operador = OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=10,
            codigo="001",
            nome="Operador",
            tipo="VENDEDOR",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.outro_operador = OperadorHub.objects.create(
            hub=self.outro_hub,
            retaguarda_usuario_id=11,
            codigo="002",
            nome="Outro Operador",
            tipo="VENDEDOR",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.sessao_operador = self.criar_sessao_operador(self.terminal, self.operador, "SESSAO")
        self.outro_sessao_operador = self.criar_sessao_operador(self.outro_terminal, self.outro_operador, "OUTRA")
        self.autenticar()

    def autenticar(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {self.terminal_token}",
            HTTP_X_SYSVAR_OPERADOR_SESSION="SESSAO",
        )

    def criar_sessao_operador(self, terminal, operador, token):
        return SessaoOperadorHub.objects.create(
            terminal=terminal,
            operador=operador,
            token_hash=SessaoOperadorHub.hash_token(token),
            token_prefixo=token,
            ativa=True,
            ultima_atividade_em=timezone.now(),
        )

    def abrir_caixa(self, caixa=None, terminal=None, operador=None, sessao_operador=None):
        caixa = caixa or self.caixa
        return SessaoCaixaHub.objects.create(
            caixa=caixa,
            status=SessaoCaixaHub.STATUS_ABERTO,
            chave_caixa_aberto=caixa.pk,
            valor_abertura=Decimal("100.00"),
            aberto_em=timezone.now(),
            terminal_abertura=terminal or self.terminal,
            operador_abertura=operador or self.operador,
            sessao_operador_abertura=sessao_operador or self.sessao_operador,
        )

    def criar_tipo_despesa(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_id": 101,
            "codigo": "LAN",
            "descricao": "Lanche",
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

    def post_movimentacao(self, payload):
        return self.client.post("/api/terminal/caixa/movimentacoes/", payload, format="json")

    def test_exige_terminal_e_operador(self):
        self.client.credentials()

        resposta = self.client.get("/api/terminal/caixa/movimentacoes/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_exige_caixa_aberto(self):
        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "10.00"})

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Caixa não está aberto.")

    def test_despesa_valida_e_criada(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa()

        resposta = self.post_movimentacao(
            {"tipo": "DESPESA", "valor": "25.90", "tipo_despesa_id": tipo.retaguarda_id, "documento": "NF-1"}
        )

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(MovimentacaoCaixaHub.objects.count(), 1)
        self.assertEqual(resposta.data["movimentacao"]["tipo"], "DESPESA")
        self.assertEqual(resposta.data["movimentacao"]["valor"], "25.90")

    def test_despesa_exige_tipo_despesa_id(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00"})

        self.assertEqual(resposta.status_code, 400)

    def test_tipo_despesa_deve_ser_do_mesmo_hub(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa(hub=self.outro_hub)

        resposta = self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": tipo.retaguarda_id})

        self.assertEqual(resposta.status_code, 400)

    def test_tipo_ausente_ou_inativo_nao_pode_ser_usado(self):
        self.abrir_caixa()
        ausente = self.criar_tipo_despesa(retaguarda_id=101, presente_retaguarda=False)
        inativo = self.criar_tipo_despesa(retaguarda_id=102, codigo="INA", ativo=False)

        self.assertEqual(self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": ausente.retaguarda_id}).status_code, 400)
        self.assertEqual(self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": inativo.retaguarda_id}).status_code, 400)

    def test_exige_documento_true_bloqueia_documento_vazio(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa(exige_documento=True)

        resposta = self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": tipo.retaguarda_id})

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Documento obrigatório para este tipo de despesa.")

    def test_exige_documento_false_permite_documento_vazio(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa(exige_documento=False)

        resposta = self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": tipo.retaguarda_id})

        self.assertEqual(resposta.status_code, 201)
        self.assertTrue(resposta.data["movimentacao"]["documento"].startswith("DESP-"))

    def test_snapshot_fica_persistido(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa(natureza_descricao="Natureza original")

        self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": tipo.retaguarda_id})

        movimento = MovimentacaoCaixaHub.objects.get()
        self.assertEqual(movimento.tipo_despesa_descricao, "Lanche")
        self.assertEqual(movimento.natureza_descricao, "Natureza original")

    def test_alteracao_posterior_tipo_nao_altera_snapshot_historico(self):
        self.abrir_caixa()
        tipo = self.criar_tipo_despesa(descricao="Original")
        self.post_movimentacao({"tipo": "DESPESA", "valor": "10.00", "tipo_despesa_id": tipo.retaguarda_id})

        tipo.descricao = "Alterado"
        tipo.save(update_fields=["descricao"])

        self.assertEqual(MovimentacaoCaixaHub.objects.get().tipo_despesa_descricao, "Original")

    def test_sangria_valida(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "100.00", "historico": "Retirada"})

        self.assertEqual(resposta.status_code, 201)
        self.assertIsNone(resposta.data["movimentacao"]["tipo_despesa"])

    def test_suprimento_valido(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SUPRIMENTO", "valor": "50.00"})

        self.assertEqual(resposta.status_code, 201)
        self.assertIsNone(resposta.data["movimentacao"]["tipo_despesa"])

    def test_sangria_nao_aceita_tipo_despesa_id(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "10.00", "tipo_despesa_id": 101})

        self.assertEqual(resposta.status_code, 400)

    def test_suprimento_nao_aceita_tipo_despesa_id(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SUPRIMENTO", "valor": "10.00", "tipo_despesa_id": 101})

        self.assertEqual(resposta.status_code, 400)

    def test_valor_zero_bloqueado(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "0.00"})

        self.assertEqual(resposta.status_code, 400)

    def test_valor_negativo_bloqueado(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "-10.00"})

        self.assertEqual(resposta.status_code, 400)

    def test_valor_com_mais_de_duas_casas_bloqueado(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": "1.999"})

        self.assertEqual(resposta.status_code, 400)

    def test_nan_infinity_e_float_bloqueados(self):
        self.abrir_caixa()

        for valor in ["NaN", "Infinity", 1.25]:
            with self.subTest(valor=valor):
                resposta = self.post_movimentacao({"tipo": "SANGRIA", "valor": valor})
                self.assertEqual(resposta.status_code, 400)

    def test_ids_enviados_nao_alteram_contexto_autenticado(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao(
            {
                "tipo": "SANGRIA",
                "valor": "10.00",
                "hub_id": self.outro_hub.pk,
                "caixa_id": self.outro_caixa.pk,
                "terminal_id": self.outro_terminal.pk,
                "operador_id": self.outro_operador.pk,
            }
        )

        movimento = MovimentacaoCaixaHub.objects.get()
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(movimento.hub, self.hub)
        self.assertEqual(movimento.caixa, self.caixa)
        self.assertEqual(movimento.terminal, self.terminal)
        self.assertEqual(movimento.operador, self.operador)

    def test_get_retorna_apenas_movimentos_da_sessao_aberta_corrente(self):
        sessao_atual = self.abrir_caixa()
        movimento = registrar_movimentacao_caixa(
            self.terminal,
            self.operador,
            self.sessao_operador,
            tipo="SANGRIA",
            valor="10.00",
        )
        MovimentacaoCaixaHub.objects.create(
            hub=self.outro_hub,
            caixa=self.outro_caixa,
            sessao_caixa=self.abrir_caixa(self.outro_caixa, self.outro_terminal, self.outro_operador, self.outro_sessao_operador),
            terminal=self.outro_terminal,
            operador=self.outro_operador,
            sessao_operador=self.outro_sessao_operador,
            tipo=MovimentacaoCaixaHub.TIPO_SANGRIA,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
            valor=Decimal("99.00"),
            documento="OUT",
            historico="Outro",
            ocorrido_em=timezone.now(),
        )

        resposta = self.client.get("/api/terminal/caixa/movimentacoes/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["sessao_caixa_uuid"], str(sessao_atual.sessao_uuid))
        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["movimentacoes"][0]["uuid"], str(movimento.movimento_uuid))

    def test_get_nao_vaza_movimentos_de_outro_caixa_ou_hub(self):
        self.abrir_caixa()
        registrar_movimentacao_caixa(self.terminal, self.operador, self.sessao_operador, tipo="SANGRIA", valor="10.00")

        resposta = self.client.get("/api/terminal/caixa/movimentacoes/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["total"], 1)

    def test_ordenacao_deterministica(self):
        self.abrir_caixa()
        primeiro = registrar_movimentacao_caixa(self.terminal, self.operador, self.sessao_operador, tipo="SANGRIA", valor="10.00")
        segundo = registrar_movimentacao_caixa(self.terminal, self.operador, self.sessao_operador, tipo="SUPRIMENTO", valor="20.00")

        resposta = self.client.get("/api/terminal/caixa/movimentacoes/")

        self.assertEqual(
            [item["uuid"] for item in resposta.data["movimentacoes"]],
            [str(primeiro.movimento_uuid), str(segundo.movimento_uuid)],
        )

    def test_documento_automatico_eh_gerado_quando_permitido_e_omitido(self):
        self.abrir_caixa()

        resposta = self.post_movimentacao({"tipo": "SUPRIMENTO", "valor": "20.00"})

        self.assertTrue(resposta.data["movimentacao"]["documento"].startswith("SUP-"))

    def test_movimento_uuid_eh_unico(self):
        self.abrir_caixa()
        self.post_movimentacao({"tipo": "SANGRIA", "valor": "10.00"})
        self.post_movimentacao({"tipo": "SUPRIMENTO", "valor": "20.00"})

        uuids = list(MovimentacaoCaixaHub.objects.values_list("movimento_uuid", flat=True))
        self.assertEqual(len(uuids), len(set(uuids)))

    def test_operacao_atomica_em_falha(self):
        self.abrir_caixa()

        with patch("core.services.movimentacoes_caixa.MovimentacaoCaixaHub.objects.create", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                registrar_movimentacao_caixa(
                    self.terminal,
                    self.operador,
                    self.sessao_operador,
                    tipo="SANGRIA",
                    valor="10.00",
                )

        self.assertEqual(MovimentacaoCaixaHub.objects.count(), 0)

    def test_servico_rejeita_float_sem_persistir(self):
        self.abrir_caixa()

        with self.assertRaises(MovimentacaoCaixaValidationError):
            registrar_movimentacao_caixa(
                self.terminal,
                self.operador,
                self.sessao_operador,
                tipo="SANGRIA",
                valor=1.25,
            )

        self.assertEqual(MovimentacaoCaixaHub.objects.count(), 0)
