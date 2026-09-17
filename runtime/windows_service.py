import logging
import os
import sys
import threading

from runtime.windows_runtime import ENV_FILE, load_env_file


LOGGER = logging.getLogger(__name__)


def bootstrap():
    load_env_file(ENV_FILE)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sysvarhub.settings")


class HubWaitressRuntime:
    def __init__(self, application_factory=None, server_factory=None):
        self.application_factory = application_factory
        self.server_factory = server_factory
        self.server = None
        self._lock = threading.RLock()

    def run(self):
        bootstrap()
        import django
        from django.core.wsgi import get_wsgi_application
        from waitress.server import create_server

        django.setup()
        host = os.environ.get("HUB_BIND_HOST", "0.0.0.0")
        port = int(os.environ.get("HUB_PORT", "8000"))
        application_factory = self.application_factory or get_wsgi_application
        server_factory = self.server_factory or create_server
        server = server_factory(application_factory(), host=host, port=port)
        with self._lock:
            self.server = server
        try:
            server.run()
        finally:
            self.stop()

    def stop(self):
        with self._lock:
            server = self.server
            self.server = None
        if server is None:
            return
        dispatcher = getattr(server, "task_dispatcher", None)
        if dispatcher is not None:
            dispatcher.shutdown()
        server.close()


def run_console(runtime=None):
    (runtime or HubWaitressRuntime()).run()


def stop_runtime(runtime):
    if runtime is not None:
        runtime.stop()


def run_manage(argv):
    bootstrap()
    from django.core.management import execute_from_command_line

    execute_from_command_line(["SysvarHubService.exe", *argv])


try:
    import win32event
    import win32service
    import win32serviceutil
    import servicemanager
except ImportError:
    win32event = win32service = win32serviceutil = servicemanager = None


if win32serviceutil is not None:
    class SysvarHubService(win32serviceutil.ServiceFramework):
        _svc_name_ = "SysvarHub"
        _svc_display_name_ = "Sysvar Hub"
        _svc_description_ = "Servico local do Sysvar Hub."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.runtime = HubWaitressRuntime()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            stop_runtime(self.runtime)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            servicemanager.LogInfoMsg("Sysvar Hub iniciando.")
            try:
                run_console(self.runtime)
            except Exception as exc:
                servicemanager.LogErrorMsg(f"Sysvar Hub falhou: {exc}")
                raise
            finally:
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "service"
    if command == "console":
        run_console()
        return
    if command == "manage":
        run_manage(sys.argv[2:])
        return
    if win32serviceutil is None:
        raise RuntimeError("pywin32 nao esta disponivel para registrar/executar servico Windows.")
    win32serviceutil.HandleCommandLine(SysvarHubService)


if __name__ == "__main__":
    main()
