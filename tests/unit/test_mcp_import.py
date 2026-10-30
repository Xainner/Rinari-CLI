"""Importing MCP servers from other tools (plan de conectores C4).

Never automatic: scans list candidates and only marked ones are created.
Deterministic: a temporary RINARI_HOME (app_ctx), a fake user home and fake
environment for the other apps' config files (never the real ones), the file
credential store and no network.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.mcp_connectors import McpConnectorMethods
from rinari.engine_protocol.server import EngineServer
from rinari.mcp.importers import (
    app_config_paths,
    candidate_from_spec,
    parse_pasted,
    scan_apps,
    scan_project,
    strip_jsonc,
)
from rinari.mcp.project_offers import ProjectNotTrustedError, ProjectOffers
from rinari.mcp.service import canonical_root

SECRET_ENV = "env-secret-value-for-tests"
SECRET_HEADER = "header-secret-value-for-tests"
SECRET_QUERY = "query-secret-value-for-tests"
SECRET_PASSWORD = "url-password-for-tests"
SECRETS = (SECRET_ENV, SECRET_HEADER, SECRET_QUERY, SECRET_PASSWORD)


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir(exist_ok=True)
    return build_services(app_ctx, user_home=user_home)


@pytest.fixture
def fake_pc(tmp_path):
    """A fake home and environment with each app's config (Windows layout)."""
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(home / "AppData" / "Local")}

    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    write(
        appdata / "Claude" / "claude_desktop_config.json",
        json.dumps(
            {
                "mcpServers": {
                    "files": {
                        "command": "npx",
                        "args": ["-y", "@example/files-server", "C:/work"],
                        "env": {"FILES_TOKEN": SECRET_ENV},
                    }
                }
            }
        ),
    )
    write(
        home / ".claude.json",
        json.dumps(
            {
                "numStartups": 3,
                "mcpServers": {
                    "tracker": {
                        "type": "http",
                        "url": "https://mcp.tracker.example/mcp",
                        "headers": {"X-Api-Key": SECRET_HEADER},
                    }
                },
                "projects": {
                    str(home / "repo"): {
                        "mcpServers": {"local-db": {"type": "stdio", "command": "db-mcp"}}
                    }
                },
            }
        ),
    )
    write(
        home / ".cursor" / "mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "search": {
                        "url": f"https://search.example/mcp?api_key={SECRET_QUERY}&team=core"
                    }
                }
            }
        ),
    )
    write(
        appdata / "Code" / "User" / "mcp.json",
        """{
  // VS Code's own format, with comments and trailing commas
  "inputs": [{"id": "token", "type": "promptString", "password": true}],
  "servers": {
    "gh": {"type": "http", "url": "https://api.example.dev/mcp/",
           "headers": {"Authorization": "Bearer ${env:GH_TOKEN}"}},
    "asks": {"type": "stdio", "command": "uvx", "args": ["asks-mcp"],
             "env": {"ASK_TOKEN": "${input:token}", "MODE": "fast"}},
    "old": {"type": "sse", "url": "https://legacy.example/sse"},
  },
}
""",
    )
    write(
        appdata / "Code" / "User" / "settings.json",
        json.dumps({"mcp": {"servers": {"from-settings": {"command": "node", "args": ["s.js"]}}}}),
    )
    write(
        home / ".codex" / "config.toml",
        f"""
model = "some-model"

[mcp_servers.docs]
command = "npx"
args = ["-y", "docs-mcp"]
env = {{ DOCS_KEY = "{SECRET_ENV}" }}

[mcp_servers.remote]
url = "https://remote.example/mcp"
bearer_token_env_var = "REMOTE_TOKEN"
env_http_headers = {{ "X-Org" = "ORG_ID" }}
""",
    )
    return home, env


def _db_text(services) -> str:
    return json.dumps([dict(r) for r in services.ctx.db.query("SELECT * FROM mcp_servers")])


