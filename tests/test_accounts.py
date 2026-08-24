from sparser import accounts, store


def test_profile_is_encrypted_and_round_trips_after_reopen(tmp_path, monkeypatch):
    db_path = tmp_path / "profile.db"
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))

    conn = store.connect(db_path)
    accounts.set_profile(conn, "Example Person", "01/02/1990")
    raw = conn.execute("SELECT full_name, dob FROM profile WHERE id = 1").fetchone()
    assert b"Example Person" not in raw["full_name"]
    assert b"01/02/1990" not in raw["dob"]
    conn.close()

    reopened = store.connect(db_path)
    assert accounts.get_profile(reopened) == {
        "full_name": "Example Person",
        "dob": "01/02/1990",
        "updated_at": accounts.get_profile(reopened)["updated_at"],
    }
    reopened.close()


def test_every_member_profile_derives_passwords_not_only_the_first(tmp_path, monkeypatch):
    """A second member's statement must open on the second member's details.

    Password derivation read the pre-members profile alone, so a statement for
    anyone but the first member stayed locked — "encrypted and no supplied
    password worked" — however correctly their name and date of birth had been
    entered on their own profile.
    """
    from pathlib import Path

    from sparser import bank_pipeline, portal
    from sparser.decrypt import candidate_passwords

    db_path = tmp_path / "profiles.db"
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    conn = store.connect(db_path)
    owner = portal.register(conn, "owner", "secure-pass", "Owner")
    first = owner["members"][0]["id"]
    second = portal.add_member(conn, owner["id"], "Second")["id"]
    accounts.set_profile(conn, "First Person", "01/02/1990")
    accounts.set_profile(conn, "First Person", "01/02/1990", member_id=first)
    accounts.set_profile(conn, "Second Person", "03/04/1992", member_id=second)

    names = [p["full_name"] for p in accounts.all_profiles(conn)]
    assert names == ["First Person", "Second Person"]
    # The run's own member is tried first, so the common case stays cheap.
    assert accounts.all_profiles(conn, second)[0]["full_name"] == "Second Person"
    conn.close()

    wanted = candidate_passwords("Second Person", "03/04/1992")[0]
    for creds in ({}, {"_member_id": first}, {"_member_id": second}):
        assert wanted in bank_pipeline._password_candidates(Path(db_path), creds)
