import os

# The management surface fails closed without a credential. Unit tests fix one
# before aco.app is imported (its module-level apps resolve the env).
os.environ.setdefault("ACO_MANAGEMENT_TOKEN", "test-management-token")

import pytest
from fastapi.testclient import TestClient

from aco.app import create_management_app, create_session_app

MGMT_TOKEN = "test-management-token"
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}


@pytest.fixture
def client(tmp_path):
    """Management-surface client: every request carries the management bearer."""
    app = create_management_app(data_root=str(tmp_path), token=MGMT_TOKEN)
    return TestClient(app, headers=MGMT_AUTH)


@pytest.fixture
def session_client(tmp_path):
    """Session-surface client: the untrusted agent's view (no default auth)."""
    return TestClient(create_session_app(data_root=str(tmp_path)))


@pytest.fixture
def register(client):
    def _register(kind, name, version, content, assets=None, expect=(200, 201)):
        resp = client.post(
            "/v1/versions",
            json={"kind": kind, "name": name, "version": version, "content": content,
                  "assets": assets or []},
        )
        assert resp.status_code in expect, resp.text
        return resp
    return _register
