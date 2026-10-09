from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from core.models import (
    CaixaHub,
    EventoSyncHub,
    FormaPagamentoHub,
    NFCeHub,
    OperadorHub,
    SessaoCaixaHub,
    SessaoOperadorHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
    VendaPagamentoParcelaHub,
)
from core.services.terminais import configurar_terminal
from core.tests_caixa import criar_hub
from core.tests_vendas import VendaHubTestMixin


class ConsultaVendasApiTests(VendaHubTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.forma = FormaPagamentoHub.objects.create(
            hub=self.hub,
            retaguarda_id=301,
            codigo="DIN",
            descricao="Dinheiro",
            tipo="DINHEIRO",
            num_parcelas=1,
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def criar_venda_consulta(self, **overrides):
        finalizada_em = overrides.pop("finalizada_em", timezone.now())
        venda = VendaHub.objects.create(
            hub=overrides.pop("hub", self.hub),
            sessao_caixa=overrides.pop("sessao_caixa", self.sessao_caixa),
            terminal=overrides.pop("terminal", self.terminal),
            status=overrides.pop("status", VendaHub.STATUS_FINALIZADA),
            operador_criacao=overrides.pop("operador_criacao", self.operador),
            sessao_operador_criacao=overrides.pop("sessao_operador_criacao", self.sessao_operador),
            finalizada_em=finalizada_em,
            terminal_finalizacao=overrides.pop("terminal_finalizacao", self.terminal),
            operador_finalizacao=overrides.pop("operador_finalizacao", self.operador),
            sessao_operador_finalizacao=overrides.pop("sessao_operador_finalizacao", self.sessao_operador),
            subtotal=overrides.pop("subtotal", Decimal("100.00")),
            desconto_geral=overrides.pop("desconto_geral", Decimal("0.00")),
            total=overrides.pop("total", Decimal("100.00")),
            valor_recebido=overrides.pop("valor_recebido", Decimal("100.00")),
            troco=overrides.pop("troco", Decimal("0.00")),
            documento=overrides.pop("documento", "VE0050000002"),
            cliente_nome=overrides.pop("cliente_nome", "Maria Silva"),
            cliente_documento=overrides.pop("cliente_documento", "12345678901"),
            cliente_uuid=overrides.pop("cliente_uuid", uuid4()),
            vendedor_retaguarda_id=overrides.pop("vendedor_retaguarda_id", 77),
            vendedor_matricula=overrides.pop("vendedor_matricula", "V077"),
            vendedor_nome=overrides.pop("vendedor_nome", "Ana Vendedora"),
            **overrides,
        )
        VendaItemHub.objects.create(
            venda=venda,
            catalogo_item=self.catalogo_item,
            retaguarda_produto_id=self.catalogo_item.retaguarda_produto_id,
            retaguarda_sku_id=self.catalogo_item.retaguarda_sku_id,
            ean13=self.catalogo_item.ean13,
            referencia=self.catalogo_item.referencia,
            codigo_item_ref=self.catalogo_item.codigo_item_ref,
            descricao=self.catalogo_item.descricao,
            cor_descricao=self.catalogo_item.cor_descricao,
            tamanho_descricao=self.catalogo_item.tamanho_descricao,
            quantidade=1,
            preco_unitario=Decimal("100.0000"),
            total_item=Decimal("100.00"),
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
            terminal_inclusao=self.terminal,
        )
        pagamento = VendaPagamentoHub.objects.create(
            operacao_uuid=uuid4(),
            venda=venda,
            forma_pagamento=self.forma,
            retaguarda_forma_pagamento_id=self.forma.retaguarda_id,
            codigo=self.forma.codigo,
            descricao=self.forma.descricao,
            tipo=self.forma.tipo,
            num_parcelas=1,
            valor=Decimal("100.00"),
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=self.terminal,
            operador_inclusao=self.operador,
            sessao_operador_inclusao=self.sessao_operador,
        )
        VendaPagamentoParcelaHub.objects.create(pagamento=pagamento, ordem=1, dias=0, percentual=Decimal("100.000000"))
        return venda

    def criar_nfce(self, venda, numero=123):
        return NFCeHub.objects.create(
            hub=venda.hub,
            venda=venda,
            ambiente="2",
            serie=1,
            numero=numero,
            codigo_numerico="12345678",
            digito_verificador="9",
            chave_acesso=f"{numero:044d}",
            protocolo=f"PROTO{numero}",
            status=NFCeHub.STATUS_AUTORIZADA,
            emitida_em=venda.finalizada_em,
            autorizada_em=venda.finalizada_em,
            sync_versao=1,
        )

    def criar_evento_venda(self, venda, status=EventoSyncHub.STATUS_SINCRONIZADO):
        return EventoSyncHub.objects.create(
            hub=venda.hub,
            chave_idempotencia=f"VENDA:{venda.venda_uuid}:FINALIZADA",
            tipo="VENDA_FINALIZADA",
            payload={"venda_uuid": str(venda.venda_uuid), "documento": venda.documento},
            status=status,
            ultimo_erro="timeout" if status == EventoSyncHub.STATUS_ERRO else "",
        )

    def test_listagem_filtra_por_data_documento_e_pagina_no_hub_autenticado(self):
        venda_hoje = self.criar_venda_consulta(documento="VE0050000002")
        self.criar_evento_venda(venda_hoje)
        self.criar_venda_consulta(documento="VE0050000001", finalizada_em=timezone.now() - timedelta(days=2))
        self.criar_venda_outro_hub()

        resposta_padrao = self.client.get("/api/terminal/vendas/")
        self.assertEqual(resposta_padrao.status_code, 200)
        self.assertEqual(resposta_padrao.data["count"], 1)
        self.assertEqual(resposta_padrao.data["results"][0]["documento"], "VE0050000002")
        self.assertEqual(resposta_padrao.data["results"][0]["sincronizacao"]["status"], EventoSyncHub.STATUS_SINCRONIZADO)

        data_ini = (timezone.localdate() - timedelta(days=3)).isoformat()
        data_fim = timezone.localdate().isoformat()
        resposta = self.client.get(
            f"/api/terminal/vendas/?data_ini={data_ini}&data_fim={data_fim}&documento=VE005&page=1&page_size=1"
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["count"], 2)
        self.assertEqual(resposta.data["page_size"], 1)
        self.assertEqual(resposta.data["total_pages"], 2)

    def test_filtros_cliente_forma_pagamento_nfce_e_isolamento_hub(self):
        venda = self.criar_venda_consulta(cliente_nome="Cliente Filtro", cliente_documento="99988877766")
        self.criar_nfce(venda, numero=321)
        self.criar_venda_outro_hub()

        resposta = self.client.get("/api/terminal/vendas/?cliente=999888&forma_pagamento=DIN&nfce=321")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["count"], 1)
        self.assertEqual(resposta.data["results"][0]["venda_uuid"], str(venda.venda_uuid))
        self.assertEqual(resposta.data["results"][0]["nfce"]["numero"], 321)

    def test_detalhe_retorna_itens_pagamentos_nfce_sync_e_404_outro_hub(self):
        venda = self.criar_venda_consulta()
        nfce = self.criar_nfce(venda)
        evento = self.criar_evento_venda(venda, status=EventoSyncHub.STATUS_ERRO)
        EventoSyncHub.objects.create(
            hub=self.hub,
            chave_idempotencia=f"NFCE:{nfce.nfce_uuid}:V:{nfce.sync_versao}",
            tipo="NFCE_ATUALIZADA",
            payload={"nfce_uuid": str(nfce.nfce_uuid)},
            status=EventoSyncHub.STATUS_PENDENTE,
        )
        outro_hub_venda = self.criar_venda_outro_hub()

        resposta = self.client.get(f"/api/terminal/vendas/{venda.venda_uuid}/")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["itens"][0]["descricao"], self.catalogo_item.descricao)
        self.assertEqual(resposta.data["pagamentos"][0]["parcelas"][0]["ordem"], 1)
        self.assertEqual(resposta.data["nfce"]["numero"], nfce.numero)
        self.assertEqual(resposta.data["sincronizacao"]["venda_finalizada"]["status"], evento.status)
        self.assertEqual(resposta.data["sincronizacao"]["nfce_atualizada"]["status"], EventoSyncHub.STATUS_PENDENTE)

        resposta_404 = self.client.get(f"/api/terminal/vendas/{outro_hub_venda.venda_uuid}/")
        self.assertEqual(resposta_404.status_code, 404)

    def test_data_invalida_retorna_400(self):
        resposta = self.client.get("/api/terminal/vendas/?data_ini=2026-10-10&data_fim=2026-10-09")
        self.assertEqual(resposta.status_code, 400)

    def criar_venda_outro_hub(self):
        outro_hub = criar_hub(retaguarda_hub_id=8, empresa_id=12, loja_id=42)
        outro_caixa = CaixaHub.objects.create(
            hub=outro_hub,
            retaguarda_id=30,
            codigo="CX-OUTRO",
            descricao="Caixa Outro",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        outro_terminal = configurar_terminal(outro_hub, "PDV-OUTRO", "PDV Outro", outro_caixa.retaguarda_id)
        outro_operador = OperadorHub.objects.create(
            hub=outro_hub,
            retaguarda_usuario_id=91,
            codigo="caixa.outro",
            nome="Outro Operador",
            tipo="Caixa",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        outra_sessao_operador = SessaoOperadorHub(
            terminal=outro_terminal,
            operador=outro_operador,
            ultima_atividade_em=timezone.now(),
        )
        outra_sessao_operador.gerar_token()
        outra_sessao_operador.save()
        outra_sessao = SessaoCaixaHub.objects.create(
            caixa=outro_caixa,
            valor_abertura=Decimal("10.00"),
            aberto_em=timezone.now(),
            terminal_abertura=outro_terminal,
            operador_abertura=outro_operador,
            sessao_operador_abertura=outra_sessao_operador,
        )
        return self.criar_venda_consulta(
            hub=outro_hub,
            sessao_caixa=outra_sessao,
            terminal=outro_terminal,
            terminal_finalizacao=outro_terminal,
            operador_criacao=outro_operador,
            sessao_operador_criacao=outra_sessao_operador,
            operador_finalizacao=outro_operador,
            sessao_operador_finalizacao=outra_sessao_operador,
            documento="VE0099999999",
        )
