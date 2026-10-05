from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import EventoSyncHub, HubConfig, OperadorHub, SessaoOperadorHub, Terminal, ValeTrocaHub, VendaDevolucaoHub


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
        self.evento(status=EventoSyncHub.STATUS_ERRO, ultimo_erro="timeout Central", payload={"nfce_uuid": str(uuid4()), "numero": 7, "serie": 1})
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

    def test_retry_reusa_worker_sem_zerar_tentativas_nem_idempotencia(self):
        evento = self.evento(status=EventoSyncHub.STATUS_ERRO, tentativas=3, ultimo_erro="timeout")

        with patch("core.services.pendencias_sync.sincronizar_eventos_pendentes", return_value={"enviados": 1}) as worker:
            resposta = self.client.post(f"/api/terminal/pendencias-sync/{evento.pk}/retry/")

        evento.refresh_from_db()
        self.assertEqual(resposta.status_code, 200, resposta.data)
        worker.assert_called_once_with(hub=self.hub, limite=1)
        self.assertEqual(evento.tentativas, 3)
        self.assertTrue(evento.chave_idempotencia.startswith("evt-"))

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
