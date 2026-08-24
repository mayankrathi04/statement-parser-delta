"""Opening the PDF behind a statement that has already been imported.

The Cards and Bank Accounts tabs list every statement they parsed, by file name.
The file itself was reachable only while the statement sat in the review queue —
once imported, the name on screen pointed at nothing openable.
"""
import importlib

import pikepdf
from fastapi.testclient import TestClient

from sparser import accounts, store


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARSER_DB", str(tmp_path / "statements.db"))
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    (tmp_path / "inbox").mkdir(exist_ok=True)
    monkeypatch.setenv("SPARSER_INBOX", str(tmp_path / "inbox"))
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    from sparser import api as api_module

    importlib.reload(api_module)
    client = TestClient(api_module.app)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def _pdf(path, password=None):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    if password:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password))
    else:
        pdf.save(path)
    return path


def _imported_card_statement(db_path, pdf_path, member_id):
    """The rows an import leaves behind: a card, its statement, its ingest file."""
    conn = store.connect(db_path)
    card = conn.execute(
        """INSERT INTO cards(issuer,masked_number,last4,display_name,member_id)
           VALUES('Test','XXXX1111','1111','Test ••1111',?)""",
        (member_id,),
    ).lastrowid
    statement = conn.execute(
        """INSERT INTO statements(card_id,source_file,statement_date,period_start,period_end)
           VALUES(?,?,'2026-01-15','2025-12-16','2026-01-15')""",
        (card, pdf_path.name),
    ).lastrowid
    run = conn.execute(
        "INSERT INTO ingest_runs (kind,status) VALUES ('import','done')"
    ).lastrowid
    conn.execute(
        """INSERT INTO ingest_files (run_id,filename,status,path) VALUES (?,?,'ok',?)""",
        (run, pdf_path.name, str(pdf_path)),
    )
    conn.commit()
    conn.close()
    return int(statement)


def _imported_bank_statement(db_path, pdf_path, member_id):
    """The rows a bank import leaves behind, mirroring `_imported_card_statement`."""
    conn = store.connect(db_path)
    accounts.ensure_schema(conn)
    account = conn.execute(
        """INSERT INTO bank_accounts(bank_code,bank_name,masked_number,last4,display_name,
                                     account_fingerprint,member_id,created_at)
           VALUES('HDFC','HDFC Bank','XXXX0972','0972','HDFC ••0972','fp-1',?,'2026-01-01T00:00:00')""",
        (member_id,),
    ).lastrowid
    statement = conn.execute(
        """INSERT INTO bank_statements(account_id,source_file,parser_id,period_start,
                                       period_end,opening_balance,closing_balance,imported_at)
           VALUES(?,?,'hdfc_bank_v1','2026-01-01','2026-01-31',0,0,'2026-02-01T00:00:00')""",
        (account, pdf_path.name),
    ).lastrowid
    run = conn.execute(
        "INSERT INTO ingest_runs (kind,status) VALUES ('bank_import','done')"
    ).lastrowid
    conn.execute(
        "INSERT INTO ingest_files (run_id,filename,status,path) VALUES (?,?,'ok',?)",
        (run, pdf_path.name, str(pdf_path)),
    )
    conn.commit()
    conn.close()
    return int(statement)