def _assert_no_secret(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# -- where the files are ------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        (
            "win32",
            {"APPDATA": "{home}/AppData/Roaming"},
            [
                "AppData/Roaming/Claude/claude_desktop_config.json",
                ".claude.json",
                ".cursor/mcp.json",
                "AppData/Roaming/Code/User/mcp.json",
                "AppData/Roaming/Code/User/settings.json",
                ".codex/config.toml",
            ],
        ),
        (
            "darwin",
            {},
            [
                "Library/Application Support/Claude/claude_desktop_config.json",
                ".claude.json",
                ".cursor/mcp.json",
                "Library/Application Support/Code/User/mcp.json",
                ".codex/config.toml",
            ],
        ),
        (
            "linux",
            {},
            [
                ".config/Claude/claude_desktop_config.json",
                ".claude.json",
                ".config/Code/User/mcp.json",
                ".config/Code - Insiders/User/settings.json",
                ".codex/config.toml",
            ],
        ),
    ],
)
def test_app_config_paths_cover_each_platform_from_the_home_and_environment(
    tmp_path, platform, env, expected
) -> None:
    home = tmp_path / "someone"
    env = {k: v.format(home=home) for k, v in env.items()}
    paths = {
        p.relative_to(home).as_posix(): source
        for source, p, _flavor in app_config_paths(home, env, platform)
    }
    for item in expected:
        assert item in paths, (platform, item)
    assert set(paths.values()) == {"claude_desktop", "claude_code", "cursor", "vscode", "codex"}


def test_environment_overrides_move_the_config_folders(tmp_path) -> None:
    home = tmp_path / "someone"
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }
    paths = {str(p) for _s, p, _f in app_config_paths(home, env, "linux")}
    assert str(tmp_path / "xdg" / "Claude" / "claude_desktop_config.json") in paths
    assert str(tmp_path / "codex" / "config.toml") in paths
    assert str(tmp_path / "claude" / ".claude.json") in paths


