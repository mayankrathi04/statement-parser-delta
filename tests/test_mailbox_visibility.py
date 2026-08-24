"""A connected mailbox must stay visible to the member selector.

Mailboxes are swept by every scan whoever owns them — what they deliver is
filed under the owner, not under whoever happens to be selected. The bootstrap
flag behind the Pipeline banner was nonetheless computed over the *selected*
member's mailboxes, so choosing a member who owns none reported "No mailbox
connected yet" while the scan picker on the same screen listed all of them.
"""
import importlib

from fastapi.testclient import TestClient

from sparser import accounts, store


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARSER_DB", str(tmp_path / "statements.db"))
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    monkeypatch.delenv("SPARSER_GMAIL", raising=False)
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    from sparser import api as api_module

    importlib.reload(api_module)
    client = TestClient(api_module.app)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_a_mailbox_owned_by_one_member_stays_connected_for_another(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    personal = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    second = client.post("/api/members", headers=headers, json={"name": "Golu"}).json()["id"]

    conn = store.connect(tmp_path / "statements.db")
    try:
        accounts.add(conn, "owner@example.com", "aaaabbbbccccdddd", member_id=personal)
    finally:
        conn.close()

    assert client.get("/api/bootstrap", headers=headers).json()["mailboxes_configured"]
    # The member selector narrows what is analysed, never whether a mailbox exists.
    scoped = client.get(f"/api/bootstrap?members={second}", headers=headers).json()
    assert scoped["mailboxes_configured"]
    # And it agrees with the list the scan picker on the same screen is built from.
    assert [box["address"] for box in client.get("/api/mailboxes", headers=headers).json()
            ["mailboxes"]] == ["owner@example.com"]
