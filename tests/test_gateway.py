"""Gateway seam tests (#38): routing, denial, secret-free evidence, and
provider error paths (429/5xx relay, unreachable upstream, mid-stream
close, upstream timeout)."""

import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aco import gateway as gateway_module


class _Stub:
    """Records requests; answers /v1/session/* as the session surface would."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization")))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests.append(("POST", self.path, self.headers.get("Authorization")))
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _spin_gateway(monkeypatch, evidence, provider, session):
    """Start a gateway against explicit upstreams; returns (base, server)."""
    monkeypatch.setenv("ACO_GATEWAY_PROVIDER_UPSTREAM", provider)
    monkeypatch.setenv("ACO_GATEWAY_SESSION_UPSTREAM", session)
    monkeypatch.setenv("ACO_GATEWAY_EVIDENCE", str(evidence))
    import importlib
    importlib.reload(gateway_module)
    server = gateway_module.ThreadingHTTPServer(("127.0.0.1", 0), gateway_module._Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


class _FlexStub:
    """Runs one custom handler class; records nothing itself."""

    def __init__(self, handler_cls):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    stub = _Stub()
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence, f"{stub.base}/api/v3", stub.base)
    yield base, stub, evidence
    server.shutdown()
    stub.close()


def get(base, path, auth=None):
    import urllib.request
    request = urllib.request.Request(base + path)
    if auth:
        request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_session_prefix_routes_to_session_upstream(gateway):
    base, stub, _ = gateway
    status, body = get(base, "/v1/session/task", auth="Bearer trial-token")
    assert status == 200 and body == '{"ok": true}'
    assert stub.requests[-1][:2] == ("GET", "/v1/session/task")
    assert stub.requests[-1][2] == "Bearer trial-token"  # credential forwarded


def test_provider_prefix_routes_to_provider_upstream(gateway):
    base, stub, _ = gateway
    import urllib.request
    request = urllib.request.Request(base + "/api/v3/responses", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer paid-key"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    # the stub answers any POST with 401 (like a provider auth failure) —
    # what matters is that the request REACHED the fixed upstream
    assert status == 401
    assert stub.requests[-1][:2] == ("POST", "/api/v3/responses")
    assert stub.requests[-1][2] == "Bearer paid-key"


def test_unlisted_targets_are_denied_and_recorded(gateway):
    base, stub, evidence = gateway
    before = len(stub.requests)
    status, body = get(base, "/admin")
    assert status == 403 and "egress_denied" in body
    assert len(stub.requests) == before  # nothing reached any upstream
    entry = json.loads(evidence.read_text().strip().splitlines()[-1])
    assert entry["route"] == "denied" and entry["path"] == "/admin"


def test_evidence_never_records_credentials_or_bodies(gateway):
    base, stub, evidence = gateway
    get(base, "/v1/session/task", auth="Bearer secret-token-123")
    get(base, "/api/v3/responses", auth="Bearer secret-key-456")
    _last_entry(evidence, min_lines=2)  # both relay rows are on disk
    log = evidence.read_text()
    assert "secret-token-123" not in log and "secret-key-456" not in log
    entry = json.loads(log.strip().splitlines()[-2])
    assert set(entry) <= {"route", "method", "path", "status", "ms", "at", "error"}


def test_gateway_requires_upstream_config(monkeypatch):
    monkeypatch.delenv("ACO_GATEWAY_PROVIDER_UPSTREAM", raising=False)
    monkeypatch.delenv("ACO_GATEWAY_SESSION_UPSTREAM", raising=False)
    monkeypatch.setattr(gateway_module, "PROVIDER_UPSTREAM", gateway_module.urlsplit(""))
    monkeypatch.setattr(gateway_module, "SESSION_UPSTREAM", gateway_module.urlsplit(""))
    assert gateway_module.main() == 2


def post(base, path, auth=None):
    import urllib.request
    request = urllib.request.Request(base + path, data=b"{}", method="POST")
    if auth:
        request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def _status_handler(status):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = f'{{"error": {status}}}'.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class _HangingHandler(BaseHTTPRequestHandler):
    delay = 1.0

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        time.sleep(self.delay)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            pass  # gateway already gave up on us


class _MidStreamCloseHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: partial\n\n")
        self.wfile.flush()
        # close mid-body without a final chunk: clean FIN, streamed bytes
        # must still reach the client, then the upstream response hits EOF
        self.close_connection = True


class _SlowStreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i in range(3):
                self.wfile.write(f"data: chunk{i}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass  # the relayed client went away mid-stream


def _last_entry(evidence_path, min_lines=1, timeout=2.0):
    """Evidence rows are written after the relay completes; poll for them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if evidence_path.exists():
            lines = evidence_path.read_text().strip().splitlines()
            if len(lines) >= min_lines:
                return json.loads(lines[-1])
        time.sleep(0.05)
    raise AssertionError(f"gateway recorded <{min_lines} evidence rows")


