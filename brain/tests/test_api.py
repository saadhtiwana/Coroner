from fastapi.testclient import TestClient

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.api import app

client = TestClient(app)


def test_healthz_reports_versions() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["contract_version"] == CONTRACT_VERSION
