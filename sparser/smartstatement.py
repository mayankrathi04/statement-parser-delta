"""HDFC "smart statement" retrieval: a mailed link, not a mailed PDF.

Every other issuer attaches the statement to the mail. HDFC sends a link to a
password gate, and the account statement only exists once that gate is passed.
So this module is a small, faithful client of that gate:

    GET  GetStatement.jsp?job=…      -> the form, carrying `ke` and `sequence`
    GET  GetToken?job={ke}        -> a per-attempt nonce
    POST service/app/htmlformat    -> authenticates `encrypt(nonce + password)`
    POST service/app/pdfformat     -> the PDF this pipeline actually wants

The gate is stateful: the first GET sets JSESSIONID and the TS* WAF cookies, and
every later call is rejected without them. One cookie jar therefore spans the
whole exchange, which is also why the token is re-fetched per password attempt
rather than reused.

Two of the bank's own ciphers appear here. Neither is a secret we are keeping:
both keys ship in the page that uses them, and they are implemented only because
the endpoint refuses to talk to a client that does not speak them.
"""
from __future__ import annotations

import base64
import logging
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Iterable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("sparser.smartstatement")


class SmartStatementError(RuntimeError):
    """The link could not be turned into a statement PDF."""


#: The mail body carries this. Matched on the host and read loosely from there,
#: so a changed campaign query string still resolves. What makes it the link is
#: the host plus a ``job`` — the two things the gate itself consumes — never
#: the wording of the button it sits under. (A tracking redirect that percent-
#: encodes the whole URL inside its own would not be seen; HDFC does not use one.)
SMART_LINK = re.compile(
    r"https?://[\w.-]*smartstatements\.hdfc\.bank\.in/[^\s\"'<>\\]+", re.I
)

_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
}

#: The gate is a live banking endpoint; a wrong password is a failed login there
#: too. The caller only ever hands us passwords a person actually entered — the
#: one supplied for the run and the ones saved per account, never a derived
#: guess — so this cap is a hard ceiling on those, not a guessing budget. It
#: stays low so that a user with several saved accounts still cannot rack up
#: failed logins against the gate.
MAX_PASSWORD_ATTEMPTS = 3

_TIMEOUT = 45


# --------------------------------------------------------------- the gate's ciphers

#: Not a key in any meaningful sense: the gate's own page carries it in clear,
#: and it is implemented here only because the endpoint refuses to talk to a
#: client that does not speak it. The real one lives in a local gate profile.
_XOR_KEY = "exampleKey"


def _byte(ch: str) -> int:
    """charCodeAt(0) under the gate's latin-1 assumption."""
    code = ord(ch)
    return code if code < 256 else 256


def encrypt(text: str, key: str = _XOR_KEY, iv: Optional[int] = None) -> str:
    """Port of the gate's `encrypt()` — what the password field actually sends.

    A chained XOR stream: each byte is added to the previous *cipher* byte mod
    255, then XORed with the cycling key. The random lead byte is transmitted in
    clear as the first hex pair, so the result is self-describing and a fresh
    call on the same input differs every time.
    """
    previous = random.randint(1, 255) if iv is None else iv
    out = [f"{previous:02x}"]
    for index, ch in enumerate(text):
        current = (_byte(ch) + previous) % 255
        current ^= _byte(key[index % len(key)])
        out.append(f"{current:02x}")
        previous = current
    return "".join(out).upper()


def decrypt(payload: str, key: str = _XOR_KEY) -> str:
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


_DATA_CALL = re.compile(r"""Data\(\s*["']([^"']+)["']\s*,\s*["']([^"']*)["']\s*\)""", re.S)


