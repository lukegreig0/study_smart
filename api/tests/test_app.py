import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.config import Config, ConfigError


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_devices(client):
    body = client.get("/api/devices").json()
    assert len(body) == 4
    assert body[0] == {
        "device_id": "placemat-0007", "space_id": "desk-07", "node_id": "node-floor2",
        "node_online": True, "device_type": "placemat", "firmware": "0.1.0",
        "state": "occupied", "since": "2026-11-16T12:05:00Z",
    }


def test_device_never_seen_in_data_has_null_since(client):
    body = client.get("/api/devices/placemat-0099").json()
    assert (body["state"], body["since"], body["device_type"]) == ("unknown", None, None)


def test_device_not_found(client):
    assert client.get("/api/devices/nope").status_code == 404
    assert client.get("/api/devices/nope/history").status_code == 404


def test_history(client):
    body = client.get("/api/devices/placemat-0007/history").json()
    assert body == [
        {"state": "occupied", "changed_at": "2026-11-16T12:05:00Z"},
        {"state": "free", "changed_at": "2026-11-16T12:00:00Z"},
    ]


def test_history_since_accepts_any_timezone(client):
    # 13:01 in UTC+1 is 12:01Z, so only the 12:05Z change is included.
    r = client.get("/api/devices/placemat-0007/history",
                   params={"since": "2026-11-16T13:01:00+01:00"})
    assert [h["changed_at"] for h in r.json()] == ["2026-11-16T12:05:00Z"]


@pytest.mark.parametrize("since", ["yesterday", "2026-11-16T12:00:00"])
def test_history_rejects_bad_since(client, since):
    r = client.get("/api/devices/placemat-0007/history", params={"since": since})
    assert r.status_code == 422


@pytest.mark.parametrize("limit", [0, 1001, "many"])
def test_limit_is_validated(client, limit):
    assert client.get("/api/events", params={"limit": limit}).status_code == 422


def test_events(client):
    body = client.get("/api/events", params={"limit": 2}).json()
    assert body[0] == {"device_id": "placemat-0007", "space_id": "desk-07",
                       "state": "occupied", "changed_at": "2026-11-16T12:05:00Z"}
    assert len(body) == 2


def test_nodes(client):
    body = client.get("/api/nodes").json()
    assert [(n["node_id"], n["online"], n["device_count"]) for n in body] == [
        ("node-floor2", True, 3), ("node-floor3", False, 1),
    ]


def test_summary(client):
    assert client.get("/api/summary").json() == {
        "free": 1, "occupied": 1, "unknown": 2, "total": 4,
    }


def test_web_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Study space desks" in r.text


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/devices/{device_id}/history" in paths


def test_api_sees_host_writes_without_restart(client, db_path):
    # The host writes while the API is running; the next request sees it.
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE devices SET state='free', since='2026-11-16T12:20:00Z'"
                     " WHERE device_id='placemat-0007'")
    conn.close()
    assert client.get("/api/devices/placemat-0007").json()["state"] == "free"


def test_503_before_host_has_created_the_database(tmp_path):
    client = TestClient(create_app(Config(db_path=str(tmp_path / "not-yet.db"))))
    for path in ("/api/health", "/api/devices", "/api/summary"):
        assert client.get(path).status_code == 503
    assert client.get("/").status_code == 200  # the page still loads and shows the error


def test_503_when_database_has_no_tables(tmp_path):
    path = tmp_path / "blank.db"
    sqlite3.connect(path).close()
    client = TestClient(create_app(Config(db_path=str(path))))
    assert client.get("/api/health").status_code == 503
    assert client.get("/api/devices").status_code == 503


def test_config_from_env():
    cfg = Config.from_env({"API_PORT": "9000", "DB_PATH": "/data/x.db", "LOG_LEVEL": "debug"})
    assert (cfg.api_port, cfg.db_path, cfg.log_level, cfg.api_host) == (
        9000, "/data/x.db", "DEBUG", "127.0.0.1")


@pytest.mark.parametrize("env", [{"API_PORT": "x"}, {"API_PORT": "0"}, {"LOG_LEVEL": "LOUD"}])
def test_config_rejects(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)
