"""A mailed link behind a password gate, turned into the statement PDF.

Most issuers attach the statement to the mail. A few mail a link instead, to a
gate that asks for the same password the PDF would have wanted and only serves
the document once it is satisfied. This module is a faithful client of that
*shape* of gate — and only the shape. Which host, which endpoints, which form
fields and which cipher key are not in this file. They live in a **gate
profile**: a small JSON document that sits beside this module and is not
committed.

    sparser/gates/<name>.json                  (or $SPARSER_GATES)

`.gitignore` keeps every profile in that folder out of the repository except
`sparser/gates/example.json`, which documents the format and is what the tests
run against. **No real institution's profile ships with this project.** A profile is
a description of one bank's private endpoints, and publishing one would make
this repository the thing that hands them out; that is a different object from a
tool that fetches your own statement from your own bank. Write the profile you
need locally — nothing else here has to change — and with none installed, link
detection matches nothing and a sweep simply reports a mail that carried no
attachment, which is what such a mail is without a gate to walk.

The exchange every profile describes:

    GET  the link                -> the form, carrying a job id and a sequence
    GET  the token endpoint      -> a per-attempt nonce
    POST the authenticate path   -> encrypt(nonce + password)
    POST the pdf path            -> the PDF this pipeline actually wants

The gate is stateful: the first GET sets the session and WAF cookies and every
later call is rejected without them, so one cookie jar spans the whole exchange
— which is also why the token is re-fetched per attempt rather than reused.

The two ciphers are implemented, never invented: a gate that speaks them refuses
to talk to a client that does not, and both keys are readable in the page that
uses them — the XOR key names itself in the profile, and the AES key arrives in
the response.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("sparser.smartstatement")


class SmartStatementError(RuntimeError):
    """The link could not be turned into a statement PDF."""


#: A hard ceiling, deliberately not a profile field. Every attempt here is a real
#: login against a real bank, and a wrong one is a failed login on the user's own
#: account. A profile may ask for fewer; it can never ask for more.
MAX_PASSWORD_ATTEMPTS = 3

_TIMEOUT = 45

_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
}

#: The response carries its own AES key in a `Data(key, ciphertext)` call, which
#: is a CryptoJS convention rather than any one bank's, so it is the default a
#: profile inherits instead of a value every profile would have to repeat.
DEFAULT_PAYLOAD_CALL = r"""Data\(\s*["']([^"']+)["']\s*,\s*["']([^"']*)["']\s*\)"""

#: Likewise generic: a gate answers a wrong password with a page, not an error.
DEFAULT_REFUSAL = r"invalid\s+password|incorrect\s+password|please\s+enter\s+the\s+valid"


# ------------------------------------------------------------------ gate profiles

@dataclass(frozen=True)
class GateProfile:
    """Everything about one institution's gate that this module does not know."""

    name: str
    link: re.Pattern
    job_query_param: str
    job_field: str
    sequence_fields: tuple[str, ...]
    password_field: str
    token_path: str
    authenticate_path: str
    pdf_path: str
    cipher_key: str
    payload_call: re.Pattern = field(default=re.compile(DEFAULT_PAYLOAD_CALL, re.S))
    refusal: re.Pattern = field(default=re.compile(DEFAULT_REFUSAL, re.I))
    button: Optional[re.Pattern] = None
    max_attempts: int = MAX_PASSWORD_ATTEMPTS

    @classmethod
    def from_json(cls, data: dict, source: str = "") -> "GateProfile":
        try:
            form = data.get("form", {})
            endpoints = data["endpoints"]
            cipher = data.get("password_cipher", {})
            response = data.get("response", {})
            button = data.get("button")
            return cls(
                name=data.get("name") or Path(source).stem or "unnamed gate",
                link=re.compile(data["link_pattern"], re.I),
                job_query_param=form.get("job_query_param", "job"),
                job_field=form.get("job_field", "ke"),
                sequence_fields=tuple(form.get("sequence_fields", ["sequence"])),
                password_field=form.get("password_field", "pwd"),
                token_path=endpoints["token"],
                authenticate_path=endpoints["authenticate"],
                pdf_path=endpoints["pdf"],
                cipher_key=cipher["key"],
                payload_call=re.compile(
                    response.get("payload_call", DEFAULT_PAYLOAD_CALL), re.S
                ),
                refusal=re.compile(response.get("refusal", DEFAULT_REFUSAL), re.I),
                button=re.compile(button, re.I) if button else None,
                # Clamped, not trusted: see MAX_PASSWORD_ATTEMPTS.
                max_attempts=min(
                    int(data.get("max_password_attempts", MAX_PASSWORD_ATTEMPTS)),
                    MAX_PASSWORD_ATTEMPTS,
                ),
            )
        except (KeyError, re.error, TypeError, ValueError) as exc:
            raise SmartStatementError(f"unusable gate profile {source or '<inline>'}: {exc}")


