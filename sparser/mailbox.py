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
import hashlib
import imaplib
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import inbox as inbox_layout
from .doctype import classify_bank_mail, classify_mail

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

# The same two lists for deposit accounts. Kept separate rather than shared: a
# bank sends both products from one address, so only the subject can tell them
# apart, and widening the card lists would pull account statements into card
# scans (and the reverse) before any classifier got a say.
BANK_STATEMENT_SENDERS = [
    "hdfcbank.net", "hdfcbank.com", "icicibank.com",
    "axisbank.com", "kotak.com", "idfcfirstbank.com", "indusind.com", "sbi.co.in",
    "yesbank.in", "rblbank.com", "aubank.in", "federalbank.co.in", "pnb.co.in",
]

BANK_SUBJECT_SEARCHES = [
    "account statement", "bank statement", "savings account statement",
    "statement of account", "smart statement", "e-statement",
]


def _account_label(address: str) -> str:
    """Recognisable in local logs without printing the full mailbox address."""
    user, sep, domain = address.partition("@")
    if not sep:
        return "mailbox"
    shown = user[:2] + "***" if len(user) > 2 else "***"
    return f"{shown}@{domain}"


def _gmail_search_query(
    senders: Iterable[str], since: dt.date, before: Optional[dt.date],
    subject_searches: Iterable[str] = SUBJECT_SEARCHES,
    require_pdf: bool = True,
) -> str:
    """One Gmail-native query replacing many sequential IMAP SEARCH calls.

    ``require_pdf`` is not always true. HDFC's smart statement mail carries a
    link to a password gate and no attachment at all, so a bank sweep that
    insisted on ``has:attachment`` would never see the one statement it was
    opened for.
    """
    sender_terms = [f"from:{sender}" for sender in senders]
    subject_terms = [f'subject:"{phrase}"' for phrase in subject_searches]
    dates = f"after:{since:%Y/%m/%d}"
    if before:
        dates += f" before:{before:%Y/%m/%d}"
    # Gmail's braces mean OR. Escape inner phrase quotes because the complete
    # X-GM-RAW value itself is sent as one quoted IMAP argument.
    groups = []
    if sender_terms:
        groups.append("{" + " ".join(sender_terms) + "}")
    if subject_terms:
        groups.append("{" + " ".join(subject_terms) + "}")
    raw = f"{dates} {' '.join(groups)}"
    if require_pdf:
        raw += " has:attachment filename:pdf"
    return '"' + raw.replace('\\', '\\\\').replace('"', '\\"') + '"'


@dataclass
class Account:
    address: str
    password: str
    #: Household member this mailbox belongs to; statements fetched from it are
    #: attributed to them. ``None`` for env-configured mailboxes, which have no owner.
    member_id: Optional[int] = None

    @property
    def label(self) -> str:
        return self.address


# The built-in lists above are a starting point, not a rule. They are what a scan
# searches for a card or account that has no rules of its own — which is every
# unrecognized one, since only an import can create a saved rule. That makes them
# the one filter a user cannot otherwise see or influence, so they are editable
# and stored per install; the built-ins remain the fallback and the reset target.
BUILT_IN_RULES = {
    "cards": (STATEMENT_SENDERS, SUBJECT_SEARCHES),
    "bank": (BANK_STATEMENT_SENDERS, BANK_SUBJECT_SEARCHES),
}

_RULE_KEY = "scan_defaults"


def _rule_kind(kind: str) -> str:
    if kind not in BUILT_IN_RULES:
        raise ValueError(f"unknown scan kind {kind!r}; expected one of {sorted(BUILT_IN_RULES)}")
    return kind


def scan_defaults(conn, kind: str) -> dict:
    """The sender/subject lists a scan falls back to, and whether they were edited."""
    import json

    senders, subjects = BUILT_IN_RULES[_rule_kind(kind)]
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?", (f"{_RULE_KEY}.{kind}",)
    ).fetchone()
    saved = json.loads(row["value"]) if row else {}
    return {
        "senders": saved.get("senders") or list(senders),
        "subjects": saved.get("subjects") or list(subjects),
        "customised": bool(saved),
        "built_in_senders": list(senders),
        "built_in_subjects": list(subjects),
    }


