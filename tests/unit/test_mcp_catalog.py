"""Connector catalog (plan de conectores C4): data, its schema and mcp.catalog.*.

Deterministic: a temporary RINARI_HOME (app_ctx), the file credential store
(RINARI_KEYRING=0) and no network — adding a server never connects it here.
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.mcp import catalog
from rinari.tools.index import connectors_from

GOOGLE_HOSTS = {
    "google-workspace": "https://workspacemcp.googleapis.com/mcp/v1",
    "google-gmail": "https://gmailmcp.googleapis.com/mcp/v1",
    "google-drive": "https://drivemcp.googleapis.com/mcp/v1",
    "google-docs": "https://docsmcp.googleapis.com/mcp/v1",
    "google-sheets": "https://sheetsmcp.googleapis.com/mcp/v1",
    "google-slides": "https://slidesmcp.googleapis.com/mcp/v1",
    "google-calendar": "https://calendarmcp.googleapis.com/mcp/v1",
    "google-chat": "https://chatmcp.googleapis.com/mcp/v1",
    "google-people": "https://people.googleapis.com/mcp/v1",
}

#: The C0 table of the connectors plan (verified 2026-10-19): URL and what
#: Rinari needs to sign in.
C0_TABLE = {
    "notion": ("https://mcp.notion.com/mcp", "registration"),
    "linear": ("https://mcp.linear.app/mcp", "registration"),
    "todoist": ("https://ai.todoist.net/mcp", "registration"),
    "airtable": ("https://mcp.airtable.com/mcp", "registration"),
    "sentry": ("https://mcp.sentry.dev/mcp", "registration"),
    "canva": ("https://mcp.canva.com/mcp", "registration"),
    "clickup": ("https://mcp.clickup.com/mcp", "registration"),
    "vercel": ("https://mcp.vercel.com", "registration"),
    "supabase": ("https://mcp.supabase.com/mcp", "registration"),
    "stripe": ("https://mcp.stripe.com", "registration"),
    "figma": ("https://mcp.figma.com/mcp", "registration"),
    "dropbox": ("https://mcp.dropbox.com/mcp", "registration"),
    "atlassian": ("https://mcp.atlassian.com/v1/mcp", "registration"),
    "elevenlabs": ("https://api.elevenlabs.io/v1/mcp", "client_metadata"),
    "slack": ("https://mcp.slack.com/mcp", "own_client"),
    "asana": ("https://mcp.asana.com/v2/mcp", "own_client"),
    "hubspot": ("https://mcp.hubspot.com", "own_client"),
    "github": ("https://api.githubcopilot.com/mcp/", "token"),
    **{key: (url, "own_client") for key, url in GOOGLE_HOSTS.items()},
}


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = itertools.count(1)


def _call(server, method, params=None):
    request = {"id": f"r{next(_ids)}", "method": method, "params": params or {}}
    return server.handle_line(json.dumps(request))


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _err(response):
    assert response is not None and response["ok"] is False, response
    return response["error"]


def _db_text(services) -> str:
    rows = services.ctx.db.query("SELECT * FROM mcp_servers")
    return json.dumps([dict(r) for r in rows])


# -- the data ----------------------------------------------------------------------


def test_shipped_catalog_matches_its_schema() -> None:
    document = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    assert catalog.validate_catalog(document) == []
    assert document["version"] == 1


@pytest.mark.parametrize(
    "breakage",
    [
        lambda e: e.pop("setup"),
        lambda e: e["server"].update(url="http://mcp.example.com/mcp"),
        lambda e: e["setup"].update(kind="magic"),
        lambda e: e["description"].pop("en"),
        lambda e: e.update(logo="Logo With Spaces.png"),
        lambda e: e["server"]["auth"].update(kind="bearer"),  # registration needs oauth
    ],
)
def test_catalog_schema_rejects_a_broken_entry(breakage) -> None:
    document = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    broken = copy.deepcopy(document)
    notion = next(e for e in broken["entries"] if e["id"] == "notion")
    breakage(notion)
    assert catalog.validate_catalog(broken) != []


def test_catalog_schema_rejects_duplicate_ids_and_urls() -> None:
    document = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    broken = copy.deepcopy(document)
    broken["entries"].append(copy.deepcopy(broken["entries"][0]))
    errors = catalog.validate_catalog(broken)
    assert any("duplicate id" in e for e in errors)
    assert any("same URL" in e for e in errors)


def test_every_c0_service_matches_the_c0_table() -> None:
    entries = {e["id"]: e for e in catalog.entries()}
    for entry_id, (url, setup) in C0_TABLE.items():
        entry = entries[entry_id]
        assert entry["server"] == {**entry["server"], "transport": "http", "url": url}, entry_id
        assert entry["setup"]["kind"] == setup, entry_id
        wanted = "bearer" if setup == "token" else "oauth"
        assert entry["server"]["auth"]["kind"] == wanted, entry_id
    # GitHub: a token as Bearer, or the owner's own OAuth app.
    github = entries["github"]["setup"]
    assert [a["kind"] for a in github["alternatives"]] == ["own_client"]
    # ElevenLabs: Rinari's client metadata document, or an own client_id.
    assert [a["kind"] for a in entries["elevenlabs"]["setup"]["alternatives"]] == ["own_client"]
    for entry_id in ("slack", "hubspot"):
        required = {f["name"] for f in entries[entry_id]["setup"]["fields"] if f["required"]}
        assert required == {"client_id", "client_secret"}, entry_id
    asana = {f["name"] for f in entries["asana"]["setup"]["fields"] if f["required"]}
    assert asana == {"client_id"}


def test_google_entries_use_an_own_client_with_offline_access_and_the_preview_notice() -> None:
    google = [e for e in catalog.entries() if e["category"] == "google"]
    assert {e["id"] for e in google} == set(GOOGLE_HOSTS)
    for entry in google:
        assert entry["server"]["auth"]["authorize_params"] == {"access_type": "offline"}
        required = {f["name"] for f in entry["setup"]["fields"] if f["required"]}
        assert required == {"client_id", "client_secret"}
        assert entry["preview"] is True and entry["group"] == "google"
        assert "Developer Preview" in entry["notes"]["es"]
        assert "Developer Preview" in entry["notes"]["en"]


def test_local_database_entries_take_their_connection_as_a_secret() -> None:
    postgres = catalog.entry("postgres")
    sqlite = catalog.entry("sqlite")
    assert postgres["server"]["transport"] == "stdio"
    assert "--prebuilt=postgres" in postgres["server"]["command"]
    secret = {e["name"] for e in postgres["server"]["env"] if e["secret"]}
    assert secret == {"POSTGRES_PASSWORD"}
    assert sqlite["server"]["connection_string"]["env"] == {"path": "SQLITE_DATABASE"}


def test_the_catalog_carries_no_owner_server_and_leaves_docker_and_planka_out() -> None:
    text = catalog.CATALOG_PATH.read_text(encoding="utf-8").lower()
    for word in ("xainner", "visual lab", "pufferpanel", "docker", "planka"):
        assert word not in text
    assert all(e["description"]["es"] and e["description"]["en"] for e in catalog.entries())


# -- turning an entry into a server --------------------------------------------------


def test_catalog_add_registration_creates_an_oauth_server_to_sign_in(server, services) -> None:
    listed = _ok(_call(server, "mcp.catalog.list"))
    notion = next(e for e in listed["entries"] if e["id"] == "notion")
    assert notion["added"] is False and notion["servers"] == []

    added = _ok(_call(server, "mcp.catalog.add", {"id": "notion"}))
    assert added["next"] == "sign_in" and added["entry_id"] == "notion"
    assert added["server"]["name"] == "notion"
    assert added["server"]["url"] == "https://mcp.notion.com/mcp"
    assert added["server"]["auth"]["kind"] == "oauth"

    listed = _ok(_call(server, "mcp.catalog.list"))
    notion = next(e for e in listed["entries"] if e["id"] == "notion")
    assert notion["added"] is True
    assert notion["servers"][0]["name"] == "notion"
    assert notion["servers"][0]["scope"] == "global"


def test_catalog_add_refuses_a_taken_name(server) -> None:
    _ok(_call(server, "mcp.catalog.add", {"id": "linear"}))
    error = _err(_call(server, "mcp.catalog.add", {"id": "linear", "name": "linear"}))
    assert error["code"] == "INVALID_PARAMS"
    # Without a name, a second one gets its own.
    again = _ok(_call(server, "mcp.catalog.add", {"id": "linear"}))
    assert again["server"]["name"] == "linear-2"


def test_catalog_add_google_needs_the_owners_client_and_keeps_its_secret_hidden(
    server, services
) -> None:
    error = _err(_call(server, "mcp.catalog.add", {"id": "google-gmail"}))
    assert error["code"] == "INVALID_PARAMS" and "client_id" in error["message"]

    secret = "client-secret-for-tests-0123"
    response = _call(
        server,
        "mcp.catalog.add",
        {
            "id": "google-gmail",
            "client_id": "1234-test.apps.example.com",
            "client_secret": secret,
            "redirect_port": 38111,
        },
    )
    added = _ok(response)
    auth = added["server"]["auth"]
    assert auth["kind"] == "oauth"
    assert auth["client_id"] == "1234-test.apps.example.com"
    assert auth["client_secret"]["configured"] is True
    assert auth["authorize_params"] == {"access_type": "offline"}
    assert auth["redirect_port"] == 38111
    assert secret not in json.dumps(response)
    assert secret not in _db_text(services)
    row = services.mcp.show("google-gmail")
    ref = json.loads(row["config_json"])["auth"]["client_secret_ref"]
    assert services.credentials.resolve(ref) == secret


def test_catalog_add_github_with_a_token_or_an_own_app(server, services) -> None:
    error = _err(_call(server, "mcp.catalog.add", {"id": "github"}))
    assert "token" in error["message"]
    token = "ghp_testtokenvalue0123456789"
    response = _call(server, "mcp.catalog.add", {"id": "github", "token": token})
    added = _ok(response)
    assert added["next"] == "connect"
    assert added["server"]["auth"] == {
        "kind": "bearer",
        "token": {"configured": True, "source": "stored"},
    }
    assert token not in json.dumps(response) and token not in _db_text(services)

    own = _ok(
        _call(
            server,
            "mcp.catalog.add",
            {"id": "github", "name": "github-app", "client_id": "Iv1.test", "client_secret": "x"},
        )
    )
    assert own["server"]["auth"]["kind"] == "oauth" and own["next"] == "sign_in"


def test_catalog_add_elevenlabs_uses_the_metadata_document_or_an_own_client(server) -> None:
    plain = _ok(_call(server, "mcp.catalog.add", {"id": "elevenlabs"}))
    assert plain["server"]["auth"]["kind"] == "oauth"
    assert plain["server"]["auth"]["client_id"] is None
    own = _ok(
        _call(server, "mcp.catalog.add", {"id": "elevenlabs", "name": "el", "client_id": "el-1"})
    )
    assert own["server"]["auth"]["client_id"] == "el-1"


def test_catalog_add_postgres_splits_the_connection_string_into_stored_secrets(
    server, services
) -> None:
    password = "pg-password-for-tests"
    dsn = f"postgresql://reporter:{password}@db.internal:6543/sales?sslmode=require"
    response = _call(server, "mcp.catalog.add", {"id": "postgres", "connection_string": dsn})
    added = _ok(response)
    assert added["next"] == "connect"
    view = added["server"]
    assert view["transport"] == "stdio"
    names = {e["name"] for e in view["env"]}
    assert names == {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_QUERY_PARAMS",
    }
    assert password not in json.dumps(response) and password not in _db_text(services)
    env = json.loads(services.mcp.show("postgres")["config_json"])["env"]
    resolve = services.credentials.resolve
    assert resolve(env["POSTGRES_PASSWORD"]) == password
    assert resolve(env["POSTGRES_HOST"]) == "db.internal"
    assert resolve(env["POSTGRES_PORT"]) == "6543"
    assert resolve(env["POSTGRES_DATABASE"]) == "sales"
    assert resolve(env["POSTGRES_QUERY_PARAMS"]) == "sslmode=require"


def test_catalog_add_postgres_without_a_password_names_what_is_missing(server) -> None:
    error = _err(
        _call(
            server, "mcp.catalog.add", {"id": "postgres", "connection_string": "postgres://u@h/db"}
        )
    )
    assert "POSTGRES_PASSWORD" in error["message"]
    wrong = _err(
        _call(
            server,
            "mcp.catalog.add",
            {"id": "postgres", "connection_string": "mysql://u:hunter2-secret@h/db"},
        )
    )
    assert "hunter2-secret" not in json.dumps(wrong)


def test_catalog_add_sqlite_takes_a_path_or_a_sqlite_url(server, services, tmp_path) -> None:
    database = tmp_path / "data.db"
    _ok(
        _call(
            server,
            "mcp.catalog.add",
            {"id": "sqlite", "secrets": {"SQLITE_DATABASE": str(database)}},
        )
    )
    env = json.loads(services.mcp.show("sqlite")["config_json"])["env"]
    assert services.credentials.resolve(env["SQLITE_DATABASE"]) == str(database)
    params = catalog.create_params(
        catalog.entry("sqlite"), {"connection_string": "sqlite:///C:/x/a.db"}
    )
    assert params["env"] == {"SQLITE_DATABASE": "C:/x/a.db"}
    params = catalog.create_params(
        catalog.entry("sqlite"), {"connection_string": "sqlite:////srv/data/a.db"}
    )
    assert params["env"] == {"SQLITE_DATABASE": "/srv/data/a.db"}
    params = catalog.create_params(
        catalog.entry("sqlite"), {"connection_string": "file:///C:/x/b.db"}
    )
    assert params["env"] == {"SQLITE_DATABASE": "C:/x/b.db"}


def test_catalog_add_refuses_inputs_that_do_not_belong_to_the_entry(server) -> None:
    error = _err(_call(server, "mcp.catalog.add", {"id": "notion", "secrets": {"X": "y"}}))
    assert error["code"] == "INVALID_PARAMS"
    error = _err(_call(server, "mcp.catalog.add", {"id": "sqlite", "token": "abc"}))
    assert error["code"] == "INVALID_PARAMS"
    error = _err(_call(server, "mcp.catalog.add", {"id": "sqlite", "secrets": {"OTHER": "y"}}))
    assert "OTHER" in error["message"]
    assert _err(_call(server, "mcp.catalog.add", {"id": "nope"}))["code"] == "NOT_FOUND"


# -- the hook for C6 ---------------------------------------------------------------------


def test_catalog_description_of_an_added_server(services) -> None:
    services.mcp.add(
        "work-notes", transport="http", url="https://mcp.notion.com/mcp/", auth={"kind": "oauth"}
    )
    services.mcp.add("mine", transport="http", url="https://mcp.example.com/mcp")
    row = services.mcp.show("work-notes")
    assert catalog.catalog_description(row) == catalog.entry("notion")["description"]["en"]
    assert catalog.catalog_description(row, "es") == catalog.entry("notion")["description"]["es"]
    assert catalog.catalog_description(services.mcp.show("mine")) is None
    assert catalog.catalog_description_for(services.mcp, "work-notes", "en").startswith("Search")
    assert catalog.catalog_description_for(services.mcp, "missing") is None


def test_the_catalog_capability_is_advertised() -> None:
    assert CAPABILITIES["mcp_catalog_v1"] is True


def test_the_index_hook_finds_the_entry_from_a_connector_row(services) -> None:
    # McpService.connectors() rows carry the name and state, not the saved config.
    services.mcp.add(
        "work-notes", transport="http", url="https://mcp.notion.com/mcp/", auth={"kind": "oauth"}
    )
    services.mcp.add("mine", transport="http", url="https://mcp.example.com/mcp")
    hook = catalog.catalog_lookup(services.mcp)
    notion = catalog.entry("notion")["description"]["en"]
    assert hook({"name": "work-notes", "status": "connecting", "hint": None}) == notion
    assert hook({"name": "mine", "status": "connecting", "hint": None}) is None
    assert hook({"name": "gone", "status": "error", "hint": None}) is None

    connectors = connectors_from(
        [{"name": "work-notes", "status": "connecting", "hint": None}], catalog=hook
    )
    assert connectors[0].catalog_description == notion


def test_the_index_shows_a_project_server_only_in_its_own_folder(services, tmp_path) -> None:
    home, other = tmp_path / "proj-a", tmp_path / "proj-b"
    home.mkdir()
    other.mkdir()
    services.trust.add(home)
    services.trust.add(other)
    services.mcp.add(
        "local-notes",
        transport="http",
        url="http://127.0.0.1:9/mcp",
        scope="project",
        project_root=home,
    )
    services.mcp.reconnect("local-notes", project=home)

    assert "local-notes" in [c["name"] for c in services.mcp.connectors(home)]
    assert "local-notes" not in [c["name"] for c in services.mcp.connectors(other)]
    assert "local-notes" not in [c["name"] for c in services.mcp.connectors(None)]