def gates_dir() -> Path:
    """Where installed profiles live: next to this module, ignored by git.

    In the folder rather than in ``~/.config`` because a profile is not a
    credential — it unlocks nothing on its own — and keeping it with the code
    that reads it means one directory to copy when the project moves machines.
    ``.gitignore`` is what keeps it unpublished, and the packaging manifest
    ships only ``example.json``, so an install carries no profile either.
    """
    override = os.environ.get("SPARSER_GATES")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "gates"


_cache: Optional[tuple[Path, tuple[GateProfile, ...]]] = None


def profiles(reload: bool = False) -> tuple[GateProfile, ...]:
    """Every installed profile. Empty — and that is a normal state — when none is."""
    global _cache
    directory = gates_dir()
    if _cache is not None and _cache[0] == directory and not reload:
        return _cache[1]
    loaded: list[GateProfile] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                loaded.append(
                    GateProfile.from_json(
                        json.loads(path.read_text(encoding="utf-8")), str(path)
                    )
                )
            except (SmartStatementError, json.JSONDecodeError, OSError) as exc:
                # One malformed profile must not take the others down with it.
                log.warning("ignoring gate profile %s: %s", path.name, exc)
    _cache = (directory, tuple(loaded))
    return _cache[1]


def profile_for(link: str) -> GateProfile:
    for profile in profiles():
        if profile.link.search(link):
            return profile
    raise SmartStatementError(
        f"no gate profile matches {urllib.parse.urlsplit(link).netloc or link[:60]} — "
        f"install one in {gates_dir()} (see sparser/gates/example.json for the format)"
    )


def links_in(text: str) -> list[str]:
    """Every gate link the text carries, best candidate first.

    A campaign mail repeats one link in the button, the fallback text and the
    footer; they are the same job and following one is enough. What identifies a
    link is its host and its job parameter — the two things a gate itself
    consumes — never the wording of the button it sits under, which the bank is
    free to change and which an image or another language would not carry at all.
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for profile in profiles():
        for match in profile.link.finditer(text):
            link = match.group(0).rstrip(").,;'\"").replace("&amp;", "&")
            if f"{profile.job_query_param}=".lower() not in link.lower():
                continue
            if link in seen:
                continue
            seen.add(link)
            # The campaign tag is the button's own words, machine-readable and
            # immune to the markup the visible label is wrapped in.
            ranked = 1
            if profile.button and profile.button.search(
                urllib.parse.unquote(link).replace("_", " ")
            ):
                ranked = 0
            found.append((ranked, link))
    return [link for _, link in sorted(found, key=lambda pair: pair[0])]


# --------------------------------------------------------------- the gate's ciphers

def _byte(ch: str) -> int:
    """charCodeAt(0) under the gate's latin-1 assumption."""
    code = ord(ch)
    return code if code < 256 else 256


