import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings


class BackupCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-backup-command-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / ".send-plan-fake.json"
        self.plan = dict(version=1, service="SysvarHubMySQL", port=3307,
                         backup_root=str(self.root), policy=dict(version=1, enabled=False, bandwidth_bytes_per_second=262144,
                             cycle_seconds=120, request_timeout_seconds=30, finalize_timeout_seconds=300))

    def invoke(self):
        self.path.write_text(json.dumps(self.plan), encoding="utf-8")
        output = io.StringIO()
        call_command("transmitir_backups_hub", plan=str(self.path), stdout=output)
        return output.getvalue().strip()

    def test_disabled_without_database_or_http(self):
        with patch("integracao.management.commands.transmitir_backups_hub.HubConfig.objects.get") as get_hub:
            self.assertEqual(self.invoke(), "DESATIVADO")
            get_hub.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.path])

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.mysql", "NAME": "sysvarhub_db",
                                             "PORT": "3306", "HOST": "127.0.0.1"}})
    def test_dev_connection_blocked_before_hub_lookup(self):
        self.plan["policy"]["enabled"] = True
        with patch("integracao.management.commands.transmitir_backups_hub.HubConfig.objects.get") as get_hub:
            self.assertEqual(self.invoke(), "INTERVENCAO_SUPORTE")
            get_hub.assert_not_called()
        state = json.loads((self.root / "backup-transmission-preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(state["failure_code"], "OFFICIAL_INSTANCE_REQUIRED")

    def test_wrong_service_blocked(self):
        self.plan["policy"]["enabled"] = True
        self.plan["service"] = "MySQL80"
        self.assertEqual(self.invoke(), "INTERVENCAO_SUPORTE")

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.mysql", "NAME": "sysvarhub_db",
                                             "PORT": "3307", "HOST": "127.0.0.1"}})
    def test_native_database_error_sanitized_and_does_not_start_sender(self):
        self.plan["policy"]["enabled"] = True
        with patch("integracao.management.commands.transmitir_backups_hub.HubConfig.objects.get",
                   side_effect=RuntimeError("FAKE-SECRET-NEVER-LOG")):
            self.assertEqual(self.invoke(), "INTERVENCAO_SUPORTE")
        self.assertNotIn("FAKE-SECRET", (self.root / "backup-transmission-preflight.json").read_text(encoding="utf-8"))
