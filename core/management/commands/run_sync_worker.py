from django.core.management.base import BaseCommand

from runtime.sync_worker import SyncWorker


class Command(BaseCommand):
    help = "Executa o SyncWorker do Sysvar Hub em foreground."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=10)

    def handle(self, *args, **options):
        worker = SyncWorker(interval=options["interval"])
        self.stdout.write(self.style.SUCCESS("SyncWorker do Sysvar Hub iniciado. Pressione Ctrl+C para encerrar."))
        try:
            worker.run()
        except KeyboardInterrupt:
            worker.stop_event.set()
            self.stdout.write(self.style.WARNING("SyncWorker do Sysvar Hub encerrado."))
