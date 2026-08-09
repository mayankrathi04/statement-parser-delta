"""Fetch statement PDFs straight from Gmail over IMAP.

Why IMAP and not the Gmail API: this has to run unattended every month across
*several* mailboxes. An app password per account is a two-minute setup, needs no
OAuth client, no consent screen and no token refresh daemon, and the same code
works for any IMAP provider. Credentials come from the environment, never a file
in the repo.

Setup (once per account, requires 2-Step Verification):
    https://myaccount.google.com/apppasswords
    export SPARSER_GMAIL="you@gmail.com:abcdefghijklmnop,other@gmail.com:qrstuvwxyz"

Nothing is deleted or modified in the mailbox — statements are read-only fetches.
"""
from __future__ import annotations

import datetime as dt
import email
import imaplib
import logging
import os
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Iterable, Optional

from .doctype import classify_mail

log = logging.getLogger("sparser.mailbox")

IMAP_HOST = "imap.gmail.com"

# Without an explicit timeout imaplib blocks forever on a stalled socket, and the
# caller — a browser request or a scheduled job — just hangs with no explanation.
IMAP_TIMEOUT = 30

# Senders that issue card statements. Matched against the From header, so a
# marketing mail from the same bank without a PDF simply yields nothing.
STATEMENT_SENDERS = [
    "hdfcbank.net", "hdfcbank.com", "icicibank.com", "axisbank.com",
    "yesbank.in", "sbicard.com", "kotak.com", "americanexpress.com",
    "idfcfirstbank.com", "indusind.com", "rblbank.com", "aubank.in",
]

# Searched independently of the sender, so an issuer missing from the list above
# is still found. The server does the substring matching; doctype.classify_mail
# then makes the real decision.
SUBJECT_SEARCHES = ["credit card statement", "credit card e-statement", "card statement"]


def _account_label(address: str) -> str:
    """Recognisable in local logs without printing the full mailbox address."""
    user, sep, domain = address.partition("@")
    if not sep:
        return "mailbox"
    shown = user[:2] + "***" if len(user) > 2 else "***"
    return f"{shown}@{domain}"


def _gmail_search_query(
    senders: Iterable[str], since: dt.date, before: Optional[dt.date]
) -> str:
    """One Gmail-native query replacing many sequential IMAP SEARCH calls."""
    terms = [*(f"from:{sender}" for sender in senders)]
    terms += [f'subject:"{phrase}"' for phrase in SUBJECT_SEARCHES]
    dates = f"after:{since:%Y/%m/%d}"
    if before:
        dates += f" before:{before:%Y/%m/%d}"
    # Gmail's braces mean OR. Escape inner phrase quotes because the complete
    # X-GM-RAW value itself is sent as one quoted IMAP argument.
    raw = f"{dates} {{{' '.join(terms)}}}"
    return '"' + raw.replace('\\', '\\\\').replace('"', '\\"') + '"'


@dataclass
class Account:
    address: str
    password: str

    @property
    def label(self) -> str:
        return self.address


@dataclass
class MailScan:
    """What one mailbox sweep did — including what it deliberately did not take."""

    saved: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (subject, reason)
    examined: int = 0


def accounts_from_env(var: str = "SPARSER_GMAIL") -> list[Account]:
    """Parse "addr:apppassword,addr2:apppassword2" from the environment."""
    raw = os.environ.get(var, "").strip()
    out: list[Account] = []
    for chunk in filter(None, (c.strip() for c in raw.split(","))):
        if ":" not in chunk:
            raise ValueError(f"{var} entry must look like address:app_password, got {chunk!r}")
        addr, pw = chunk.split(":", 1)
        # App passwords are shown grouped in fours; users paste them with spaces.
        out.append(Account(addr.strip(), pw.strip().replace(" ", "")))
    return out


def accounts_from_store(conn) -> list[Account]:
    """Mailboxes connected through the UI, plus anything configured in the env.

    The env form stays supported because a scheduled job on a server has no UI to
    click through; the two sources are merged with the stored ones winning.
    """
    from . import accounts as store_accounts

    out: list[Account] = []
    seen: set[str] = set()
    for row in store_accounts.listing(conn):
        if not row["secret_ok"]:
            continue
        secret = store_accounts.secret_for(conn, row["address"])
        if secret:
            out.append(Account(row["address"], secret))
            seen.add(row["address"])
    for acct in accounts_from_env():
        if acct.address not in seen:
            out.append(acct)
    return out


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _pdf_parts(msg: Message) -> Iterable[tuple[str, bytes]]:
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _decode(part.get_filename())
        if not name or not name.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            yield name, payload


def _message_from_fetch(data) -> Optional[Message]:
    """Turn an IMAP FETCH response into a message, ignoring protocol metadata."""
    if not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return email.message_from_bytes(item[1])
    return None


def _safe_name(account: str, subject: str, filename: str, when: Optional[dt.datetime]) -> str:
    stamp = (when or dt.datetime.now()).strftime("%Y%m")
    user = account.split("@")[0]
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:60]
    return f"{stamp}_{user}_{stem}.pdf"


