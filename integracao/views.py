from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from core.models import HubConfig
from integracao.services.ativacao import (
    AtivacaoHubError,
    ativar_hub_local,
    normalizar_retaguarda_url,
    serializar_status_ativacao,
)


def _hub_config_atual():
    total = HubConfig.objects.count()
    if total > 1:
        raise AtivacaoHubError("Existe mais de uma configuracao local do Hub.")
    if total == 0:
        return None
    return HubConfig.objects.get()


def _is_loopback(request):
    addr = request.META.get("REMOTE_ADDR", "")
    return addr in {"127.0.0.1", "::1"}


@method_decorator(csrf_exempt, name="dispatch")
class HubAtivacaoView(View):
    http_method_names = ["get", "post"]

    def get(self, _request):
        try:
            hub = _hub_config_atual()
        except AtivacaoHubError as exc:
            return JsonResponse({"detail": str(exc)}, status=409)
        return JsonResponse(serializar_status_ativacao(hub))

    def post(self, request):
        if not _is_loopback(request):
            return JsonResponse(
                {"detail": "Ativacao deve ser realizada no computador onde o Sysvar Hub esta instalado."},
                status=403,
            )
        try:
            import json

            payload = json.loads(request.body.decode("utf-8") or "{}")
        except ValueError:
            return JsonResponse({"detail": "Payload invalido."}, status=400)

        codigo = str(payload.get("codigo") or "").strip()
        retaguarda_url = str(payload.get("retaguarda_url") or "").strip()
        if not codigo:
            return JsonResponse({"detail": "Informe o codigo de ativacao."}, status=400)

        try:
            hub_atual = _hub_config_atual()
            url = retaguarda_url or (hub_atual.retaguarda_url if hub_atual else "")
            normalizar_retaguarda_url(url)
            hub = ativar_hub_local(codigo, url)
        except AtivacaoHubError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        return JsonResponse(serializar_status_ativacao(hub))
