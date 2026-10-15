"""Bounded, resumable backup transport. No operational writes or automatic activation."""
import hashlib
import http.client
import json
import os
import shutil
import ssl
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from integracao.services.retaguarda import RetaguardaClient

BLOCK = 64 * 1024
MAX_PART = 16 * 1024 * 1024


class TransferError(Exception):
    def __init__(self, code, support=False):
        super().__init__(code)
        self.code, self.support = code, support


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise TransferError("LOCAL_PATH", True)
    for entry in (path, *path.parents):
        if entry.exists() or entry.is_symlink():
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise TransferError("LOCAL_LINK", True)
            if entry.is_file() and info.st_nlink != 1:
                raise TransferError("LOCAL_LINK", True)
    return path


def read_json(path, limit=8 * 1024 * 1024):
    path = safe_path(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise TransferError("LOCAL_METADATA", True)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise TransferError("LOCAL_METADATA", True) from None


def save_json(path, value):
    path = safe_path(path)
    if shutil.disk_usage(path.parent).free < 8 * 1024 * 1024:
        raise TransferError("LOCAL_SPACE")
    temporary = safe_path(path.with_name(".send-" + uuid.uuid4().hex + ".json"))
    try:
        # Inherits the protected backup root DACL on Windows.
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def sender_lock(root):
    path = safe_path(Path(root) / ".backup-send.lock")
    with path.open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise TransferError("SENDER_BUSY") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def file_hash(stream, length=None):
    digest = hashlib.sha256()
    remaining = length
    while remaining is None or remaining:
        data = stream.read(BLOCK if remaining is None else min(BLOCK, remaining))
        if not data:
            if remaining:
                raise TransferError("LOCAL_TRUNCATED", True)
            break
        digest.update(data)
        if remaining is not None:
            remaining -= len(data)
    return digest.hexdigest()


class BackupClient(RetaguardaClient):
    """Uses the paired Central URL/token, with stricter transport for backup bodies."""
    def __init__(self, url, token, *, bandwidth, timeout=30, finalize_timeout=300):
        super().__init__(url, timeout=timeout)
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path != "/" or parsed.query or parsed.fragment or "\\" in url):
            raise TransferError("HTTPS_CONFIGURATION", True)
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise TransferError("HUB_CREDENTIAL", True)
        self.host, self.port = parsed.hostname, parsed.port or 443
        self.token, self.bandwidth = token, bandwidth
        self.finalize_timeout = finalize_timeout
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def request(self, method, route, payload=None, *, stream=None, length=0, headers=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        fields = {"Authorization": "Hub " + self.token, "Accept": "application/json"}
        if stream is not None:
            fields.update({"Content-Type": "application/octet-stream", "Content-Length": str(length)})
        elif body is not None:
            fields.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        fields.update(headers or {})
        connection = http.client.HTTPSConnection(self.host, self.port, context=self.context,
                                                timeout=self.finalize_timeout if route.endswith("finalizar/") else self.timeout)
        try:
            connection.putrequest(method, "/api/hub/backups/" + route)
            for name, value in fields.items():
                connection.putheader(name, value)
            connection.endheaders(body)
            if stream is not None:
                remaining, sent, started = length, 0, time.monotonic()
                while remaining:
                    data = stream.read(min(BLOCK, remaining))
                    if not data:
                        raise TransferError("LOCAL_TRUNCATED", True)
                    # Pace before each small write; no full-file buffer or initial burst.
                    delay = (sent + len(data)) / self.bandwidth - (time.monotonic() - started)
                    if delay > 0:
                        time.sleep(delay)
                    connection.send(data)
                    sent += len(data)
                    remaining -= len(data)
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise TransferError("CENTRAL_RESPONSE", True)
            if not 200 <= response.status < 300:
                # Never log the body, URL, headers, token or native exception message.
                support = response.status not in (408, 429, 500, 502, 503, 504, 507)
                raise TransferError("HTTP_" + str(response.status), support)
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except ValueError:
                raise TransferError("CENTRAL_RESPONSE", True) from None
        except ssl.SSLError:
            raise TransferError("TLS_INVALID", True) from None
        except (OSError, http.client.HTTPException):
            raise TransferError("CONNECTION") from None
        finally:
            connection.close()


def validate_progress(response, entry, previous=None):
    try:
        if uuid.UUID(response["backup_uuid"]).hex != entry["id"]:
            raise ValueError()
        uuid.UUID(response["upload_uuid"])
        part, total, offset, index, attempt = (response[key] for key in
            ("tamanho_parte", "tamanho_total", "offset", "proxima_parte", "tentativa"))
        if any(type(value) is not int for value in (part, total, offset, index, attempt)):
            raise ValueError()
        if not 1 <= part <= MAX_PART or total != entry["size"] or not 0 <= offset <= total or attempt < 1:
            raise ValueError()
        if index != (offset + part - 1) // part or (offset != total and offset % part):
            raise ValueError()
        if response["estado_validacao"] not in ("PENDENTE", "VALIDO"):
            raise ValueError()
        if response["estado"] not in ("ABERTO", "RECEBIDO") or response.get("codigo_erro"):
            raise ValueError()
        if response["estado_recebimento"] != ("RECEBIDO" if response["estado"] == "RECEBIDO" else "PENDENTE"):
            raise ValueError()
        if response["estado"] == "RECEBIDO" and offset != total:
            raise ValueError()
        if previous and (response["upload_uuid"] != previous["upload_uuid"] or part != previous["tamanho_parte"]
                         or attempt != previous["tentativa"] or offset < previous["offset"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TransferError("PROGRESS_CONFLICT", True) from None
    keys = ("backup_uuid", "upload_uuid", "tentativa", "offset", "proxima_parte", "tamanho_parte", "tamanho_total", "estado", "codigo_erro", "estado_recebimento", "estado_validacao")
    return {key: response[key] for key in keys}


def load_entries(plan, hub):
    root = safe_path(plan["backup_root"])
    identity = {"hub_uuid": str(hub.hub_uuid).replace("-", ""), "empresa_id": hub.empresa_id,
                "loja_id": hub.loja_id, "retaguarda_hub_id": hub.retaguarda_hub_id}
    entries, last, full, seen = [], None, None, set()
    for relative in plan["directories"]:
        if not isinstance(relative, str) or Path(relative).name != relative or relative in (".", ".."):
            raise TransferError("LOCAL_PATH", True)
        directory = safe_path(root / relative)
        path = safe_path(directory / "manifest.json")
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise TransferError("LOCAL_METADATA", True)
        # utf-8 (not utf-8-sig): BOM is preserved in the string sent to Central.
        text = raw.decode("utf-8")
        manifest = json.loads(text.lstrip("\ufeff"))
        actual = dict(manifest["installation"])
        actual["hub_uuid"] = uuid.UUID(actual["hub_uuid"]).hex
        if actual != identity or manifest["mysql"]["server_uuid"] != plan["server_uuid"] or manifest["mysql"]["database"] != "sysvarhub_db" or manifest["status"] != "VALIDO":
            raise TransferError("IDENTITY_CONFLICT", True)
        identifier = uuid.UUID(manifest.get("backup_id") or manifest["incremental_id"]).hex
        digest = hashlib.sha256(raw).hexdigest()
        if identifier in seen:
            raise TransferError("CHAIN_CONFLICT", True)
        seen.add(identifier)
        if manifest["type"] == "FULL":
            full, sequence = identifier, 0
        elif manifest["type"] == "INCREMENTAL":
            if not last or manifest["full_backup_id"] != full or manifest["previous_id"] != last["id"] or manifest["previous_manifest_sha256"] != last["manifest_hash"] or manifest["sequence"] != sequence + 1:
                raise TransferError("CHAIN_CONFLICT", True)
            sequence += 1
        else:
            raise TransferError("CHAIN_CONFLICT", True)
        archive = manifest["archive"]
        if archive["name"] != ("sysvarhub_db.sql.gz" if manifest["type"] == "FULL" else "binlog-segment.sql.gz") or archive["compression"] != "gzip":
            raise TransferError("LOCAL_ARCHIVE", True)
        archive_path = safe_path(directory / archive["name"])
        if archive_path.stat().st_size != archive["size_bytes"] or archive["size_bytes"] < 1:
            raise TransferError("LOCAL_ARCHIVE", True)
        # The selector verified archive SHA and chain anchors under the generation lock.
        # Do not reread an entire archive in each resumed upload cycle: each part is
        # hashed below and Central must verify the original global SHA before receipt.
        last = {"id": identifier, "manifest_hash": digest, "manifest_path": path, "path": archive_path,
                "size": archive["size_bytes"], "hash": archive["sha256"], "type": manifest["type"]}
        entries.append(last)
    return entries


class BackupSender:
    def __init__(self, root, client, *, max_seconds=120, clock=time.time):
        self.root, self.client = safe_path(root), client
        self.max_seconds, self.clock = max_seconds, clock
        self.path = self.root / "backup-transmission-state.json"

    def save(self, state):
        save_json(self.path, state)
        save_json(self.root / "backup-transmission-diagnostic.json",
                  {key: value for key, value in state.items() if key != "files"})

    def run(self, entries, *, resume=False):
        with sender_lock(self.root):
            state = read_json(self.path, limit=64 * 1024 * 1024) if self.path.exists() else {
                "version": 1, "files": {}, "status": "BACKUP_LOCAL_VALIDO", "failures": 0,
                "last_full": None, "last_incremental": None, "last_confirmed_utc": None,
                "next_attempt": 0, "support_required": False, "connection_failures": 0}
            if state.get("version") != 1 or not isinstance(state.get("files"), dict):
                raise TransferError("LOCAL_STATE", True)
            if state.get("support_required") and not resume:
                return state
            now = self.clock()
            if state.get("next_attempt", 0) > now and not resume:
                return state
            state.update(last_attempt_utc=now, status="ENVIO_PENDENTE", support_required=False)
            started = time.monotonic()
            self.update_pending(state, entries)
            try:
                self.save(state)  # A failed local checkpoint must prevent any HTTP write.
                for entry in entries:
                    saved = state["files"].get(entry["id"], {})
                    if saved and saved.get("manifest_hash") != entry["manifest_hash"]:
                        raise TransferError("MANIFEST_CONFLICT", True)
                known_ids = {entry["id"] for entry in entries}
                if any(identifier not in known_ids and record.get("status") != "RECEBIDO_CENTRAL"
                       for identifier, record in state["files"].items()):
                    raise TransferError("LOCAL_CHAIN_MISSING", True)
                pending_indices = [index for index, entry in enumerate(entries)
                                   if state["files"].get(entry["id"], {}).get("status") != "RECEBIDO_CENTRAL"]
                # Confirm the immediate predecessor, avoiding remote rescans and starvation.
                start = max(0, pending_indices[0] - 1) if pending_indices else len(entries)
                for entry in entries[start:]:
                    saved = state["files"].get(entry["id"], {})
                    if time.monotonic() - started >= self.max_seconds:
                        break
                    record = dict(saved, manifest_hash=entry["manifest_hash"], status="ENVIO_EM_ANDAMENTO")
                    state["files"][entry["id"]] = record
                    state["status"] = "ENVIO_EM_ANDAMENTO"
                    self.save(state)
                    if saved.get("progress"):
                        progress = validate_progress(self.client.request("GET", entry["id"] + "/progresso/"), entry, saved["progress"])
                    else:
                        with safe_path(entry["manifest_path"]).open("rb") as manifest_stream:
                            original = manifest_stream.read(1024 * 1024 + 1)
                        if hashlib.sha256(original).hexdigest() != entry["manifest_hash"]:
                            raise TransferError("MANIFEST_CONFLICT", True)
                        progress = validate_progress(self.client.request("POST", "iniciar/", {"manifesto": original.decode("utf-8")}), entry)
                    record["progress"] = progress
                    self.save(state)
                    if progress["estado"] == "RECEBIDO":
                        record.update(progress=progress, status="RECEBIDO_CENTRAL")
                        state["last_full" if entry["type"] == "FULL" else "last_incremental"] = entry["id"]
                        state["last_confirmed_utc"] = self.clock()
                        if saved.get("status") != "RECEBIDO_CENTRAL":
                            state.update(failures=0, next_attempt=0, failure_code=None)
                        self.update_pending(state, entries)
                        self.save(state)
                        continue
                    with safe_path(entry["path"]).open("rb") as stream:
                        if os.fstat(stream.fileno()).st_size != entry["size"]:
                            raise TransferError("ARCHIVE_CONFLICT", True)
                        while progress["offset"] < entry["size"]:
                            if time.monotonic() - started >= self.max_seconds:
                                state["status"] = "ENVIO_PENDENTE"
                                state["elapsed_seconds"] = round(time.monotonic() - started, 3)
                                self.update_pending(state, entries)
                                self.save(state)
                                return state
                            offset = progress["offset"]
                            length = min(progress["tamanho_parte"], entry["size"] - offset)
                            stream.seek(offset)
                            digest = file_hash(stream, length)
                            stream.seek(offset)
                            response = self.client.request("PUT", entry["id"] + "/partes/" + str(progress["proxima_parte"]) + "/",
                                stream=stream, length=length, headers={"X-Upload-Id": progress["upload_uuid"],
                                "X-Upload-Offset": str(offset), "X-Chunk-SHA256": digest})
                            confirmed = validate_progress(response, entry, progress)
                            if confirmed["offset"] != offset + length:
                                raise TransferError("PROGRESS_CONFLICT", True)
                            progress = confirmed
                            record["progress"] = progress
                            self.save(state)
                    # Only a confirmed final response permits the next chain member.
                    # Receiving a file does not imply validation for recovery.
                    response = self.client.request("POST", entry["id"] + "/finalizar/", {"upload_uuid": progress["upload_uuid"]})
                    progress = validate_progress(response, entry, progress)
                    if progress["estado"] != "RECEBIDO":
                        raise TransferError("RECEIPT_REQUIRED", True)
                    record.update(progress=progress, status="RECEBIDO_CENTRAL")
                    state["last_full" if entry["type"] == "FULL" else "last_incremental"] = entry["id"]
                    state.update(last_confirmed_utc=self.clock(), failures=0, next_attempt=0, failure_code=None)
                    self.update_pending(state, entries)
                    self.save(state)
                state["status"] = "ENVIO_PENDENTE" if state["pending_files"] else "RECEBIDO_CENTRAL"
            except TransferError as error:
                state.update(status="INTERVENCAO_SUPORTE" if error.support else "FALHA_REPETIR",
                             support_required=error.support, failure_code=error.code)
                state["failures"] += 1
                state["last_failure_utc"] = self.clock()
                if error.code == "CONNECTION" or error.code in ("HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504"):
                    state["connection_failures"] = state.get("connection_failures", 0) + 1
                state["next_attempt"] = self.clock() + min(86400, 900 * (2 ** min(state["failures"] - 1, 7)))
                if error.code == "HTTP_408":
                    # Renew the same original manifest next cycle, retaining the UUID.
                    for record in state["files"].values():
                        if record.get("status") != "RECEBIDO_CENTRAL":
                            record.pop("progress", None)
                self.update_pending(state, entries)
            except (OSError, ValueError, KeyError, TypeError):
                state.update(status="INTERVENCAO_SUPORTE", support_required=True, failure_code="LOCAL_STATE_OR_IO")
            state["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save(state)
            return state

    @staticmethod
    def update_pending(state, entries):
        pending = [entry for entry in entries if state["files"].get(entry["id"], {}).get("status") != "RECEBIDO_CENTRAL"]
        state["pending_files"] = len(pending)
        state["pending_bytes"] = sum(entry["size"] for entry in pending)
        state["remaining_bytes"] = sum(entry["size"] - state["files"].get(entry["id"], {}).get("progress", {}).get("offset", 0) for entry in pending)
