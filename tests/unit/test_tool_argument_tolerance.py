"""Tool calls that wasted a round trip in real sessions.

An audit of sessions across providers (local llama.cpp models included)
found the same variants again and again: `timeout_ms` for `timeout_s`, a
one-file patch with top-level `edits`, a line range on fs.read, regexes sent
to a literal search, writes into folders that did not exist yet, skills with
their steps as peer headings, project memory asked for in a plain chat, and
globs over folders that hold only folders. Each test pins the behavior that
replaces the wasted call, and the safety line around it: only exact
equivalences are rewritten, and policy judges the rewritten call.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

import pytest

from rinari.skills.manifest import PROCEDURE_EXAMPLE, load_skill_manifest, validate_skill
from rinari.tools import normalize
from rinari.tools.definition import ToolErrorCode
from tests.unit import test_tool_runtime as base
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project


def _marker_argv(path) -> list[str]:
    return [sys.executable, "-c", f"open({str(path)!r}, 'w').write('x')"]


# -- 1. argument normalization -------------------------------------------------


def test_shell_exec_runs_with_timeout_ms_converted_to_seconds(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="full-access")
    runtime, events = _runtime(ctx, tmp_path, answer="y")
    marker = root / "ran.txt"

    result = runtime.execute(
        "shell.exec", {"argv": _marker_argv(marker), "timeout_ms": 300000}, ctx, tool_call_id="c1"
    )

    assert result.ok, result.error
    assert marker.exists()
    assert result.notes == ("used timeout_s=300 (received timeout_ms=300000)",)
    # Command output reaches the model as plain text; the note travels with it.
    assert "notes: " + result.notes[0] in result.to_model_text("shell.exec")
    normalized = [payload for kind, payload in events if kind == "ToolArgumentsNormalized"]
    assert normalized == [{"tool": "shell.exec", "notes": list(result.notes), "tool_call_id": "c1"}]


def test_normalization_notes_survive_the_round_projection(project) -> None:
    from rinari.models.types import ToolCall

    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    call = ToolCall(id="c1", name="fs.read_lines", arguments={"path": "src/a.py", "end_line": 1})
    result = runtime.execute(call.name, call.arguments, ctx, tool_call_id=call.id)

    [projected] = runtime.project_round([(call, result)], ctx)

    assert projected.notes == ("used end=1 (received end_line)",)


def test_a_bare_timeout_is_already_in_seconds() -> None:
    arguments, notes = normalize.shell_exec({"command": "make", "timeout": 120})

    assert arguments == {"command": "make", "timeout_s": 120}
    assert notes == ["used timeout_s=120 (received timeout=120)"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "make", "timeout_ms": 1000, "timeout_s": 5},
        {"command": "make", "timeout_ms": 1000, "timeout": 5},
        {"command": "make", "timeout_ms": "1000"},
        {"command": "make", "timeout_ms": True},
    ],
)
def test_an_ambiguous_timeout_is_left_for_validation(arguments) -> None:
    assert normalize.shell_exec(arguments) == (arguments, [])


def test_process_wait_takes_timeout_ms_as_seconds() -> None:
    arguments, notes = normalize.process_wait({"handle": "p1", "timeout_ms": 1500})

    assert arguments == {"handle": "p1", "timeout_s": 1.5}
    assert notes == ["used timeout_s=1.5 (received timeout_ms=1500)"]


def test_command_beside_argv_runs_only_argv(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="full-access")
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    from_argv = root / "argv.txt"
    from_command = root / "command.txt"
    command = f"\"{sys.executable}\" -c \"open(r'{from_command}', 'w').write('x')\""

    result = runtime.execute(
        "shell.exec", {"command": command, "argv": _marker_argv(from_argv)}, ctx
    )

    assert result.ok, result.error
    assert from_argv.exists() and not from_command.exists()
    assert "ignored command" in result.notes[0]
    assert normalize.process_start({"command": "x", "argv": ["y"]})[0] == {"argv": ["y"]}


def test_command_beside_an_invalid_argv_is_not_rewritten() -> None:
    arguments = {"command": "ls", "argv": []}

    assert normalize.shell_exec(arguments) == (arguments, [])


def test_fs_patch_with_top_level_edits_is_the_single_file_batch(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    target = root / "src" / "a.py"

    result = runtime.execute(
        "fs.patch",
        {"path": "src/a.py", "edits": [{"old_string": "world", "new_string": "there"}]},
        ctx,
    )

    assert result.ok, result.error
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"
    assert result.notes == ("used files=[{path, edits}] (received top-level path and edits)",)


def test_fs_patch_policy_judges_the_normalized_path(project) -> None:
    tmp_path, root, outside = project
    ctx = _ctx(tmp_path, root)
    runtime, events = _runtime(ctx, tmp_path, answer="n")

    result = runtime.execute(
        "fs.patch",
        {"path": str(outside), "edits": [{"old_string": "outside", "new_string": "x"}]},
        ctx,
    )

    assert result.ok is False
    assert result.error.code in {ToolErrorCode.APPROVAL_DENIED, ToolErrorCode.POLICY_DENIED}
    assert outside.read_text(encoding="utf-8") == "outside"
    assert any(kind == "PolicyDecision" for kind, _ in events)


def test_fs_patch_shapes_that_mean_something_else_are_not_rewritten() -> None:
    edits = [{"old_string": "a", "new_string": "b"}]
    for arguments in (
        {"path": "a", "edits": edits, "replace_all": True},
        {"path": "a", "edits": edits, "old_string": "a", "new_string": "b"},
        {"path": "a", "edits": "a->b"},
        {"files": [{"path": "a", "edits": edits}]},
    ):
        assert normalize.fs_patch(arguments) == (arguments, [])
    single, _ = normalize.fs_patch({"path": "a", "edits": edits, "expected_hash": "h"})
    assert single == {"files": [{"path": "a", "edits": edits, "expected_hash": "h"}]}


def test_fs_read_with_a_line_range_points_to_fs_read_lines(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.read", {"path": "src/a.py", "start_line": 2, "end_line": 9}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert "fs.read_lines" in result.error.message
    assert '{"path": "src/a.py", "start": 2, "end": 9}' in result.error.message


def test_fs_read_lines_accepts_start_line_and_end_line(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute(
        "fs.read_lines", {"path": "src/a.py", "start_line": 1, "end_line": 1}, ctx
    )

    assert result.ok, result.error
    assert result.data["text"] == "1| hello"
    assert result.notes == ("used start=1 (received start_line)", "used end=1 (received end_line)")


def test_normalization_errors_reach_the_model_before_anything_runs(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, events = _runtime(ctx, tmp_path)

    runtime.execute("fs.read", {"path": "src/a.py", "offset": 10}, ctx)

    assert not any(kind == "PolicyChecked" for kind, _ in events)


# -- 2. fs.search_text ----------------------------------------------------------


def test_literal_search_for_a_regex_says_how_to_search_it(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.search_text", {"pattern": "hello|world"}, ctx)

    assert result.ok and result.data["matches"] == []
    assert "set regex=true or use search.regex" in result.data["note"]


def test_search_text_regex_is_case_insensitive_with_absolute_paths(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.search_text", {"pattern": "HELLO|World", "regex": True}, ctx)

    assert result.ok, result.error
    [match] = result.data["matches"]
    assert match["file"] == str(root / "src" / "a.py")
    assert [line["line"] for line in match["lines"]] == [1, 2]
    assert "note" not in result.data


def test_a_plain_literal_miss_has_no_regex_hint(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.search_text", {"pattern": "absent words"}, ctx)

    assert result.ok and "note" not in result.data


def test_search_regex_stays_case_sensitive(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    assert runtime.execute("search.regex", {"pattern": "HELLO"}, ctx).data["matches"] == []
    assert runtime.execute("search.regex", {"pattern": "hel+o"}, ctx).data["matches"]


# -- 3. fs.write into a missing folder -------------------------------------------


def test_write_creates_missing_folders_inside_the_workspace(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.write", {"path": "pkg/sub/mod.py", "content": "x = 1\n"}, ctx)

    assert result.ok, result.error
    assert (root / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.data["created_dirs"] == [str(root / "pkg"), str(root / "pkg" / "sub")]


def test_write_outside_the_workspace_does_not_invent_folders(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    target = tmp_path / "elsewhere" / "deep" / "note.txt"

    result = runtime.execute("fs.write", {"path": str(target), "content": "x"}, ctx)

    assert result.error.code is ToolErrorCode.NOT_FOUND
    assert "create_parents=true" in result.error.message
    assert not (tmp_path / "elsewhere").exists()

    created = runtime.execute(
        "fs.write", {"path": str(target), "content": "x", "create_parents": True}, ctx
    )
    assert created.ok, created.error
    assert target.read_text(encoding="utf-8") == "x"


def _full_access(ctx):
    from rinari.policy.sandbox import FilesystemSandbox

    return dataclasses.replace(ctx, sandbox=FilesystemSandbox(read_root=None, unrestricted=True))


def test_full_access_creates_missing_folders_in_its_own_workspace(project) -> None:
    # Full access grants no explicit roots; before, it was the only profile
    # that could not create `src/` in its own project and models looped on it.
    tmp_path, root, _ = project
    ctx = _full_access(_ctx(tmp_path, root, profile="full-access"))
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.write", {"path": "web/app/main.ts", "content": "x\n"}, ctx)

    assert result.ok, result.error
    assert (root / "web" / "app" / "main.ts").read_text(encoding="utf-8") == "x\n"
    created = [Path(p).resolve() for p in result.data["created_dirs"]]
    assert created == [(root / "web").resolve(), (root / "web" / "app").resolve()]


def test_full_access_still_asks_for_create_parents_outside_its_folders(project) -> None:
    tmp_path, root, _ = project
    ctx = _full_access(_ctx(tmp_path, root, profile="full-access"))
    runtime, _ = _runtime(ctx, tmp_path)
    target = tmp_path / "elsewhere" / "deep" / "note.txt"

    result = runtime.execute("fs.write", {"path": str(target), "content": "x"}, ctx)

    assert result.error.code is ToolErrorCode.NOT_FOUND
    assert not (tmp_path / "elsewhere").exists()


def test_write_to_a_folder_path_is_an_invalid_argument(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("fs.write", {"path": "src", "content": "x"}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT


# -- 4. skills with peer step headings -----------------------------------------------


def _skill(tmp_path, body: str):
    folder = tmp_path / "demo"
    folder.mkdir(exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: Demo skill.\nrisk: low\n---\n{body}", encoding="utf-8"
    )
    return load_skill_manifest(folder, "user")


@pytest.mark.parametrize(
    ("body", "procedure"),
    [
        ("# Procedure\n## Prepare\n- a\n### Detail\n- b\n", "## Prepare\n- a\n### Detail\n- b"),
        ("## Procedure\n## Step 1\n- a\n## Step 2\n- b\n", "## Step 1\n- a\n## Step 2\n- b"),
        ("# Procedure\n# Paso 1\n- a\n# Verification\n- v\n", "# Paso 1\n- a"),
    ],
)
def test_procedure_keeps_its_subheadings(tmp_path, body, procedure) -> None:
    manifest = _skill(tmp_path, body)

    assert manifest.procedure == procedure
    assert validate_skill(manifest, set()) == []


def test_a_procedure_with_content_still_ends_at_a_peer_heading(tmp_path) -> None:
    manifest = _skill(tmp_path, "# Procedure\n- a\n# Appendix\nother\n")

    assert manifest.procedure == "- a"


def test_an_unusable_procedure_error_shows_a_valid_example(tmp_path) -> None:
    manifest = _skill(tmp_path, "# Procedure\n\n# Verification\n- v\n")

    [issue] = validate_skill(manifest, set())
    assert issue["code"] == "EMPTY_PROCEDURE"
    assert issue["message"].endswith(PROCEDURE_EXAMPLE)
    assert "# Procedure\\n## Prepare" in PROCEDURE_EXAMPLE


# -- 5. project memory without a project ------------------------------------------------


class _Memory:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_user(self, query, *, kind=None, limit=10):
        self.calls.append("user")
        return [{"id": "u1", "text": "prefers tabs"}]

    def search_project(self, root, query, *, kind=None, limit=10):
        self.calls.append("project")
        return []


def test_recall_of_project_memory_in_a_chat_searches_user_memory(project) -> None:
    from rinari.tools.native.memory import memory_recall

    tmp_path, root, _ = project
    memory = _Memory()
    ctx = dataclasses.replace(_ctx(tmp_path, root, kind="CHAT"), memory=memory)

    result = memory_recall({"scope": "project", "query": "tabs"}, ctx)

    assert result.ok and memory.calls == ["user"]
    assert result.data["scope"] == "user" and result.data["count"] == 1
    assert "searched user memory" in result.notes[0]


def test_remember_project_memory_in_a_chat_refuses_with_the_way_out(project) -> None:
    from rinari.tools.native.memory import memory_remember

    tmp_path, root, _ = project
    service = type("Service", (), {"extraction_allowed": lambda self, sid: True})()
    ctx = dataclasses.replace(_ctx(tmp_path, root, kind="CHAT"), memory=service)

    result = memory_remember({"scope": "project", "topic": "style", "text": "tabs"}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert "scope=user" in result.error.message
    assert "Do not retry with scope=project" in result.error.message


# -- 6. fs.glob over folders -----------------------------------------------------------


def test_glob_over_folders_only_says_include_dirs(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    for name in ("one", "two"):
        (root / "only" / name).mkdir(parents=True)

    files_only = runtime.execute("fs.glob", {"pattern": "*", "path": "only"}, ctx)
    listed = runtime.execute("fs.glob", {"pattern": "*", "path": "only", "include_dirs": True}, ctx)

    assert files_only.data["matches"] == []
    assert files_only.data["note"] == (
        "no files match; 2 folders match the pattern. Set include_dirs=true to list folders."
    )
    assert listed.data["matches"] == [str(root / "only" / n) + os.sep for n in ("one", "two")]
    assert "note" not in listed.data


def test_glob_include_dirs_lists_files_and_folders(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    (root / "src" / "pkg").mkdir()

    result = runtime.execute("fs.glob", {"pattern": "src/*", "include_dirs": True}, ctx)

    assert sorted(result.data["matches"]) == sorted(
        [str(root / "src" / "a.py"), str(root / "src" / "pkg") + os.sep]
    )


# -- 7. compact fs.read_lines -----------------------------------------------------------


def test_read_lines_is_compact_and_continues_from_next_line(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    (root / "long.txt").write_text("".join(f"row {n}\n" for n in range(1, 11)), encoding="utf-8")

    first = runtime.execute("fs.read_lines", {"path": "long.txt", "start": 3, "end": 4}, ctx)
    whole = runtime.execute("fs.read_lines", {"path": "long.txt", "start": 9}, ctx)

    assert first.data["text"] == "3| row 3\n4| row 4"
    assert (first.data["end_line"], first.data["next_line"]) == (4, 5)
    assert first.data["total_lines"] is None
    assert whole.data["text"] == "9| row 9\n10| row 10"
    assert (whole.data["next_line"], whole.data["total_lines"]) == (None, 10)
    assert '"lines"' not in first.to_model_text("fs.read_lines")


def test_notes_reach_the_model_in_compact_command_output() -> None:
    from rinari.tools.definition import ToolResult

    result = ToolResult(
        ok=True,
        data={"exit_code": 0, "stdout": "ok", "stderr": ""},
        notes=("used timeout_s=300 (received timeout_ms=300000)",),
    )
    text = result.to_model_text("shell.exec")
    assert "notes: used timeout_s=300 (received timeout_ms=300000)" in text


def test_the_history_redaction_marker_is_never_written_into_a_file(project) -> None:
    """A model rebuilding a .env from redacted history would write the marker."""
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    env = root / ".env.example"
    env.write_text("API_URL=http://localhost\n", encoding="utf-8")

    for name, args in (
        ("fs.write", {"path": str(env), "content": "API_KEY=[REDACTED]\n"}),
        (
            "fs.patch",
            {
                "path": str(env),
                "old_string": "API_URL",
                "new_string": "API_KEY=[REDACTED]\nAPI_URL",
            },
        ),
        (
            "fs.patch",
            {
                "files": [
                    {
                        "path": str(env),
                        "edits": [{"old_string": "API_URL", "new_string": "X=[REDACTED]\nAPI_URL"}],
                    }
                ]
            },
        ),
    ):
        result = runtime.execute(name, args, ctx, tool_call_id=name)
        assert not result.ok and "[REDACTED]" in result.error.message, name
    assert env.read_text(encoding="utf-8") == "API_URL=http://localhost\n"
    # A file that already documents the marker can still be edited.
    doc = root / "SECURITY.md"
    doc.write_text("Secrets show as [REDACTED] in history.\n", encoding="utf-8")
    ok = runtime.execute(
        "fs.patch",
        {"path": str(doc), "old_string": "history", "new_string": "stored history"},
        ctx,
        tool_call_id="d",
    )
    assert ok.ok, ok.error
