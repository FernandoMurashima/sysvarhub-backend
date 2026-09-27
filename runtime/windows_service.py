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
    def __init__(self, application_factory=None, server_factory=None, shutdown_timeout=10):
        self.application_factory = application_factory
        self.server_factory = server_factory
        self.shutdown_timeout = shutdown_timeout
        self.server = None
        self.worker_stop_event = threading.Event()
        self.worker_thread = None
        self.runtime_thread = None
        self.runtime_error = None
        self.ready_event = threading.Event()
        self.stopped_event = threading.Event()
        self.stop_requested = threading.Event()
        self._lock = threading.RLock()

    def start(self):
        with self._lock:
            if self.runtime_thread and self.runtime_thread.is_alive():
                return self.runtime_thread
            self.ready_event.clear()
            self.stopped_event.clear()
            self.stop_requested.clear()
            self.worker_stop_event.clear()
            self.runtime_error = None
            self.runtime_thread = threading.Thread(
                target=self._run_guarded,
                name="SysvarHubWaitressRuntime",
                daemon=False,
            )
            self.runtime_thread.start()
            return self.runtime_thread

    def _run_guarded(self):
        try:
            self.run()
        except Exception as exc:
            self.runtime_error = exc
            self.stopped_event.set()
            LOGGER.exception("Runtime do Sysvar Hub falhou.")

    def run(self):
        bootstrap()

        import django
        from django.core.wsgi import get_wsgi_application
        from waitress.server import create_server

        django.setup()

        from runtime.sync_worker import start_worker_thread

        host = os.environ.get("HUB_BIND_HOST", "0.0.0.0")
        port = int(os.environ.get("HUB_PORT", "8000"))
        application_factory = self.application_factory or get_wsgi_application
        server_factory = self.server_factory or create_server
        server = server_factory(application_factory(), host=host, port=port)

        with self._lock:
            self.server = server
            if self.stop_requested.is_set():
                LOGGER.info("Parada solicitada antes do inicio do Waitress.")
                _shutdown_waitress_server(server)
                self.server = None
                self.stopped_event.set()
                return
            if self.worker_thread is None:
                _worker, self.worker_thread = start_worker_thread(
                    self.worker_stop_event
                )
            self.ready_event.set()

        try:
            LOGGER.info("Waitress do Sysvar Hub iniciado em %s:%s.", host, port)
            server.run()
        finally:
            self.stop()
            self.stopped_event.set()

    def stop(self):
        self.stop_requested.set()
        self.worker_stop_event.set()

        with self._lock:
            server = self.server
            self.server = None
            worker_thread = self.worker_thread
            self.worker_thread = None

        if server is None:
            if worker_thread is not None:
                _join_thread(worker_thread, self.shutdown_timeout, "SyncWorker")
            return

        LOGGER.info("Encerrando Waitress do Sysvar Hub.")
        _shutdown_waitress_server(server, timeout=self.shutdown_timeout)

        if worker_thread is not None:
            LOGGER.info("Encerrando SyncWorker do Sysvar Hub.")
            _join_thread(worker_thread, self.shutdown_timeout, "SyncWorker")
        LOGGER.info("Runtime do Sysvar Hub encerrado.")

    def wait_started(self, timeout=None):
        return self.ready_event.wait(timeout)

    def wait_stopped(self, timeout=None):
        return self.stopped_event.wait(timeout)

    def join(self, timeout=None):
        thread = self.runtime_thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True


def _join_thread(thread, timeout, component_name):
    thread.join(timeout=timeout)
    if thread.is_alive():
        LOGGER.warning("%s nao encerrou dentro de %.1f segundo(s).", component_name, timeout)
        return False
    return True


def _shutdown_waitress_server(server, timeout=10):
    # Waitress 3.0.2: BaseWSGIServer.close() fecha listener/trigger, mas nao
    # fecha canais keep-alive no mapa. Fechamos o mapa inteiro para acordar
    # asyncore.loop() e liberar a porta de forma previsivel no servico Windows.
    try:
        server.accepting = False
    except Exception:
        LOGGER.debug("Nao foi possivel desabilitar accept do Waitress.", exc_info=True)

    try:
        server.close()
    except Exception:
        LOGGER.warning("Falha ao fechar socket principal do Waitress.", exc_info=True)

    server_map = getattr(server, "_map", None) or getattr(server, "map", None)
    asyncore_module = getattr(server, "asyncore", None)
    if server_map is not None and asyncore_module is not None and hasattr(asyncore_module, "close_all"):
        try:
            asyncore_module.close_all(server_map)
        except Exception:
            LOGGER.warning("Falha ao fechar canais ativos do Waitress.", exc_info=True)

    trigger = getattr(server, "trigger", None)
    if trigger is not None and hasattr(server, "pull_trigger"):
        try:
            server.pull_trigger()
        except Exception:
            LOGGER.debug("Trigger do Waitress ja estava fechado.", exc_info=True)

    dispatcher = getattr(server, "task_dispatcher", None)
    if dispatcher is not None:
        LOGGER.info("Encerrando task dispatcher do Waitress.")
        dispatcher.shutdown(timeout=timeout)


def run_console(runtime=None):
    (runtime or HubWaitressRuntime()).run()


def stop_runtime(runtime):
    if runtime is not None:
        runtime.stop()


def run_manage(argv):
    bootstrap()

    from django.core.management import execute_from_command_line

    execute_from_command_line(["SysvarHubService.exe", *argv])


def run_service_dispatcher(service_class=None):
    if servicemanager is None:
        raise RuntimeError(
            "pywin32 nao esta disponivel para executar servico Windows."
        )

    service_class = service_class or SysvarHubService
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(service_class)
    servicemanager.StartServiceCtrlDispatcher()


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
            servicemanager.LogInfoMsg("Sysvar Hub recebeu solicitacao de parada.")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            servicemanager.LogInfoMsg("Sysvar Hub iniciando.")

            try:
                self.runtime.start()
                self.runtime.wait_started(timeout=30)
                while True:
                    result = win32event.WaitForSingleObject(self.stop_event, 5000)
                    if result == win32event.WAIT_OBJECT_0:
                        break
                    if self.runtime.stopped_event.is_set():
                        if self.runtime.runtime_error is not None:
                            raise self.runtime.runtime_error
                        break
                servicemanager.LogInfoMsg("Sysvar Hub encerrando runtime.")
                stop_runtime(self.runtime)
                self.runtime.join(timeout=30)
            except Exception as exc:
                servicemanager.LogErrorMsg(f"Sysvar Hub falhou: {exc}")
                raise
            finally:
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)


def main():
    if len(sys.argv) == 1:
        run_service_dispatcher()
        return

    command = sys.argv[1]

    if command == "console":
        run_console()
        return

    if command == "manage":
        run_manage(sys.argv[2:])
        return

    if win32serviceutil is None:
        raise RuntimeError(
            "pywin32 nao esta disponivel para registrar/executar servico Windows."
        )

    win32serviceutil.HandleCommandLine(SysvarHubService)


if __name__ == "__main__":
    main()
