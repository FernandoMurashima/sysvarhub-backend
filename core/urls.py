from django.urls import path

from core.api import (
    CaixaAbrirView,
    CaixaFecharView,
    CaixaStatusView,
    OperadorContextoView,
    OperadorLoginView,
    OperadorLogoutView,
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
    path("operador/login/", OperadorLoginView.as_view(), name="operador-login"),
    path("operador/contexto/", OperadorContextoView.as_view(), name="operador-contexto"),
    path("operador/logout/", OperadorLogoutView.as_view(), name="operador-logout"),
    path("caixa/status/", CaixaStatusView.as_view(), name="terminal-caixa-status"),
    path("caixa/abrir/", CaixaAbrirView.as_view(), name="terminal-caixa-abrir"),
    path("caixa/fechar/", CaixaFecharView.as_view(), name="terminal-caixa-fechar"),
]
