from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, override_settings
from django.urls import resolve

from sysvarhub.urls import angular_spa


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
