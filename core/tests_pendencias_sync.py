from uuid import uuid4

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import CaixaHub, EventoSyncHub, HubConfig, NFCeHub, OperadorHub, SessaoCaixaHub, SessaoOperadorHub, Terminal, ValeTrocaHub, VendaDevolucaoHub, VendaHub
from core.services.pendencias_sync import tentar_reprocessar_evento


class PendenciasSyncApiTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            nome="Hub Loja 1",
            retaguarda_url="http://central.test",
            retaguarda_token="TOKEN-SECRETO",
            loja_id=1,
            loja_nome="Loja 1",
        )
        self.outro_hub = HubConfig.objects.create(nome="Outro Hub", retaguarda_url="http://central-2.test", loja_id=2)
        self.terminal = Terminal.objects.create(hub=self.hub, codigo="PDV-01", nome="PDV 01")
        self.terminal_token = "terminal-token"
        self.terminal.armazenar_token(self.terminal_token)
        self.terminal.save()
        self.operador = OperadorHub.objects.create(
            hub=self.hub,
            retaguarda_usuario_id=10,
            codigo="101",
            nome="Operadora",
            tipo="CAIXA",
            sincronizado_em=timezone.now(),
        )
        SessaoOperadorHub.objects.create(
            terminal=self.terminal,
            operador=self.operador,
            token_hash=SessaoOperadorHub.hash_token("sessao-token"),
            ultima_atividade_em=timezone.now(),
        )
        self.sessao_operador = SessaoOperadorHub.objects.get(token_hash=SessaoOperadorHub.hash_token("sessao-token"))
        self.client = APIClient()
        self.autenticar()

    def autenticar(self, terminal_token=None, operador_token="sessao-token"):
        headers = {"HTTP_AUTHORIZATION": f"Terminal {terminal_token or self.terminal_token}"}
        if operador_token:
            headers["HTTP_X_SYSVAR_OPERADOR_SESSION"] = operador_token
        self.client.credentials(**headers)

    def evento(self, **overrides):
        payload = overrides.pop("payload", {"venda_uuid": str(uuid4()), "documento": "Venda/NFC-e 6"})
        defaults = {
            "hub": self.hub,
            "tipo": "VENDA_FINALIZADA",
            "chave_idempotencia": f"evt-{uuid4()}",
            "payload": payload,
            "status": EventoSyncHub.STATUS_PENDENTE,
        }
        defaults.update(overrides)
        return EventoSyncHub.objects.create(**defaults)

    def test_exige_terminal_e_operador_autenticados(self):
        self.client.credentials()
        sem_terminal = self.client.get("/api/terminal/pendencias-sync/")
        self.autenticar(operador_token=None)
        sem_operador = self.client.get("/api/terminal/pendencias-sync/")

        self.assertEqual(sem_terminal.status_code, 403)
        self.assertEqual(sem_operador.status_code, 403)

    def test_lista_eventos_do_hub_com_resumo_filtros_busca_e_paginacao(self):
        self.evento(tipo="NFCE_ATUALIZADA", status=EventoSyncHub.STATUS_ERRO, ultimo_erro="timeout Central", payload={"nfce_uuid": str(uuid4()), "numero": 7, "serie": 1})
        self.evento(tipo="NFCE_ATUALIZADA", status=EventoSyncHub.STATUS_SINCRONIZADO, payload={"numero": 6, "serie": 1, "venda_uuid": str(uuid4())})
        EventoSyncHub.objects.create(
            hub=self.outro_hub,
            tipo="VENDA_FINALIZADA",
            chave_idempotencia="outro-hub",
            payload={"documento": "Venda vazada"},
            status=EventoSyncHub.STATUS_CONFLITO,
        )

        resposta = self.client.get("/api/terminal/pendencias-sync/?status=ERRO&q=timeout&page=1&page_size=1")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(resposta.data["paginacao"]["total"], 1)
        self.assertEqual(resposta.data["eventos"][0]["status"], EventoSyncHub.STATUS_ERRO)
        self.assertIn("NFC-e 7", resposta.data["eventos"][0]["documento"])
        self.assertEqual(resposta.data["resumo"]["erros"], 1)
        self.assertEqual(resposta.data["resumo"]["sincronizados"], 1)
        self.assertNotIn("Venda vazada", str(resposta.data))

    def test_serializa_dependencia_de_devolucao_e_bloqueia_retry(self):
        devolucao = VendaDevolucaoHub.objects.create(
            hub=self.hub,
            operador=self.operador,
            terminal=self.terminal,
            valor_total="10.00",
            finalizada_em=timezone.now(),
            venda_origem_documento="Cupom 9",
        )
        ValeTrocaHub.objects.create(
            hub=self.hub,
            devolucao=devolucao,
            documento="VT-PROV-1",
            valor_original="10.00",
            saldo="10.00",
            origem=ValeTrocaHub.ORIGEM_HUB_PROVISORIO,
            provisorio=True,
        )
        evento = self.evento(
            status=EventoSyncHub.STATUS_ERRO,
            payload={"venda_uuid": str(uuid4()), "dependencias": {"devolucoes_uuid": [str(devolucao.devolucao_uuid)]}},
        )

        resposta = self.client.get("/api/terminal/pendencias-sync/?status=AGUARDANDO_DEPENDENCIA")
        retry = self.client.post(f"/api/terminal/pendencias-sync/{evento.pk}/retry/")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(resposta.data["eventos"][0]["status_operacional"], "AGUARDANDO_DEPENDENCIA")
        self.assertTrue(resposta.data["eventos"][0]["bloqueado_por_dependencia"])
        self.assertEqual(retry.status_code, 409)

    def test_retry_processa_evento_selecionado_e_preserva_outro_elegivel(self):
        antigo = self.evento(status=EventoSyncHub.STATUS_ERRO, tentativas=2, ultimo_erro="timeout antigo", chave_idempotencia="evento-antigo")
        selecionado = self.evento(status=EventoSyncHub.STATUS_ERRO, tentativas=3, ultimo_erro="timeout selecionado", chave_idempotencia="evento-selecionado")

        class Client:
            eventos = []

            def sync_push(self, *, token, eventos):
                self.eventos = eventos
                return {"resultados": [{"chave_idempotencia": eventos[0]["chave_idempotencia"], "status": "PROCESSADO"}]}

        client = Client()
        visao, erro, resultado = tentar_reprocessar_evento(self.hub, selecionado.pk, client=client)

        antigo.refresh_from_db()
        selecionado.refresh_from_db()
        self.assertEqual(erro, "")
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(client.eventos[0]["chave_idempotencia"], "evento-selecionado")
        self.assertEqual(visao["status"], EventoSyncHub.STATUS_SINCRONIZADO)
        self.assertEqual(selecionado.tentativas, 4)
        self.assertEqual(selecionado.status, EventoSyncHub.STATUS_SINCRONIZADO)
        self.assertEqual(antigo.tentativas, 2)
        self.assertEqual(antigo.status, EventoSyncHub.STATUS_ERRO)
        self.assertEqual(antigo.ultimo_erro, "timeout antigo")

    def test_conflito_nao_permite_retry_e_payload_sanitiza_segredo(self):
        evento = self.evento(
            status=EventoSyncHub.STATUS_CONFLITO,
            payload={"documento": "Cupom Z", "senha": "segredo"},
            ultimo_erro="cliente divergente",
        )

        listagem = self.client.get("/api/terminal/pendencias-sync/")
        retry = self.client.post(f"/api/terminal/pendencias-sync/{evento.pk}/retry/")

        self.assertEqual(listagem.status_code, 200, listagem.data)
        self.assertEqual(listagem.data["eventos"][0]["payload_tecnico"]["senha"], "***")
        self.assertEqual(retry.status_code, 409)

    def test_dependencia_filtrada_antes_da_paginacao_e_priorizada(self):
        devolucao = VendaDevolucaoHub.objects.create(
            hub=self.hub,
            operador=self.operador,
            terminal=self.terminal,
            valor_total="10.00",
            finalizada_em=timezone.now(),
            venda_origem_documento="Cupom 10",
        )
        ValeTrocaHub.objects.create(
            hub=self.hub,
            devolucao=devolucao,
            documento="VT-PROV-10",
            valor_original="10.00",
            saldo="10.00",
            origem=ValeTrocaHub.ORIGEM_HUB_PROVISORIO,
            provisorio=True,
        )
        for idx in range(5):
            self.evento(status=EventoSyncHub.STATUS_ERRO, ultimo_erro=f"erro {idx}")
        dependente = self.evento(
            status=EventoSyncHub.STATUS_ERRO,
            payload={"venda_uuid": str(uuid4()), "dependencias": {"devolucoes_uuid": [str(devolucao.devolucao_uuid)]}},
        )

        resposta = self.client.get("/api/terminal/pendencias-sync/?status=AGUARDANDO_DEPENDENCIA&page=1&page_size=1")
        geral = self.client.get("/api/terminal/pendencias-sync/?page=1&page_size=10")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(resposta.data["paginacao"]["total"], 1)
        self.assertEqual(resposta.data["paginacao"]["pages"], 1)
        self.assertEqual(resposta.data["eventos"][0]["id"], dependente.pk)
        pos_conflito_erro = [evento["status_operacional"] for evento in geral.data["eventos"]]
        self.assertIn("AGUARDANDO_DEPENDENCIA", pos_conflito_erro)
        self.assertLess(pos_conflito_erro.index("AGUARDANDO_DEPENDENCIA"), pos_conflito_erro.index("PENDENTE") if "PENDENTE" in pos_conflito_erro else len(pos_conflito_erro))

    def test_venda_finalizada_deriva_documento_da_nfce_local_sem_payload_artificial(self):
        caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=99,
            codigo="CX-01",
            descricao="Caixa",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        sessao_caixa = SessaoCaixaHub.objects.create(
            caixa=caixa,
            valor_abertura="100.00",
            aberto_em=timezone.now(),
            terminal_abertura=self.terminal,
            operador_abertura=self.operador,
            sessao_operador_abertura=self.sessao_operador,
        )
        venda = VendaHub.objects.create(
            hub=self.hub,
            sessao_caixa=sessao_caixa,
            terminal=self.terminal,
            status=VendaHub.STATUS_FINALIZADA,
            operador_criacao=self.operador,
            sessao_operador_criacao=self.sessao_operador,
            finalizada_em=timezone.now(),
            terminal_finalizacao=self.terminal,
            operador_finalizacao=self.operador,
            sessao_operador_finalizacao=self.sessao_operador,
            total="25.00",
        )
        NFCeHub.objects.create(
            hub=self.hub,
            venda=venda,
            ambiente="HOMOLOGACAO",
            serie=3,
            numero=42,
            codigo_numerico="12345678",
            digito_verificador="9",
            chave_acesso="1" * 44,
            status=NFCeHub.STATUS_AUTORIZADA,
            emitida_em=timezone.now(),
        )
        self.evento(status=EventoSyncHub.STATUS_ERRO, payload={"venda_uuid": str(venda.venda_uuid)})

        resposta = self.client.get("/api/terminal/pendencias-sync/")

        self.assertEqual(resposta.status_code, 200, resposta.data)
        self.assertEqual(resposta.data["eventos"][0]["documento"], "NFC-e 42 · Série 3")