def set_scan_defaults(conn, kind: str, senders: Iterable[str], subjects: Iterable[str]) -> dict:
    """Replace the fallback lists. Empty for either field restores the built-in one,
    so a scan can never be left with nothing to search."""
    import json

    cleaned = {
        "senders": [value.strip().lower() for value in senders if value.strip()],
        "subjects": [value.strip().lower() for value in subjects if value.strip()],
    }
    conn.execute(
        """INSERT INTO app_metadata (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (f"{_RULE_KEY}.{_rule_kind(kind)}", json.dumps(cleaned)),
    )
    conn.commit()
    return scan_defaults(conn, kind)


def clear_scan_defaults(conn, kind: str) -> dict:
    conn.execute("DELETE FROM app_metadata WHERE key = ?", (f"{_RULE_KEY}.{_rule_kind(kind)}",))
    conn.commit()
    return scan_defaults(conn, kind)


#: (subject, filename, sender) -> (accept?, reason). ``doctype`` supplies one
#: per product; the sweep itself stays neutral about which is in force.
Classifier = Callable[..., tuple[bool, str]]

#: (message, subject, sender) -> the (filename, bytes) pairs retrieved by
#: following whatever the body links to.
LinkFetcher = Callable[[Message, str, str], Iterable[tuple[str, bytes]]]


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


def accounts_from_store(conn, purpose: Optional[str] = None) -> list[Account]:
    """Mailboxes connected through the UI, plus anything configured in the env.

    The env form stays supported because a scheduled job on a server has no UI to
    click through; the two sources are merged with the stored ones winning.

    ``purpose`` is "cards" or "bank". A mailbox can be marked as carrying only
    one of the two, and then it is swept only by that pipeline — a personal
    account with no cards should not be searched on every card scan. Mailboxes
    configured through the environment have no such marking and serve both.
    """
    from . import accounts as store_accounts

    column = {"cards": "use_for_cards", "bank": "use_for_bank"}.get(purpose or "")
    out: list[Account] = []
    seen: set[str] = set()
    for row in store_accounts.listing(conn):
        if not row["secret_ok"]:
            continue
        if column and not row.get(column, 1):
            continue
        secret = store_accounts.secret_for(conn, row["address"])
        if secret:
            out.append(Account(row["address"], secret, row["member_id"]))
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


def _download_path(
    dest: Path,
    account: str,
    subject: str,
    filename: str,
    when: Optional[dt.datetime],
    uid: bytes,
    part_no: int,
    payload: bytes,
    known: Optional[dict] = None,
) -> tuple[Path, bool]:
    """Choose a stable path without confusing same-named bank attachments.

    Axis, among others, calls every attachment ``Credit_Card_Statement.pdf``.
    Keep legacy paths valid, but when that name already contains different bytes,
    use the immutable mailbox UID and attachment number as a collision suffix.
    Comparing content makes repeat scans idempotent for both naming schemes.

    ``known`` maps file name to wherever the inbox already holds it. Downloads
    land in ``_unsorted`` but are filed under their card or account once parsed,
    so an attachment we already have is usually *not* at the path we would write
    it to — without this lookup every sweep would re-download the lot.
    """
    def _placed(name: str) -> Optional[Path]:
        here = dest / name
        if here.exists():
            return here
        return (known or {}).get(name)

    legacy_name = _safe_name(account, subject, filename, when)
    legacy = _placed(legacy_name)
    if legacy is None:
        return dest / legacy_name, False
    if legacy.read_bytes() == payload:
        return legacy, True

    uid_text = re.sub(r"[^A-Za-z0-9_-]+", "_", uid.decode("ascii", "ignore")) or "message"
    stem = Path(legacy_name).stem
    collided_name = f"{stem}_uid{uid_text}_{part_no}.pdf"
    collided = _placed(collided_name)
    if collided is None:
        return dest / collided_name, False
    if collided.read_bytes() == payload:
        return collided, True
    # Defensive only: a UID/part tuple should be immutable, but never overwrite
    # a local PDF if a provider violates that assumption.
    digest = hashlib.sha256(payload).hexdigest()[:12]
    digested_name = f"{stem}_uid{uid_text}_{part_no}_{digest}.pdf"
    digested = _placed(digested_name)
    if digested is None:
        return dest / digested_name, False
    return digested, digested.read_bytes() == payload


def fetch_account(
    account: Account,
    root: Path,
    *,
    kind: str = inbox_layout.CARDS,
    since: Optional[dt.date] = None,
    before: Optional[dt.date] = None,
    folder: str = '"[Gmail]/All Mail"',
    senders: Iterable[str] = STATEMENT_SENDERS,
    verbose: bool = True,
    include_existing: bool = False,
    subject_searches: Iterable[str] = SUBJECT_SEARCHES,
    classifier: Classifier = classify_mail,
    require_pdf: bool = True,
    link_fetcher: Optional[LinkFetcher] = None,
) -> list[Path]:
    """Download statement PDFs from one mailbox.

    Only the small Subject/From/Date header is fetched for each search hit first.
    Full messages (which can contain multi-megabyte PDFs) are fetched only after
    the mail-header classifier accepts them.

    Everything product-specific arrives as an argument, because card and account
    statements differ only in *which* mail counts and *how* the document is
    attached — never in the sweep itself:

    ``classifier``   decides from the headers, and is the one place the card and
                     bank pipelines disagree about what they are looking for.
    ``require_pdf``  drops the ``has:attachment`` clause when statements can
                     arrive as a link instead of a file.
    ``link_fetcher`` is given a message that carried no PDF, and may return
                     ``(filename, bytes)`` pairs it retrieved by following what
                     the body links to. Whatever it returns is saved, deduplicated
                     and reported exactly like an attachment.

    ``root`` is the inbox root, not a download folder: attachments land in that
    kind's ``_unsorted`` and the pipeline files them under their card or account
    once parsing says which one it is. See :mod:`sparser.inbox`.
    """
    root = Path(root)
    dest = inbox_layout.landing(root, kind)
    # Snapshot once per mailbox: an attachment already filed away is still an
    # attachment we have, and re-reading the tree per message would be quadratic.
    known = inbox_layout.index(root)
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
        subject_list = list(subject_searches)
        log.info("%s Gmail combined search 1/1", label)
        try:
            status, data = conn.search(
                None, "X-GM-RAW",
                _gmail_search_query(sender_list, window, before, subject_list, require_pdf),
            )
        except imaplib.IMAP4.error:
            status, data = "NO", []
        if status == "OK":
            if data and data[0]:
                uids.update(data[0].split())
        else:
            # Kept for non-Gmail-compatible servers and future provider support.
            query_total = len(sender_list) + len(subject_list)
            log.warning(
                "%s combined search unsupported; falling back to %d standard searches",
                label, query_total,
            )
            for query_no, sender in enumerate(sender_list, 1):
                log.info("%s fallback search %d/%d", label, query_no, query_total)
                status, data = conn.search(None, "FROM", f'"{sender}"', *date_clause)
                if status == "OK" and data and data[0]:
                    uids.update(data[0].split())
            for offset, phrase in enumerate(subject_list, len(sender_list) + 1):
                log.info("%s fallback search %d/%d", label, offset, query_total)
                status, data = conn.search(None, "SUBJECT", f'"{phrase}"', *date_clause)
                if status == "OK" and data and data[0]:
                    uids.update(data[0].split())

        log.info("%s found %d candidate message(s); checking headers", label, len(uids))
        rejected = existing = downloaded = 0

        for uid in sorted(uids):
            status, data = conn.fetch(
                uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            header = _message_from_fetch(data) if status == "OK" else None
            if header is None:
                continue
            subject = _decode(header.get("Subject"))
            sender = _decode(header.get("From"))
            custom_match = (
                any(rule.lower() in sender.lower() for rule in sender_list)
                and any(rule.lower() in subject.lower() for rule in subject_list)
            )
            accepted, reason = classifier(subject, sender=sender)
            if custom_match:
                accepted, reason = True, "matched configured card mail rules"
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

            parts = list(_pdf_parts(msg))
            if not parts and link_fetcher is not None:
                # No attachment, but the mail passed the header gate — so the
                # document it announces is behind a link. Failures here are the
                # message's alone and must not abort the sweep.
                try:
                    parts = list(link_fetcher(msg, subject, sender))
                except Exception as exc:
                    rejected += 1
                    if verbose:
                        print(f"  skipped {subject[:60]}  ({exc})")
                    log.warning("%s could not follow %s: %s", label, subject[:60], exc)
                    continue
            for part_no, (filename, payload) in enumerate(parts, 1):
                # Re-check with the filename. It can strengthen an ambiguous
                # issuer subject, while the PDF-text gate in pipeline.py remains
                # the final authority on card statement vs bank account.
                accepted, reason = classifier(subject, filename, sender)
                if custom_match:
                    accepted, reason = True, "matched configured card mail rules"
                if not accepted:
                    rejected += 1
                    if verbose:
                        print(f"  skipped {filename[:60]}  ({reason})")
                    continue
                out, already_present = _download_path(
                    dest, account.address, subject, filename, when, uid, part_no, payload,
                    known,
                )
                if already_present:
                    existing += 1
                    if include_existing and out not in saved:
                        saved.append(out)
                    continue
                out.write_bytes(payload)
                known[out.name] = out
                saved.append(out)
                downloaded += 1
                if verbose:
                    print(f"  saved {out.name}  <- {subject[:60]}")
                log.info("%s downloaded %s (%d bytes)", label, out.name, len(payload))
        log.info(
            "%s mailbox scan complete: %d downloaded, %d already present, %d rejected",
            label, downloaded, existing, rejected,
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


# ------------------------------------------------- statements that arrive as links

def _body_text(msg: Message) -> str:
    """Every text/html and text/plain part, concatenated, for link hunting."""
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, "replace"))
    return "\n".join(chunks)


#: The words on the button that opens a statement gate, however they are spelt
#: and wherever they are written — the visible label, an image's alt text, or the
#: campaign tag in the URL itself. Which words those are is a property of the
#: institution, so it comes from its gate profile; this is only the fallback for
#: a profile that names none.
DEFAULT_STATEMENT_BUTTON = re.compile(r"view\s*(your\s*)?statement", re.I)


class _Anchors(HTMLParser):
    """Collect ``href`` → the words a reader sees on it.

    Written out rather than regexed because the label is routinely broken up by
    nested markup — ``<a><b>View your</b> SmartStatement</a>`` — or carried by an
    image instead of text, and a regex over the raw anchor sees neither as one
    phrase.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: dict[str, str] = {}
        self._href: Optional[str] = None
        self._words: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "a":
            self._flush()
            self._href = (values.get("href") or "").strip()
            self._words = []
        elif tag == "img" and self._href is not None:
            self._words.append(values.get("alt") or values.get("title") or "")

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._words.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def _flush(self) -> None:
        if self._href:
            label = " ".join(" ".join(self._words).split())
            # The first label that says anything wins: the same href is usually
            # repeated in a footer that wraps it around nothing readable.
            self.labels[self._href] = self.labels.get(self._href, "") or label
        self._href, self._words = None, []

    def close(self) -> None:  # pragma: no cover - parser bookkeeping
        self._flush()
        super().close()


def _anchor_labels(body: str) -> dict[str, str]:
    parser = _Anchors()
    try:
        parser.feed(body)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed part must not lose the links
        log.debug("could not parse anchors out of the mail body", exc_info=True)
    return {href.replace("&amp;", "&"): label for href, label in parser.labels.items()}


def smart_statement_links(msg: Message) -> list[str]:
    """Statement-gate URLs in a mail body, most promising first, deduplicated.

    The link itself is recognised by :func:`sparser.smartstatement.links_in`,
    against the profiles installed on this machine — host and job parameter, the
    two things a gate itself consumes. What this adds is the mail: an anchor's
    visible label, or its image's alt text, which live in the markup and not in
    the URL. They *order* the candidates when a mail carries several; they never
    gate them, so a plain-text mail, a relabelled campaign or another language is
    still followed.
    """
    from .smartstatement import links_in, profiles

    body = _body_text(msg)
    labels = _anchor_labels(body)
    found = links_in(body)
    buttons = [profile.button for profile in profiles() if profile.button] \
        or [DEFAULT_STATEMENT_BUTTON]

    def rank(link: str) -> int:
        label = labels.get(link, "")
        return 0 if any(button.search(label) for button in buttons) else 1

    # Stable, so links of equal standing keep the order links_in gave them.
    return sorted(found, key=rank)


def linked_statement_fetcher(passwords: Iterable[str]) -> LinkFetcher:
    """A :data:`LinkFetcher` that walks a mailed password gate for the PDF behind it.

    Bound to the password candidates the pipeline resolved, so the mailbox sweep
    itself never has to know how a statement password is derived. Which gates can
    be walked is whatever :mod:`sparser.smartstatement` has profiles for; with
    none installed, a linked statement is simply a mail with no attachment.
    """
    candidates = list(passwords)

    def fetch(msg: Message, subject: str, sender: str) -> list[tuple[str, bytes]]:
        from .smartstatement import SmartStatementError, fetch_pdf

        links = smart_statement_links(msg)
        if not links:
            raise SmartStatementError("no attachment, and no smart statement link in the body")
        pdf, used = fetch_pdf(links[0], candidates)
        log.info("smart statement retrieved for %s (%d bytes)", subject[:60], len(pdf))
        # Named after the mail, not the gate: the gate serves every statement
        # from one path, so its own name distinguishes nothing.
        stem = re.sub(r"[^A-Za-z0-9]+", "_", subject).strip("_")[:48] or "smart_statement"
        return [(f"{stem}.pdf", pdf)]

    return fetch


def fetch_bank_account(
    account: Account,
    root: Path,
    *,
    since: Optional[dt.date] = None,
    before: Optional[dt.date] = None,
    verbose: bool = True,
    include_existing: bool = False,
    senders: Iterable[str] = BANK_STATEMENT_SENDERS,
    subject_searches: Iterable[str] = BANK_SUBJECT_SEARCHES,
    passwords: Iterable[str] = (),
) -> list[Path]:
    """The mailbox sweep, pointed at deposit-account statements.

    Same machinery as the card sweep with three substitutions: the bank header
    classifier, no attachment requirement, and a link fetcher for HDFC's smart
    statement. Everything downstream — naming, deduplication, the review queue —
    is shared.
    """
    return fetch_account(
        account, root, kind=inbox_layout.BANK,
        since=since, before=before, verbose=verbose, include_existing=include_existing,
        senders=senders, subject_searches=subject_searches,
        classifier=classify_bank_mail,
        require_pdf=False,
        link_fetcher=linked_statement_fetcher(passwords),
    )


def fetch_all(
    accounts: list[Account], root: Path, *, months: int = 12, verbose: bool = True
) -> list[Path]:
    since = dt.date.today() - dt.timedelta(days=31 * months)
    out: list[Path] = []
    for acct in accounts:
        if verbose:
            print(f"{acct.label}: searching since {since:%b %Y}")
        try:
            out += fetch_account(acct, root, since=since, verbose=verbose)
        except imaplib.IMAP4.error as exc:
            print(f"  ! {acct.label}: {exc}")
    return out
