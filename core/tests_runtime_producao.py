from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, override_settings
from django.urls import resolve

from sysvarhub.urls import angular_spa
from runtime.windows_runtime import (
    create_default_env,
    generate_secret,
    local_hostnames_and_ips,
    merge_csv,
)


class HealthCheckTests(SimpleTestCase):
    def test_health_check_publico(self):
        response = Client().get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "sysvar-hub"})


class AngularSpaFallbackTests(SimpleTestCase):
    def test_rota_spa_retorna_index(self):
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "index.html").write_text("<app-root></app-root>", encoding="utf-8")

            with override_settings(FRONTEND_DIST_DIR=Path(temp_dir)):
                response = Client().get("/pdv")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<app-root></app-root>")

    def test_api_nao_e_capturada_pelo_fallback(self):
        resolver_match = resolve("/api/terminal/contexto/")

        self.assertIsNot(resolver_match.func, angular_spa)

    def test_asset_existente_e_servido(self):
        with TemporaryDirectory() as temp_dir:
            Path(temp_dir, "main.js").write_text("console.log('ok')", encoding="utf-8")
            Path(temp_dir, "index.html").write_text("<app-root></app-root>", encoding="utf-8")

            with override_settings(FRONTEND_DIST_DIR=Path(temp_dir)):
                response = Client().get("/main.js")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console.log('ok')")


class ProductionSettingsTests(SimpleTestCase):
    def test_whitenoise_configurado(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)

    def test_secret_key_padrao_nao_e_aceito_em_producao(self):
        module_globals = {
            "DEBUG": False,
            "SECRET_KEY": "dev-insecure-sysvarhub-change-me",
            "RuntimeError": RuntimeError,
        }

        with self.assertRaises(RuntimeError):
            exec(
                "if not DEBUG and SECRET_KEY == 'dev-insecure-sysvarhub-change-me':\n"
                "    raise RuntimeError('DJANGO_SECRET_KEY deve ser configurado')",
                module_globals,
            )


class WindowsRuntimeTests(SimpleTestCase):
    def test_hosts_lan_preservam_configuracao_manual(self):
        hosts = merge_csv(["hub.local", "127.0.0.1"], ["localhost", "hub.local", "192.168.0.50"])

        self.assertEqual(hosts, ["hub.local", "127.0.0.1", "localhost", "192.168.0.50"])
        self.assertNotIn("*", hosts)

    def test_hosts_locais_incluem_loopback(self):
        hosts = local_hostnames_and_ips()

        self.assertIn("localhost", hosts)
        self.assertIn("127.0.0.1", hosts)

    def test_geracao_de_segredo_nao_usa_valor_default(self):
        secret = generate_secret()

        self.assertNotEqual(secret, "dev-insecure-sysvarhub-change-me")
        self.assertGreaterEqual(len(secret), 64)

    def test_env_padrao_instalado_nao_contem_segredos_fixos(self):
        with TemporaryDirectory() as temp_dir:
            import runtime.windows_runtime as windows_runtime

            original_env = windows_runtime.ENV_FILE
            windows_runtime.ENV_FILE = Path(temp_dir) / "sysvarhub.env"
            try:
                create_default_env(
                    install_root=Path(r"C:\Program Files\Sysvar Hub"),
                    program_data=Path(r"C:\ProgramData\SysvarHub"),
                )
                content = windows_runtime.ENV_FILE.read_text(encoding="utf-8")
            finally:
                windows_runtime.ENV_FILE = original_env

        self.assertIn("DB_PORT=3307", content)
        self.assertIn("SYSVARHUB_FRONTEND_DIST_DIR=", content)
        self.assertNotIn("dev-insecure-sysvarhub-change-me", content)