def encrypt(text: str, key: str, iv: Optional[int] = None) -> str:
    """What the password field actually sends.

    A chained XOR stream: each byte is added to the previous *cipher* byte mod
    255, then XORed with the cycling key. The random lead byte is transmitted in
    clear as the first hex pair, so the result is self-describing and a fresh
    call on the same input differs every time — two attempts with one password
    do not look alike on the wire.
    """
    previous = random.randint(1, 255) if iv is None else iv
    out = [f"{previous:02x}"]
    for index, ch in enumerate(text):
        current = (_byte(ch) + previous) % 255
        current ^= _byte(key[index % len(key)])
        out.append(f"{current:02x}")
        previous = current
    return "".join(out).upper()


def decrypt(payload: str, key: str) -> str:
    """Inverse of :func:`encrypt`. Only the tests need it — kept so the port is
    demonstrably reversible rather than merely plausible."""
    previous = int(payload[:2], 16)
    out = []
    for index, offset in enumerate(range(2, len(payload), 2)):
        cipher = int(payload[offset:offset + 2], 16)
        value = cipher ^ _byte(key[index % len(key)])
        out.append(chr((value - previous) % 255))
        previous = cipher
    return "".join(out)


def decrypt_statement_page(html: str, pattern: Optional[re.Pattern] = None) -> str:
    """Unwrap the AES-ECB payload the gate answers with.

    The page is a CryptoJS bundle plus one `Data(key, ciphertext)` call, and the
    key is the call's own first argument. Read from the response rather than
    hardcoded, so a rotated key keeps working — and so no key is ever written
    down here.
    """
    match = (pattern or re.compile(DEFAULT_PAYLOAD_CALL, re.S)).search(html)
    if not match:
        raise SmartStatementError("statement page carried no Data(key, payload) call")
    key = base64.b64decode(match.group(1))
    ciphertext = base64.b64decode(match.group(2))
    if not ciphertext or len(ciphertext) % 16:
        raise SmartStatementError("statement payload is not a whole number of AES blocks")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    pad = plain[-1]
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain.decode("utf-8", "replace")


# ------------------------------------------------------------------ the exchange

