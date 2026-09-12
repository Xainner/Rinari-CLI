import sys
from pathlib import Path

from rinari.application.ssh_aliases import resolve_alias
from rinari.tools.catalog import builtin_catalog
from rinari.tools.native.fs import fs_glob
from rinari.tools.native.search import search_files
from tests.unit.test_tool_runtime import _ctx, _runtime


def test_all_105_builtins_have_data_contracts():
    registry = builtin_catalog()
    assert len(registry.names()) == 105
    assert all(registry.get(name).output_schema for name in registry.names())


def test_argv_unicode_and_mutation_receipt(tmp_path):
    root = tmp_path / "space folder"
    root.mkdir()
    ctx = _ctx(tmp_path, root, profile="full_access")
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    args = {
        "argv": [
            sys.executable,
            "-c",
            "from pathlib import Path; p=Path('count'); "
            "p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('hola')",
        ],
        "request_id": "create-once",
    }
    first = runtime.execute("shell.exec", args, ctx)
    assert first.ok and first.data["exit_code"] == 0
    assert runtime.execute("shell.exec", args, ctx).ok
    assert (root / "count").read_text() == "x"
    conflict = runtime.execute("shell.exec", {**args, "argv": ["different"]}, ctx)
    assert not conflict.ok and conflict.error.code.value == "CONFLICT"


def test_static_ssh_alias_does_not_execute_config(tmp_path):
    folder = tmp_path / ".ssh"
    folder.mkdir()
    config = folder / "config"
    config.write_text(
        "Host casa3090\n HostName 192.168.0.3\n User owner\n Port 22\n", encoding="utf-8"
    )
    result = resolve_alias("casa3090", tmp_path)
    assert result["host"] == "192.168.0.3"
    assert result["username"] == "owner"
    config.write_text("Match exec touch-danger\n HostName bad\n", encoding="utf-8")
    assert resolve_alias("casa3090", tmp_path) is None


def test_shared_glob_ignores_and_pagination(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "node_modules").mkdir()
    for name in ("a.py", "ñ.py"):
        (root / "tests" / name).write_text("x", encoding="utf-8")
    (root / "node_modules" / "ignored.py").write_text("x")
    ctx = _ctx(tmp_path, root)
    args = {"pattern": "tests/**/*.py", "limit": 1}
    first = fs_glob(args, ctx)
    assert first.ok and first.data == search_files(args, ctx).data
    assert first.data["next_offset"] == 1
    second = fs_glob({**args, "offset": 1}, ctx)
    assert Path(second.data["matches"][0]).name == "ñ.py"


def test_memory_optimistic_version_and_episode_dedup(app_ctx):
    import pytest

    from rinari.memory.service import MemoryService
    from rinari.shared.errors import InvalidUsageError

    memory = MemoryService(app_ctx)
    row = memory.remember_user("one", topic="test")
    version = memory.repo.user_get(row["id"])["updated_at"]
    memory.update_user(row["id"], text="two", expected_version=version)
    with pytest.raises(InvalidUsageError, match="version conflict"):
        memory.update_user(row["id"], text="three", expected_version=version)
    first = memory.record_episodic("session", "project", "completed", outcome="passed")
    second = memory.record_episodic("session", "project", "completed", outcome="passed")
    assert first["id"] == second["id"] and second["deduplicated"]


def test_search_lsp_classification_uses_real_path():
    registry = builtin_catalog()
    for name in [n for n in registry.names() if n.startswith(("search.", "lsp."))]:
        action = registry.get(name).classify_action({"path": "C:/project with spaces/file.py"})
        assert action.target == "C:/project with spaces/file.py"


def test_batch_patch_prevalidates_and_authorizes_every_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("one", "two"):
        (root / name).write_text("before")
    outside = tmp_path / "outside"
    outside.write_text("before")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    def edit(path, old="before"):
        return {"path": path, "edits": [{"old_string": old, "new_string": "after"}]}

    result = runtime.execute("fs.patch", {"files": [edit("one"), edit("two", "absent")]}, ctx)
    assert not result.ok and (root / "one").read_text() == "before"
    result = runtime.execute("fs.patch", {"files": [edit("one"), edit(str(outside))]}, ctx)
    assert not result.ok and (root / "one").read_text() == "before"
    result = runtime.execute("fs.patch", {"files": [edit("one"), edit("two")]}, ctx)
    assert result.ok and result.data["file_count"] == 2
    assert (root / "one").read_text() == (root / "two").read_text() == "after"


