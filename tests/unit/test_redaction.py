import pytest

from rinari.shared.redaction import REDACTED, Redactor, redact_secret, redact_text


def test_redact_short_value_fully_masked():
    assert redact_secret("sk-123") == REDACTED
    assert redact_secret("") == REDACTED


def test_redact_long_value_keeps_identifying_edges():
    masked = redact_secret("sk-live-abcdefghijklmnop1234")
    assert "abcdefghijklmnop" not in masked
    assert masked.startswith("sk-")
    assert masked.endswith("1234")
    assert "..." in masked


def test_redactor_replaces_known_secrets():
    redactor = Redactor(["super-secret-value", "tok_123"])
    text = "using super-secret-value and tok_123 in output"
    assert redactor.redact(text) == f"using {REDACTED} and {REDACTED} in output"


def test_redactor_ignores_unknown_values():
    redactor = Redactor(["abc"])
    assert redactor.redact("xyz stays") == "xyz stays"


def test_redactor_skips_empty_secrets():
    assert Redactor([""]).redact("text") == "text"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            'curl -H "Authorization: Bearer p8OXw3xohFXz1t65TYKGH87p" http://x',
            "p8OXw3xohFXz1t65TYKGH87p",
        ),
        ("x-api-key: 0123456789abcdef", "0123456789abcdef"),
        ("OPENAI_API_KEY=sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234"),
        (
            "git clone https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/x",
            "ghp_abcdefghij",
        ),
        ("GET /cb?code=1&access_token=abc.def-123 HTTP/1.1", "abc.def-123"),
        ('password = "hunter22"', "hunter22"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here", "eyJzdWIiOiIxIn0"),
    ],
)
def test_redact_text_hides_secret_shapes(text, secret):
    redacted = redact_text(text)
    assert secret not in redacted and REDACTED in redacted


def test_redact_text_keeps_ordinary_text_and_hides_known_values():
    plain = "ssh saturno ps aux --sort=-%mem | head; token count 42"
    assert redact_text(plain) == plain
    assert redact_text("value abc-123 here", known=("abc-123",)) == f"value {REDACTED} here"
