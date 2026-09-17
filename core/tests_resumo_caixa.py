from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    CaixaHub,
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


class ResumoCaixaApiTests(TestCase):
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
        self.cartao = self.criar_forma(3, "CAR", "Cartao", "CARTAO")
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

    def criar_movimento(self, sessao, tipo, valor, ocorrido_em=None):
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
            ocorrido_em=ocorrido_em or timezone.now(),
        )

    def get_resumo(self):
        return self.client.get("/api/terminal/caixa/resumo/")

    def test_exige_terminal_e_operador(self):
        self.client.credentials()

        resposta = self.get_resumo()

        self.assertIn(resposta.status_code, (401, 403))

    def test_retorna_409_sem_caixa_aberto(self):
        resposta = self.get_resumo()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Caixa não está aberto.")

    def test_retorna_sessao_aberta_correta_e_valor_abertura(self):
        sessao = self.abrir_caixa(valor="125.50")

        resposta = self.get_resumo()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["sessao"]["uuid"], str(sessao.sessao_uuid))
        self.assertEqual(resposta.data["sessao"]["valor_abertura"], "125.50")
        self.assertEqual(resposta.data["dinheiro"]["valor_abertura"], "125.50")

    def test_vendas_abertas_canceladas_e_de_outra_sessao_sao_ignoradas(self):
        sessao = self.abrir_caixa()
        outra_sessao = self.abrir_caixa(self.outro_caixa, self.outro_terminal, self.outro_operador, self.outro_sessao_operador)
        venda_finalizada = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "100.00", "110.00", "10.00")
        self.criar_venda(sessao, VendaHub.STATUS_ABERTA, "50.00")
        self.criar_venda(sessao, VendaHub.STATUS_CANCELADA, "60.00")
        self.criar_venda(outra_sessao, VendaHub.STATUS_FINALIZADA, "999.00")
        self.criar_pagamento(venda_finalizada, self.dinheiro, "110.00")

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["vendas"]["quantidade"], 1)
        self.assertEqual(resposta.data["vendas"]["total"], "100.00")
        self.assertEqual(resposta.data["vendas"]["valor_recebido"], "110.00")
        self.assertEqual(resposta.data["vendas"]["troco"], "10.00")

    def test_pagamentos_ativos_sao_agrupados_e_removidos_ignorados(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "300.00", "300.00")
        self.criar_pagamento(venda, self.dinheiro, "100.00")
        self.criar_pagamento(venda, self.dinheiro, "50.00")
        self.criar_pagamento(venda, self.pix, "150.00")
        self.criar_pagamento(venda, self.pix, "999.00", status=VendaPagamentoHub.STATUS_REMOVIDO)

        resposta = self.get_resumo()

        formas = resposta.data["pagamentos"]["formas"]
        self.assertEqual(len(formas), 2)
        self.assertEqual(formas[0]["tipo"], "DINHEIRO")
        self.assertEqual(formas[0]["quantidade"], 2)
        self.assertEqual(formas[0]["valor"], "150.00")
        self.assertEqual(formas[1]["tipo"], "PIX")
        self.assertEqual(formas[1]["valor"], "150.00")

    def test_dinheiro_bruto_ignora_pix_cartao_e_subtrai_troco(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "150.00", "160.00", "10.00")
        self.criar_pagamento(venda, self.cartao, "100.00")
        self.criar_pagamento(venda, self.dinheiro, "60.00")

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["pagamentos"]["dinheiro_bruto"], "60.00")
        self.assertEqual(resposta.data["pagamentos"]["troco"], "10.00")
        self.assertEqual(resposta.data["pagamentos"]["dinheiro_liquido"], "50.00")
        self.assertEqual(resposta.data["vendas"]["total"], "150.00")

    def test_movimentacoes_efetivas_sao_somadas_por_tipo_e_ordenadas(self):
        sessao = self.abrir_caixa()
        segundo = self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "30.00", timezone.now())
        primeiro = self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_DESPESA, "10.00", segundo.ocorrido_em - timedelta(minutes=1))
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SANGRIA, "20.00")

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["movimentacoes"]["despesas"], {"quantidade": 1, "total": "10.00"})
        self.assertEqual(resposta.data["movimentacoes"]["sangrias"], {"quantidade": 1, "total": "20.00"})
        self.assertEqual(resposta.data["movimentacoes"]["suprimentos"], {"quantidade": 1, "total": "30.00"})
        self.assertEqual(
            [item["uuid"] for item in resposta.data["movimentacoes"]["itens"][:2]],
            [str(primeiro.movimento_uuid), str(segundo.movimento_uuid)],
        )

    def test_dinheiro_esperado_usa_formula_correta(self):
        sessao = self.abrir_caixa(valor="100.00")
        venda = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "199.90", "200.00", "0.10")
        self.criar_pagamento(venda, self.dinheiro, "200.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_DESPESA, "10.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SANGRIA, "20.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "30.00")

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["dinheiro"]["vendas_dinheiro_bruto"], "200.00")
        self.assertEqual(resposta.data["dinheiro"]["troco"], "0.10")
        self.assertEqual(resposta.data["dinheiro"]["vendas_dinheiro_liquido"], "199.90")
        self.assertEqual(resposta.data["dinheiro"]["esperado"], "399.90")

    def test_outro_hub_nao_vaza_dados(self):
        sessao = self.abrir_caixa()
        outra_sessao = self.abrir_caixa(self.outro_caixa, self.outro_terminal, self.outro_operador, self.outro_sessao_operador)
        self.criar_venda(outra_sessao, VendaHub.STATUS_FINALIZADA, "999.00")
        self.criar_movimento(outra_sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "999.00")

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["sessao"]["uuid"], str(sessao.sessao_uuid))
        self.assertEqual(resposta.data["vendas"]["total"], "0.00")
        self.assertEqual(resposta.data["movimentacoes"]["suprimentos"]["total"], "0.00")

    def test_valores_monetarios_sao_strings_com_duas_casas(self):
        self.abrir_caixa()

        resposta = self.get_resumo()

        self.assertEqual(resposta.data["vendas"]["total"], "0.00")
        self.assertEqual(resposta.data["pagamentos"]["dinheiro_bruto"], "0.00")
        self.assertEqual(resposta.data["dinheiro"]["esperado"], "100.00")

    def test_endpoint_nao_altera_registros(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, VendaHub.STATUS_FINALIZADA, "100.00")
        self.criar_pagamento(venda, self.dinheiro, "100.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_SUPRIMENTO, "30.00")
        contagens_antes = (
            SessaoCaixaHub.objects.count(),
            VendaHub.objects.count(),
            VendaPagamentoHub.objects.count(),
            MovimentacaoCaixaHub.objects.count(),
        )

        resposta = self.get_resumo()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            contagens_antes,
            (
                SessaoCaixaHub.objects.count(),
                VendaHub.objects.count(),
                VendaPagamentoHub.objects.count(),
                MovimentacaoCaixaHub.objects.count(),
            ),
        )
