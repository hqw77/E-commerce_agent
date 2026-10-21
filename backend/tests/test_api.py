from fastapi.testclient import TestClient

from api.routes import app


def test_health_endpoint_reports_runtime_mode():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["env"] in {"dev", "staging", "production"}


def test_capabilities_endpoint_returns_manifest():
    with TestClient(app) as client:
        response = client.get("/capabilities")
    assert response.status_code == 200
    assert "features" in response.json()


def test_request_id_header_is_returned():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.headers.get("X-Request-Id")
