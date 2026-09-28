from django.core.management import get_commands
from django.test import SimpleTestCase


class ManagementCommandTests(SimpleTestCase):
    def test_run_sync_worker_registrado_no_core(self):
        self.assertEqual(get_commands().get("run_sync_worker"), "core")
