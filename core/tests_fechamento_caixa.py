from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    CaixaHub,
    ContextoVendaTerminalHub,
    FormaPagamentoHub,
    HubConfig,
    MovimentacaoCaixaHub,
    OperadorHub,
    SessaoCaixaHub,
    SessaoOperadorHub,
    VendaHub,
    VendaPagamentoHub,
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


class FechamentoCaixaApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = criar_hub()
        self.outro_hub = criar_hub(
            retaguarda_url="http://central-2.test",
            retaguarda_hub_id=8,
            empresa_id=12,
            loja_id=42,
        )
        self.caixa = self.criar_caixa(self.hub, 29, "CX-01")
        self.outro_caixa = self.criar_caixa(self.outro_hub, 30, "CX-02")
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", self.caixa.retaguarda_id)
        self.token_terminal = self.terminal.gerar_token()
        self.terminal.save()
        self.outro_terminal = configurar_terminal(self.outro_hub, "PDV-02", "PDV 02", self.outro_caixa.retaguarda_id)
        self.operador = self.criar_operador(self.hub, 10, "001")
        self.outro_operador = self.criar_operador(self.outro_hub, 11, "002")
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, self.operador)
        self.outro_sessao_operador, _token = self.criar_sessao_operador(self.outro_terminal, self.outro_operador)
        self.dinheiro = self.criar_forma(1, "DIN", "Dinheiro", "DINHEIRO")
        self.pix = self.criar_forma(2, "PIX", "Pix", "PIX")
        self.autenticar()

    def autenticar(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {self.token_terminal}",
            HTTP_X_SYSVAR_OPERADOR_SESSION=self.token_operador,
        )

    def criar_caixa(self, hub, retaguarda_id, codigo):
        return CaixaHub.objects.create(
            hub=hub,
            retaguarda_id=retaguarda_id,
            codigo=codigo,
            descricao=f"Caixa {codigo}",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def criar_operador(self, hub, usuario_id, codigo):
        return OperadorHub.objects.create(
            hub=hub,
            retaguarda_usuario_id=usuario_id,
            codigo=codigo,
            nome=f"Operador {codigo}",
            tipo="Caixa",
            perfil_retaguarda_id=5,
            perfil_nome="Operador de Caixa",
            credencial_hash=make_password("1234"),
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def criar_sessao_operador(self, terminal, operador):
        sessao = SessaoOperadorHub(terminal=terminal, operador=operador, ultima_atividade_em=timezone.now())
        token = sessao.gerar_token()
        sessao.save()
        return sessao, token

    def abrir_caixa(self, caixa=None, terminal=None, operador=None, sessao_operador=None, valor="100.00"):
        caixa = caixa or self.caixa
        return SessaoCaixaHub.objects.create(
            caixa=caixa,
            status=SessaoCaixaHub.STATUS_ABERTO,
            chave_caixa_aberto=caixa.pk,
            valor_abertura=Decimal(valor),
            aberto_em=timezone.now(),
            terminal_abertura=terminal or self.terminal,
            operador_abertura=operador or self.operador,
            sessao_operador_abertura=sessao_operador or self.sessao_operador,
        )

    def criar_forma(self, retaguarda_id, codigo, descricao, tipo):
        return FormaPagamentoHub.objects.create(
            hub=self.hub,
            retaguarda_id=retaguarda_id,
            codigo=codigo,
            descricao=descricao,
            tipo=tipo,
            num_parcelas=1,
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def criar_venda(self, sessao, status, total, valor_recebido=None, troco="0.00"):
        return VendaHub.objects.create(
            hub=sessao.caixa.hub,
            sessao_caixa=sessao,
            terminal=self.terminal if sessao.caixa.hub == self.hub else self.outro_terminal,
            status=status,
            operador_criacao=self.operador if sessao.caixa.hub == self.hub else self.outro_operador,
            sessao_operador_criacao=self.sessao_operador if sessao.caixa.hub == self.hub else self.outro_sessao_operador,
            total=Decimal(total),
            valor_recebido=Decimal(valor_recebido if valor_recebido is not None else total),
            troco=Decimal(troco),
        )

    def criar_pagamento(self, venda, forma, valor, status=VendaPagamentoHub.STATUS_ATIVO):
        return VendaPagamentoHub.objects.create(
            operacao_uuid=uuid4(),
            venda=venda,
            forma_pagamento=forma,
            retaguarda_forma_pagamento_id=forma.retaguarda_id,
            codigo=forma.codigo,
            descricao=forma.descricao,
            tipo=forma.tipo,
            num_parcelas=forma.num_parcelas,
            valor=Decimal(valor),
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=status,
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )

    def criar_movimento(self, sessao, tipo, valor):
        return MovimentacaoCaixaHub.objects.create(
            hub=sessao.caixa.hub,
            caixa=sessao.caixa,
            sessao_caixa=sessao,
            terminal=self.terminal if sessao.caixa.hub == self.hub else self.outro_terminal,
            operador=self.operador if sessao.caixa.hub == self.hub else self.outro_operador,
            sessao_operador=self.sessao_operador if sessao.caixa.hub == self.hub else self.outro_sessao_operador,
            tipo=tipo,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
            valor=Decimal(valor),
            documento=f"DOC-{tipo}",
            historico=tipo,
            ocorrido_em=timezone.now(),
        )

    def post_fechar(self, payload):
        return self.client.post("/api/terminal/caixa/fechar/", payload, format="json")

    def test_fechamento_exige_autenticacao_terminal_e_operador(self):
        self.client.credentials()

        resposta = self.post_fechar({"valor_contado": "100.00"})

        self.assertIn(resposta.status_code, (401, 403))

    def test_exige_valor_contado(self):
        self.abrir_caixa()

        resposta = self.post_fechar({})

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Valor contado inválido.")

    def test_rejeita_valores_invalidos_negativos_e_com_mais_de_duas_casas(self):
        self.abrir_caixa()

        for valor in ("", "abc", "1e2", "NaN", "Infinity", "-0.01", "10.001", 10, 10.0, True):
            with self.subTest(valor=valor):
                resposta = self.post_fechar({"valor_contado": valor})
                self.assertEqual(resposta.status_code, 400)
                self.assertEqual(resposta.data["detail"], "Valor contado inválido.")

    def test_aceita_zero_fecha_sessao_e_grava_auditoria(self):
        sessao = self.abrir_caixa(valor="0.00")

        resposta = self.post_fechar({"valor_contado": "0.00"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(sessao.status, SessaoCaixaHub.STATUS_FECHADO)
        self.assertIsNotNone(sessao.fechado_em)
        self.assertEqual(sessao.terminal_fechamento, self.terminal)
        self.assertEqual(sessao.operador_fechamento, self.operador)
        self.assertEqual(sessao.sessao_operador_fechamento, self.sessao_operador)
        self.assertEqual(sessao.valor_esperado_fechamento, Decimal("0.00"))
        self.assertEqual(sessao.valor_contado_fechamento, Decimal("0.00"))
        self.assertEqual(sessao.diferenca_fechamento, Decimal("0.00"))
        self.assertEqual(sessao.situacao_fechamento, SessaoCaixaHub.SITUACAO_OK)
        self.assertEqual(resposta.data["fechamento"]["situacao"], "OK")

    def test_diferenca_zero_positiva_e_negativa_define_situacao(self):
        cenarios = [
            ("249.90", "249.90", "0.00", "OK"),
            ("249.90", "250.00", "0.10", "SOBRA"),
            ("249.90", "240.00", "-9.90", "FALTA"),
        ]
        for esperado, contado, diferenca, situacao in cenarios:
            with self.subTest(situacao=situacao):
                sessao = self.abrir_caixa(valor=esperado)
                resposta = self.post_fechar({"valor_contado": contado})
                sessao.refresh_from_db()
                self.assertEqual(resposta.data["fechamento"]["diferenca"], diferenca)
                self.assertEqual(sessao.situacao_fechamento, situacao)

    def test_preserva_observacao_e_rejeita_excesso(self):
        sessao = self.abrir_caixa()

        resposta = self.post_fechar({"valor_contado": "100.00", "observacao": "Conferido pelo operador"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(sessao.observacao_fechamento, "Conferido pelo operador")

        self.abrir_caixa()
        resposta = self.post_fechar({"valor_contado": "100.00", "observacao": "x" * 501})
        self.assertEqual(resposta.status_code, 400)

    def test_bloqueia_venda_aberta_e_nao_fecha(self):
        sessao = self.abrir_caixa()
        self.criar_venda(sessao, VendaHub.STATUS_ABERTA, "10.00")

        resposta = self.post_fechar({"valor_contado": "100.00"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Existe venda em andamento neste caixa.")
        self.assertEqual(sessao.status, SessaoCaixaHub.STATUS_ABERTO)

    def test_resumo_considera_finalizadas_canceladas_pagamentos_removidos_troco_e_movimentos(self):
        sessao = self.abrir_caixa(valor="100.00")
        venda = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "199.90", "200.00", "0.10")
        self.criar_pagamento(venda, self.dinheiro, "200.00")
        self.criar_pagamento(venda, self.pix, "999.00", status=VendaPagamentoHub.STATUS_REMOVIDO)
        self.criar_venda(sessao, VendaHub.STATUS_CANCELADA, "500.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_DESPESA, "10.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SANGRIA, "20.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "30.00")

        resposta = self.post_fechar({"valor_contado": "399.90"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(sessao.valor_esperado_fechamento, Decimal("399.90"))
        self.assertEqual(sessao.resumo_fechamento["quantidade_vendas"], 1)
        self.assertEqual(sessao.resumo_fechamento["total_vendas"], "199.90")
        self.assertEqual(sessao.resumo_fechamento["troco"], "0.10")
        self.assertEqual(sessao.resumo_fechamento["dinheiro_bruto"], "200.00")
        self.assertEqual(sessao.resumo_fechamento["dinheiro_liquido"], "199.90")
        self.assertEqual(sessao.resumo_fechamento["despesas"], "10.00")
        self.assertEqual(sessao.resumo_fechamento["sangrias"], "20.00")
        self.assertEqual(sessao.resumo_fechamento["suprimentos"], "30.00")
        self.assertEqual(sessao.resumo_fechamento["dinheiro_esperado"], "399.90")

    def test_sessao_fechada_nao_e_sobrescrita_e_sem_aberta_retorna_409(self):
        sessao = self.abrir_caixa()
        primeira = self.post_fechar({"valor_contado": "100.00"})

        segunda = self.post_fechar({"valor_contado": "999.00"})
        sessao.refresh_from_db()

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(segunda.data["detail"], "Caixa não está aberto.")
        self.assertEqual(sessao.valor_contado_fechamento, Decimal("100.00"))

    def test_fechamento_limpa_chave_contexto_e_nao_cria_movimentacao_extra(self):
        sessao = self.abrir_caixa()
        ContextoVendaTerminalHub.objects.create(terminal=self.terminal)
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "30.00")

        resposta = self.post_fechar({"valor_contado": "130.00"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(sessao.chave_caixa_aberto)
        self.assertEqual(ContextoVendaTerminalHub.objects.count(), 0)
        self.assertEqual(MovimentacaoCaixaHub.objects.count(), 1)

    def test_f4_continua_funcionando_com_contrato_existente(self):
        sessao = self.abrir_caixa()
        self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "50.00")

        resposta = self.client.get("/api/terminal/caixa/resumo/")

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("vendas", resposta.data)
        self.assertIn("pagamentos", resposta.data)
        self.assertIn("movimentacoes", resposta.data)
        self.assertIn("dinheiro", resposta.data)

    def test_outro_hub_outra_sessao_nao_entra_no_fechamento(self):
        sessao = self.abrir_caixa()
        outra_sessao = self.abrir_caixa(self.outro_caixa, self.outro_terminal, self.outro_operador, self.outro_sessao_operador)
        self.criar_venda(outra_sessao, VendaHub.STATUS_FINALIZADA, "999.00")
        self.criar_movimento(outra_sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "999.00")

        resposta = self.post_fechar({"valor_contado": "100.00"})
        sessao.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(sessao.valor_esperado_fechamento, Decimal("100.00"))
        self.assertEqual(sessao.resumo_fechamento["total_vendas"], "0.00")
        self.assertEqual(sessao.resumo_fechamento["suprimentos"], "0.00")