def test_store_build_of_claude_desktop_is_found_on_windows(tmp_path) -> None:
    home = tmp_path / "someone"
    local = tmp_path / "local"
    package = local / "Packages" / "Claude_abc123"
    (package / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    paths = {str(p) for _s, p, _f in app_config_paths(home, {"LOCALAPPDATA": str(local)}, "win32")}
    assert (
        str(package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json") in paths
    )


# -- each tool's format ---------------------------------------------------------------------


def test_scan_reads_each_tools_format_and_shows_only_secret_names(fake_pc) -> None:
    home, env = fake_pc
    candidates, reports = scan_apps(home, env, platform="win32")
    by_key = {c.key: c for c in candidates}
    found = {r.source for r in reports if r.found}
    assert found == {"claude_desktop", "claude_code", "cursor", "vscode", "codex"}

    files = by_key["claude_desktop:files"]
    assert files.transport == "stdio"
    assert files.command == ["npx", "-y", "@example/files-server", "C:/work"]
    assert files.secrets == [{"where": "env", "name": "FILES_TOKEN", "kind": "value"}]

    tracker = by_key["claude_code:tracker"]
    assert tracker.url == "https://mcp.tracker.example/mcp"
    assert tracker.secrets == [{"where": "header", "name": "X-Api-Key", "kind": "value"}]
    local = next(c for c in candidates if c.name == "local-db")
    assert local.project_root == str(home / "repo") and local.importable

    search = by_key["cursor:search"]
    assert search.url == "https://search.example/mcp?api_key=***&team=core"
    assert {"where": "url", "name": "query", "kind": "value"} in search.secrets

    gh = by_key["vscode:gh"]
    assert gh.params["auth"] == {"kind": "bearer", "token": "env://GH_TOKEN"}
    asks = by_key["vscode:asks"]
    assert "unresolved_variable:env.ASK_TOKEN" in asks.warnings
    assert asks.params["env"] == {"MODE": "fast"}
    old = by_key["vscode:old"]
    assert not old.importable and old.reason == "legacy_sse_unsupported"
    assert by_key["vscode:from-settings"].command == ["node", "s.js"]

    docs = by_key["codex:docs"]
    assert docs.secrets == [{"where": "env", "name": "DOCS_KEY", "kind": "value"}]
    remote = by_key["codex:remote"]
    assert remote.params["auth"] == {"kind": "bearer", "token": "env://REMOTE_TOKEN"}
    assert remote.params["headers"] == {"X-Org": {"value": "env://ORG_ID", "secret": True}}

    _assert_no_secret(json.dumps([c.view() for c in candidates]))
    _assert_no_secret(json.dumps([r.view() for r in reports]))


def test_a_url_with_user_and_password_becomes_a_basic_header() -> None:
    candidate = candidate_from_spec(
        "basic",
        {"url": f"https://bob:{SECRET_PASSWORD}@mcp.example.com/mcp"},
        source="pasted",
        key="pasted:basic",
    )
    assert candidate.importable
    assert candidate.url == "https://mcp.example.com/mcp"
    header = candidate.params["headers"]["Authorization"]
    assert header["secret"] is True and header["value"].startswith("Basic ")
    _assert_no_secret(json.dumps(candidate.view()))


def test_a_secret_on_the_command_line_is_not_importable() -> None:
    for args in (
        ["server", "--api-key", "sk-abcdefghijklmnop"],
        ["server", f"postgresql://app:{SECRET_PASSWORD}@db/sales"],
        ["server", "--token=abc"],
    ):
        candidate = candidate_from_spec(
            "leaky", {"command": "npx", "args": args}, source="pasted", key="pasted:leaky"
        )
        assert candidate.reason == "secret_in_command", args
        _assert_no_secret(json.dumps(candidate.view()))
        assert "sk-abcdefghijklmnop" not in json.dumps(candidate.view())


def test_jsonc_comments_and_trailing_commas_are_understood_but_strings_are_kept() -> None:
    text = '{"a": "http://x//y", /* c */ "b": [1, 2,], // end\n}'
    assert json.loads(strip_jsonc(text)) == {"a": "http://x//y", "b": [1, 2]}


def test_pasted_json_in_each_shape_and_errors_without_echoing_it() -> None:
    for document in (
        {"mcpServers": {"one": {"command": "a"}}},
        {"servers": {"one": {"command": "a"}}},
        {"one": {"command": "a"}},
    ):
        assert [c.name for c in parse_pasted(json.dumps(document))] == ["one"]
    with pytest.raises(ValueError) as broken:
        parse_pasted('{"mcpServers": {"x": {"env": {"K": "' + SECRET_ENV + '"}}')
    assert SECRET_ENV not in str(broken.value)
    with pytest.raises(ValueError):
        parse_pasted('{"nothing": 1}')


# -- importing: only what is marked ----------------------------------------------------------


def test_nothing_is_imported_without_marking_and_apply_creates_only_marked(
    services, fake_pc
) -> None:
    home, env = fake_pc
    methods = McpConnectorMethods(services, home=lambda: home, env=lambda: env, platform="win32")
    scanned = methods.import_scan({})
    assert services.mcp.list() == []  # a scan never creates anything
    keys = {c["key"] for c in scanned["candidates"]}
    assert {"claude_desktop:files", "cursor:search", "codex:remote"} <= keys
    _assert_no_secret(json.dumps(scanned))

    applied = methods.import_apply(
        {"keys": ["claude_desktop:files", "cursor:search", "claude_code:tracker"]}
    )
    assert [item["key"] for item in applied["created"]] == [
        "claude_desktop:files",
        "cursor:search",
        "claude_code:tracker",
    ]
    assert applied["failed"] == []
    assert sorted(r["name"] for r in services.mcp.list()) == ["files", "search", "tracker"]
    _assert_no_secret(json.dumps(applied), _db_text(services))

    files = json.loads(services.mcp.show("files")["config_json"])
    assert services.credentials.resolve(files["env"]["FILES_TOKEN"]) == SECRET_ENV
    tracker = json.loads(services.mcp.show("tracker")["config_json"])
    assert services.credentials.resolve(tracker["headers"]["X-Api-Key"]["ref"]) == SECRET_HEADER
    search = services.mcp.show("search")
    assert search["url"] == "https://search.example/mcp?api_key=***&team=core"
    url_ref = json.loads(search["config_json"])["url_ref"]
    assert SECRET_QUERY in services.credentials.resolve(url_ref)
    # The transport gets the whole URL; logs and views only the redacted one.
    transport = services.mcp._transport_for(search)
    assert transport._url == f"https://search.example/mcp?api_key={SECRET_QUERY}&team=core"
    assert services.mcp.view(search)["url_secret"] == {"configured": True, "source": "stored"}

    again = methods.import_scan({})
    by_key = {c["key"]: c for c in again["candidates"]}
    assert by_key["claude_desktop:files"]["existing"] == "files"
    assert by_key["codex:docs"]["existing"] is None


def test_apply_reports_what_cannot_be_imported_and_keeps_going(services, fake_pc) -> None:
    home, env = fake_pc
    methods = McpConnectorMethods(services, home=lambda: home, env=lambda: env, platform="win32")
    applied = methods.import_apply({"keys": ["vscode:old", "nope:x", "codex:docs"]})
    assert [item["key"] for item in applied["created"]] == ["codex:docs"]
    assert {item["key"] for item in applied["failed"]} == {"vscode:old", "nope:x"}


def test_claude_code_project_servers_stay_tied_to_their_folder(services, fake_pc) -> None:
    home, env = fake_pc
    (home / "repo").mkdir()
    methods = McpConnectorMethods(services, home=lambda: home, env=lambda: env, platform="win32")
    key = next(c["key"] for c in methods.import_scan({})["candidates"] if c["name"] == "local-db")
    created = methods.import_apply({"keys": [key]})["created"][0]["server"]
    assert created["scope"] == "project"
    assert created["project_root"] == canonical_root(home / "repo")


def test_pasted_json_through_the_protocol(services, tmp_path) -> None:
    engine = EngineServer(services, user_home=tmp_path / "home")
    ids = itertools.count(1)

    def call(method, params):
        response = engine.handle_line(
            json.dumps({"id": f"p{next(ids)}", "method": method, "params": params})
        )
        assert response["ok"], response
        return response["result"]

    try:
        text = json.dumps({"mcpServers": {"pasted": {"command": "x", "env": {"K": SECRET_ENV}}}})
        scanned = call("mcp.import.scan", {"json": text})
        assert [c["key"] for c in scanned["candidates"]] == ["pasted:pasted"]
        assert scanned["sources"] == []
        applied = call(
            "mcp.import.apply",
            {"json": text, "keys": ["pasted:pasted"], "names": {"pasted:pasted": "from-json"}},
        )
        assert applied["created"][0]["server"]["name"] == "from-json"
        _assert_no_secret(json.dumps(scanned), json.dumps(applied), _db_text(services))
        error = engine.handle_line(
            json.dumps({"id": "bad", "method": "mcp.import.apply", "params": {"keys": []}})
        )
        assert error["ok"] is False and error["error"]["code"] == "INVALID_PARAMS"
    finally:
        engine.close()


# -- a project's .mcp.json: asked once per server -----------------------------------------------


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "lint": {
                        "command": "npx",
                        "args": ["-y", "lint-mcp"],
                        "env": {"LINT_KEY": SECRET_ENV},
                    },
                    "issues": {"type": "http", "url": "https://issues.example/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    (root / ".vscode").mkdir()
    (root / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": {"lint": {"command": "other"}, "notes": {"command": "notes-mcp"}}}),
        encoding="utf-8",
    )
    return root


def test_an_untrusted_folder_offers_nothing_and_accepts_nothing(services, tmp_path) -> None:
    root = _project(tmp_path)
    offers = ProjectOffers(services.ctx, services.mcp, services.trust)
    listed = offers.list(root)
    assert listed["trusted"] is False and listed["offers"] == []
    with pytest.raises(ProjectNotTrustedError):
        offers.decide(root, [{"key": "lint", "accept": True}])
    assert services.mcp.list() == []


def test_the_question_comes_once_per_server_and_accepted_ones_get_project_scope(
    services, tmp_path
) -> None:
    root = _project(tmp_path)
    services.trust.add(root)
    offers = ProjectOffers(services.ctx, services.mcp, services.trust)
    listed = offers.list(root)
    assert [o["key"] for o in listed["offers"]] == ["lint", "issues", "notes"]
    lint = listed["offers"][0]
    assert lint["path"] == ".mcp.json" and lint["command"] == ["npx", "-y", "lint-mcp"]
    _assert_no_secret(json.dumps(listed))

    result = offers.decide(
        root, [{"key": "lint", "accept": True}, {"key": "issues", "accept": False}]
    )
    assert [c["key"] for c in result["created"]] == ["lint"] and result["rejected"] == ["issues"]
    row = services.mcp.show("lint", "project")
    assert row is not None and row["project_root"] == canonical_root(root)
    env = json.loads(row["config_json"])["env"]
    assert services.credentials.resolve(env["LINT_KEY"]) == SECRET_ENV
    _assert_no_secret(_db_text(services))

    again = offers.list(root)
    assert [o["key"] for o in again["offers"]] == ["notes"]
    assert again["answered"] == 2
    offers.decide(root, [{"key": "notes", "accept": False}])
    assert offers.list(root)["offers"] == []
    # Answered in one folder, asked again in another one with the same file.
    other = tmp_path / "other"
    other.mkdir()
    (other / ".mcp.json").write_bytes((root / ".mcp.json").read_bytes())
    services.trust.add(other)
    assert [o["key"] for o in offers.list(other)["offers"]] == ["lint", "issues"]


def test_an_offer_names_the_owner_variables_it_would_send(services, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "author": {
                        "type": "http",
                        "url": "https://author.example/mcp",
                        "headers": {
                            "X-Api-Key": "${AWS_SECRET_ACCESS_KEY}",
                            "Authorization": "Bearer ${GITHUB_TOKEN}",
                        },
                    },
                    "tool": {"command": "tool-mcp", "env": {"KEY": "${env:OPENAI_API_KEY}"}},
                }
            }
        ),
        encoding="utf-8",
    )
    services.trust.add(root)
    offers = ProjectOffers(services.ctx, services.mcp, services.trust).list(root)["offers"]
    variables = {o["key"]: {s["name"]: s.get("variable") for s in o["secrets"]} for o in offers}
    # The owner sees which of their variables would go to the author's host.
    assert variables["author"] == {
        "X-Api-Key": "AWS_SECRET_ACCESS_KEY",
        "Authorization": "GITHUB_TOKEN",
    }
    assert variables["tool"] == {"KEY": "OPENAI_API_KEY"}


