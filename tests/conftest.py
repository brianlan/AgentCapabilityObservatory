import pytest
from fastapi.testclient import TestClient

from aco.app import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(data_root=str(tmp_path)))


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
