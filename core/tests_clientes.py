from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import CaixaHub, ClienteHub, HubConfig, OperadorHub, SessaoOperadorHub
from core.services.terminais import configurar_terminal
from integracao.services.clientes import sincronizar_clientes


CPF_VALIDO = "52998224725"
CNPJ_VALIDO = "11222333000181"


class ClienteLocalApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
            retaguarda_hub_id=7,
            clientes_versao=1,
            clientes_sincronizado_em=timezone.now(),
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

    def payload(self, **overrides):
        dados = {
            "tipo_pessoa": "PF",
            "documento": CPF_VALIDO,
            "nome_cliente": "Cliente Local",
        }
        dados.update(overrides)
        return dados

    def post_cliente(self, **overrides):
        return self.client.post("/api/terminal/clientes/", self.payload(**overrides), format="json")

    def criar_cliente(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_id": 123,
            "origem": ClienteHub.ORIGEM_RETAGUARDA,
            "presente_retaguarda": True,
            "tipo_pessoa": "PF",
            "documento": CPF_VALIDO,
            "cliente_padrao": False,
            "nome_cliente": "Cliente Retaguarda",
            "ativo": True,
            "bloqueio": False,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return ClienteHub.objects.create(**dados)

    def test_pf_valido_cria_cliente_local_pendente(self):
        resposta = self.post_cliente()
        cliente = ClienteHub.objects.get()

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(cliente.origem, ClienteHub.ORIGEM_LOCAL)
        self.assertIsNone(cliente.retaguarda_id)
        self.assertFalse(cliente.presente_retaguarda)
        self.assertIsNone(cliente.sincronizado_em)
        self.assertTrue(cliente.ativo)
        self.assertFalse(cliente.bloqueio)
        self.assertFalse(cliente.cliente_padrao)
        self.assertTrue(resposta.data["cliente"]["pendente_sincronizacao"])

    def test_cpf_com_mascara_armazena_digitos(self):
        resposta = self.post_cliente(documento="529.982.247-25")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(ClienteHub.objects.get().documento, CPF_VALIDO)

    def test_cpf_matematicamente_invalido_retorna_400(self):
        resposta = self.post_cliente(documento="52998224724")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "CPF inválido.")

    def test_cpf_com_digitos_iguais_retorna_400(self):
        resposta = self.post_cliente(documento="11111111111")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "CPF inválido.")

    def test_documento_reservado_ao_cliente_padrao_retorna_400(self):
        resposta = self.post_cliente(documento="00000000000")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Documento 00000000000 é reservado ao cliente padrão.")

    def test_pj_valido_cria_com_documento_normalizado(self):
        resposta = self.post_cliente(tipo_pessoa="pj", documento="11.222.333/0001-81")

        self.assertEqual(resposta.status_code, 201)
        cliente = ClienteHub.objects.get()
        self.assertEqual(cliente.tipo_pessoa, "PJ")
        self.assertEqual(cliente.documento, CNPJ_VALIDO)

    def test_cnpj_matematicamente_invalido_retorna_400(self):
        resposta = self.post_cliente(tipo_pessoa="PJ", documento="11222333000180")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "CNPJ inválido.")

    def test_pf_com_quatorze_digitos_retorna_400(self):
        resposta = self.post_cliente(documento=CNPJ_VALIDO)

        self.assertEqual(resposta.status_code, 400)

    def test_pj_com_onze_digitos_retorna_400(self):
        resposta = self.post_cliente(tipo_pessoa="PJ", documento=CPF_VALIDO)

        self.assertEqual(resposta.status_code, 400)

    def test_tipo_pessoa_invalido_retorna_400(self):
        resposta = self.post_cliente(tipo_pessoa="XX")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Tipo de pessoa inválido.")

    def test_nome_vazio_retorna_400(self):
        resposta = self.post_cliente(nome_cliente="  ")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Informe o nome do cliente.")

    def test_documento_existente_retaguarda_retorna_409(self):
        existente = self.criar_cliente()

        resposta = self.post_cliente()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["detail"], "Já existe um cliente com este CPF.")
        self.assertEqual(resposta.data["cliente_uuid"], str(existente.cliente_uuid))
        self.assertEqual(ClienteHub.objects.count(), 1)

    def test_documento_existente_local_retorna_409(self):
        self.criar_cliente(retaguarda_id=None, origem=ClienteHub.ORIGEM_LOCAL, presente_retaguarda=False, sincronizado_em=None)

        resposta = self.post_cliente()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(ClienteHub.objects.count(), 1)

    def test_integrity_error_concorrente_vira_409(self):
        existente = self.criar_cliente()

        with patch("core.services.clientes.ClienteHub.objects") as manager:
            manager.select_for_update.return_value.filter.return_value.first.return_value = None
            manager.filter.return_value.first.return_value = existente
            with patch.object(ClienteHub, "save", side_effect=IntegrityError("duplicado")):
                resposta = self.post_cliente()

        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.data["cliente_uuid"], str(existente.cliente_uuid))

    def test_duplicidade_respeita_hub_do_terminal_e_payload_nao_escolhe_hub(self):
        outro_hub = HubConfig.objects.create(retaguarda_url="http://central-b.test", empresa_id=22, loja_id=55, retaguarda_hub_id=8)
        self.criar_cliente(hub=outro_hub)

        resposta = self.post_cliente()

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(ClienteHub.objects.filter(hub=self.hub).count(), 1)
        self.assertEqual(ClienteHub.objects.filter(hub=outro_hub).count(), 1)

    def test_campos_protegidos_retorna_400(self):
        resposta = self.client.post(
            "/api/terminal/clientes/",
            self.payload(hub_id=999, origem=ClienteHub.ORIGEM_RETAGUARDA, ativo=False),
            format="json",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Campos protegidos não podem ser enviados.")

    def test_email_normalizado_lowercase(self):
        self.post_cliente(email=" CLIENTE@EXAMPLE.COM ")

        self.assertEqual(ClienteHub.objects.get().email, "cliente@example.com")

    def test_email_invalido_retorna_400(self):
        resposta = self.post_cliente(email="email-invalido")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "E-mail inválido.")

    def test_telefone_com_mascara_armazena_digitos(self):
        self.post_cliente(telefone1="(21) 99999-0000", telefone2="3333-4444")
        cliente = ClienteHub.objects.get()

        self.assertEqual(cliente.telefone1, "21999990000")
        self.assertEqual(cliente.telefone2, "33334444")

    def test_telefone_invalido_retorna_400(self):
        resposta = self.post_cliente(telefone1="1234567")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Telefone inválido.")

    def test_cep_com_mascara_armazena_oito_digitos(self):
        self.post_cliente(cep="20000-001")

        self.assertEqual(ClienteHub.objects.get().cep, "20000001")

    def test_uf_convertida_para_uppercase(self):
        self.post_cliente(estado="rj")

        self.assertEqual(ClienteHub.objects.get().estado, "RJ")

    def test_uf_invalida_retorna_400(self):
        resposta = self.post_cliente(estado="R1")

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Informe a UF com duas letras.")

    def test_aniversario_futuro_retorna_400(self):
        futuro = timezone.localdate().replace(year=timezone.localdate().year + 1).isoformat()

        resposta = self.post_cliente(aniversario=futuro)

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.data["detail"], "Aniversário não pode ser futuro.")

    def test_cliente_local_aparece_imediatamente_no_get(self):
        self.post_cliente(nome_cliente="Maria Local")

        resposta = self.client.get("/api/terminal/clientes/")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["clientes"][0]["nome_cliente"], "Maria Local")

    def test_busca_documento_telefone_e_email_encontra_cliente_local(self):
        self.post_cliente(email="local@example.com", telefone1="(21) 99999-0000")

        por_documento = self.client.get("/api/terminal/clientes/", {"q": "529.982.247-25"})
        por_telefone = self.client.get("/api/terminal/clientes/", {"q": "21 99999-0000"})
        por_email = self.client.get("/api/terminal/clientes/", {"q": "local@example.com"})

        self.assertEqual(por_documento.data["total"], 1)
        self.assertEqual(por_telefone.data["total"], 1)
        self.assertEqual(por_email.data["total"], 1)

    def test_cadastro_nao_chama_central(self):
        with patch("integracao.services.retaguarda.RetaguardaClient.clientes") as clientes:
            resposta = self.post_cliente()

        self.assertEqual(resposta.status_code, 201)
        clientes.assert_not_called()

    def test_serializacao_retaguarda_mostra_pendente_false(self):
        self.criar_cliente()

        resposta = self.client.get("/api/terminal/clientes/")

        self.assertFalse(resposta.data["clientes"][0]["pendente_sincronizacao"])

    def test_cliente_local_reconciliado_pela_central_preserva_uuid_e_origem(self):
        resposta = self.post_cliente(nome_cliente="Maria Local")
        cliente_uuid = resposta.data["cliente"]["cliente_uuid"]
        payload = resposta_clientes_retaguarda(self.hub, retaguarda_id=987, documento=CPF_VALIDO)

        resultado = sincronizar_clientes(self.hub, payload)

        cliente = ClienteHub.objects.get()
        self.assertEqual(resultado["clientes_reconciliados_por_documento"], 1)
        self.assertEqual(str(cliente.cliente_uuid), cliente_uuid)
        self.assertEqual(cliente.retaguarda_id, 987)
        self.assertTrue(cliente.presente_retaguarda)
        self.assertIsNotNone(cliente.sincronizado_em)
        self.assertEqual(cliente.origem, ClienteHub.ORIGEM_LOCAL)


def resposta_clientes_retaguarda(hub, *, retaguarda_id, documento):
    return {
        "clientes_versao": 1,
        "gerado_em": timezone.now().isoformat(),
        "hub": {"id": hub.retaguarda_hub_id, "hub_uuid": str(hub.hub_uuid)},
        "empresa": {"id": hub.empresa_id},
        "loja": {"id": hub.loja_id},
        "clientes": [
            {
                "id": retaguarda_id,
                "tipo_pessoa": "PF",
                "documento": documento,
                "cliente_padrao": False,
                "nome_cliente": "Maria Central",
                "apelido": "",
                "endereco": "",
                "numero": "",
                "complemento": "",
                "cep": "",
                "bairro": "",
                "cidade": "",
                "estado": "",
                "telefone1": "",
                "telefone2": "",
                "email": "",
                "categoria": "",
                "bloqueio": False,
                "motivo_bloqueio": None,
                "aniversario": None,
                "mala_direta": False,
                "aceita_email": False,
                "aceita_whatsapp": False,
                "aceita_sms": False,
                "consentimento_em": None,
                "origem_consentimento": "",
                "ativo": True,
            }
        ],
    }
