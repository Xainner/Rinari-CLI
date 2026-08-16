from rinari.shared.redaction import REDACTED, Redactor, redact_secret


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
