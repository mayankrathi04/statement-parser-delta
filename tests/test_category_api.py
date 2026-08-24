"""The HTTP surface the Categories screen drives.

The module-level tests cover the rules; these cover the wiring, because the two
have been wrong independently — a working `link()` reached through a route that
never passed `major_id` would still leave the screen doing nothing.
"""
import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARSER_DB", str(tmp_path / "statements.db"))
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    from sparser import api as api_module

    importlib.reload(api_module)
    client = TestClient(api_module.app)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_the_list_arrives_split_into_majors_subs_and_what_is_unfiled(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    body = client.get("/api/categories", headers=headers).json()

    assert {"categories", "majors", "unmapped", "provider_categories"} <= set(body)
    names = {row["name"] for row in body["majors"]}
    assert {"Household & Grocery", "Outing", "Transfers"} <= names
    # The seeded majors are NOT in the sub list — that mixing is the bug this split fixes.
    assert names.isdisjoint(
        {row["name"] for row in body["categories"] if row["pattern"] is None}
    )
    outing = next(row for row in body["majors"] if row["name"] == "Outing")
    assert {"Food & Dining", "Entertainment"} <= {row["name"] for row in outing["children"]}


def test_every_list_carries_usage_counts(tmp_path, monkeypatch):
    """`majors` and `unmapped` are separate reads of the same rows.

    Decorating only the flat list left the others without `usage`, and the
    screen reads `usage.cards` — so the Categories page rendered blank rather
    than showing a missing number.
    """
    client, headers = _client(tmp_path, monkeypatch)
    body = client.get("/api/categories", headers=headers).json()

    everywhere = [
        *body["categories"], *body["unmapped"],
        *(child for major in body["majors"] for child in major["children"]),
    ]
    assert everywhere
    for row in everywhere:
        assert set(row.get("usage", {})) == {"cards", "bank"}, row["name"]


def test_a_sub_can_be_created_under_a_major_and_moved_between_them(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    majors = client.get("/api/categories", headers=headers).json()["majors"]
    outing = next(row for row in majors if row["name"] == "Outing")
    gifting = next(row for row in majors if row["name"] == "Gifting")

    created = client.post("/api/categories", headers=headers, json={
        "name": "Board games", "pattern": None, "applies_to": "both", "major_id": outing["id"],
    }).json()
    assert created["major"] == "Outing"

    moved = client.put(
        f"/api/categories/{created['id']}/major", headers=headers, json={"major_id": gifting["id"]},
    ).json()
    assert moved["major"] == "Gifting"

    unfiled = client.put(
        f"/api/categories/{created['id']}/major", headers=headers, json={"major_id": None},
    ).json()
    assert unfiled["major"] is None
    body = client.get("/api/categories", headers=headers).json()
    assert "Board games" in {row["name"] for row in body["unmapped"]}


def test_majors_can_be_added_renamed_and_deleted_over_http(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    created = client.post("/api/major-categories", headers=headers, json={"name": "Hobbies"}).json()
    assert created["children"] == []

    assert client.post(
        "/api/major-categories", headers=headers, json={"name": "hobbies"},
    ).status_code == 422

    renamed = client.put(
        f"/api/major-categories/{created['id']}", headers=headers, json={"name": "Hobby spend"},
    ).json()
    assert renamed["name"] == "Hobby spend"

    outing = next(
        row for row in client.get("/api/categories", headers=headers).json()["majors"]
        if row["name"] == "Outing"
    )
    removed = client.delete(f"/api/major-categories/{outing['id']}", headers=headers).json()
    assert removed["unmapped"] == len(outing["children"])
    body = client.get("/api/categories", headers=headers).json()
    assert "Outing" not in {row["name"] for row in body["majors"]}
    # Its rules survive, unfiled.
    assert "Food & Dining" in {row["name"] for row in body["unmapped"]}


def test_an_unknown_major_is_a_404_not_a_silent_unfiling(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    sub = client.get("/api/categories", headers=headers).json()["categories"][0]
    assert client.put(
        f"/api/categories/{sub['id']}/major", headers=headers, json={"major_id": 99999},
    ).status_code == 404