def fetch_account(
    account: Account,
    dest: Path,
    *,
    since: Optional[dt.date] = None,
    before: Optional[dt.date] = None,
    folder: str = '"[Gmail]/All Mail"',
    senders: Iterable[str] = STATEMENT_SENDERS,
    verbose: bool = True,
) -> list[Path]:
    """Download credit-card statement PDFs from one mailbox.

    Only the small Subject/From/Date header is fetched for each search hit first.
    Full messages (which can contain multi-megabyte PDFs) are fetched only after
    the mail-header classifier accepts them.
    """
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    label = _account_label(account.address)

    log.info("%s connecting to %s", label, IMAP_HOST)
    conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT)
    try:
        conn.login(account.address, account.password)
        log.info("%s authenticated", label)
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":  # some mailboxes localise the All Mail folder name
            conn.select("INBOX", readonly=True)
            log.info("%s using INBOX (All Mail unavailable)", label)

        # Always bound the search by date: an unbounded FROM search over years of
        # All Mail is slow enough to look like a hang.
        window = since or (dt.date.today() - dt.timedelta(days=365))
        date_clause = ["SINCE", window.strftime("%d-%b-%Y")]
        if before:
            date_clause += ["BEFORE", before.strftime("%d-%b-%Y")]
        log.info(
            "%s searching mail from %s%s",
            label, window.isoformat(), f" to {before.isoformat()}" if before else "",
        )

        uids: set[bytes] = set()
        sender_list = list(senders)
        log.info("%s Gmail combined search 1/1", label)
        try:
            status, data = conn.search(
                None, "X-GM-RAW", _gmail_search_query(sender_list, window, before)
            )
        except imaplib.IMAP4.error:
            status, data = "NO", []
        if status == "OK":
            if data and data[0]:
                uids.update(data[0].split())
        else:
            # Kept for non-Gmail-compatible servers and future provider support.
            query_total = len(sender_list) + len(SUBJECT_SEARCHES)
            log.warning(
                "%s combined search unsupported; falling back to %d standard searches",
                label, query_total,
            )
            for query_no, sender in enumerate(sender_list, 1):
                log.info("%s fallback search %d/%d", label, query_no, query_total)
                status, data = conn.search(None, "FROM", f'"{sender}"', *date_clause)
                if status == "OK" and data and data[0]:
                    uids.update(data[0].split())
            for offset, phrase in enumerate(SUBJECT_SEARCHES, len(sender_list) + 1):
                log.info("%s fallback search %d/%d", label, offset, query_total)
                status, data = conn.search(None, "SUBJECT", f'"{phrase}"', *date_clause)
                if status == "OK" and data and data[0]:
                    uids.update(data[0].split())

        log.info("%s found %d candidate message(s); checking headers", label, len(uids))
        rejected = existing = 0

        for uid in sorted(uids):
            status, data = conn.fetch(
                uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            header = _message_from_fetch(data) if status == "OK" else None
            if header is None:
                continue
            subject = _decode(header.get("Subject"))
            sender = _decode(header.get("From"))
            accepted, reason = classify_mail(subject, sender=sender)
            if not accepted:
                rejected += 1
                if verbose:
                    print(f"  skipped {subject[:60]}  ({reason})")
                continue

            status, data = conn.fetch(uid, "(BODY.PEEK[])")
            msg = _message_from_fetch(data) if status == "OK" else None
            if msg is None:
                continue
            when = None
            try:
                when = email.utils.parsedate_to_datetime(msg.get("Date"))
            except Exception:
                pass

            for filename, payload in _pdf_parts(msg):
                # Re-check with the filename. It can strengthen an ambiguous
                # issuer subject, while the PDF-text gate in pipeline.py remains
                # the final authority on card statement vs bank account.
                accepted, reason = classify_mail(subject, filename, sender)
                if not accepted:
                    rejected += 1
                    if verbose:
                        print(f"  skipped {filename[:60]}  ({reason})")
                    continue
                out = dest / _safe_name(account.address, subject, filename, when)
                if out.exists():
                    existing += 1
                    continue
                out.write_bytes(payload)
                saved.append(out)
                if verbose:
                    print(f"  saved {out.name}  <- {subject[:60]}")
                log.info("%s downloaded %s (%d bytes)", label, out.name, len(payload))
        log.info(
            "%s mailbox scan complete: %d downloaded, %d already present, %d rejected",
            label, len(saved), existing, rejected,
        )
        return saved
    except Exception:
        log.exception("%s mailbox scan failed", label)
        raise
    finally:
        try:
            conn.logout()
            log.info("%s disconnected", label)
        except Exception:
            pass


def fetch_all(
    accounts: list[Account], dest: Path, *, months: int = 12, verbose: bool = True
) -> list[Path]:
    since = dt.date.today() - dt.timedelta(days=31 * months)
    out: list[Path] = []
    for acct in accounts:
        if verbose:
            print(f"{acct.label}: searching since {since:%b %Y}")
        try:
            out += fetch_account(acct, dest, since=since, verbose=verbose)
        except imaplib.IMAP4.error as exc:
            print(f"  ! {acct.label}: {exc}")
    return out
