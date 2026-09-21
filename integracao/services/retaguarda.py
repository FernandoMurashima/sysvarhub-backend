import json
import os
import tempfile
from urllib import error, request
from urllib.parse import urljoin


class RetaguardaError(Exception):
    """Erro controlado na comunicação com a retaguarda."""


CONTENT_TYPE_IMAGEM_EXTENSOES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


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

    def bootstrap(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/bootstrap/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def catalogo(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/catalogo/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def baixar_catalogo_imagem(self, *, token, imagem_id, destino, limite_bytes=5 * 1024 * 1024):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        url = urljoin(self.base_url, f"api/hub/catalogo/imagens/{imagem_id}/")
        req = request.Request(
            url,
            headers={"Authorization": f"Hub {token}", "Accept": "image/*"},
            method="GET",
        )
        tmp = None
        total = 0
        destino_final = None
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type()
                extensao = CONTENT_TYPE_IMAGEM_EXTENSOES.get(content_type)
                if not extensao:
                    raise RetaguardaError("Retaguarda retornou imagem com Content-Type inválido.")
                destino_final = destino.with_suffix(extensao)
                destino_final.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=destino_final.name + ".", suffix=".tmp", dir=str(destino_final.parent))
                with os.fdopen(fd, "wb") as arquivo:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limite_bytes:
                            raise RetaguardaError("Imagem da retaguarda excedeu o limite permitido.")
                        arquivo.write(chunk)
                os.replace(tmp, destino_final)
                tmp = None
        except error.HTTPError as exc:
            raise RetaguardaError(f"Retaguarda retornou HTTP {exc.code}.") from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", "indisponível")
            raise RetaguardaError(f"Não foi possível baixar imagem da retaguarda: {reason}") from exc
        except TimeoutError as exc:
            raise RetaguardaError("Tempo esgotado ao baixar imagem da retaguarda.") from exc
        except OSError as exc:
            raise RetaguardaError(f"Falha ao gravar imagem da retaguarda: {exc.__class__.__name__}.") from exc
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return destino_final

    def operadores(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/operadores/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def vendedores(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/vendedores/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def formas_pagamento(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/formas-pagamento/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def tipos_despesa_pdv(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/tipos-despesa-pdv/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def clientes(self, *, token):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._request_json(
            "api/hub/clientes/",
            method="GET",
            headers={"Authorization": f"Hub {token}"},
        )

    def sync_push(self, *, token, eventos):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._post_json(
            "api/hub/sync/push/",
            {"versao": 1, "eventos": eventos},
            headers={"Authorization": f"Hub {token}"},
        )

    def atualizar_status_sincronizacao(self, *, token, sincronizacao_id, status, etapa_atual="", mensagem_erro=""):
        if not token:
            raise RetaguardaError("Token da retaguarda não informado.")
        return self._post_json(
            f"api/hub/sincronizacoes/{sincronizacao_id}/status/",
            {
                "status": status,
                "etapa_atual": etapa_atual,
                "mensagem_erro": mensagem_erro,
            },
            headers={"Authorization": f"Hub {token}"},
        )

    def _post_json(self, path, payload, headers=None):
        return self._request_json(path, method="POST", payload=payload, headers=headers)

    def _request_json(self, path, *, method, payload=None, headers=None):
        url = urljoin(self.base_url, path)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Accept": "application/json",
             "User-Agent": "SysvarHub/1.0",
        }
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        req = request.Request(
            url,
            data=body,
            headers=req_headers,
            method=method,
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
