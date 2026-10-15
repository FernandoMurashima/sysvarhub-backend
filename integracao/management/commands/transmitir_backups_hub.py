from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import HubConfig
from integracao.services.backup_transmission import (
    BackupClient, BackupSender, TransferError, load_entries, read_json, safe_path, save_json,
)


class Command(BaseCommand):
    help = "Transmite somente o plano privado validado pelos scripts oficiais de backup."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--plan", required=True)
        parser.add_argument("--resume-after-support", action="store_true")

    def handle(self, *args, **options):
        root = None
        try:
            plan = read_json(options["plan"])
            policy = plan["policy"]
            if policy.get("enabled") is not True:
                self.stdout.write("DESATIVADO")
                return
            if plan["version"] != 1 or plan["service"] != "SysvarHubMySQL" or plan["port"] != 3307:
                raise TransferError("OFFICIAL_INSTANCE_REQUIRED", True)
            root = safe_path(plan["backup_root"])
            bounds = {"bandwidth_bytes_per_second": (16384, 16777216), "cycle_seconds": (1, 900),
                      "request_timeout_seconds": (1, 120), "finalize_timeout_seconds": (1, 3600)}
            if policy.get("version") != 1 or any(type(policy.get(key)) is not int or not low <= policy[key] <= high
                                                 for key, (low, high) in bounds.items()):
                raise TransferError("TRANSMISSION_POLICY", True)
            if safe_path(Path(options["plan"])).parent != root:
                raise TransferError("LOCAL_PLAN", True)
            database = settings.DATABASES["default"]
            if (database["ENGINE"] != "django.db.backends.mysql" or database["NAME"] != "sysvarhub_db"
                    or str(database["PORT"]) != "3307" or database["HOST"] not in ("localhost", "127.0.0.1")):
                raise TransferError("OFFICIAL_INSTANCE_REQUIRED", True)
            hub = HubConfig.objects.get()
            if not hub.ativo or not hub.retaguarda_token or not hub.retaguarda_hub_id:
                raise TransferError("HUB_NOT_PAIRED", True)
            entries = load_entries(plan, hub)
            client = BackupClient(hub.retaguarda_url, hub.retaguarda_token,
                                  bandwidth=policy["bandwidth_bytes_per_second"],
                                  timeout=policy["request_timeout_seconds"],
                                  finalize_timeout=policy["finalize_timeout_seconds"])
            self.diagnostic(root, None, ready=True)
            result = BackupSender(root, client, max_seconds=policy["cycle_seconds"]).run(
                entries, resume=options["resume_after_support"])
            self.stdout.write(result["status"])
        except TransferError as error:
            if error.code == "SENDER_BUSY":
                self.stdout.write("ENVIO_EM_ANDAMENTO")
                return
            self.diagnostic(root, error.code, support=error.support)
            # No exception text, credentials, URL or native database message escapes.
            self.stdout.write("INTERVENCAO_SUPORTE" if error.support else "FALHA_REPETIR")
        except Exception:
            self.diagnostic(root, "LOCAL_CONFIGURATION")
            self.stdout.write("INTERVENCAO_SUPORTE")

    @staticmethod
    def diagnostic(root, code, *, support=True, ready=False):
        if root is not None:
            try:
                import time
                save_json(root / "backup-transmission-preflight.json", dict(
                    version=1, status="BACKUP_LOCAL_VALIDO" if ready else ("INTERVENCAO_SUPORTE" if support else "FALHA_REPETIR"),
                    failure_code=code, last_attempt_utc=time.time()))
            except Exception:
                pass
