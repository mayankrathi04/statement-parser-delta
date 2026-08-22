"""The inbox is a folder per card and per account, not a flat pile of PDFs.

Two things have to hold for that layout to be safe. A statement must be filed
under the card the *parser* identified, never under a guess from the file name;
and a statement that has been filed must still count as "already downloaded",
or every mail sweep would fetch the whole archive again.
"""
import datetime as dt

import pytest

from sparser import inbox
from sparser.mailbox import _download_path


def _pdf(path, payload=b"%PDF-1.4 statement"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_slug_turns_a_display_name_into_a_folder_name():
    assert inbox.slug("Example Bank Regalia ••1111") == "example-bank-regalia-1111"
    assert inbox.slug("Second FIRST Bank Savings ••2222") == "second-first-bank-savings-2222"
    # Nothing usable left is still a folder, never an empty path component.
    assert inbox.slug("••••") == "unknown"
    assert inbox.slug("") == "unknown"


def test_landing_is_the_unsorted_folder_for_that_kind(tmp_path):
    assert inbox.landing(tmp_path, inbox.CARDS) == tmp_path / "cards" / "_unsorted"
    assert inbox.landing(tmp_path, inbox.BANK).is_dir()


def test_file_under_moves_a_download_into_its_card_folder(tmp_path):
    pdf = _pdf(inbox.landing(tmp_path, inbox.CARDS) / "202601_you_statement.pdf")

    filed = inbox.file_under(pdf, tmp_path, inbox.CARDS, "Example Bank Regalia ••1111")

    assert filed == tmp_path / "cards" / "example-bank-regalia-1111" / "202601_you_statement.pdf"
    assert filed.is_file() and not pdf.exists()
    # Filing again is a no-op rather than a second move.
    assert inbox.file_under(filed, tmp_path, inbox.CARDS, "Example Bank Regalia ••1111") == filed


def test_file_under_leaves_files_outside_the_inbox_alone(tmp_path):
    """Importing someone's ~/Downloads must not relocate their files."""
    elsewhere = _pdf(tmp_path / "downloads" / "statement.pdf")

    assert inbox.file_under(elsewhere, tmp_path / "inbox", inbox.CARDS, "Some Card ••1111") == elsewhere
    assert elsewhere.is_file()


def test_file_under_without_a_label_keeps_the_file_unsorted(tmp_path):
    pdf = _pdf(inbox.landing(tmp_path, inbox.BANK) / "mystery.pdf")

    assert inbox.file_under(pdf, tmp_path, inbox.BANK, "") == pdf


def test_filing_the_same_bytes_twice_keeps_one_copy(tmp_path):
    label = "Example Bank Savings ••3333"
    first = _pdf(inbox.landing(tmp_path, inbox.BANK) / "statement.pdf", b"same")
    filed = inbox.file_under(first, tmp_path, inbox.BANK, label)

    again = _pdf(inbox.landing(tmp_path, inbox.BANK) / "statement.pdf", b"same")
    assert inbox.file_under(again, tmp_path, inbox.BANK, label) == filed
    assert not again.exists()
    assert len(list(filed.parent.iterdir())) == 1


def test_filing_a_different_statement_of_the_same_name_keeps_both(tmp_path):
    label = "Example Bank Savings ••3333"
    first = inbox.file_under(
        _pdf(inbox.landing(tmp_path, inbox.BANK) / "statement.pdf", b"january"),
        tmp_path, inbox.BANK, label,
    )
    second = inbox.file_under(
        _pdf(inbox.landing(tmp_path, inbox.BANK) / "statement.pdf", b"february"),
        tmp_path, inbox.BANK, label,
    )

    assert second != first
    assert second.name == "statement-2.pdf"
    assert first.read_bytes() == b"january" and second.read_bytes() == b"february"


def test_pdfs_and_index_see_into_every_folder(tmp_path):
    a = _pdf(tmp_path / "cards" / "example-bank-regalia-1111" / "one.pdf")
    b = _pdf(tmp_path / "bank" / "_unsorted" / "two.PDF")
    _pdf(tmp_path / "cards" / "notes.txt", b"not a statement")

    assert inbox.pdfs(tmp_path) == sorted([a, b])
    assert inbox.index(tmp_path) == {"one.pdf": a, "two.PDF": b}
    assert inbox.find(tmp_path, "two.PDF") == b
    assert inbox.find(tmp_path, "missing.pdf") is None


def test_a_filed_attachment_still_counts_as_already_downloaded(tmp_path):
    """The regression this layout could have caused.

    Downloads land in ``_unsorted`` but are filed under their card once parsed,
    so the path a sweep would write to is empty on the next run. Without the
    inbox-wide lookup every month's sweep would re-download the whole archive.
    """
    landing = inbox.landing(tmp_path, inbox.CARDS)
    args = (landing, "you@gmail.com", "Statement", "Credit_Card_Statement.pdf")
    when = dt.datetime(2026, 6, 10)

    first, exists = _download_path(*args, when, b"101", 1, b"airtel", {})
    assert not exists
    first.write_bytes(b"airtel")
    filed = inbox.file_under(first, tmp_path, inbox.CARDS, "Third Bank Airtel ••4444")
    assert filed.parent.name == "third-bank-airtel-4444"

    found, exists = _download_path(
        *args, when, b"101", 1, b"airtel", inbox.index(tmp_path)
    )
    assert (found, exists) == (filed, True)


def test_a_different_attachment_of_the_same_name_is_still_downloaded(tmp_path):
    landing = inbox.landing(tmp_path, inbox.CARDS)
    args = (landing, "you@gmail.com", "Statement", "Credit_Card_Statement.pdf")
    when = dt.datetime(2026, 6, 10)

    first, _ = _download_path(*args, when, b"101", 1, b"airtel", {})
    first.write_bytes(b"airtel")
    inbox.file_under(first, tmp_path, inbox.CARDS, "Third Bank Airtel ••4444")

    other, exists = _download_path(
        *args, when, b"202", 1, b"flipkart", inbox.index(tmp_path)
    )
    assert not exists
    assert other == landing / "202606_you_Credit_Card_Statement_uid202_1.pdf"


@pytest.mark.parametrize("kind", inbox.KINDS)
def test_root_follows_the_configured_inbox(monkeypatch, tmp_path, kind):
    monkeypatch.setenv("SPARSER_INBOX", str(tmp_path / "elsewhere"))
    assert inbox.landing(inbox.root(), kind) == tmp_path / "elsewhere" / kind / "_unsorted"


def test_an_upload_lands_in_the_bank_folder_and_not_in_a_hash_directory(tmp_path, monkeypatch):
    """Uploads used to go to inbox/bank-uploads/<32 hex chars>/<file>.

    One folder per upload made the inbox unreadable and told nobody anything the
    ingest run did not already record, so an upload now lands in the same place
    a mail download does and is filed under its account by the parser.
    """
    import importlib

    monkeypatch.setenv("SPARSER_DB", str(tmp_path / "statements.db"))
    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    monkeypatch.setenv("SPARSER_INBOX", str(tmp_path / "inbox"))
    monkeypatch.delenv("DEFAULT_USERNAME", raising=False)
    monkeypatch.delenv("DEFAULT_PASSWORD", raising=False)
    from fastapi.testclient import TestClient

    from sparser import api as api_module

    importlib.reload(api_module)
    client = TestClient(api_module.app)
    token = client.post("/api/auth/register", json={
        "username": "owner", "password": "secure-pass", "display_name": "Owner",
    }).json()["token"]

    response = client.post(
        "/api/bank/ingest/upload",
        files={"files": ("Acct Statement_5555.pdf", b"%PDF-1.4 not really", "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    # The parse runs on a background thread; wait for it to let go of the lock,
    # so the assertions below see a settled inbox rather than a racing one.
    assert api_module._bank_lock.acquire(timeout=30)
    api_module._bank_lock.release()

    root = tmp_path / "inbox"
    saved = inbox.pdfs(root)
    assert len(saved) == 1
    # Directly in the bank landing folder: parent is _unsorted, not a hash.
    assert saved[0].parent == root / "bank" / "_unsorted"
    assert saved[0].name.endswith("_upload_Acct Statement_5555.pdf")
    assert not (root / "bank-uploads").exists()


def test_refile_reports_a_statement_it_cannot_read_instead_of_guessing(tmp_path, monkeypatch):
    """`refile` re-reads _unsorted because being stuck there is temporary — a
    password gets saved, a parser gets written. What it still cannot identify it
    reports, and leaves exactly where it is."""
    from sparser import refile, store

    monkeypatch.setenv("SPARSER_KEY_FILE", str(tmp_path / "secret.key"))
    db = tmp_path / "statements.db"
    store.connect(db).close()
    root = tmp_path / "inbox"
    stuck = _pdf(inbox.landing(root, inbox.CARDS) / "not-really-a-pdf.pdf", b"%PDF-1.4 junk")

    found = refile.refile(db, root)

    assert len(found) == 1
    assert not found[0].fileable
    assert found[0].reason
    assert stuck.is_file(), "an unidentifiable statement must stay put"
    assert inbox.pdfs(root) == [stuck]