@pytest.mark.parametrize("status", [429, 500])
def test_error_statuses_are_relayed_verbatim(tmp_path, monkeypatch, status):
    stub = _FlexStub(_status_handler(status))
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence, f"{stub.base}/api/v3",
                                 "http://127.0.0.1:1")
    try:
        code, body = post(base, "/api/v3/responses")
        assert code == status and str(status) in body
        entry = _last_entry(evidence)
        assert entry["status"] == status and entry["route"].startswith("127.0.0.1")
    finally:
        server.shutdown()
        stub.close()


def test_unreachable_upstream_becomes_502(tmp_path, monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # nothing listens here anymore
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence,
                                 f"http://127.0.0.1:{dead_port}/api/v3",
                                 "http://127.0.0.1:1")
    try:
        code, _ = post(base, "/api/v3/responses")
        assert code == 502
        entry = _last_entry(evidence)
        assert entry["status"] == 0 and entry["error"] == "ConnectionRefusedError"
    finally:
        server.shutdown()


def test_upstream_timeout_becomes_502(tmp_path, monkeypatch):
    stub = _FlexStub(_HangingHandler)
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence, f"{stub.base}/api/v3",
                                 "http://127.0.0.1:1")
    try:
        monkeypatch.setattr(gateway_module, "UPSTREAM_TIMEOUT_SEC", 0.3)
        started = time.monotonic()
        code, _ = post(base, "/api/v3/responses")
        assert code == 502
        assert time.monotonic() - started < 1.0  # gave up well before the stub's 1.0s
        entry = _last_entry(evidence)
        assert entry["status"] == 0 and entry["error"] == "TimeoutError"
    finally:
        server.shutdown()
        stub.close()


def test_upstream_mid_body_close_is_relayed_and_recorded(tmp_path, monkeypatch):
    stub = _FlexStub(_MidStreamCloseHandler)
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence, f"{stub.base}/api/v3",
                                 stub.base)
    try:
        import http.client as http_client
        from urllib.parse import urlsplit
        parts = urlsplit(base)
        conn = http_client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        conn.request("POST", "/api/v3/responses", body=b"{}")
        response = conn.getresponse()
        assert response.status == 200
        data = response.read()
        assert b"data: partial" in data  # streamed bytes reached the client

        entry = _last_entry(evidence)
        assert entry["status"] == 200  # connection result recorded at upstream EOF

        # the gateway thread is alive and serves the next request
        status, body = get(base, "/v1/session/task", auth="Bearer t")
        assert status == 200 and body == '{"ok": true}'
    finally:
        server.shutdown()
        stub.close()


def test_client_disconnect_mid_stream_still_records(tmp_path, monkeypatch):
    """The relayed client hanging up mid-stream is an OSError the gateway
    survives, and the connection result is still recorded (#38 evidence)."""
    stub = _FlexStub(_SlowStreamHandler)
    evidence = tmp_path / "gateway.jsonl"
    base, server = _spin_gateway(monkeypatch, evidence, f"{stub.base}/api/v3",
                                 stub.base)
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(base)
        sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
        sock.sendall(b"POST /api/v3/responses HTTP/1.1\r\n"
                     b"Host: gateway\r\nContent-Length: 2\r\n\r\n{}")
        data = b""
        while b"data: chunk0\n\n" not in data:
            piece = sock.recv(4096)
            if not piece:
                break
            data += piece
        assert b"data: chunk0" in data  # first chunk relayed
        # RST-close: the gateway's next write must fail and be tolerated
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()

        entry = _last_entry(evidence)
        assert entry["status"] == 200

        status, body = get(base, "/v1/session/task", auth="Bearer t")
        assert status == 200 and body == '{"ok": true}'
    finally:
        server.shutdown()
        stub.close()
