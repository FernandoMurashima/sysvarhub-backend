import json
from urllib import error, request
from urllib.parse import urljoin


class RetaguardaError(Exception):
    """Erro controlado na comunicação com a retaguarda."""


class RetaguardaClient:
    def __init__(self, retaguarda_url, timeout=15):
        self.base_url = self._normalizar_base_url(retaguarda_url)
        self.timeout = timeout

    @staticmethod
    def _normalizar_base_url(retaguarda_url):
        if not retaguarda_url:
            raise RetaguardaError("URL da retaguarda não informada.")
        return retaguarda_url.rstrip("/") + "/"

    def ativar_hub(self, *, codigo, hub_uuid, nome, hostname, versao):
        payload = {
            "codigo": codigo,
            "hub_uuid": str(hub_uuid),
            "nome": nome,
            "hostname": hostname,
            "versao": versao,
        }
        return self._post_json("api/hub/ativar/", payload)

    def heartbeat(self, *, token, hostname, versao):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        payload = {
            "hostname": hostname,
            "versao": versao,
        }
        return self._post_json(
            "api/hub/heartbeat/",
            payload,
            headers={"Authorization": f"Hub {token}"},
        )

    def _post_json(self, path, payload, headers=None):
        url = urljoin(self.base_url, path)
        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        req = request.Request(
            url,
            data=body,
            headers=req_headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_json_response(response)
        except error.HTTPError as exc:
            raise RetaguardaError(f"Retaguarda retornou HTTP {exc.code}.") from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", "indisponível")
            raise RetaguardaError(
                f"Não foi possível conectar à retaguarda: {reason}"
            ) from exc
        except TimeoutError as exc:
            raise RetaguardaError("Tempo esgotado ao chamar a retaguarda.") from exc
        except json.JSONDecodeError as exc:
            raise RetaguardaError("Retaguarda retornou JSON inválido.") from exc

    @staticmethod
    def _decode_json_response(response):
        data = response.read()
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))