def test_a_changed_definition_is_offered_again(services, tmp_path) -> None:
    root = _project(tmp_path)
    services.trust.add(root)
    offers = ProjectOffers(services.ctx, services.mcp, services.trust)
    offers.decide(root, [{"key": "issues", "accept": False}])
    assert "issues" not in [o["key"] for o in offers.list(root)["offers"]]
    document = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    document["mcpServers"]["issues"]["url"] = "https://elsewhere.example/mcp"
    (root / ".mcp.json").write_text(json.dumps(document), encoding="utf-8")
    offer = next(o for o in offers.list(root)["offers"] if o["key"] == "issues")
    assert offer["changed"] is True and offer["previous_decision"] == "rejected"


def test_an_accepted_project_server_does_not_run_in_another_trusted_project(
    services, tmp_path
) -> None:
    root = _project(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    services.trust.add(root)
    services.trust.add(other)
    ProjectOffers(services.ctx, services.mcp, services.trust).decide(
        root, [{"key": "issues", "accept": True}]
    )
    row = services.mcp.show("issues", "project")
    assert services.mcp._trusted(row, root) is True
    assert services.mcp._trusted(row, other) is False
    assert services.mcp._trusted(row, None) is False


def test_project_offers_through_the_protocol(services, tmp_path) -> None:
    root = _project(tmp_path)
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        untrusted = engine.handle_line(
            json.dumps(
                {"id": "a", "method": "mcp.project_offers.list", "params": {"path": str(root)}}
            )
        )
        assert untrusted["ok"] and untrusted["result"]["offers"] == []
        refused = engine.handle_line(
            json.dumps(
                {
                    "id": "b",
                    "method": "mcp.project_offers.decide",
                    "params": {"path": str(root), "decisions": [{"key": "lint", "accept": True}]},
                }
            )
        )
        assert refused["ok"] is False
        assert refused["error"]["details"]["hint"] == "project_not_trusted"
        services.trust.add(root)
        decided = engine.handle_line(
            json.dumps(
                {
                    "id": "c",
                    "method": "mcp.project_offers.decide",
                    "params": {
                        "path": str(root),
                        "decisions": [{"key": "notes", "accept": True, "name": "notes-here"}],
                    },
                }
            )
        )
        assert decided["ok"], decided
        server = decided["result"]["created"][0]["server"]
        assert server["name"] == "notes-here" and server["scope"] == "project"
    finally:
        engine.close()


def test_project_scan_skips_a_repeated_name_in_a_later_file(tmp_path) -> None:
    root = _project(tmp_path)
    candidates, reports = scan_project(root)
    lint = next(c for c in candidates if c.key == "lint")
    assert lint.path == ".mcp.json" and lint.command[0] == "npx"
    assert [r.path for r in reports if r.found] == [".mcp.json", ".vscode/mcp.json"]


def test_rinari_import_home_reads_nothing_of_the_owner(services, fake_pc, tmp_path) -> None:
    # Tests and sandboxes: only the stand-in folder is read, never the owner's
    # home nor the folders the system environment points to (APPDATA…).
    home, env = fake_pc
    stand_in = tmp_path / "stand-in"
    (stand_in / ".cursor").mkdir(parents=True)
    env = {**env, "RINARI_IMPORT_HOME": str(stand_in)}
    methods = McpConnectorMethods(services, home=lambda: home, env=lambda: env, platform="win32")
    assert methods.import_scan({})["candidates"] == []

    (stand_in / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"own": {"command": "own-mcp"}}}), encoding="utf-8"
    )
    scanned = methods.import_scan({})
    assert [c["key"] for c in scanned["candidates"]] == ["cursor:own"]
    assert all(str(home) not in report["path"] for report in scanned["sources"])
