"""A mailed statement gate: its two ciphers, and the exchange that uses them.

Runs against `sparser/gates/example.json` — the shipped fixture profile, whose
host does not resolve and whose key is nobody's. No real institution's profile is
in this repository, so these tests describe the mechanism, which is the part the
repository actually contains.
"""
import base64
import email
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sparser import smartstatement
from sparser.mailbox import linked_statement_fetcher, smart_statement_links
from sparser.smartstatement import SmartStatementError, decrypt, encrypt

GATES = Path(smartstatement.__file__).parent / "gates"
EXAMPLE = smartstatement.GateProfile.from_json(
    json.loads((GATES / "example.json").read_text(encoding="utf-8")), "example.json"
)
KEY = EXAMPLE.cipher_key
HOST = "https://statements.example.bank"
SERVICE = f"{HOST}/StatementService"


@pytest.fixture(autouse=True)
def installed_profile(monkeypatch):
    """Install the fixture profile for every test.

    Whatever profiles this machine happens to carry are irrelevant to the code
    under test, and a developer's own gate must never be what a test suite talks
    to — so the search path is pointed at the shipped example instead.
    """
    monkeypatch.setenv("SPARSER_GATES", str(GATES))
    smartstatement.profiles(reload=True)
    yield
    smartstatement.profiles(reload=True)


# ------------------------------------------------------------------ profiles

def test_the_example_profile_documents_every_field_the_client_needs():
    assert EXAMPLE.link.search(f"{SERVICE}/GetStatement.jsp?job=k1")
    assert EXAMPLE.job_query_param and EXAMPLE.password_field and EXAMPLE.cipher_key
    assert "{job}" in EXAMPLE.token_path and "{sequence}" in EXAMPLE.pdf_path


def test_a_profile_can_lower_the_attempt_cap_but_never_raise_it():
    """The cap protects a real account from a wrong password, so it is the
    module's to enforce, not a profile's to relax."""
    greedy = smartstatement.GateProfile.from_json(
        {**json.loads((GATES / "example.json").read_text()), "max_password_attempts": 50}
    )
    assert greedy.max_attempts == smartstatement.MAX_PASSWORD_ATTEMPTS
    modest = smartstatement.GateProfile.from_json(
        {**json.loads((GATES / "example.json").read_text()), "max_password_attempts": 1}
    )
    assert modest.max_attempts == 1


def test_a_profile_missing_its_endpoints_is_rejected_with_its_name():
    with pytest.raises(SmartStatementError, match="unusable gate profile"):
        smartstatement.GateProfile.from_json({"link_pattern": "x"}, "broken.json")


