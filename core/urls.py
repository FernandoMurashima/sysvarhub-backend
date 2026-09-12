from django.urls import path

from core.api import (
    ParearTerminalView,
    TerminalCatalogoView,
    TerminalContextoView,
    TerminalHeartbeatView,
)


urlpatterns = [
    path("parear/", ParearTerminalView.as_view(), name="terminal-parear"),
    path("contexto/", TerminalContextoView.as_view(), name="terminal-contexto"),
    path("heartbeat/", TerminalHeartbeatView.as_view(), name="terminal-heartbeat"),
    path("catalogo/", TerminalCatalogoView.as_view(), name="terminal-catalogo"),
]
