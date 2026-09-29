from datetime import timedelta

from django.utils import timezone


CENTRAL_STATUS_VERIFICANDO = "VERIFICANDO"
CENTRAL_STATUS_ONLINE = "ONLINE"
CENTRAL_STATUS_OFFLINE = "OFFLINE"
CENTRAL_ONLINE_WINDOW_SECONDS = 60


def calcular_status_central(hub, *, agora=None, janela_segundos=CENTRAL_ONLINE_WINDOW_SECONDS):
    agora = agora or timezone.now()
    if hub is None:
        return {
            "status": CENTRAL_STATUS_VERIFICANDO,
            "online": False,
            "ultimo_contato_em": None,
            "ultima_tentativa_em": None,
        }

    ultimo_contato = hub.ultimo_heartbeat_em
    ultima_tentativa = hub.ultima_tentativa_central_em

    if ultimo_contato and agora - ultimo_contato <= timedelta(seconds=janela_segundos):
        status = CENTRAL_STATUS_ONLINE
    elif ultima_tentativa or ultimo_contato:
        status = CENTRAL_STATUS_OFFLINE
    else:
        status = CENTRAL_STATUS_VERIFICANDO

    return {
        "status": status,
        "online": status == CENTRAL_STATUS_ONLINE,
        "ultimo_contato_em": ultimo_contato.isoformat() if ultimo_contato else None,
        "ultima_tentativa_em": ultima_tentativa.isoformat() if ultima_tentativa else None,
    }