def decrypt_statement_page(html: str) -> str:
    """Unwrap the AES-ECB payload the HTML gate answers with.

    The page is a CryptoJS bundle plus one `Data(key, ciphertext)` call, and the
    key is the call's own first argument. Read from the response rather than
    hardcoded, so a rotated key keeps working.
    """
    match = _DATA_CALL.search(html)
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
    """One cookie-sharing conversation with the smart-statement host."""

    def __init__(self, link: str):
        parts = urllib.parse.urlsplit(link)
        if not parts.scheme.startswith("http") or not parts.netloc:
            raise SmartStatementError(f"not a usable smart statement link: {link[:120]}")
        # ".../StatementService/GetStatement.jsp" -> ".../StatementService"
        base_path = parts.path.rsplit("/", 1)[0]
        self.base = f"{parts.scheme}://{parts.netloc}{base_path}"
        self.link = link
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

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
            raise SmartStatementError(f"cannot reach the smart statement host: {exc.reason}") from exc

    def open_form(self) -> tuple[str, str]:
        """Fetch the gate and read the two identifiers its form posts back."""
        with self._open(self.link) as response:
            page = response.read().decode("utf-8", "replace")
        job = _hidden(page, "ke") or urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.link).query
        ).get("job", [""])[0]
        sequence = _hidden(page, "sequence") or _hidden(page, "sequence")
        if not job or not sequence:
            raise SmartStatementError(
                "the statement link no longer serves its password form — it has probably "
                "expired; open the mail and use a fresher link"
            )
        log.info("smart statement gate opened (job %s…)", job[:12])
        return job, sequence

    def token(self, job: str) -> str:
        """A one-shot nonce the password is bound to."""
        with self._open(
            f"{self.base}/GetToken?job={urllib.parse.quote(job)}",
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
            "ke": job,
            "sequence": sequence,
            "pwd": encrypt(token + password),
        }).encode()
        with self._open(
            f"{self.base}/service/app/htmlformat",
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": f"{urllib.parse.urlsplit(self.base).scheme}://"
                               f"{urllib.parse.urlsplit(self.base).netloc}"},
        ) as response:
            page = response.read().decode("utf-8", "replace")
        return decrypt_statement_page(page)

    def pdf(self, job: str, sequence: str) -> bytes:
        """The same statement as a PDF, on the session the password unlocked."""
        query = urllib.parse.urlencode({"job": job, "reqid": sequence, "format": "pdf"})
        with self._open(
            f"{self.base}/service/app/pdfformat?{query}",
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
    the gate is a live bank login and is deliberately never given one. The attempt
    count is capped on top of that, because every attempt here is a real login.
    """
    tried: list[str] = []
    for candidate in passwords:
        if not candidate or candidate in tried:
            continue
        if len(tried) >= MAX_PASSWORD_ATTEMPTS:
            raise SmartStatementError(
                f"stopped after {MAX_PASSWORD_ATTEMPTS} password attempts without success — "
                f"set this account's statement password on the Bank tab so the gate is given "
                f"the right one directly"
            )
        tried.append(candidate)
        # A fresh gate per attempt: the token is single-use and a rejected
        # password leaves the previous session in a state the form will not reuse.
        gate = _Gate(link)
        job, sequence = gate.open_form()
        try:
            html = gate.authenticate(job, sequence, candidate)
        except SmartStatementError as exc:
            log.info("smart statement attempt %d rejected: %s", len(tried), exc)
            continue
        if _looks_like_a_refusal(html):
            log.info("smart statement attempt %d: password not accepted", len(tried))
            continue
        return gate.pdf(job, sequence), candidate
    if not tried:
        raise SmartStatementError(
            "no statement password available — set this account's statement password on the "
            "Bank tab. The smart statement gate is a live bank login, so it is only ever given "
            "a password you saved, never one derived from your name and date of birth"
        )
    raise SmartStatementError(
        f"the statement password was not accepted ({len(tried)} attempt(s))"
    )


_REFUSAL = re.compile(r"invalid\s+password|incorrect\s+password|please\s+enter\s+the\s+valid", re.I)


def _looks_like_a_refusal(html: str) -> bool:
    """The gate answers a wrong password with a page, not an HTTP error."""
    return bool(_REFUSAL.search(html)) and "<table" not in html.lower()
