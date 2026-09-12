"""LSP client/manager tests against a deterministic fake language server.

The fixture (tests/fixtures/fake_lsp_server.py) speaks real LSP framing on
stdio, so these tests cover handshake, framing, capability gating, documents,
diagnostics, crash/timeout behavior, and the tool boundary -- without any
network or installed language server.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

from rinari.lsp import (
    LspClient,
    LspError,
    LspManager,
    LspProtocolError,
    LspRequestTimeout,
    LspServerCrashed,
    LspServerSpec,
    encode_message,
    language_id_for_path,
    read_message,
)
from rinari.policy.engine import normalize_profile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext, ToolErrorCode
from rinari.tools.native.lsp import lsp_definition, lsp_rename, lsp_symbols

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_lsp_server.py"

_MANAGERS: list = []
_CLIENTS: list = []


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for manager in _MANAGERS:
        with contextlib.suppress(Exception):
            manager.shutdown_all()
    _MANAGERS.clear()
    for client in _CLIENTS:
        with contextlib.suppress(Exception):
            client.close()
    _CLIENTS.clear()


def _spec(mode: str = "full", request_timeout: float = 10.0) -> LspServerSpec:
    command = [sys.executable, str(FIXTURE)]
    if mode != "full":
        command.append(f"--{mode}")
    return LspServerSpec(
        name=f"fake-{mode}",
        languages=("python",),
        command=tuple(command),
        request_timeout=request_timeout,
    )


def _client(spec: LspServerSpec, root: Path) -> LspClient:
    client = LspClient(spec, root)
    client.start()
    _CLIENTS.append(client)
    return client


def _manager(root: Path, mode: str = "full", request_timeout: float = 10.0) -> LspManager:
    manager = LspManager(root, specs=[_spec(mode, request_timeout)])
    _MANAGERS.append(manager)
    return manager


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text(
        "from x import y\n\ndef alpha(x):\n    return x\n", encoding="utf-8"
    )
    return root


# -- framing ------------------------------------------------------------------


def test_framing_roundtrip(tmp_path) -> None:
    import io

    payload = {"jsonrpc": "2.0", "id": 1, "result": {"a": "héllo"}}
    stream = io.BytesIO(encode_message(payload))
    assert read_message(stream) == payload
    assert read_message(stream) is None  # EOF


def test_framing_invalid_header() -> None:
    import io

    with pytest.raises(LspProtocolError):
        read_message(io.BytesIO(b"garbage\r\n\r\n"))
    with pytest.raises(LspProtocolError):
        read_message(io.BytesIO(b"Content-Length: zz\r\n\r\n{}"))


# -- handshake ---------------------------------------------------------------


def test_client_handshake_stores_capabilities(proj) -> None:
    client = _client(_spec("full"), proj)
    assert client.alive
    assert client.server_info.get("name") == "fake"
    caps = client.capabilities.get("textDocument", {})
    assert caps.get("definitionProvider") is True
    assert caps.get("renameProvider") is True


def test_language_id_mapping(proj) -> None:
    assert language_id_for_path(proj / "a.py") == "python"
    assert language_id_for_path(proj / "a.ts") == "typescript"
    assert language_id_for_path(proj / "a.txt") is None


# -- manager operations ---------------------------------------------------------


def test_manager_symbols_flattened(proj) -> None:
    manager = _manager(proj)
    symbols = manager.symbols(proj / "src" / "mod.py")
    names = [s["qualified_name"] for s in symbols]
    assert "alpha" in names
    assert "alpha.beta" in names
    alpha = next(s for s in symbols if s["name"] == "alpha")
    assert alpha["line"] == 1  # fixture selectionRange start line 0 -> 1-based
    assert alpha["kind"] == 12


def test_manager_definition_maps_positions(proj) -> None:
    manager = _manager(proj)
    targets = manager.definition(proj / "src" / "mod.py", line=3, column=5)
    assert targets == [
        {"file": str(proj / "src" / "mod.py"), "relative": "src/mod.py", "line": 10, "column": 5}
    ]


def test_manager_references_multiple_files(proj) -> None:
    manager = _manager(proj)
    refs = manager.references(proj / "src" / "mod.py", line=2, column=1)
    assert [r["relative"] for r in refs] == ["src/mod.py", "src/mod.py.more"]
    assert refs[0]["line"] == 2 and refs[0]["column"] == 3
    assert refs[1]["line"] == 3 and refs[1]["column"] == 2


def test_manager_hover_and_signature(proj) -> None:
    manager = _manager(proj)
    hover = manager.hover(proj / "src" / "mod.py", line=3, column=5)
    assert hover["contents"] == "alpha: int"
    sig = manager.signature_help(proj / "src" / "mod.py", line=3, column=5)
    assert sig["active_signature"] == 0
    assert sig["signatures"][0]["label"] == "alpha(x: int, y: str = 'a')"
    assert sig["signatures"][0]["parameters"] == ["x: int", "y: str = 'a'"]


def test_manager_rename_plans_edit_without_applying(proj) -> None:
    manager = _manager(proj)
    plan = manager.rename(proj / "src" / "mod.py", line=3, column=5, new_name="omega")
    assert "src/mod.py" in plan["changes"]
    edits = plan["changes"]["src/mod.py"]
    assert edits[0]["new_text"] == "omega"
    assert (edits[0]["start"], edits[0]["end"]) == ((0, 6), (0, 11))
    original = (proj / "src" / "mod.py").read_text(encoding="utf-8")
    assert "alpha" in original  # file untouched: rename is plan-only

    with pytest.raises(LspError, match="invalid name"):
        manager.rename(proj / "src" / "mod.py", line=3, column=5, new_name="bad")


def test_manager_publish_diagnostics_on_open(proj) -> None:
    (proj / "boom.py").write_text("boom here\n", encoding="utf-8")
    manager = _manager(proj)
    # publishDiagnostics is a server push: poll briefly for it to land (the
    # tool reports the last published state, which settles in <<1s here).
    diags: list = []
    for _ in range(100):
        diags = manager.diagnostics(proj / "boom.py")
        if diags:
            break
        time.sleep(0.02)
    assert [d["message"] for d in diags] == ["boom detected"]
    assert diags[0]["severity"] == 1 and diags[0]["code"] == "E001"
    assert diags[0]["line"] == 1


def test_manager_position_validation(proj) -> None:
    manager = _manager(proj)
    with pytest.raises(LspError):
        manager.definition(proj / "src" / "mod.py", line=0, column=1)


# -- capability gating and availability -----------------------------------------


def test_capability_gating_blocks_unsupported_ops(proj) -> None:
    manager = _manager(proj, mode="minimal")
    assert not manager.supports("python", "definition")
    with pytest.raises(LspError, match="does not support definition"):
        manager.definition(proj / "src" / "mod.py", line=2, column=1)


def test_no_server_for_language_errors(proj) -> None:
    manager = LspManager(proj)  # no specs at all -> no server anywhere
    with pytest.raises(LspError, match="no LSP server available"):
        manager.definition(proj / "src" / "mod.py", line=2, column=1)


def test_unknown_language_errors(proj) -> None:
    manager = _manager(proj)
    (proj / "a.xyz").write_text("x\n", encoding="utf-8")
    with pytest.raises(LspError, match="no LSP language id"):
        manager.symbols(proj / "a.xyz")


# -- lifecycle: crash, timeout, shutdown ----------------------------------------


def test_request_timeout(proj) -> None:
    client = _client(_spec("full", request_timeout=0.5), proj)
    with pytest.raises(LspRequestTimeout):
        client.request("textDocument/hang", {}, timeout=0.5)


def test_crash_detection(proj) -> None:
    client = _client(_spec("crash"), proj)
    assert client.alive
    client.open_document(proj / "src" / "mod.py", "x", "python")  # server exits here
    time.sleep(0.3)
    assert not client.alive
    with pytest.raises(LspServerCrashed):
        client.request(
            "textDocument/definition",
            {"textDocument": {"uri": "file:///x"}, "position": {"line": 0, "character": 0}},
        )


def test_manager_remembers_spawn_failure(tmp_path) -> None:
    broken = LspServerSpec(
        name="broken",
        languages=("python",),
        command=(str(Path("nope-not-a-real-lsp") / "lsp"),),
        startup_timeout=2.0,
    )
    manager = LspManager(tmp_path / "proj", specs=[broken])
    # First attempt fails, is remembered: repeated calls stay fast and None.
    assert manager.server_for("python") is None
    started = time.monotonic()
    assert manager.server_for("python") is None
    assert time.monotonic() - started < 2.0
    with contextlib.suppress(Exception):
        manager.shutdown_all()
    _MANAGERS.append(manager)


def test_shutdown_all_closes_clients(proj) -> None:
    manager = _manager(proj)
    client = manager.server_for("python")
    assert client is not None and client.alive
    process = client._proc
    manager.shutdown_all()
    assert client.closed
    process.wait(timeout=10)
    assert manager.server_for("python") is None  # already torn down


# -- tool boundary ---------------------------------------------------------------


def _ctx(proj: Path, manager=None) -> ToolContext:
    return ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=proj,
        project_root=proj,
        user_home=proj.parent,
        profile=normalize_profile("workspace"),
        sandbox=FilesystemSandbox(read_root=proj, write_roots=(proj,)),
        limits=ProcessLimits(timeout_s=10, max_output_bytes=1024 * 1024),
        artifact_root=proj / "art",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
        lsp=manager,
    )


def test_tool_without_manager_advertises_fallback(proj) -> None:
    result = lsp_definition({"path": "src/mod.py", "line": 2, "column": 1}, _ctx(proj))
    assert not result.ok
    assert result.error.code is ToolErrorCode.DEPENDENCY_ERROR
    assert "search.*" in result.error.message


def test_tool_with_manager_returns_targets(proj) -> None:
    manager = _manager(proj)
    result = lsp_definition({"path": "src/mod.py", "line": 2, "column": 1}, _ctx(proj, manager))
    assert result.ok
    assert result.data[0]["relative"] == "src/mod.py"
    assert result.data[0]["line"] == 10

    symbols = lsp_symbols({"path": "src/mod.py"}, _ctx(proj, manager))
    assert symbols.ok
    assert {s["name"] for s in symbols.data} >= {"alpha", "beta"}

    rename = lsp_rename(
        {"path": "src/mod.py", "line": 3, "column": 5, "new_name": "omega"}, _ctx(proj, manager)
    )
    assert rename.ok
    assert "src/mod.py" in rename.data["changes"]


def test_tool_rejects_bad_positions(proj) -> None:
    manager = _manager(proj)
    result = lsp_definition({"path": "src/mod.py", "line": 0}, _ctx(proj, manager))
    assert not result.ok
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT


def test_diagnostic_versions_reject_stale_notifications(proj, monkeypatch):
    client = LspClient(_spec(), proj)
    monkeypatch.setattr(client, "notify", lambda *args: None)
    path = proj / "src/mod.py"
    client.open_document(path, "one", "python")

    def publish(version):
        client._on_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": path.as_uri(), "version": version, "diagnostics": []},
            }
        )

    publish(1)
    assert client.diagnostics_state(path)["version_verified"]
    client.change_document(path, "two")
    assert client.diagnostics_state(path)["status"] == "waiting"
    publish(1)
    assert client.diagnostics_state(path)["status"] == "waiting"
    publish(2)
    assert client.diagnostics_state(path)["version_verified"]
