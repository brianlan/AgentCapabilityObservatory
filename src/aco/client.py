"""Minimal HTTP client for the ACO management API (#17).

Stdlib-only (urllib): the CLI is a thin management client — it translates
commands into API calls and never talks to the database, the execution
engine, or computes statistics. Credentials are sent as a bearer header and
are never included in error messages or output.
"""

import json
import urllib.error
import urllib.request


class ApiError(Exception):
    """Non-2xx API response with a parsed error envelope."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"[{code}] {message}")


class ApiUnavailable(Exception):
    """The API could not be reached at all."""


class Client:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload=None,
                idempotency_key: str | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                err = json.loads(raw).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            # only the API's own error envelope is surfaced; request headers
            # (which carry the token) are never part of any message
            raise ApiError(exc.code, err.get("code", "http_error"),
                           err.get("message", raw.decode(errors="replace")[:200])) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiUnavailable(f"cannot reach API at {self.base_url}: {reason}") from None

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)

    def post(self, path: str, payload=None,
             idempotency_key: str | None = None) -> tuple[int, dict]:
        return self.request("POST", path, payload, idempotency_key=idempotency_key)
