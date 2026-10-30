"""Large files and large outputs (plan 0.4.0 #76).

A large file is read by pages of the original, never copied; an output that
cannot be repeated (a command, a page, an MCP) is saved as the text inside
it and paged with the same tools as any file.
"""

from __future__ import annotations

from tests.unit import test_tool_runtime as base
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project


def test_a_large_markdown_comes_back_as_its_first_page(project) -> None:
    tmp_path, root, _ = project
    text = "".join(f"## Sección {i}\nTexto de la sección {i}.\n" for i in range(2500))
    (root / "guia.md").write_text(text, encoding="utf-8")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    page = runtime.execute("fs.read", {"path": "guia.md"}, ctx, tool_call_id="r1")

    assert page.ok, page.error
    data = page.data
    assert data["start_line"] == 1 and data["total_lines"] == 5000
    assert data["text"].startswith("## Sección 0\n")
    assert data["next_line"] == data["end_line"] + 1
    assert f"Continue with fs.read_lines from line {data['next_line']}" in data["note"]
    assert "delivery_partial" not in data
    assert not (tmp_path / "artifacts" / "s1" / "runtime").exists()

    rest = runtime.execute(
        "fs.read_lines",
        {"path": "guia.md", "start": data["next_line"], "end": data["next_line"] + 1},
        ctx,
        tool_call_id="r2",
    )
    assert rest.ok, rest.error
    first = text.splitlines()[data["next_line"] - 1]
    assert rest.data["text"].startswith(f"{data['next_line']}| {first}")


def test_a_small_file_is_still_read_whole(project) -> None:
    tmp_path, root, _ = project
    (root / "corto.md").write_bytes(b"# Hola\nmundo\n")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute("fs.read", {"path": "corto.md"}, ctx)
    assert result.ok and result.data["text"] == "# Hola\nmundo\n"
    assert "next_line" not in result.data


def test_a_saved_output_is_paged_and_searched_like_a_file(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    saved = runtime._spill("call-1-complete", "uno\ndos\nERROR: tres\ncuatro\n", ctx)

    lines = runtime.execute("fs.read_lines", {"path": saved.uri, "start": 2, "end": 3}, ctx)
    assert lines.ok, lines.error
    assert lines.data["text"] == "2| dos\n3| ERROR: tres"

    found = runtime.execute("fs.search_text", {"path": saved.uri, "pattern": "ERROR"}, ctx)
    assert found.ok, found.error
    assert "ERROR: tres" in str(found.data)


def test_another_sessions_output_cannot_be_read(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    for uri in ("artifact://otra/runtime/x.txt", "artifact://s1/../otra/runtime/x.txt"):
        result = runtime.execute("fs.read_lines", {"path": uri}, ctx)
        assert not result.ok
        assert result.error.code == "SANDBOX_VIOLATION"


def test_a_batch_shares_the_budget_between_its_files(project) -> None:
    tmp_path, root, _ = project
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (root / name).write_text("línea larga de texto\n" * 3000, encoding="utf-8")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute(
        "fs.read", {"paths": ["a.txt", "b.txt", "c.txt", "d.txt"]}, ctx, tool_call_id="b1"
    )
    assert result.ok, result.error
    rows = result.data["files"]
    assert all(row["data"]["next_line"] for row in rows)
    assert len(result.to_model_text("fs.read").encode("utf-8")) <= ctx.observation_budget_bytes
    assert "delivery_partial" not in result.data


def _with_reads(ctx):
    import dataclasses
    from types import SimpleNamespace

    from rinari.tools.read_cache import for_agent

    return dataclasses.replace(
        ctx, reads=for_agent(SimpleNamespace(history=[], compact_revision=None))
    )


def test_writing_over_a_file_changed_since_it_was_read_asks_to_read_again(project) -> None:
    tmp_path, root, _ = project
    target = root / "notas.md"
    target.write_bytes(b"version 1\n")
    ctx = _with_reads(_ctx(tmp_path, root))
    runtime, _ = _runtime(ctx, tmp_path)
    assert runtime.execute("fs.read", {"path": "notas.md"}, ctx).ok

    target.write_bytes(b"version 2, from the user\n")
    refused = runtime.execute("fs.write", {"path": "notas.md", "content": "mine\n"}, ctx)
    assert not refused.ok and refused.error.code == "CONFLICT"
    assert target.read_bytes() == b"version 2, from the user\n"

    assert runtime.execute("fs.read", {"path": "notas.md"}, ctx).ok
    assert runtime.execute("fs.write", {"path": "notas.md", "content": "mine\n"}, ctx).ok
    # Its own write is what it last saw: writing again needs no new read.
    assert runtime.execute("fs.write", {"path": "notas.md", "content": "again\n"}, ctx).ok


def test_reading_lines_of_a_file_that_moved_says_so(project) -> None:
    tmp_path, root, _ = project
    target = root / "log.txt"
    target.write_bytes(b"a\nb\nc\n")
    ctx = _with_reads(_ctx(tmp_path, root))
    runtime, _ = _runtime(ctx, tmp_path)
    assert runtime.execute("fs.read", {"path": "log.txt"}, ctx).ok
    target.write_bytes(b"nuevo\na\nb\nc\n")
    lines = runtime.execute("fs.read_lines", {"path": "log.txt", "start": 1, "end": 2}, ctx)
    assert lines.ok and "changed since your last read" in lines.data["note"]
    again = runtime.execute("fs.read_lines", {"path": "log.txt", "start": 1, "end": 2}, ctx)
    assert "note" not in again.data