class _Gate:
    """One cookie-sharing conversation with a gate host."""

    def __init__(self, link: str, profile: GateProfile):
        parts = urllib.parse.urlsplit(link)
        if not parts.scheme.startswith("http") or not parts.netloc:
            raise SmartStatementError(f"not a usable statement link: {link[:120]}")
        # ".../<service>/GetStatement.jsp" -> ".../<service>"
        base_path = parts.path.rsplit("/", 1)[0]
        self.base = f"{parts.scheme}://{parts.netloc}{base_path}"
        self.link = link
        self.profile = profile
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _url(self, template: str, **values: str) -> str:
        quoted = {key: urllib.parse.quote(value, safe="") for key, value in values.items()}
        return f"{self.base}{template.format(**quoted)}"

    def _open(self, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None):
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        for name, value in {**_HEADERS, "referer": self.link, **(headers or {})}.items():
            request.add_header(name, value)
        try:
            return self.opener.open(request, timeout=_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise SmartStatementError(
                f"{urllib.parse.urlsplit(url).path} returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SmartStatementError(f"cannot reach the statement host: {exc.reason}") from exc

    def open_form(self) -> tuple[str, str]:
        """Fetch the gate and read the two identifiers its form posts back."""
        with self._open(self.link) as response:
            page = response.read().decode("utf-8", "replace")
        job = _hidden(page, self.profile.job_field) or urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.link).query
        ).get(self.profile.job_query_param, [""])[0]
        sequence = next(
            (value for name in self.profile.sequence_fields if (value := _hidden(page, name))),
            "",
        )
        if not job or not sequence:
            raise SmartStatementError(
                "the statement link no longer serves its password form — it has probably "
                "expired; open the mail and use a fresher link"
            )
        log.info("%s gate opened (job %s…)", self.profile.name, job[:12])
        return job, sequence

    def token(self, job: str) -> str:
        """A one-shot nonce the password is bound to."""
        with self._open(
            self._url(self.profile.token_path, job=job),
            headers={"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
        ) as response:
            value = response.read().decode("utf-8", "replace").strip()
        if not value:
            raise SmartStatementError("the gate issued an empty token")
        return value

    def authenticate(self, job: str, sequence: str, password: str) -> str:
        """Post the scrambled password; returns the decrypted statement HTML."""
        token = self.token(job)
        body = urllib.parse.urlencode({
            self.profile.job_field: job,
            self.profile.sequence_fields[0]: sequence,
            self.profile.password_field: encrypt(token + password, self.profile.cipher_key),
        }).encode()
        origin = urllib.parse.urlsplit(self.base)
        with self._open(
            self._url(self.profile.authenticate_path),
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": f"{origin.scheme}://{origin.netloc}"},
        ) as response:
            page = response.read().decode("utf-8", "replace")
        return decrypt_statement_page(page, self.profile.payload_call)

    def pdf(self, job: str, sequence: str) -> bytes:
        """The same statement as a PDF, on the session the password unlocked."""
        with self._open(
            self._url(self.profile.pdf_path, job=job, sequence=sequence),
            data=b"",
            headers={"accept": "application/pdf,*/*"},
        ) as response:
            payload = response.read()
        if not payload.startswith(b"%PDF-"):
            raise SmartStatementError(
                f"the gate returned {len(payload)} bytes that are not a PDF"
            )
        return payload


def _hidden(page: str, name: str) -> str:
    """Read one hidden input's value out of the gate's form."""
    match = re.search(
        rf"""<input[^>]*\bname\s*=\s*["']?{re.escape(name)}["']?[^>]*>""", page, re.I
    )
    if not match:
        return ""
    value = re.search(r"""\bvalue\s*=\s*["']([^"']*)["']""", match.group(0), re.I)
    return value.group(1).strip() if value else ""


def fetch_pdf(link: str, passwords: Iterable[str]) -> tuple[bytes, str]:
    """Walk the gate and return (pdf_bytes, password_that_worked).

    The passwords here are only ones a person actually entered — the supplied
    password first, then any saved per-account passwords — never a derived guess.
    Deriving a password from a name and date of birth happens only for an
    already-downloaded PDF, on this machine, where a wrong attempt reaches no one;
    a gate is a live bank login and is deliberately never given one. The attempt
    count is capped on top of that, because every attempt here is a real login.
    """
    profile = profile_for(link)
    tried: list[str] = []
    for candidate in passwords:
        if not candidate or candidate in tried:
            continue
        if len(tried) >= profile.max_attempts:
            raise SmartStatementError(
                f"stopped after {profile.max_attempts} password attempts without success — "
                f"set this account's statement password on the Bank tab so the gate is given "
                f"the right one directly"
            )
        tried.append(candidate)
        # A fresh gate per attempt: the token is single-use and a rejected
        # password leaves the previous session in a state the form will not reuse.
        gate = _Gate(link, profile)
        job, sequence = gate.open_form()
        try:
            html = gate.authenticate(job, sequence, candidate)
        except SmartStatementError as exc:
            log.info("gate attempt %d rejected: %s", len(tried), exc)
            continue
        if _looks_like_a_refusal(html, profile):
            log.info("gate attempt %d: password not accepted", len(tried))
            continue
        return gate.pdf(job, sequence), candidate
    if not tried:
        raise SmartStatementError(
            "no statement password available — set this account's statement password on the "
            "Bank tab. The gate is a live bank login, so it is only ever given a password you "
            "saved, never one derived from your name and date of birth"
        )
    raise SmartStatementError(
        f"the statement password was not accepted ({len(tried)} attempt(s))"
    )


def _looks_like_a_refusal(html: str, profile: GateProfile) -> bool:
    """The gate answers a wrong password with a page, not an HTTP error."""
    return bool(profile.refusal.search(html)) and "<table" not in html.lower()
