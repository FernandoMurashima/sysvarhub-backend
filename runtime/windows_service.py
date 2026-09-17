import logging
import os
import sys

from runtime.windows_runtime import ENV_FILE, load_env_file


LOGGER = logging.getLogger(__name__)


def bootstrap():
    load_env_file(ENV_FILE)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sysvarhub.settings")


def run_console():
    bootstrap()
    import django
    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    django.setup()
    host = os.environ.get("HUB_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("HUB_PORT", "8000"))
    serve(get_wsgi_application(), host=host, port=port)


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

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("Sysvar Hub iniciando.")
            try:
                run_console()
            except Exception as exc:
                servicemanager.LogErrorMsg(f"Sysvar Hub falhou: {exc}")
                raise


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