def test_batch_reads_run_concurrently_and_preserve_order(tmp_path):
    from threading import Barrier

    from rinari.tools.definition import ToolResult
    from rinari.tools.read_batch import read_batch

    barrier = Barrier(4, timeout=3)

    def read(arguments, ctx):
        barrier.wait()
        return ToolResult(ok=True, data=arguments["path"])

    result = read_batch(["a", "b", "c", "d"], _ctx(tmp_path, tmp_path), read)
    assert result.ok
    assert [row["data"] for row in result.data["files"]] == ["a", "b", "c", "d"]


def test_workspace_revision_changes_with_content_metadata(tmp_path):
    from rinari.verify.revision import revision

    file = tmp_path / "source.py"
    file.write_text("before")
    before = revision(tmp_path)
    file.write_text("changed content")
    assert revision(tmp_path) != before


def test_batch_patch_rejects_oversized_output_before_mutation(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "source"
    file.write_text("before")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute(
        "fs.patch",
        {
            "files": [
                {
                    "path": "source",
                    "edits": [{"old_string": "before", "new_string": "x" * (1024 * 1024 + 1)}],
                }
            ]
        },
        ctx,
    )
    assert not result.ok
    assert file.read_text() == "before"


def test_partial_failure_keeps_successful_reads_visible(tmp_path):
    import json

    from rinari.tools.definition import ToolResult
    from rinari.tools.read_batch import read_batch

    def read(arguments, ctx):
        if arguments["path"] == "gone":
            raise FileNotFoundError("gone")
        return ToolResult(ok=True, data="kept")

    result = read_batch(["exists", "gone"], _ctx(tmp_path, tmp_path), read)
    observation = json.loads(result.to_model_text())
    assert not observation["ok"]
    assert observation["data"]["files"][0]["data"] == "kept"
    assert observation["data"]["files"][1]["error"]["code"] == "NOT_FOUND"


def test_nested_outputs_spill_and_redact_before_artifact(tmp_path):
    import json
    from dataclasses import replace

    from rinari.tools.definition import ToolResult

    ctx = _ctx(tmp_path, tmp_path)
    runtime, _ = _runtime(ctx, tmp_path, secrets=("private-token",))
    definition = runtime.registry.get("fs.stat")
    runtime.registry.register(
        replace(
            definition,
            max_output_bytes=1024,
            output_schema={"type": "array"},
            handler=lambda args, context: ToolResult(
                ok=True, data=[{"value": "private-token" + "x" * 100} for _ in range(100)]
            ),
        )
    )
    result = runtime.execute("fs.stat", {"path": "."}, ctx, tool_call_id="nested")
    assert result.ok and result.truncated and result.artifacts
    path = ctx.artifact_root / ctx.session_id / "runtime" / "nested.txt"
    text = path.read_text(encoding="utf-8")
    assert "private-token" not in text
    assert len(json.loads(text)) == 100


def test_lsp_unicode_column_is_translated_to_utf16(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from rinari.tools.native.lsp import lsp_hover

    path = tmp_path / "source.py"
    path.write_text("a\U0001f680b", encoding="utf-8")
    positions = []
    host = SimpleNamespace(hover=lambda path, line, column: positions.append(column) or {})
    ctx = replace(_ctx(tmp_path, tmp_path), lsp=host)
    assert lsp_hover(
        {"path": str(path), "line": 1, "column": 3, "column_encoding": "unicode"}, ctx
    ).ok
    assert positions == [4]


def test_ssh_alias_port_and_quoted_identity_preserve_known_host_lookup(tmp_path):
    folder = tmp_path / ".ssh"
    folder.mkdir()
    identity = folder / "key with spaces"
    identity.write_text("fixture identity")
    (folder / "config").write_text(
        f'Host casa\n HostName=example.test\n Port = 2200\n IdentityFile "{identity}"\n',
        encoding="utf-8",
    )
    destination = resolve_alias("casa", tmp_path)
    assert destination["port"] == 2200
    assert destination["host"] == "example.test"
    assert destination["identity_path"] == str(identity)
    assert destination["host_alias"] is None  # OpenSSH applies its host:port lookup.
