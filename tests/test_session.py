"""Session handling: a reload must not sign the user out.

`/api/auth/me` used to sit behind the blanket `/api/auth/` exclusion in the auth
middleware, so it never received the user context and answered 401 for everyone.
The web app calls it on every page load and treated that 401 as a dead token, so
refreshing the page logged you out.
"""
import datetime as dt

from fastapi.testclient import TestClient

from sparser import portal, store


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "statements.db"
    monkeypatch.setenv("SPARSER_DB", str(db_path))
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    import importlib

    from sparser import api as api_module

    importlib.reload(api_module)
    return TestClient(api_module.app), db_path


def test_me_answers_for_a_valid_token_so_a_reload_keeps_the_session(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = client.get("/api/auth/me", headers=headers)
    assert first.status_code == 200
    assert first.json()["username"] == "owner"
    assert first.json()["members"]

    # A reload is just the same token on a new client.
    assert TestClient(client.app).get("/api/auth/me", headers=headers).status_code == 200


def test_only_a_rejected_token_gives_401(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    })
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Sign-in endpoints stay reachable without one.
    assert client.get("/api/auth/status").status_code == 200
    assert client.post(
        "/api/auth/login", json={"username": "owner", "password": "secure-pass"}
    ).status_code == 200


def test_signing_out_invalidates_the_token_server_side(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/auth/logout", headers=headers).json()["status"] == "signed out"
    # Dropping it in the browser alone would leave it usable for 30 more days.
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert client.get("/api/cards", headers=headers).status_code == 401


def test_expired_sessions_are_pruned_on_the_next_sign_in(tmp_path):
    conn = store.connect(tmp_path / "statements.db")
    created = portal.register(conn, "owner", "secure-pass", "Owner")
    conn.execute(
        "INSERT INTO portal_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        ("dead", created["id"], "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()

    portal.login(conn, "owner", "secure-pass")
    assert conn.execute(
        "SELECT COUNT(*) FROM portal_sessions WHERE token_hash='dead'"
    ).fetchone()[0] == 0
    conn.close()


def test_a_live_token_outlives_a_backend_restart(tmp_path):
    """Sessions live in the database, so restarting the server keeps them valid."""
    conn = store.connect(tmp_path / "statements.db")
    portal.register(conn, "owner", "secure-pass", "Owner")
    token, _ = portal.login(conn, "owner", "secure-pass")
    conn.close()

    reopened = store.connect(tmp_path / "statements.db")
    assert portal.user_for_token(reopened, token)["username"] == "owner"
    reopened.close()
