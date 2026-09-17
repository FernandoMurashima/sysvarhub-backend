from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    CaixaHub,
    FechamentoDiaFormaHub,
    FechamentoDiaHub,
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


class FechamentoDiaApiTests(TestCase):
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
        self.caixa_2 = self.criar_caixa(self.hub, 30, "CX-02")
        self.outro_caixa = self.criar_caixa(self.outro_hub, 31, "CX-03")
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", self.caixa.retaguarda_id)
        self.terminal_2 = configurar_terminal(self.hub, "PDV-02", "PDV 02", self.caixa_2.retaguarda_id)
        self.outro_terminal = configurar_terminal(self.outro_hub, "PDV-03", "PDV 03", self.outro_caixa.retaguarda_id)
        self.token_terminal = self.terminal.gerar_token()
        self.terminal.save()
        self.operador = self.criar_operador(self.hub, 10, "001")
        self.outro_operador = self.criar_operador(self.outro_hub, 11, "002")
        self.sessao_operador, self.token_operador = self.criar_sessao_operador(self.terminal, self.operador)
        self.sessao_operador_2, _token_2 = self.criar_sessao_operador(self.terminal_2, self.operador)
        self.outro_sessao_operador, _outro_token = self.criar_sessao_operador(self.outro_terminal, self.outro_operador)
        self.dinheiro = self.criar_forma(1, "DIN", "Dinheiro", "DINHEIRO")
        self.pix = self.criar_forma(2, "PIX", "Pix", "PIX")
        self.credito = self.criar_forma(3, "CRE", "Visa Credito", "CREDITO", adquirente="VISA")
        self.credito_master = self.criar_forma(4, "MCR", "Master Credito", "CREDITO", adquirente="MASTER")
        self.debito = self.criar_forma(5, "DEB", "Debito", "DEBITO")
        self.voucher = self.criar_forma(6, "VOU", "Voucher", "VOUCHER")
        self.data_operacional = timezone.localdate()
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

    def criar_forma(self, retaguarda_id, codigo, descricao, tipo, adquirente=None):
        return FormaPagamentoHub.objects.create(
            hub=self.hub,
            retaguarda_id=retaguarda_id,
            codigo=codigo,
            descricao=descricao,
            tipo=tipo,
            num_parcelas=1,
            adquirente=adquirente,
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def abrir_caixa(self, caixa=None, terminal=None, operador=None, sessao_operador=None, status=SessaoCaixaHub.STATUS_FECHADO):
        caixa = caixa or self.caixa
        terminal = terminal or self.terminal
        operador = operador or self.operador
        sessao_operador = sessao_operador or self.sessao_operador
        return SessaoCaixaHub.objects.create(
            caixa=caixa,
            status=status,
            chave_caixa_aberto=caixa.pk if status == SessaoCaixaHub.STATUS_ABERTO else None,
            valor_abertura=Decimal("100.00"),
            aberto_em=timezone.now(),
            terminal_abertura=terminal,
            operador_abertura=operador,
            sessao_operador_abertura=sessao_operador,
            fechado_em=None if status == SessaoCaixaHub.STATUS_ABERTO else timezone.now(),
            terminal_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else terminal,
            operador_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else operador,
            sessao_operador_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else sessao_operador,
            valor_esperado_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else Decimal("100.00"),
            valor_contado_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else Decimal("100.00"),
            diferenca_fechamento=None if status == SessaoCaixaHub.STATUS_ABERTO else Decimal("0.00"),
            situacao_fechamento="" if status == SessaoCaixaHub.STATUS_ABERTO else SessaoCaixaHub.SITUACAO_OK,
        )

    def criar_venda(self, sessao, total, status=VendaHub.STATUS_FINALIZADA, finalizada_em=None, troco="0.00"):
        return VendaHub.objects.create(
            hub=sessao.caixa.hub,
            sessao_caixa=sessao,
            terminal=sessao.terminal_abertura,
            status=status,
            operador_criacao=sessao.operador_abertura,
            sessao_operador_criacao=sessao.sessao_operador_abertura,
            finalizada_em=finalizada_em if status == VendaHub.STATUS_FINALIZADA else None,
            terminal_finalizacao=sessao.terminal_abertura if status == VendaHub.STATUS_FINALIZADA else None,
            operador_finalizacao=sessao.operador_abertura if status == VendaHub.STATUS_FINALIZADA else None,
            sessao_operador_finalizacao=sessao.sessao_operador_abertura if status == VendaHub.STATUS_FINALIZADA else None,
            total=Decimal(total),
            valor_recebido=Decimal(total) + Decimal(troco),
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
            adquirente=forma.adquirente,
            valor=Decimal(valor),
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=status,
            terminal_inclusao=venda.terminal,
            operador_inclusao=venda.operador_criacao,
            sessao_operador_inclusao=venda.sessao_operador_criacao,
        )

    def criar_movimento(self, sessao, tipo, valor):
        return MovimentacaoCaixaHub.objects.create(
            hub=sessao.caixa.hub,
            caixa=sessao.caixa,
            sessao_caixa=sessao,
            terminal=sessao.terminal_abertura,
            operador=sessao.operador_abertura,
            sessao_operador=sessao.sessao_operador_abertura,
            tipo=tipo,
            status=MovimentacaoCaixaHub.STATUS_EFETIVA,
            valor=Decimal(valor),
            documento=f"DOC-{tipo}",
            historico=tipo,
            ocorrido_em=timezone.now(),
        )

    def get_previa(self, data=None):
        data = data or self.data_operacional.isoformat()
        return self.client.get(f"/api/terminal/fechamento-dia/?data={data}")

    def post_fechamento(self, formas=None, data=None):
        return self.client.post(
            "/api/terminal/fechamento-dia/",
            {
                "data_operacional": data or self.data_operacional.isoformat(),
                "formas_pagamento": formas if formas is not None else [],
                "observacao": "Conferido",
            },
            format="json",
        )

    def forma(self, tipo, valor):
        return {"tipo": tipo, "valor_conferido": valor}

    def test_previa_consolida_dinheiro_pix_credito_debito_venda_mista_e_troco(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "350.00", troco="10.00")
        self.criar_pagamento(venda, self.dinheiro, "110.00")
        self.criar_pagamento(venda, self.pix, "50.00")
        self.criar_pagamento(venda, self.credito, "100.00")
        self.criar_pagamento(venda, self.debito, "100.00")

        resposta = self.get_previa()

        self.assertEqual(resposta.status_code, 200)
        formas = {forma["tipo"]: forma for forma in resposta.data["formas_pagamento"]}
        self.assertEqual(formas["DINHEIRO"]["valor_sistema"], "100.00")
        self.assertEqual(formas["PIX"]["valor_sistema"], "50.00")
        self.assertEqual(formas["CREDITO"]["valor_sistema"], "100.00")
        self.assertEqual(formas["DEBITO"]["valor_sistema"], "100.00")
        self.assertEqual(resposta.data["vendas"]["total"], "350.00")

    def test_agrupa_por_tipo_preserva_detalhes_e_mais_de_uma_forma_do_mesmo_tipo(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "200.00")
        self.criar_pagamento(venda, self.credito, "120.00")
        self.criar_pagamento(venda, self.credito_master, "80.00")

        resposta = self.get_previa()

        credito = resposta.data["formas_pagamento"][0]
        self.assertEqual(credito["tipo"], "CREDITO")
        self.assertEqual(credito["quantidade"], 2)
        self.assertEqual(credito["valor_sistema"], "200.00")
        self.assertEqual(len(credito["detalhes"]), 2)
        self.assertEqual(credito["detalhes"][0]["adquirente"], "MASTER")
        self.assertIn("retaguarda_forma_pagamento_id", credito["detalhes"][0])

    def test_tipos_desconhecidos_sao_dinamicos_e_ordenados_apos_tipos_preferenciais(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "130.00")
        self.criar_pagamento(venda, self.voucher, "130.00")

        resposta = self.get_previa()

        self.assertEqual(resposta.data["formas_pagamento"][0]["tipo"], "VOUCHER")
        self.assertEqual(resposta.data["formas_pagamento"][0]["descricao"], "Voucher")

    def test_considera_somente_vendas_finalizadas_pagamentos_ativos_e_hub_atual(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        cancelada = self.criar_venda(sessao, "999.00", status=VendaHub.STATUS_CANCELADA)
        aberta = self.criar_venda(sessao, "999.00", status=VendaHub.STATUS_ABERTA)
        self.criar_pagamento(venda, self.pix, "100.00")
        self.criar_pagamento(venda, self.pix, "999.00", status=VendaPagamentoHub.STATUS_REMOVIDO)
        self.criar_pagamento(cancelada, self.pix, "999.00")
        self.criar_pagamento(aberta, self.pix, "999.00")
        outra_sessao = self.abrir_caixa(self.outro_caixa, self.outro_terminal, self.outro_operador, self.outro_sessao_operador)
        outra_venda = self.criar_venda(outra_sessao, "999.00")
        self.criar_pagamento(outra_venda, self.pix, "999.00")

        resposta = self.get_previa()

        self.assertEqual(resposta.data["vendas"]["quantidade"], 1)
        self.assertEqual(resposta.data["vendas"]["total"], "100.00")
        self.assertEqual(resposta.data["formas_pagamento"][0]["valor_sistema"], "100.00")

    @override_settings(USE_TZ=True, TIME_ZONE="America/Sao_Paulo")
    def test_data_operacional_usa_intervalo_local_timezone_aware(self):
        sessao = self.abrir_caixa()
        local = timezone.get_current_timezone()
        dentro = timezone.make_aware(datetime(2026, 9, 17, 23, 30), local)
        fora = timezone.make_aware(datetime(2026, 9, 18, 0, 30), local)
        venda_dentro = self.criar_venda(sessao, "100.00", finalizada_em=dentro)
        venda_fora = self.criar_venda(sessao, "200.00", finalizada_em=fora)
        self.criar_pagamento(venda_dentro, self.pix, "100.00")
        self.criar_pagamento(venda_fora, self.pix, "200.00")

        resposta = self.get_previa("2026-09-17")

        self.assertEqual(resposta.data["vendas"]["quantidade"], 1)
        self.assertEqual(resposta.data["vendas"]["total"], "100.00")

    def test_multiplos_terminais_caixas_da_mesma_loja_entram_no_fechamento(self):
        sessao_1 = self.abrir_caixa()
        sessao_2 = self.abrir_caixa(self.caixa_2, self.terminal_2, self.operador, self.sessao_operador_2)
        venda_1 = self.criar_venda(sessao_1, "100.00")
        venda_2 = self.criar_venda(sessao_2, "200.00")
        self.criar_pagamento(venda_1, self.pix, "100.00")
        self.criar_pagamento(venda_2, self.pix, "200.00")

        resposta = self.get_previa()

        self.assertEqual(resposta.data["vendas"]["quantidade"], 2)
        self.assertEqual(resposta.data["vendas"]["total"], "300.00")

    def test_previa_bloqueia_pode_fechar_com_caixa_ou_venda_aberta(self):
        sessao = self.abrir_caixa(status=SessaoCaixaHub.STATUS_ABERTO)
        self.criar_venda(sessao, "10.00", status=VendaHub.STATUS_ABERTA)

        resposta = self.get_previa()

        self.assertFalse(resposta.data["pode_fechar"])
        self.assertIn("Existe sessão de caixa aberta.", resposta.data["impedimentos"])
        self.assertIn("Existe venda em andamento.", resposta.data["impedimentos"])

    def test_post_bloqueia_caixa_aberto_e_venda_aberta(self):
        sessao = self.abrir_caixa(status=SessaoCaixaHub.STATUS_ABERTO)
        self.criar_venda(sessao, "10.00", status=VendaHub.STATUS_ABERTA)

        resposta = self.post_fechamento([])

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(FechamentoDiaHub.objects.count(), 0)

    def test_post_grava_ok_sobra_falta_e_divergencias_compensadas_continuam_divergente(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "200.00")
        self.criar_pagamento(venda, self.pix, "100.00")
        self.criar_pagamento(venda, self.dinheiro, "100.00")

        resposta = self.post_fechamento([self.forma("DINHEIRO", "110.00"), self.forma("PIX", "90.00")])
        fechamento = FechamentoDiaHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(fechamento.diferenca_total, Decimal("0.00"))
        self.assertEqual(fechamento.situacao, FechamentoDiaHub.SITUACAO_DIVERGENTE)
        formas = {forma.tipo: forma for forma in FechamentoDiaFormaHub.objects.all()}
        self.assertEqual(formas["DINHEIRO"].situacao, FechamentoDiaFormaHub.SITUACAO_SOBRA)
        self.assertEqual(formas["PIX"].situacao, FechamentoDiaFormaHub.SITUACAO_FALTA)

    def test_post_com_valores_iguais_fica_ok(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")

        resposta = self.post_fechamento([self.forma("PIX", "100.00")])

        self.assertEqual(resposta.data["fechamento"]["situacao"], "OK")

    def test_unicidade_por_hub_data_e_segundo_post_nao_duplica(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")
        primeira = self.post_fechamento([self.forma("PIX", "100.00")])
        segunda = self.post_fechamento([self.forma("PIX", "100.00")])

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(FechamentoDiaHub.objects.count(), 1)

    def test_get_informa_dia_ja_fechado_e_devolve_registro(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")
        self.post_fechamento([self.forma("PIX", "100.00")])

        resposta = self.get_previa()

        self.assertTrue(resposta.data["fechado"])
        self.assertIn("fechamento", resposta.data)

    def test_dia_sem_vendas_fecha_com_lista_vazia(self):
        resposta = self.post_fechamento([])

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["fechamento"]["quantidade_vendas"], 0)
        self.assertEqual(resposta.data["fechamento"]["formas_pagamento"], [])

    def test_post_exige_tipos_exatos_sem_duplicados_desconhecidos_ou_ausentes(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")

        cenarios = [
            [self.forma("PIX", "100.00"), self.forma("PIX", "100.00")],
            [self.forma("DINHEIRO", "100.00")],
            [],
        ]
        for payload in cenarios:
            with self.subTest(payload=payload):
                resposta = self.post_fechamento(payload)
                self.assertEqual(resposta.status_code, 400)
        self.assertEqual(FechamentoDiaHub.objects.count(), 0)

    def test_rejeita_float_valor_negativo_escala_errada_e_limite(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")

        for valor in (100.00, "-1.00", "100", "100.0", "100.001", "10000000000000000.00"):
            with self.subTest(valor=valor):
                resposta = self.post_fechamento([{"tipo": "PIX", "valor_conferido": valor}])
                self.assertEqual(resposta.status_code, 400)

    def test_inconsistencia_entre_pagamentos_e_vendas_bloqueia_fechamento(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "90.00")

        resposta = self.post_fechamento([self.forma("PIX", "90.00")])

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Pagamentos não reconciliam com o total das vendas finalizadas.")
        self.assertEqual(FechamentoDiaHub.objects.count(), 0)

    def test_snapshot_preserva_resumos_identificacoes_e_formas(self):
        sessao = self.abrir_caixa()
        venda = self.criar_venda(sessao, "100.00")
        self.criar_pagamento(venda, self.pix, "100.00")
        self.criar_movimento(sessao, MovimentacaoCaixaHub.TIPO_DESPESA, "10.00")

        resposta = self.post_fechamento([self.forma("PIX", "100.00")])
        fechamento = FechamentoDiaHub.objects.get()

        self.assertEqual(resposta.status_code, 200)
        snapshot = fechamento.resumo_snapshot
        self.assertEqual(snapshot["data_operacional"], self.data_operacional.isoformat())
        self.assertEqual(snapshot["formas_pagamento"][0]["detalhes"][0]["codigo"], "PIX")
        self.assertEqual(snapshot["movimentacoes"]["despesas"], "10.00")
        self.assertEqual(snapshot["operador_fechamento"]["codigo"], self.operador.codigo)
        self.assertEqual(snapshot["terminal_fechamento"]["codigo"], self.terminal.codigo)
        self.assertIn("fechado_em", snapshot)
