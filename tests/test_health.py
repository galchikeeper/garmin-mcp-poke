"""The process health endpoint must never authenticate or refresh Garmin."""
import sys
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_health_is_responsive_without_credentials_and_never_contacts_garmin(monkeypatch):
    import requests
    from garminconnect import Garmin
    from garth.http import Client

    def forbidden(*args, **kwargs):
        raise AssertionError("health must not contact Garmin or external storage")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(Garmin, "login", forbidden)
    monkeypatch.setattr(Client, "refresh_oauth2", forbidden)
    import server
    from garmin_client import GarminProxy
    monkeypatch.setattr(server, "garmin_client", GarminProxy())

    with TestClient(server.mcp.http_app()) as client:
        for _ in range(3):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["authenticated"] is False
            assert data["server_password_login_enabled"] is False
            assert data["oauth1_expires_at"] is None
            assert data["last_login_at"] is None
            assert "tokens" not in data
            assert "GITHUB_TOKEN" not in response.text