def test_a_link_no_profile_claims_says_where_profiles_go(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARSER_GATES", str(tmp_path))
    smartstatement.profiles(reload=True)
    with pytest.raises(SmartStatementError, match="no gate profile matches"):
        smartstatement.fetch_pdf("https://statements.example.bank/x?job=1", ["PASS"])


def test_with_no_profile_installed_a_mail_carries_no_links(monkeypatch, tmp_path):
    """The honest empty state: nothing to walk, so nothing is found."""
    monkeypatch.setenv("SPARSER_GATES", str(tmp_path))
    smartstatement.profiles(reload=True)
    assert smartstatement.links_in(f"{SERVICE}/GetStatement.jsp?job=k1") == []


# ------------------------------------------------------------------ the XOR cipher

def test_encrypt_round_trips_through_its_own_inverse():
    for text in ["TEST0101", "202601010000000000000000000000000000000000TEST0101", "a"]:
        assert decrypt(encrypt(text, KEY), KEY) == text


def test_a_fixed_payload_decodes_to_the_documented_shape():
    """The payload's documented shape: timestamp, request id, name stem, DDMM."""
    plain = "202601010000000000000000000000000000000000TEST0101"
    payload = encrypt(plain, KEY, iv=0x19)
    assert decrypt(payload, KEY) == plain
    # The lead byte is the IV in clear, so re-encrypting with it must be exact.
    assert encrypt(decrypt(payload, KEY), KEY, iv=int(payload[:2], 16)) == payload


def test_the_lead_byte_randomises_every_call():
    """Same input, different ciphertext — the gate would otherwise leak that two
    attempts used the same password."""
    assert len({encrypt("TEST0101", KEY) for _ in range(20)}) > 1


def test_output_is_two_hex_characters_per_byte_plus_the_iv():
    assert len(encrypt("TEST0101", KEY)) == 2 + 2 * len("TEST0101")


# ------------------------------------------------------------------ the AES payload

def _wrap(html: str, key: bytes = b"1234^^^^^^^^^^^^") -> str:
    """Build a page shaped like the one a gate answers with."""
    payload = html.encode()
    pad = 16 - len(payload) % 16
    payload += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(payload) + encryptor.finalize()
    return (
        "<html><head><script>/* CryptoJS */</script><script>"
        f'Data("{base64.b64encode(key).decode()}", "{base64.b64encode(ciphertext).decode()}")'
        "</script></head><body></body></html>"
    )


def test_statement_page_is_unwrapped_with_the_key_it_carries():
    assert "<table>rows</table>" in smartstatement.decrypt_statement_page(
        _wrap("<html><table>rows</table></html>")
    )


def test_a_page_without_the_data_call_is_an_error_not_an_empty_statement():
    with pytest.raises(SmartStatementError):
        smartstatement.decrypt_statement_page("<html><body>Session expired</body></html>")


# ------------------------------------------------------------------- link discovery

def _mail(body: str, content_type: str = "text/html") -> email.message.Message:
    return email.message_from_string(
        f"From: Example Bank <estatement@example.bank>\nSubject: Statement\n"
        f"Content-Type: {content_type}\n\n{body}"
    )


def test_link_is_found_and_entity_decoded():
    link = smart_statement_links(_mail(
        f'<a href="{SERVICE}/GetStatement.jsp?job=abc123&amp;utm_medium=email">View</a>'
    ))[0]
    assert link.endswith("job=abc123&utm_medium=email")


def test_repeated_links_collapse_to_one():
    body = (
        f'<a href="{SERVICE}/GetStatement.jsp?job=k1">a</a>'
        f'<a href="{SERVICE}/GetStatement.jsp?job=k1">b</a>'
    )
    assert len(smart_statement_links(_mail(body))) == 1


def test_a_link_without_a_job_parameter_is_not_a_statement_link():
    assert smart_statement_links(_mail(f'<a href="{HOST}/unsubscribe">stop</a>')) == []


# ------------------------------------------------------------------- the exchange

class _FakeHost:
    """The gate, reduced to the four responses that matter — and its statefulness.

    Cookies are the point of the fixture: a real host rejects every call after
    the first unless the session cookie it set is sent back, so the fake refuses
    the same way.
    """

    JOB = "010120260000001111112222223333334444aaaabbbbccccdd"
    SEQUENCE = "320118178"
    TOKEN = "20260101000000000000000000000000000000000"
    PASSWORD = "TEST0101"

    def __init__(self, password=PASSWORD):
        self.password = password
        self.calls: list[str] = []
        self.cookie_issued = False

    def __call__(self, request):
        url = request.full_url
        path = urllib.parse.urlsplit(url).path
        self.calls.append(path)
        sent_cookie = request.get_header("Cookie", "")
        if path.endswith("GetStatement.jsp"):
            self.cookie_issued = True
            return _response(
                f"""<html><body><form action="./service/app/htmlformat" method="POST">
                <input type="hidden" name="ke" id="ke" value='{self.JOB}'/>
                <input type="hidden" name="sequence" id="sequence" value='{self.SEQUENCE}'/>
                <input type="password" name="pwd" id="pwd"/></form></body></html>""",
                set_cookie="JSESSIONID=ABC123.worker1; Path=/",
            )
        if not sent_cookie:
            raise urllib.error.HTTPError(url, 403, "no session", {}, None)
        if path.endswith("GetToken"):
            assert f"job={self.JOB}" in url
            return _response(self.TOKEN)
        if path.endswith("htmlformat"):
            fields = urllib.parse.parse_qs(request.data.decode())
            assert fields["ke"] == [self.JOB] and fields["sequence"] == [self.SEQUENCE]
            offered = decrypt(fields["pwd"][0], KEY)
            if offered != self.TOKEN + self.password:
                return _response("<html><body>Please Enter the valid password</body></html>")
            return _response(_wrap("<html><table>statement</table></html>"))
        if path.endswith("pdfformat"):
            assert f"reqid={self.SEQUENCE}" in url
            return _response(b"%PDF-1.4 the statement")
        raise AssertionError(f"unexpected call to {path}")


def _response(body, set_cookie=None):
    """What a urllib handler is expected to return, including the cookie header
    the real jar reads on the way back out."""
    import email.message
    import urllib.response

    if isinstance(body, str):
        body = body.encode()
    headers = email.message.Message()
    headers["Content-Type"] = "text/html"
    if set_cookie:
        headers["Set-Cookie"] = set_cookie
    response = urllib.response.addinfourl(io.BytesIO(body), headers, "https://gate/", 200)
    response.msg = "OK"
    return response


@pytest.fixture
def gate(monkeypatch):
    """Substitute the transport, not the opener.

    Patching ``OpenerDirector.open`` would take the cookie processor out of the
    loop and quietly make the cookie assertions vacuous. Replacing the HTTPS
    handler instead leaves the real jar attaching and extracting cookies around
    every call, which is the behaviour under test.
    """
    host = _FakeHost()
    monkeypatch.setattr(
        urllib.request.HTTPSHandler, "https_open",
        lambda self, request: host(request), raising=False,
    )
    return host


LINK = f"{SERVICE}/GetStatement.jsp?job={_FakeHost.JOB}&utm_medium=email"


def test_the_full_exchange_returns_the_pdf_and_the_password_that_worked(gate):
    pdf, used = smartstatement.fetch_pdf(LINK, ["WRONG0101", "TEST0101"])
    assert pdf.startswith(b"%PDF-")
    assert used == "TEST0101"
    # Form, then token, then password, then PDF — and the wrong password costs a
    # full round of the first three rather than reusing a spent token.
    assert gate.calls[-4:] == [
        "/StatementService/GetStatement.jsp",
        "/StatementService/GetToken",
        "/StatementService/service/app/htmlformat",
        "/StatementService/service/app/pdfformat",
    ]


def test_every_call_after_the_first_carries_the_session_cookie(gate):
    """The fake refuses cookie-less calls exactly as a real host does, so
    reaching the PDF at all proves the jar spans the whole exchange."""
    pdf, _ = smartstatement.fetch_pdf(LINK, ["TEST0101"])
    assert pdf.startswith(b"%PDF-")
    assert gate.cookie_issued


def test_a_password_that_is_never_accepted_is_reported_not_retried_forever(gate):
    with pytest.raises(SmartStatementError, match="not accepted"):
        smartstatement.fetch_pdf(LINK, ["NOPE0101", "ALSO0202"])


def test_attempts_are_capped_below_anything_that_looks_like_guessing(gate):
    with pytest.raises(SmartStatementError, match="stopped after"):
        smartstatement.fetch_pdf(LINK, [f"PASS{n:04d}" for n in range(20)])
    assert gate.calls.count("/StatementService/service/app/htmlformat") \
        == smartstatement.MAX_PASSWORD_ATTEMPTS


def test_no_password_at_all_says_what_to_do_about_it(gate):
    with pytest.raises(SmartStatementError, match="no statement password available"):
        smartstatement.fetch_pdf(LINK, [])


def test_the_mail_fetcher_turns_a_linked_mail_into_a_named_pdf(gate):
    fetcher = linked_statement_fetcher(["TEST0101"])
    message = _mail(f'<a href="{LINK}">View Statement</a>')
    [(name, payload)] = fetcher(message, "Your Example Statement", "estatement@example.bank")
    assert name.endswith(".pdf") and payload.startswith(b"%PDF-")


def test_a_mail_with_neither_attachment_nor_link_fails_loudly(gate):
    fetcher = linked_statement_fetcher(["TEST0101"])
    with pytest.raises(SmartStatementError, match="no smart statement link"):
        fetcher(_mail("<p>Your statement is delayed.</p>"), "Statement", "x@example.bank")


def test_the_view_your_statement_button_is_walked_first():
    """Two jobs in one mail: the button decides, not document order."""
    body = (
        f'<a href="{SERVICE}/GetStatement.jsp?job=older">Statement archive</a>'
        f'<a href="{SERVICE}/GetStatement.jsp?job=wanted">View your <b>Statement</b></a>'
    )
    assert smart_statement_links(_mail(body))[0].endswith("job=wanted")


def test_an_image_button_is_recognised_by_its_alt_text():
    body = (
        f'<a href="{SERVICE}/GetStatement.jsp?job=plain">help</a>'
        f'<a href="{SERVICE}/GetStatement.jsp?job=wanted">'
        '<img src="cid:btn.png" alt="View Your Statement"/></a>'
    )
    assert smart_statement_links(_mail(body))[0].endswith("job=wanted")


def test_the_campaign_tag_stands_in_when_the_button_carries_no_words():
    """`utm_tag=View_Statement1` is the same phrase, machine-readable."""
    body = (
        f'<a href="{SERVICE}/GetStatement.jsp?job=other">x</a>'
        f'<a href="{SERVICE}/GetStatement.jsp?job=wanted&amp;utm_tag=View_Statement1">'
        '<img src="cid:b.png"/></a>'
    )
    assert smart_statement_links(_mail(body))[0].startswith(
        f"{SERVICE}/GetStatement.jsp?job=wanted"
    )


def test_a_link_with_no_button_at_all_is_still_followed():
    """The label ranks the candidates; it never gates them. A plain-text mail,
    a relabelled campaign or another language must all still fetch."""
    plain = _mail(
        f"Your statement: {SERVICE}/GetStatement.jsp?job=k9", content_type="text/plain",
    )
    assert smart_statement_links(plain) == [f"{SERVICE}/GetStatement.jsp?job=k9"]
