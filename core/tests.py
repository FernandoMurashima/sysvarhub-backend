from io import StringIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import resolve
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from core.models import (
    CaixaHub,
    CatalogoItemHub,
    ClienteHub,
    HubConfig,
    OperadorHub,
    PareamentoTerminal,
    SessaoOperadorHub,
    Terminal,
)
from core.api import ParearTerminalView
from core.services.terminais import (
    PareamentoTerminalError,
    TerminalValidationError,
    configurar_terminal,
    desativar_terminal,
    gerar_pareamento_terminal,
)

class HubConfigTests(TestCase):
    def test_preserva_hub_uuid_ao_salvar(self):
        hub = HubConfig.objects.create(retaguarda_url="http://central.test")
        original_uuid = hub.hub_uuid

        hub.nome = "Sysvar Hub Loja 1"
        hub.save()

        hub.refresh_from_db()
        self.assertEqual(hub.hub_uuid, original_uuid)

    def test_str_nao_expoe_token(self):
        hub = HubConfig.objects.create(
            nome="Sysvar Hub",
            loja_id=1,
            retaguarda_url="http://central.test",
            retaguarda_token="TOKEN-SECRETO",
        )

        self.assertNotIn("TOKEN-SECRETO", str(hub))


class TerminalServiceTests(TestCase):
    def setUp(self):
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

    def test_cria_terminal_sem_caixa(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")

        self.assertEqual(terminal.caixa_retaguarda_id, None)

    def test_cria_terminal_com_caixahub_ativo(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        self.assertEqual(terminal.caixa_retaguarda_id, 29)

    def test_rejeita_caixahub_inexistente(self):
        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 999)

    def test_rejeita_caixahub_inativo(self):
        self.caixa.ativo = False
        self.caixa.save()

        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

    def test_rejeita_caixahub_de_outro_hub(self):
        outro_hub = HubConfig.objects.create(retaguarda_url="http://central-2.test")
        CaixaHub.objects.create(
            hub=outro_hub,
            retaguarda_id=30,
            codigo="CX-OUTRA",
            ativo=True,
            sincronizado_em=timezone.now(),
        )

        with self.assertRaises(TerminalValidationError):
            configurar_terminal(self.hub, "PDV-01", "PDV 01", 30)

    def test_mesmo_hub_codigo_nao_duplica_terminal(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01")
        configurar_terminal(self.hub, "PDV-01", "PDV 01 atualizado")

        self.assertEqual(Terminal.objects.filter(hub=self.hub, codigo="PDV-01").count(), 1)

    def test_configuracao_existente_preserva_terminal_uuid(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        terminal_uuid = terminal.terminal_uuid

        atualizado = configurar_terminal(self.hub, "PDV-01", "PDV 01 atualizado")

        self.assertEqual(atualizado.terminal_uuid, terminal_uuid)

    def test_atualizacao_altera_nome_corretamente(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01")

        terminal = configurar_terminal(self.hub, "PDV-01", "PDV Principal")

        self.assertEqual(terminal.nome, "PDV Principal")

    def test_configuracao_existente_reativa_terminal_inativo(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01")
        terminal.ativo = False
        terminal.save()

        atualizado = configurar_terminal(self.hub, "PDV-01", "PDV 01")

        self.assertTrue(atualizado.ativo)

    def test_caixa_pode_ser_alterado_posteriormente(self):
        outra_caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=31,
            codigo="CX-02",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        terminal = configurar_terminal(
            self.hub,
            "PDV-01",
            "PDV 01",
            outra_caixa.retaguarda_id,
        )

        self.assertEqual(terminal.caixa_retaguarda_id, 31)

    def test_desativacao_nao_deleta_terminal(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(Terminal.objects.filter(hub=self.hub, codigo="PDV-01").count(), 1)

    def test_desativacao_preserva_uuid(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativado = desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(desativado.terminal_uuid, terminal.terminal_uuid)

    def test_desativacao_preserva_caixa_retaguarda_id(self):
        configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

        desativado = desativar_terminal(self.hub, "PDV-01")

        self.assertEqual(desativado.caixa_retaguarda_id, 29)

    def test_listagem_nao_altera_dados(self):
        terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        antes = Terminal.objects.get(pk=terminal.pk).atualizado_em
        out = StringIO()

        call_command("listar_terminais", stdout=out)

        terminal.refresh_from_db()
        self.assertEqual(terminal.atualizado_em, antes)

    def test_comando_nao_exibe_retaguarda_token(self):
        self.hub.retaguarda_token = "TOKEN-SECRETO"
        self.hub.save()
        out = StringIO()

        call_command(
            "configurar_terminal",
            "--codigo",
            "PDV-01",
            "--nome",
            "PDV 01",
            stdout=out,
        )

        self.assertNotIn("TOKEN-SECRETO", out.getvalue())

    def test_mais_de_um_hubconfig_gera_erro(self):
        HubConfig.objects.create(retaguarda_url="http://central-2.test")

        with self.assertRaises(CommandError):
            call_command(
                "configurar_terminal",
                "--codigo",
                "PDV-01",
                "--nome",
                "PDV 01",
                stdout=StringIO(),
            )

    def test_hub_inativo_impede_configuracao(self):
        self.hub.ativo = False
        self.hub.save()

        with self.assertRaises(CommandError):
            call_command(
                "configurar_terminal",
                "--codigo",
                "PDV-01",
                "--nome",
                "PDV 01",
                stdout=StringIO(),
            )

    def test_dois_terminais_diferentes_podem_usar_mesmo_caixahub(self):
        primeiro = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        segundo = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)

        self.assertEqual(primeiro.caixa_retaguarda_id, segundo.caixa_retaguarda_id)


class TerminalPareamentoApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
            empresa_nome="Sysvar Moda Comercio e Confeccoes Ltda",
            loja_nome="Loja Barra",
            loja_apelido="Barra",
            loja_estado="RJ",
            retaguarda_token="TOKEN-SECRETO",
            bootstrap_versao=1,
        )
        self.caixa = CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)

    def gerar_codigo(self, terminal=None):
        return gerar_pareamento_terminal(terminal or self.terminal)

    def parear(self, codigo, hostname="PDV-BARRA-01"):
        return self.client.post(
            "/api/terminal/parear/",
            {"codigo": codigo, "hostname": hostname},
            format="json",
            REMOTE_ADDR="10.0.0.10",
        )

    def autenticar_terminal(self):
        _pareamento, codigo = self.gerar_codigo()
        resposta = self.parear(codigo)
        token = resposta.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {token}")
        self.terminal.refresh_from_db()
        return token

    def test_codigo_de_pareamento_e_gerado(self):
        _pareamento, codigo = self.gerar_codigo()

        self.assertRegex(codigo, r"^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")

    def test_codigo_puro_nao_e_armazenado(self):
        pareamento, codigo = self.gerar_codigo()

        self.assertNotEqual(pareamento.codigo_hash, codigo)
        self.assertNotIn(codigo, pareamento.codigo_hash)

    def test_codigo_expira_em_15_minutos(self):
        pareamento, _codigo = self.gerar_codigo()

        delta = pareamento.expira_em - pareamento.criado_em
        self.assertGreater(delta.total_seconds(), 14 * 60)
        self.assertLessEqual(delta.total_seconds(), 15 * 60)

    def test_codigo_e_uso_unico(self):
        _pareamento, codigo = self.gerar_codigo()

        primeira = self.parear(codigo)
        segunda = self.parear(codigo)

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 400)

    def test_codigo_revogado_e_rejeitado(self):
        pareamento, codigo = self.gerar_codigo()
        pareamento.revogado_em = timezone.now()
        pareamento.save()

        resposta = self.parear(codigo)

        self.assertEqual(resposta.status_code, 400)

    def test_novo_codigo_revoga_anterior_nao_usado(self):
        primeiro, _codigo = self.gerar_codigo()

        self.gerar_codigo()

        primeiro.refresh_from_db()
        self.assertIsNotNone(primeiro.revogado_em)

    def test_terminal_inativo_nao_gera_pareamento(self):
        self.terminal.ativo = False
        self.terminal.save()

        with self.assertRaises(PareamentoTerminalError):
            self.gerar_codigo()

    def test_pareamento_valido_retorna_token(self):
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("token", resposta.data)

    def test_token_puro_nao_fica_no_banco(self):
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertNotEqual(self.terminal.token_hash, resposta.data["token"])
        self.assertNotIn(resposta.data["token"], self.terminal.token_hash)

    def test_pareamento_atualiza_pareado_em(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertIsNotNone(self.terminal.pareado_em)

    def test_pareamento_atualiza_hostname(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo, hostname="PDV-BARRA-99")
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.hostname, "PDV-BARRA-99")

    def test_pareamento_atualiza_ultimo_ip(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.ultimo_ip, "10.0.0.10")

    def test_pareamento_atualiza_ultima_conexao_em(self):
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        self.terminal.refresh_from_db()

        self.assertIsNotNone(self.terminal.ultima_conexao_em)

    def test_codigo_nao_permite_escolher_outro_terminal(self):
        outro = configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        _pareamento, codigo = self.gerar_codigo()

        self.parear(codigo)
        outro.refresh_from_db()

        self.assertEqual(outro.token_hash, "")

    def test_token_valido_autentica_contexto(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.status_code, 200)

    def test_token_invalido_e_rejeitado(self):
        self.client.credentials(HTTP_AUTHORIZATION="Terminal invalido")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_terminal_inativo_com_token_valido_e_rejeitado(self):
        self.autenticar_terminal()
        self.terminal.ativo = False
        self.terminal.save()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_contexto_retorna_apenas_terminal_autenticado(self):
        configurar_terminal(self.hub, "PDV-02", "PDV 02", 29)
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["terminal"]["codigo"], "PDV-01")

    def test_contexto_retorna_caixa_correto(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["caixa"]["id"], 29)
        self.assertEqual(resposta.data["caixa"]["codigo"], "CX-BARRA")

    def test_contexto_sem_caixa_retorna_null(self):
        terminal_sem_caixa = configurar_terminal(self.hub, "BALCAO-01", "Balcao 01")
        _pareamento, codigo = self.gerar_codigo(terminal_sem_caixa)
        resposta_pareamento = self.parear(codigo)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {resposta_pareamento.data['token']}"
        )

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIsNone(resposta.data["caixa"])

    def test_contexto_retorna_loja_empresa_do_hub(self):
        self.autenticar_terminal()

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertEqual(resposta.data["loja"]["id"], 41)
        self.assertEqual(resposta.data["empresa"]["id"], 11)

    def test_heartbeat_atualiza_ultima_conexao_em(self):
        self.autenticar_terminal()
        anterior = self.terminal.ultima_conexao_em

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertGreaterEqual(self.terminal.ultima_conexao_em, anterior)

    def test_heartbeat_atualiza_ultimo_ip(self):
        self.autenticar_terminal()

        self.client.post(
            "/api/terminal/heartbeat/",
            {"hostname": "PDV-BARRA-01"},
            REMOTE_ADDR="10.0.0.11",
        )
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.ultimo_ip, "10.0.0.11")

    def test_heartbeat_nao_altera_terminal_uuid(self):
        self.autenticar_terminal()
        terminal_uuid = self.terminal.terminal_uuid

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.terminal_uuid, terminal_uuid)

    def test_heartbeat_nao_altera_caixa(self):
        self.autenticar_terminal()

        self.client.post("/api/terminal/heartbeat/", {"hostname": "PDV-BARRA-01"})
        self.terminal.refresh_from_db()

        self.assertEqual(self.terminal.caixa_retaguarda_id, 29)

    def test_repareamento_rotaciona_token(self):
        token_antigo = self.autenticar_terminal()
        self.client.credentials()
        _pareamento, codigo = self.gerar_codigo()

        resposta = self.parear(codigo)

        self.assertNotEqual(resposta.data["token"], token_antigo)

    def test_token_anterior_falha_apos_repareamento(self):
        token_antigo = self.autenticar_terminal()
        self.client.credentials()
        _pareamento, codigo = self.gerar_codigo()
        self.parear(codigo)
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {token_antigo}")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))

    def test_nenhum_endpoint_retorna_retaguarda_token(self):
        _pareamento, codigo = self.gerar_codigo()
        resposta_pareamento = self.parear(codigo)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Terminal {resposta_pareamento.data['token']}"
        )
        contexto = self.client.get("/api/terminal/contexto/")
        heartbeat = self.client.post("/api/terminal/heartbeat/", {})

        conteudo = f"{resposta_pareamento.data}{contexto.data}{heartbeat.data}"
        self.assertNotIn("TOKEN-SECRETO", conteudo)
        self.assertNotIn("retaguarda_token", conteudo)

    def test_endpoint_de_pareamento_possui_throttle(self):
        view = resolve("/api/terminal/parear/").func.view_class

        self.assertIs(view, ParearTerminalView)
        self.assertIn(ScopedRateThrottle, view.throttle_classes)
        self.assertEqual(view.throttle_scope, "terminal_pareamento")

    def test_authorization_hub_nao_autentica_como_terminal(self):
        self.client.credentials(HTTP_AUTHORIZATION="Hub TOKEN-SECRETO")

        resposta = self.client.get("/api/terminal/contexto/")

        self.assertIn(resposta.status_code, (401, 403))


class TerminalCatalogoApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
            empresa_nome="Sysvar Moda Comercio e Confeccoes Ltda",
            loja_nome="Loja Barra",
            loja_apelido="Barra",
            loja_estado="RJ",
            catalogo_versao=1,
            catalogo_sincronizado_em=timezone.now(),
            tabela_preco_codigo="PADRAO",
            tabela_preco_nome="Tabela Padrão",
        )
        CaixaHub.objects.create(
            hub=self.hub,
            retaguarda_id=29,
            codigo="CX-BARRA",
            descricao="Caixa Loja Barra",
            ativo=True,
            sincronizado_em=timezone.now(),
        )
        self.terminal = configurar_terminal(self.hub, "PDV-01", "PDV 01", 29)
        self.token = self.terminal.gerar_token()
        self.terminal.save()
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {self.token}")

    def criar_item(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_produto_id": 181,
            "retaguarda_sku_id": 10825,
            "tipo_produto": "1",
            "referencia": "27-01-01001",
            "descricao": "Calça Jeans Reta Aurora",
            "descricao_reduzida": "Calça Jeans",
            "ean13": "7892701000013",
            "codigo_item_ref": "00001",
            "cor_retaguarda_id": 7,
            "cor_descricao": "Azul",
            "tamanho_retaguarda_id": 3,
            "tamanho_descricao": "M",
            "unidade_retaguarda_id": 1,
            "unidade_codigo": "UN",
            "unidade_descricao": "Unidade",
            "preco": "199.9000",
            "preco_promocional": None,
            "preco_venda": "199.9000",
            "estoque_fisico": "4.000",
            "reserva": "0.000",
            "estoque_disponivel": "4.000",
            "vendavel": True,
            "motivos_bloqueio": [],
            "fiscal": {"ncm": "62034200"},
            "ativo": True,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return CatalogoItemHub.objects.create(**dados)

    def get_catalogo(self, **params):
        return self.client.get("/api/terminal/catalogo/", params)

    def test_terminal_autenticado_acessa_catalogo(self):
        self.criar_item()

        resposta = self.get_catalogo()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(len(resposta.data["itens"]), 1)

    def test_catalogo_retorna_imagem_url_local_quando_cache_existe(self):
        self.criar_item(imagem_versao="v1", imagem_local="catalogo-imagens/produto-181/foto.bin")

        resposta = self.get_catalogo()

        self.assertEqual(
            resposta.data["itens"][0]["imagem_url"],
            "/api/terminal/catalogo/imagens/181/v1/",
        )

    def test_catalogo_sem_foto_retorna_imagem_url_null(self):
        self.criar_item()

        resposta = self.get_catalogo()

        self.assertIsNone(resposta.data["itens"][0]["imagem_url"])

    def test_endpoint_local_entrega_imagem_cacheada(self):
        with tempfile.TemporaryDirectory() as tmp, patch("core.api.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            caminho = Path(tmp) / "catalogo-imagens/produto-181/foto.jpg"
            caminho.parent.mkdir(parents=True)
            caminho.write_bytes(b"foto-local")
            self.criar_item(imagem_versao="v1", imagem_local="catalogo-imagens/produto-181/foto.jpg")

            resposta = self.client.get("/api/terminal/catalogo/imagens/181/v1/")

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/jpeg")
            self.assertEqual(b"".join(resposta.streaming_content), b"foto-local")

    def test_endpoint_local_entrega_png_com_content_type_correto(self):
        with tempfile.TemporaryDirectory() as tmp, patch("core.api.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            caminho = Path(tmp) / "catalogo-imagens/produto-181/foto.png"
            caminho.parent.mkdir(parents=True)
            caminho.write_bytes(b"png")
            self.criar_item(imagem_versao="v1", imagem_local="catalogo-imagens/produto-181/foto.png")

            resposta = self.client.get("/api/terminal/catalogo/imagens/181/v1/")

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/png")
            self.assertEqual(b"".join(resposta.streaming_content), b"png")

    def test_endpoint_local_entrega_webp_com_content_type_correto(self):
        with tempfile.TemporaryDirectory() as tmp, patch("core.api.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            caminho = Path(tmp) / "catalogo-imagens/produto-181/foto.webp"
            caminho.parent.mkdir(parents=True)
            caminho.write_bytes(b"webp")
            self.criar_item(imagem_versao="v1", imagem_local="catalogo-imagens/produto-181/foto.webp")

            resposta = self.client.get("/api/terminal/catalogo/imagens/181/v1/")

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/webp")
            self.assertEqual(b"".join(resposta.streaming_content), b"webp")

    def test_endpoint_local_nao_permite_path_traversal_por_registro(self):
        with tempfile.TemporaryDirectory() as tmp, patch("core.api.settings.SYSVARHUB_DATA_DIR", Path(tmp)):
            self.criar_item(imagem_versao="v1", imagem_local="../segredo.bin")

            resposta = self.client.get("/api/terminal/catalogo/imagens/181/v1/")

            self.assertEqual(resposta.status_code, 404)

    def test_sem_token_e_rejeitado(self):
        self.client.credentials()

        resposta = self.get_catalogo()

        self.assertIn(resposta.status_code, (401, 403))

    def test_token_invalido_e_rejeitado(self):
        self.client.credentials(HTTP_AUTHORIZATION="Terminal invalido")

        resposta = self.get_catalogo()

        self.assertIn(resposta.status_code, (401, 403))

    def test_terminal_inativo_e_rejeitado(self):
        self.terminal.ativo = False
        self.terminal.save()

        resposta = self.get_catalogo()

        self.assertIn(resposta.status_code, (401, 403))

    def test_retorna_somente_itens_ativos(self):
        self.criar_item(retaguarda_sku_id=1, ativo=True)
        self.criar_item(retaguarda_sku_id=2, ativo=False)

        resposta = self.get_catalogo()

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["itens"][0]["sku_id"], 1)

    def test_nao_esconde_item_nao_vendavel(self):
        self.criar_item(
            vendavel=False,
            motivos_bloqueio=["SEM_ESTOQUE"],
            estoque_fisico="0.000",
            estoque_disponivel="0.000",
        )

        item = self.get_catalogo().data["itens"][0]

        self.assertFalse(item["vendavel"])
        self.assertEqual(item["motivos_bloqueio"], ["SEM_ESTOQUE"])

    def test_busca_ean_exato(self):
        self.criar_item()

        resposta = self.get_catalogo(q="7892701000013")

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["itens"][0]["ean13"], "7892701000013")

    def test_busca_referencia_exata(self):
        self.criar_item()

        resposta = self.get_catalogo(q="27-01-01001")

        self.assertEqual(resposta.data["itens"][0]["referencia"], "27-01-01001")

    def test_busca_referencia_parcial(self):
        self.criar_item()

        resposta = self.get_catalogo(q="01001")

        self.assertEqual(resposta.data["total"], 1)

    def test_busca_descricao_parcial_case_insensitive(self):
        self.criar_item()

        resposta = self.get_catalogo(q="jeans reta")

        self.assertEqual(resposta.data["total"], 1)

    def test_busca_codigo_item_ref(self):
        self.criar_item()

        resposta = self.get_catalogo(q="00001")

        self.assertEqual(resposta.data["itens"][0]["codigo_item_ref"], "00001")

    def test_busca_sem_q_funciona(self):
        self.criar_item()

        resposta = self.get_catalogo()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data["q"], "")

    def test_limit_default_e_40(self):
        resposta = self.get_catalogo(limit="invalido")

        self.assertEqual(resposta.data["limit"], 40)

    def test_limit_maximo_e_100(self):
        resposta = self.get_catalogo(limit=150)

        self.assertEqual(resposta.data["limit"], 100)

    def test_limit_invalido_ou_menor_igual_zero_usa_default(self):
        resposta = self.get_catalogo(limit=0)

        self.assertEqual(resposta.data["limit"], 40)

    def test_total_representa_total_antes_do_limit(self):
        for indice in range(45):
            self.criar_item(
                retaguarda_produto_id=indice + 1,
                retaguarda_sku_id=indice + 1,
                descricao=f"Produto Total {indice:02d}",
                referencia=f"REF-{indice:02d}",
                ean13=str(7892701000000 + indice),
                codigo_item_ref=f"IT-{indice:02d}",
            )

        resposta = self.get_catalogo(q="Produto Total", limit=10)

        self.assertEqual(resposta.data["total"], 45)
        self.assertEqual(len(resposta.data["itens"]), 10)

    def test_sku_sem_ean_retorna_null(self):
        self.criar_item(ean13="")

        item = self.get_catalogo().data["itens"][0]

        self.assertIsNone(item["ean13"])

    def test_preco_e_retornado(self):
        self.criar_item(preco="199.9000", preco_promocional="179.9000", preco_venda="179.9000")

        item = self.get_catalogo().data["itens"][0]

        self.assertEqual(item["preco"], "199.9000")
        self.assertEqual(item["preco_promocional"], "179.9000")
        self.assertEqual(item["preco_venda"], "179.9000")

    def test_estoque_fisico_e_retornado(self):
        self.criar_item(estoque_fisico="4.000")

        self.assertEqual(self.get_catalogo().data["itens"][0]["estoque_fisico"], "4.000")

    def test_reserva_e_retornada(self):
        self.criar_item(reserva="1.000")

        self.assertEqual(self.get_catalogo().data["itens"][0]["reserva"], "1.000")

    def test_estoque_disponivel_e_retornado(self):
        self.criar_item(estoque_disponivel="3.000")

        self.assertEqual(self.get_catalogo().data["itens"][0]["estoque_disponivel"], "3.000")

    def test_vendavel_e_retornado(self):
        self.criar_item(vendavel=True)

        self.assertTrue(self.get_catalogo().data["itens"][0]["vendavel"])

    def test_motivos_bloqueio_sao_retornados(self):
        self.criar_item(vendavel=False, motivos_bloqueio=["SEM_PRECO"])

        self.assertEqual(self.get_catalogo().data["itens"][0]["motivos_bloqueio"], ["SEM_PRECO"])

    def test_fiscal_e_retornado(self):
        self.criar_item(fiscal={"ncm": "62034200", "origem": "0"})

        self.assertEqual(self.get_catalogo().data["itens"][0]["fiscal"]["ncm"], "62034200")

    def test_produto_id_vem_de_retaguarda_produto_id(self):
        self.criar_item(retaguarda_produto_id=181)

        self.assertEqual(self.get_catalogo().data["itens"][0]["produto_id"], 181)

    def test_sku_id_vem_de_retaguarda_sku_id(self):
        self.criar_item(retaguarda_sku_id=10825)

        self.assertEqual(self.get_catalogo().data["itens"][0]["sku_id"], 10825)

    def test_cor_e_mapeada(self):
        self.criar_item(cor_retaguarda_id=7, cor_descricao="Azul")

        self.assertEqual(self.get_catalogo().data["itens"][0]["cor"], {"id": 7, "descricao": "Azul"})

    def test_tamanho_e_mapeado(self):
        self.criar_item(tamanho_retaguarda_id=3, tamanho_descricao="M")

        self.assertEqual(self.get_catalogo().data["itens"][0]["tamanho"], {"id": 3, "descricao": "M"})

    def test_unidade_e_mapeada(self):
        self.criar_item(unidade_retaguarda_id=1, unidade_codigo="UN", unidade_descricao="Unidade")

        self.assertEqual(
            self.get_catalogo().data["itens"][0]["unidade"],
            {"id": 1, "codigo": "UN", "descricao": "Unidade"},
        )

    def test_catalogo_versao_e_retornado(self):
        resposta = self.get_catalogo()

        self.assertEqual(resposta.data["catalogo_versao"], 1)

    def test_tabela_padrao_e_retornada(self):
        resposta = self.get_catalogo()

        self.assertEqual(resposta.data["tabela_preco"], {"codigo": "PADRAO", "nome": "Tabela Padrão"})

    def test_hub_a_nao_recebe_catalogo_hub_b(self):
        hub_b = HubConfig.objects.create(retaguarda_url="http://central-b.test", empresa_id=22, loja_id=55)
        self.criar_item(retaguarda_sku_id=1, descricao="Item Hub A")
        self.criar_item(hub=hub_b, retaguarda_sku_id=2, descricao="Item Hub B")

        resposta = self.get_catalogo()

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["itens"][0]["descricao"], "Item Hub A")

    def test_query_hub_id_nao_muda_escopo(self):
        hub_b = HubConfig.objects.create(retaguarda_url="http://central-b.test", empresa_id=22, loja_id=55)
        self.criar_item(retaguarda_sku_id=1, descricao="Item Hub A")
        self.criar_item(hub=hub_b, retaguarda_sku_id=2, descricao="Item Hub B")

        resposta = self.get_catalogo(hub_id=hub_b.id)

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["itens"][0]["descricao"], "Item Hub A")

    def test_query_loja_id_nao_muda_escopo(self):
        hub_b = HubConfig.objects.create(retaguarda_url="http://central-b.test", empresa_id=22, loja_id=55)
        self.criar_item(retaguarda_sku_id=1, descricao="Item Hub A")
        self.criar_item(hub=hub_b, retaguarda_sku_id=2, descricao="Item Hub B")

        resposta = self.get_catalogo(loja_id=hub_b.loja_id)

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["itens"][0]["descricao"], "Item Hub A")

    def test_ean_exato_recebe_prioridade_sobre_busca_textual(self):
        self.criar_item(
            retaguarda_sku_id=1,
            ean13="111",
            descricao="Produto 7892701000013 textual",
            referencia="REF-TEXT",
            codigo_item_ref="TEXT",
        )
        self.criar_item(
            retaguarda_sku_id=2,
            ean13="7892701000013",
            descricao="Produto EAN Exato",
            referencia="REF-EAN",
            codigo_item_ref="EAN",
        )

        resposta = self.get_catalogo(q="7892701000013")

        self.assertEqual(resposta.data["itens"][0]["sku_id"], 2)

    def test_endpoint_nao_chama_retaguardaclient_central(self):
        self.criar_item()

        with patch("integracao.services.retaguarda.RetaguardaClient.catalogo") as catalogo:
            resposta = self.get_catalogo()

        self.assertEqual(resposta.status_code, 200)
        catalogo.assert_not_called()


class TerminalClientesApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hub = HubConfig.objects.create(
            retaguarda_url="http://central.test",
            empresa_id=11,
            loja_id=41,
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

    def criar_cliente(self, hub=None, **overrides):
        dados = {
            "hub": hub or self.hub,
            "retaguarda_id": 123,
            "origem": ClienteHub.ORIGEM_RETAGUARDA,
            "presente_retaguarda": True,
            "tipo_pessoa": "PF",
            "documento": "12345678901",
            "cliente_padrao": False,
            "nome_cliente": "Cliente Teste",
            "apelido": "Teste",
            "telefone1": "21999990000",
            "email": "cliente@example.com",
            "cidade": "Rio de Janeiro",
            "estado": "RJ",
            "bloqueio": False,
            "motivo_bloqueio": None,
            "ativo": True,
            "sincronizado_em": timezone.now(),
        }
        dados.update(overrides)
        return ClienteHub.objects.create(**dados)

    def get_clientes(self, **params):
        return self.client.get("/api/terminal/clientes/", params)

    def test_exige_operador_autenticado(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Terminal {self.terminal_token}")

        resposta = self.get_clientes()

        self.assertIn(resposta.status_code, (401, 403))

    def test_consulta_local_nao_chama_central(self):
        self.criar_cliente()

        with patch("integracao.services.retaguarda.RetaguardaClient.clientes") as clientes:
            resposta = self.get_clientes()

        self.assertEqual(resposta.status_code, 200)
        clientes.assert_not_called()

    def test_pesquisa_por_nome(self):
        self.criar_cliente(nome_cliente="Maria Silva")
        self.criar_cliente(retaguarda_id=124, documento="12345678902", nome_cliente="Joao Souza")

        resposta = self.get_clientes(q="maria")

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["clientes"][0]["nome_cliente"], "Maria Silva")

    def test_pesquisa_por_documento(self):
        self.criar_cliente(documento="12345678901")

        resposta = self.get_clientes(q="123.456.789-01")

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["clientes"][0]["documento"], "12345678901")

    def test_limite_de_resultados(self):
        for indice in range(60):
            self.criar_cliente(
                retaguarda_id=indice + 1,
                documento=f"123456789{indice:02d}"[:11],
                nome_cliente=f"Cliente {indice:02d}",
            )

        resposta = self.get_clientes(q="Cliente")

        self.assertEqual(resposta.data["total"], 60)
        self.assertEqual(len(resposta.data["clientes"]), 50)

    def test_isolamento_pelo_hub_do_terminal(self):
        hub_b = HubConfig.objects.create(retaguarda_url="http://central-b.test", empresa_id=22, loja_id=55)
        self.criar_cliente(nome_cliente="Cliente Hub A")
        self.criar_cliente(hub=hub_b, retaguarda_id=124, documento="12345678902", nome_cliente="Cliente Hub B")

        resposta = self.get_clientes()

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["clientes"][0]["nome_cliente"], "Cliente Hub A")

    def test_nao_retorna_ausente_da_retaguarda(self):
        self.criar_cliente(presente_retaguarda=False)

        resposta = self.get_clientes()

        self.assertEqual(resposta.data["total"], 0)

    def test_retorna_local_sem_retaguarda(self):
        self.criar_cliente(
            retaguarda_id=None,
            origem=ClienteHub.ORIGEM_LOCAL,
            presente_retaguarda=False,
            documento="12345678901",
        )

        resposta = self.get_clientes()

        self.assertEqual(resposta.data["total"], 1)
        self.assertEqual(resposta.data["clientes"][0]["origem"], ClienteHub.ORIGEM_LOCAL)
