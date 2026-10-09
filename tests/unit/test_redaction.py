import pytest

from rinari.shared.redaction import REDACTED, Redactor, redact_secret, redact_text, redact_value


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


# Token prefixes are split so the fake values below never appear literally
# in the source: secret scanners (and push protection) match them as real.
_GL, _SK, _XOX, _AK = "glp" + "at-", "sk_" + "live_", "xo" + "xb-", "AK" + "IA"


# Fake credentials embedded in command lines and outputs (lote 3 audit: a
# password reached session history because the model ran a command with it).
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (r"net use \\nas01\backup Tr0ub4dor&3x /user:admin", "Tr0ub4dor"),
        (r"net use Z: \\nas01\backup Fake-Pass-91 /user:WORK\admin /persistent:no", "Fake-Pass-91"),
        (r'net use \\nas01\backup "fake pass 91" /user:admin', "fake pass 91"),
        (r"net use \\nas01\backup /user:admin FakePass91", "FakePass91"),
        ("net user backupsvc FakePass91 /add", "FakePass91"),
        ("mysql -u root -pFakePass91 -h db.local shop", "FakePass91"),
        ("sshpass -p 'FakePass91' ssh admin@host", "FakePass91"),
        ("curl -u admin:FakePass91 https://api.example.test", "FakePass91"),
        ("psql --password=FakePass91 -h db", "FakePass91"),
        ("tool login --db-password FakePass91 --verbose", "FakePass91"),
        ('Connect-Thing -Password "FakePass91"', "FakePass91"),
        ("gh auth login --token FakeTok3n0001", "FakeTok3n0001"),
        ("deploy --api-key FakeKey0001", "FakeKey0001"),
        ('$p = ConvertTo-SecureString "FakePass91" -AsPlainText -Force', "FakePass91"),
        ("export GITHUB_TOKEN=FakeTok3n0001", "FakeTok3n0001"),
        ("set DB_PASSWORD=FakePass91 && run", "FakePass91"),
        ('$env:SERVICE_SECRET = "FakeSecret91"', "FakeSecret91"),
        ("docker run -e MYSQL_ROOT_PASSWORD=FakePass91 mysql", "FakePass91"),
        ("Server=db;Database=shop;User Id=sa;Password=FakePass91;", "FakePass91"),
        ('{"password": "FakePass91", "user": "admin"}', "FakePass91"),
        ("POSTGRES_PASSWORD: FakePass91", "FakePass91"),
        ("git remote add o https://admin:FakePass91@git.example.test/r.git", "FakePass91"),
        ("postgres://app:FakePass91@db.local:5432/shop", "FakePass91"),
        ('curl -H "Authorization: Basic YWRtaW46RmFrZVBhc3M5MQ==" x', "YWRtaW46RmFrZVBhc3M5MQ"),
        ("PRIVATE-TOKEN: " + _GL + "FakeFakeFakeFake0001", _GL + "FakeFake"),
        ("key " + _SK + "FakeFakeFakeFake0001 used", _SK + "FakeFake"),
        (_XOX + "1234567890-FakeFakeFake", _XOX + "1234567890"),
        (_AK + "FAKEFAKEFAKE0001 in output", _AK + "FAKEFAKEFAKE0001"),
    ],
)
def test_redact_text_hides_credentials_in_commands_and_outputs(text, secret):
    redacted = redact_text(text)
    assert secret not in redacted and REDACTED in redacted


@pytest.mark.parametrize(
    "text",
    [
        r"net use \\nas01\backup * /user:admin",
        r"net use \\nas01\backup /user:admin",
        "net user backupsvc /add",
        "mysql -u root -p -P 3306 shop",
        "mkdir -p build/out && git log -p -3",
        "docker login --password-stdin < token.txt",
        "gh auth login --with-token < $HOME/token",
        "export PWD=/home/someone && echo $DB_PASSWORD",
        'set API_KEY=%API_KEY% && curl -H "x-api-key: $API_KEY" x',
        "def login(password: str, token: Optional[str] = None):",
        'api_key = os.environ["API_KEY"]',
        "const apiKey = process.env.API_KEY;",
        "token = request.headers.get(name)",
        "GOOGLE_APPLICATION_CREDENTIALS=/etc/app/sa.json",
        "max_tokens=4096 temperature=0.2",
        "http://localhost:8080/health",
    ],
)
def test_redact_text_keeps_references_code_and_prompts(text):
    assert redact_text(text) == text


def test_redacted_command_keeps_its_shape():
    redacted = redact_text(r'net use \\nas01\backup "fake pass" /user:admin')
    assert redacted == rf'net use \\nas01\backup "{REDACTED}" /user:admin'
    assert redact_text(redacted) == redacted


def test_redact_value_hides_credential_fields_and_nested_strings():
    value = {
        "command": "mysql -pFakePass91 shop",
        "headers": {"X-Custom-Token": "opaque-value", "Accept": "json"},
        "password": "shortpw",
        "api_key_ref": "keyring://rinari/openai",
        "secret": "env://SERVICE_SECRET",
        "items": [{"text": "PASSWORD=FakePass91"}],
        "count": 3,
    }
    redacted = redact_value(value)
    assert "FakePass91" not in str(redacted)
    assert redacted["headers"] == {"X-Custom-Token": REDACTED, "Accept": "json"}
    assert redacted["password"] == REDACTED
    assert redacted["api_key_ref"] == "keyring://rinari/openai"
    assert redacted["secret"] == "env://SERVICE_SECRET"
    assert redacted["count"] == 3


def test_redact_value_leaves_opaque_provider_blobs_untouched():
    blob = {"type": "thinking", "thinking": "x", "signature": "token=FakeFakeFake"}
    assert redact_value(blob, skip_keys=frozenset({"signature"}))["signature"] == (
        "token=FakeFakeFake"
    )
