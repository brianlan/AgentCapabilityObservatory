"""Fixed-configuration trial gateway (#38): the network seam between the
evaluated container and everything else.

One compose service per trial exposes exactly two routes to the agent
container — the ACO session surface and the single fixed provider upstream
derived from the provider profile. Everything else is denied and logged.
Stdlib only, so it runs on the pinned python image with no install step.
Logs record connection results (route, path, status) — never request or
response bodies, and never the Authorization header.
"""

import http.client
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SESSION_PREFIX = "/v1/session/"
EVIDENCE_FILE = os.environ.get("ACO_GATEWAY_EVIDENCE", "")
# per-upstream connect/read timeout; module constant so tests can shrink it
UPSTREAM_TIMEOUT_SEC = 120
# optional CA bundle for upstream TLS verification (private/mocked upstreams);
# unset means the default system trust store (real Ark uses a public CA)
CA_BUNDLE = os.environ.get("ACO_GATEWAY_CA_BUNDLE", "")

PROVIDER_UPSTREAM = urlsplit(os.environ.get("ACO_GATEWAY_PROVIDER_UPSTREAM", ""))
SESSION_UPSTREAM = urlsplit(os.environ.get("ACO_GATEWAY_SESSION_UPSTREAM", ""))
# provider path prefix on the incoming request (from the profile base URL)
PROVIDER_PREFIX = (PROVIDER_UPSTREAM.path.rstrip("/") or "") if PROVIDER_UPSTREAM.path else ""


def _record(entry: dict) -> None:
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = json.dumps(entry, sort_keys=True)
    if EVIDENCE_FILE:
        try:
            with open(EVIDENCE_FILE, "a") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # evidence is best-effort; the connection result still stands


class _Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # routed evidence replaces default stderr noise
        pass

    def _upstream_for(self, path: str):
        if path.startswith(SESSION_PREFIX) and SESSION_UPSTREAM.netloc:
            return SESSION_UPSTREAM
        if PROVIDER_UPSTREAM.netloc and (
            path.startswith(PROVIDER_PREFIX + "/") if PROVIDER_PREFIX
            else path.startswith("/")
        ):
            return PROVIDER_UPSTREAM
        return None

    @staticmethod
    def _connection_for(upstream):
        """Scheme-correct upstream connection (#38 reopen): https dials TLS
        with certificate verification; http stays plaintext for local mocks;
        anything else fails closed. SNI and Host follow netloc automatically."""
        if upstream.scheme == "https":
            if CA_BUNDLE:
                context = ssl.create_default_context(cafile=CA_BUNDLE)
            else:
                context = ssl.create_default_context()
            return http.client.HTTPSConnection(upstream.netloc, timeout=UPSTREAM_TIMEOUT_SEC,
                                               context=context)
        if upstream.scheme == "http":
            return http.client.HTTPConnection(upstream.netloc, timeout=UPSTREAM_TIMEOUT_SEC)
        return None

    def do_POST(self):
        self._relay()

    def do_GET(self):
        self._relay()

    def _relay(self):
        upstream = self._upstream_for(self.path)
        if upstream is None:
            _record({"route": "denied", "method": self.command,
                     "path": self.path, "status": 403})
            body = b'{"error": {"code": "egress_denied", "message": "target not allowed"}}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        started = time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "transfer-encoding")}
        connection = self._connection_for(upstream)
        if connection is None:
            # unknown upstream scheme: fail closed, leave secret-free evidence (#38 reopen)
            _record({"route": upstream.netloc, "method": self.command,
                     "path": self.path, "status": 502, "error": "unsupported_upstream_scheme"})
            self.send_error(502, "unsupported upstream scheme")
            return
        try:
            connection.request(self.command, self.path,
                               body=self.rfile.read(length) if length else None,
                               headers=headers)
            response = connection.getresponse()
        except OSError as exc:
            _record({"route": upstream.netloc, "method": self.command,
                     "path": self.path, "status": 0, "error": type(exc).__name__})
            self.send_error(502, "upstream unreachable")
            return

        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in ("connection", "transfer-encoding"):
                continue
            self.send_header(key, value)
        if response.getheader("Content-Length") is None:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            while chunk := response.read(4096):  # streaming relay: SSE stays live
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass  # client hung up mid-stream; nothing to record beyond the status
        finally:
            response.close()
            connection.close()
        _record({"route": upstream.netloc, "method": self.command,
                 "path": self.path, "status": response.status,
                 "ms": int((time.monotonic() - started) * 1000)})


def main() -> int:
    missing = [name for name, value in (
        ("ACO_GATEWAY_PROVIDER_UPSTREAM", PROVIDER_UPSTREAM.netloc),
        ("ACO_GATEWAY_SESSION_UPSTREAM", SESSION_UPSTREAM.netloc),
    ) if not value]
    if missing:
        print(f"gateway requires upstream config: {', '.join(missing)}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(("0.0.0.0", 80), _Gateway)
    _record({"route": "startup", "provider": PROVIDER_UPSTREAM.netloc,
             "session": SESSION_UPSTREAM.netloc, "status": 0})
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