def test_an_imported_statement_serves_its_pdf(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    member = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    statement = _imported_card_statement(
        tmp_path / "statements.db", _pdf(tmp_path / "plain.pdf"), member
    )

    response = client.get(f"/api/statements/{statement}/pdf", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_an_encrypted_statement_is_unlocked_from_the_saved_profile(tmp_path, monkeypatch):
    """The viewer derives what ingestion derived, so a statement that imported
    without a typed password opens again without one."""
    client, headers = _client(tmp_path, monkeypatch)
    member = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    client.put("/api/profile", headers=headers, params={"member_id": member},
               json={"full_name": "Example Person", "dob": "01/02/1990"})

    from sparser.decrypt import candidate_passwords

    locked = _pdf(tmp_path / "locked.pdf", candidate_passwords("Example Person", "01/02/1990")[0])
    statement = _imported_card_statement(tmp_path / "statements.db", locked, member)

    response = client.get(f"/api/statements/{statement}/pdf", headers=headers)

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_a_statement_whose_file_is_gone_says_so(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    member = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    missing = tmp_path / "deleted.pdf"
    _pdf(missing)
    statement = _imported_card_statement(tmp_path / "statements.db", missing, member)
    missing.unlink()

    response = client.get(f"/api/statements/{statement}/pdf", headers=headers)

    assert response.status_code == 404
    assert "no longer on disk" in response.json()["detail"]


def test_a_statement_outside_the_users_members_is_not_readable_by_id(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    statement = _imported_card_statement(
        tmp_path / "statements.db", _pdf(tmp_path / "other.pdf"), member_id=999
    )

    assert client.get(f"/api/statements/{statement}/pdf", headers=headers).status_code == 404


def test_a_locked_statement_asks_for_the_password_instead_of_erroring(tmp_path, monkeypatch):
    """The link is clicked by a person, so the dead end has to be actionable: a
    field and a button, not a JSON error they can do nothing with."""
    client, headers = _client(tmp_path, monkeypatch)
    member = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    locked = _pdf(tmp_path / "bank.pdf", "nothing-derives-this")
    statement = _imported_bank_statement(tmp_path / "statements.db", locked, member)

    page = client.get(f"/api/bank/statements/{statement}/pdf", headers=headers)

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'name="password"' in page.text and "View PDF" in page.text
    assert "Bank Accounts tab" in page.text
    # The identity of the file travels with the form, so the retry knows what to open.
    assert f'value="{statement}"' in page.text and 'value="bank"' in page.text


def test_the_supplied_password_opens_it_and_a_wrong_one_asks_again(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    member = client.get("/api/members", headers=headers).json()["members"][0]["id"]
    locked = _pdf(tmp_path / "bank.pdf", "typed-by-hand")
    statement = _imported_bank_statement(tmp_path / "statements.db", locked, member)

    opened = client.post("/api/pdf/unlock", headers=headers, data={
        "kind": "bank", "ident": statement, "password": "typed-by-hand",
    })
    assert opened.status_code == 200
    assert opened.headers["content-type"] == "application/pdf"
    assert opened.content.startswith(b"%PDF")

    refused = client.post("/api/pdf/unlock", headers=headers, data={
        "kind": "bank", "ident": statement, "password": "wrong",
    })
    assert refused.headers["content-type"].startswith("text/html")
    assert "did not open it" in refused.text


# --------------------------------------------- the run history's own PDF route

def _run_with_file(db_path, filename, *, status="failed", path=None, member_id=None):
    conn = store.connect(db_path)
    run = conn.execute(
        "INSERT INTO ingest_runs (kind,status) VALUES ('bank_scan','done')"
    ).lastrowid
    file_id = conn.execute(
        "INSERT INTO ingest_files (run_id,filename,status,path,member_id) VALUES (?,?,?,?,?)",
        (run, filename, status, path, member_id),
    ).lastrowid
    conn.commit()
    conn.close()
    return int(run), int(file_id)


def test_a_failed_row_opens_its_pdf_although_it_kept_no_path(tmp_path, monkeypatch):
    """The row worth looking at is the one that failed — and a failed row records
    no path at all, so the file has to be found by name in the inbox."""
    client, headers = _client(tmp_path, monkeypatch)
    _pdf(tmp_path / "inbox" / "statement.pdf")
    _, file_id = _run_with_file(tmp_path / "statements.db", "statement.pdf")

    response = client.get(f"/api/ingest/files/{file_id}/pdf", headers=headers)

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_the_run_history_says_which_files_can_still_be_opened(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    _pdf(tmp_path / "inbox" / "here.pdf")
    run, _ = _run_with_file(tmp_path / "statements.db", "here.pdf")
    conn = store.connect(tmp_path / "statements.db")
    conn.execute(
        "INSERT INTO ingest_files (run_id,filename,status) VALUES (?,'gone.pdf','failed')", (run,)
    )
    conn.commit()
    conn.close()

    files = client.get(f"/api/bank/runs/{run}", headers=headers).json()["files"]

    assert {row["filename"]: row["pdf_available"] for row in files} == {
        "here.pdf": True, "gone.pdf": False,
    }


def test_an_ingest_file_of_a_member_this_user_cannot_see_is_not_readable(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    _pdf(tmp_path / "inbox" / "theirs.pdf")
    _, file_id = _run_with_file(tmp_path / "statements.db", "theirs.pdf", member_id=999)

    assert client.get(f"/api/ingest/files/{file_id}/pdf", headers=headers).status_code == 404
