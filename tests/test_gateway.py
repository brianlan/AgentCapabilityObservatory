"""Gateway seam tests (#38): routing, denial, and secret-free evidence."""

import json
import threading
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


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    stub = _Stub()
    evidence = tmp_path / "gateway.jsonl"
    monkeypatch.setenv("ACO_GATEWAY_PROVIDER_UPSTREAM", f"{stub.base}/api/v3")
    monkeypatch.setenv("ACO_GATEWAY_SESSION_UPSTREAM", f"{stub.base}")
    monkeypatch.setenv("ACO_GATEWAY_EVIDENCE", str(evidence))
    import importlib
    importlib.reload(gateway_module)
    server = gateway_module.ThreadingHTTPServer(("127.0.0.1", 0), gateway_module._Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", stub, evidence
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
