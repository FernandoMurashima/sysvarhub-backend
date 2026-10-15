"""Isolated HTTPS Central simulator. Only fictitious data; never opens MySQL."""
import copy
import datetime
import hashlib
import io
import ipaddress
import json
import ssl
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from integracao.services.backup_transmission import (
    BackupClient, BackupSender, TransferError, file_hash, load_entries, read_json,
    safe_path, save_json, sender_lock, validate_progress,
)


class Simulator(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def handle_request(self):
        server = self.server
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server.calls.append((self.command, self.path, dict(self.headers), body))
        if server.error:
            return self.respond(server.error, {"code": "FAKE_ERROR", "detail": "never persist"})
        if self.headers.get("Authorization") != "Hub FAKE-TOKEN":
            return self.respond(401, {})
        if self.path.endswith("iniciar/"):
            text = json.loads(body)["manifesto"]
            doc = json.loads(text.lstrip("\ufeff"))
            identifier = doc.get("backup_id") or doc["incremental_id"]
            if identifier not in server.sessions:
                server.sessions[identifier] = dict(backup_uuid=str(uuid.UUID(identifier)), upload_uuid=str(uuid.uuid4()),
                    tentativa=1, offset=0, proxima_parte=0, tamanho_parte=server.part_bytes, tamanho_total=doc["archive"]["size_bytes"],
                    estado="ABERTO", codigo_erro="", estado_recebimento="PENDENTE", estado_validacao="PENDENTE")
            server.expected[identifier] = doc["archive"]["sha256"]
            progress = server.sessions[identifier]
        else:
            identifier = self.path.split("/")[4]
            progress = server.sessions[identifier]
            if "/partes/" in self.path:
                index = int(self.path.split("/")[-2])
                assert self.headers["X-Upload-Id"] == progress["upload_uuid"]
                assert self.headers["X-Chunk-SHA256"] == hashlib.sha256(body).hexdigest()
                assert int(self.headers["X-Upload-Offset"]) == index * progress["tamanho_parte"]
                assert index == progress["proxima_parte"]
                progress["offset"] += len(body)
                progress["proxima_parte"] += 1
                server.received.setdefault(identifier, bytearray()).extend(body)
                if server.disconnect_once:
                    server.disconnect_once = False
                    self.close_connection = True
                    return  # Simulates commit followed by lost acknowledgement.
            if self.path.endswith("finalizar/"):
                assert progress["offset"] == progress["tamanho_total"]
                assert json.loads(body)["upload_uuid"] == progress["upload_uuid"]
                if hashlib.sha256(server.received[identifier]).hexdigest() != server.expected[identifier]:
                    return self.respond(422, {"code": "FILE_CHECKSUM_MISMATCH"})
                progress.update(estado="RECEBIDO", estado_recebimento="RECEBIDO")
        self.respond(200, progress)

    def respond(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if status == 302:
            self.send_header("Location", "https://other.invalid/")
        self.end_headers()
        self.wfile.write(raw)


class TransmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sysvar-backup-send-tests-")
        cls.test_root = Path(cls.temporary.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        cls.cert_path = cls.test_root / "fake-cert.pem"
        cls.key_path = cls.test_root / "fake-key.pem"
        cls.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cls.key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Simulator)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert_path, cls.key_path)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def setUp(self):
        self.root = self.test_root / uuid.uuid4().hex
        self.root.mkdir()
        self.server.calls, self.server.sessions, self.server.received, self.server.expected = [], {}, {}, {}
        self.server.error, self.server.disconnect_once, self.server.part_bytes = 0, False, 7
        self.hub = SimpleNamespace(hub_uuid=uuid.uuid4(), empresa_id=1, loja_id=2, retaguarda_hub_id=3)
        self.server_uuid = str(uuid.uuid4())
        self.policy = dict(version=1, enabled=True, bandwidth_bytes_per_second=16777216,
                           cycle_seconds=120, request_timeout_seconds=2, finalize_timeout_seconds=2)
        self.plan = dict(version=1, service="SysvarHubMySQL", port=3307, server_uuid=self.server_uuid,
                         backup_root=str(self.root), policy=self.policy, directories=[])
        self.documents = []
        self.add_backup()
        self.client = BackupClient("https://127.0.0.1:" + str(self.server.server_port), "FAKE-TOKEN",
                                   bandwidth=16777216, timeout=2, finalize_timeout=2)
        # Trust only the test server certificate, with hostname and TLS verification intact.
        self.client.context = ssl.create_default_context(cafile=str(self.cert_path))
        self.sender = BackupSender(self.root, self.client, clock=lambda: 100000)

    def add_backup(self, incremental=False, raw_bom=False):
        identifier = uuid.uuid4().hex
        # Simple direct child fixtures: production directory/anchor checks are reused in PS.
        directory = self.root / (("incremental-" if incremental else "full-") + identifier)
        directory.mkdir()
        payload = b"FICTITIOUS-GZIP-FILE-ONLY-1234567890"
        archive_name = "binlog-segment.sql.gz" if incremental else "sysvarhub_db.sql.gz"
        (directory / archive_name).write_bytes(payload)
        doc = dict(manifest_version=1, type="INCREMENTAL" if incremental else "FULL", status="VALIDO",
                   installation=dict(hub_uuid=str(self.hub.hub_uuid), empresa_id=1, loja_id=2, retaguarda_hub_id=3),
                   mysql=dict(database="sysvarhub_db", server_uuid=self.server_uuid),
                   archive=dict(name=archive_name, size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), compression="gzip"))
        if incremental:
            previous = self.documents[-1]
            doc.update(incremental_id=identifier, sequence=len(self.documents), full_backup_id=self.documents[0]["id"],
                       previous_id=previous["id"], previous_manifest_sha256=previous["hash"])
        else:
            doc["backup_id"] = identifier
        raw = (("\ufeff" if raw_bom else "") + json.dumps(doc, indent=3) + "\r\n").encode("utf-8")
        (directory / "manifest.json").write_bytes(raw)
        self.plan["directories"].append(directory.name)
        self.documents.append(dict(id=identifier, hash=hashlib.sha256(raw).hexdigest(), doc=doc, directory=directory, raw=raw))
        return self.documents[-1]

    def entries(self):
        return load_entries(self.plan, self.hub)

    def run_sender(self, resume=False):
        return self.sender.run(self.entries(), resume=resume)

    def test_full_chunked_upload_and_received_is_not_validated(self):
        state = self.run_sender()
        self.assertEqual(state["status"], "RECEBIDO_CENTRAL")
        self.assertEqual(state["pending_files"], 0)
        record = state["files"][self.documents[0]["id"]]
        self.assertEqual(record["progress"]["estado_validacao"], "PENDENTE")
        parts = [call for call in self.server.calls if call[0] == "PUT"]
        self.assertEqual([len(call[3]) for call in parts], [7, 7, 7, 7, 7, 1])
        self.assertEqual(bytes(self.server.received[self.documents[0]["id"]]), b"FICTITIOUS-GZIP-FILE-ONLY-1234567890")
        self.assertIsNotNone(state["last_full"])

    def test_incrementals_in_sequence_after_confirmed_full(self):
        self.add_backup(True)
        self.add_backup(True)
        state = self.run_sender()
        order = [json.loads(call[3])["manifesto"] for call in self.server.calls if call[1].endswith("iniciar/")]
        self.assertEqual([json.loads(text)["type"] for text in order], ["FULL", "INCREMENTAL", "INCREMENTAL"])
        for index, call in enumerate(self.server.calls):
            if call[1].endswith("iniciar/") and index:
                self.assertTrue(self.server.calls[index-1][1].endswith("finalizar/"))
        self.assertEqual(state["last_incremental"], self.documents[-1]["id"])

    def test_manifest_original_utf8_bom_and_crlf_preserved(self):
        self.add_backup(False, True)
        self.run_sender()
        starts = [call for call in self.server.calls if call[1].endswith("iniciar/")]
        self.assertEqual(json.loads(starts[1][3])["manifesto"].encode("utf-8"), self.documents[1]["raw"])

    def test_disconnect_and_resume_from_central_offset_without_resending(self):
        self.server.disconnect_once = True
        first = self.run_sender()
        self.assertEqual(first["status"], "FALHA_REPETIR")
        self.assertEqual(first["files"][self.documents[0]["id"]]["progress"]["offset"], 0)
        state = self.run_sender(resume=True)
        self.assertEqual(state["status"], "RECEBIDO_CENTRAL")
        parts = [int(call[2]["X-Upload-Offset"]) for call in self.server.calls if call[0] == "PUT"]
        self.assertEqual(parts, [0, 7, 14, 21, 28, 35])
        self.assertTrue(any(call[0] == "GET" for call in self.server.calls))

    def test_repeated_cycle_does_not_reupload_received_backups(self):
        self.run_sender()
        self.server.calls.clear()
        self.run_sender()
        self.assertEqual(self.server.calls, [])

    def test_final_ack_lost_retry_does_not_upload_again(self):
        real = self.client.request
        def request(method, route, *args, **kwargs):
            result = real(method, route, *args, **kwargs)
            if route.endswith("finalizar/"):
                raise TransferError("CONNECTION")
            return result
        with patch.object(self.client, "request", side_effect=request):
            self.assertEqual(self.run_sender()["status"], "FALHA_REPETIR")
        self.server.calls.clear()
        self.assertEqual(self.run_sender(resume=True)["status"], "RECEBIDO_CENTRAL")
        self.assertEqual([call[0] for call in self.server.calls], ["GET"])

    def test_central_unavailable_backoff_and_local_archive_preserved(self):
        self.server.error = 503
        first = self.run_sender()
        self.assertEqual(first["status"], "FALHA_REPETIR")
        self.assertEqual(first["next_attempt"], 100900)
        count = len(self.server.calls)
        self.run_sender()
        self.assertEqual(len(self.server.calls), count)
        second = self.run_sender(resume=True)
        self.assertEqual(second["next_attempt"], 101800)
        self.assertTrue(self.entries()[0]["path"].exists())

    def test_token_invalid_blocks_and_is_not_logged(self):
        self.client.token = "FAKE-REVOKED"
        state = self.run_sender()
        self.assertEqual(state["status"], "INTERVENCAO_SUPORTE")
        saved = self.sender.path.read_text(encoding="utf-8")
        self.assertNotIn("FAKE-REVOKED", saved)
        self.assertNotIn("never persist", saved)
        self.assertEqual(state["failure_code"], "HTTP_401")
        self.server.calls.clear()
        self.run_sender()
        self.assertEqual(self.server.calls, [])

    def test_https_required_and_arbitrary_url_components_rejected(self):
        for url in ("http://localhost/", "https://user:pass@central.test/", "https://central.test/else/", "https://central.test/?a=b", "https://central.test/#x"):
            with self.subTest(url=url), self.assertRaises(TransferError):
                BackupClient(url, "FAKE", bandwidth=100000)

    def test_invalid_tls_certificate_is_rejected(self):
        self.client.context = ssl.create_default_context()
        state = self.run_sender()
        self.assertEqual(state["failure_code"], "TLS_INVALID")
        self.assertEqual(self.server.calls, [])

    def test_redirect_never_follows_or_exposes_token(self):
        self.server.error = 302
        self.assertEqual(self.run_sender()["failure_code"], "HTTP_302")
        self.assertEqual(len(self.server.calls), 1)

    def test_manifest_conflict_http_blocks_successors(self):
        self.add_backup(True)
        self.server.error = 409
        self.assertEqual(self.run_sender()["status"], "INTERVENCAO_SUPORTE")
        self.assertEqual(len(self.server.calls), 1)

    def test_manifest_mutation_after_first_attempt_blocked(self):
        self.server.error = 503
        self.run_sender()
        doc = self.documents[0]
        path = doc["directory"] / "manifest.json"
        path.write_bytes(doc["raw"] + b" ")
        self.server.calls.clear()
        self.assertEqual(self.run_sender(resume=True)["failure_code"], "MANIFEST_CONFLICT")
        self.assertEqual(self.server.calls, [])

    def test_corrupt_archive_never_marked_received_without_global_sha(self):
        entry = self.entries()[0]
        entry["path"].write_bytes(b"X" * entry["size"])
        state = self.run_sender()
        self.assertEqual(state["failure_code"], "HTTP_422")
        self.assertEqual(state["status"], "INTERVENCAO_SUPORTE")
        self.assertNotEqual(state["files"][entry["id"]]["status"], "RECEBIDO_CENTRAL")

    def test_missing_full_or_sequence_or_identity_blocks(self):
        self.add_backup(True)
        for mutate in (lambda plan: plan["directories"].pop(0), lambda plan: plan.update(server_uuid=str(uuid.uuid4()))):
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(TransferError):
                load_entries(plan, self.hub)
        self.hub.loja_id = 99
        with self.assertRaises(TransferError):
            self.entries()

    def test_low_local_space_prevents_network_and_preserves_old_checkpoint(self):
        self.server.error = 503
        self.run_sender()
        previous = self.sender.path.read_bytes()
        with patch("integracao.services.backup_transmission.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(TransferError):
                self.run_sender(resume=True)
        self.assertEqual(self.sender.path.read_bytes(), previous)
        self.assertEqual(len(self.server.calls), 1)

    def test_central_disk_full_is_retryable(self):
        self.server.error = 507
        self.assertEqual(self.run_sender()["status"], "FALHA_REPETIR")

    def test_concurrent_senders_blocked(self):
        with sender_lock(self.root):
            with self.assertRaises(TransferError) as error:
                self.run_sender()
        self.assertEqual(error.exception.code, "SENDER_BUSY")
        self.assertEqual(self.server.calls, [])

    def test_bandwidth_limit_paces_small_blocks(self):
        self.client.bandwidth = 16384
        with patch("integracao.services.backup_transmission.time.sleep") as sleep:
            self.run_sender()
        self.assertEqual(sleep.call_count, 6)
        self.assertTrue(all(0 < call.args[0] <= 7 / 16384 for call in sleep.call_args_list))

    def test_budget_leaves_pending_without_discarding_progress(self):
        self.sender.max_seconds = 0
        state = self.run_sender()
        self.assertEqual(state["status"], "ENVIO_PENDENTE")
        self.assertEqual(state["pending_files"], 1)
        self.assertEqual(self.server.calls, [])

    def test_progress_mismatch_and_regression_rejected(self):
        self.run_sender()
        entry = self.entries()[0]
        response = copy.deepcopy(self.server.sessions[entry["id"]])
        for field, value in (("backup_uuid", str(uuid.uuid4())), ("offset", 1), ("tamanho_total", 99), ("tamanho_parte", 2**30), ("estado_validacao", "CADEIA_INTERROMPIDA")):
            bad = dict(response, **{field: value})
            with self.subTest(field=field), self.assertRaises(TransferError):
                validate_progress(bad, entry)
        bad = dict(response, upload_uuid=str(uuid.uuid4()))
        with self.assertRaises(TransferError):
            validate_progress(bad, entry, response)

    def test_expired_attempt_renews_same_manifest_and_backup_id(self):
        self.server.disconnect_once = True
        self.run_sender()
        self.server.error = 408
        self.run_sender(resume=True)
        self.server.error = 0
        self.server.calls.clear()
        state = self.run_sender(resume=True)
        self.assertEqual(state["status"], "RECEBIDO_CENTRAL")
        start = self.server.calls[0]
        self.assertTrue(start[1].endswith("iniciar/"))
        self.assertEqual(json.loads(start[3])["manifesto"].encode(), self.documents[0]["raw"])
        self.assertEqual(len(self.server.sessions), 1)

    def test_large_part_hash_reads_bounded_blocks(self):
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 65536:
                    raise AssertionError("unbounded read")
                return super().read(size)
        data = b"FAKE" * 100000
        self.assertEqual(file_hash(Bounded(data)), hashlib.sha256(data).hexdigest())

    def test_path_traversal_rejected(self):
        self.plan["directories"] = ["../outside"]
        with self.assertRaises(TransferError):
            self.entries()

    def test_invalid_state_preserved(self):
        self.sender.path.write_bytes(b"broken-json")
        with self.assertRaises(TransferError):
            self.run_sender()
        self.assertEqual(self.sender.path.read_bytes(), b"broken-json")
        self.assertEqual(self.server.calls, [])


    def test_long_confirmed_chain_does_not_starve_pending_incremental(self):
        self.add_backup(True)
        self.add_backup(True)
        self.run_sender()
        self.add_backup(True)
        self.server.calls.clear()
        state = self.run_sender()
        self.assertEqual(state["status"], "RECEBIDO_CENTRAL")
        queries = [call for call in self.server.calls if call[0] == "GET"]
        self.assertEqual(len(queries), 1)
        self.assertIn(self.documents[-2]["id"], queries[0][1])

    def test_incremental_backoff_keeps_growing_after_predecessor_confirmation(self):
        self.run_sender()
        self.add_backup(True)
        real = self.client.request
        def fail_start(method, route, *args, **kwargs):
            if route == "iniciar/":
                raise TransferError("CONNECTION")
            return real(method, route, *args, **kwargs)
        with patch.object(self.client, "request", side_effect=fail_start):
            self.assertEqual(self.run_sender()["next_attempt"], 100900)
            state = self.run_sender(resume=True)
            self.assertEqual(state["next_attempt"], 101800)
            self.assertEqual(state["connection_failures"], 2)

    def test_cycle_budget_interrupts_between_parts_and_resumes(self):
        real = self.client.request
        def stop_after_part(method, route, *args, **kwargs):
            result = real(method, route, *args, **kwargs)
            if method == "PUT":
                self.sender.max_seconds = 0
            return result
        with patch.object(self.client, "request", side_effect=stop_after_part):
            state = self.run_sender()
        self.assertEqual(state["status"], "ENVIO_PENDENTE")
        self.assertEqual(state["remaining_bytes"], 29)
        self.sender.max_seconds = 120
        self.assertEqual(self.run_sender()["status"], "RECEBIDO_CENTRAL")
        self.assertEqual([int(call[2]["X-Upload-Offset"]) for call in self.server.calls if call[0] == "PUT"], [0, 7, 14, 21, 28, 35])

    def test_diagnostic_is_compact_and_separate_from_recovery_validation(self):
        self.run_sender()
        diagnostic = read_json(self.root / "backup-transmission-diagnostic.json")
        self.assertNotIn("files", diagnostic)
        self.assertEqual(diagnostic["status"], "RECEBIDO_CENTRAL")
        self.assertNotIn("VALIDADO", json.dumps(diagnostic))


    def test_large_negotiated_part_streams_small_blocks(self):
        self.server.part_bytes = 512 * 1024
        payload = b"FICTITIOUS" * 40000
        document = copy.deepcopy(self.documents[0]["doc"])
        document["archive"]["size_bytes"] = len(payload)
        initial = self.client.request("POST", "iniciar/", {"manifesto": json.dumps(document)})
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 65536:
                    raise AssertionError("unbounded transport read")
                return super().read(size)
        progress = self.client.request("PUT", self.documents[0]["id"] + "/partes/0/", stream=Bounded(payload),
            length=len(payload), headers={"X-Upload-Id": initial["upload_uuid"], "X-Upload-Offset": "0",
                                         "X-Chunk-SHA256": hashlib.sha256(payload).hexdigest()})
        self.assertEqual(progress["offset"], len(payload))
        self.assertEqual(bytes(self.server.received[self.documents[0]["id"]]), payload)


    def test_disappeared_pending_chain_blocks_new_full(self):
        self.server.error = 503
        self.run_sender()
        self.add_backup(False)
        self.server.error = 0
        self.server.calls.clear()
        entries = self.entries()[1:]
        state = self.sender.run(entries, resume=True)
        self.assertEqual(state["failure_code"], "LOCAL_CHAIN_MISSING")
        self.assertEqual(state["status"], "INTERVENCAO_SUPORTE")
        self.assertEqual(self.server.calls, [])
