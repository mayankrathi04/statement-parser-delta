from pathlib import Path

from sparser import portal, store


def test_registration_creates_personal_and_claims_only_legacy_rows(tmp_path: Path):
    conn = store.connect(tmp_path / "data" / "portal.db")
    conn.execute(
        """INSERT INTO cards(issuer,product,masked_number,last4,display_name)
           VALUES('Test',NULL,'XXXX1111','1111','Test ••1111')"""
    )
    first = portal.register(conn, "owner_1", "secure-pass", "Owner")
    personal = first["members"][0]
    assert personal["name"] == "Personal"
    assert conn.execute("SELECT member_id FROM cards").fetchone()["member_id"] == personal["id"]

    second = portal.register(conn, "owner_2", "secure-pass", "Second")
    assert second["members"][0]["id"] != personal["id"]
    assert portal.allowed_member_ids(conn, second["id"]) == [second["members"][0]["id"]]

    token, signed_in = portal.login(conn, "owner_1", "secure-pass")
    assert signed_in["username"] == "owner_1"
    assert portal.user_for_token(conn, token)["id"] == first["id"]
    conn.close()


def test_credential_replacement_preserves_user_and_member_ids(tmp_path: Path):
    conn = store.connect(tmp_path / "portal.db")
    created = portal.register(conn, "before", "secure-pass", "Before")
    member_id = created["members"][0]["id"]
    portal.replace_credentials(conn, created["id"], "after", "new-secure-pass")
    _, current = portal.login(conn, "after", "new-secure-pass")
    assert current["id"] == created["id"]
    assert current["members"][0]["id"] == member_id
    conn.close()
