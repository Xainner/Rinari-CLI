"""Search tools (phase 3: exact files, regex, symbols, references, hybrid)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PolicyEngine, normalize_profile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext, ToolErrorCode
from rinari.tools.native.search import (
    search_files,
    search_hybrid,
    search_references,
    search_regex,
    search_symbols,
)
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text(
        "class Service:\n"
        "    def handle(self):\n"
        "        return worker(1)\n"
        "\n"
        "def worker(n):\n"
        "    return n + 1\n"
        "def helper():\n"
        "    return worker(2)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_app.py").write_text(
        "from src.app import Service, worker\n\ndef test_worker():\n    assert worker(1) == 2\n",
        encoding="utf-8",
    )
    (root / "app.js").write_text(
        "export function render() {\n  return 'x';\n}\n"
        "export class Widget {}\nconst fmt = (x) => x;\n",
        encoding="utf-8",
    )
    return root


def _ctx(proj: Path):
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
    )


def test_search_files_exact_glob(proj) -> None:
    result = search_files({"pattern": "tests/**/*.py"}, _ctx(proj))
    assert result.ok
    assert [Path(m).name for m in result.data["matches"]] == ["test_app.py"]
    none = search_files({"pattern": "node_modules/**"}, _ctx(proj))
    assert none.ok and none.data["matches"] == []


def test_search_regex_real_regex(proj) -> None:
    result = search_regex({"pattern": r"^def\s+(\w+"}, _ctx(proj))
    assert not result.ok
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT

    ok = search_regex({"pattern": r"^def \w+"}, _ctx(proj))
    assert ok.ok
    files = {m["file"] for m in ok.data["matches"]}
    assert "src/app.py" in files
    assert all(line["text"].startswith("def ") for m in ok.data["matches"] for line in m["lines"])


def test_search_symbols_exact_and_structural(proj) -> None:
    by_name = search_symbols({"query": "worker"}, _ctx(proj))
    assert by_name.ok
    kinds = {(r["kind"], r["file"]) for r in by_name.data["results"]}
    assert ("function", "src/app.py") in kinds

    qualified = search_symbols({"query": "Service.handle"}, _ctx(proj))
    assert qualified.ok
    assert len(qualified.data["results"]) == 1
    hit = qualified.data["results"][0]
    assert hit["kind"] == "method"
    assert hit["context"] == "Service.handle"

    js = search_symbols({"query": "Widget"}, _ctx(proj))
    assert js.ok
    assert js.data["results"][0]["kind"] == "class"

    missing = search_symbols({"query": "does_not_exist"}, _ctx(proj))
    assert missing.ok and missing.data["results"] == []


def test_search_references_excludes_definitions_by_default(proj) -> None:
    result = search_references({"name": "worker"}, _ctx(proj))
    assert result.ok
    entries = {e["file"]: e for e in result.data["results"]}
    # The definition line in src/app.py is excluded by default:
    assert all(r["definition"] is False for e in entries.values() for r in e["references"])
    # Usage in tests/test_app.py is present:
    assert "tests/test_app.py" in entries

    with_defs = search_references({"name": "worker", "include_definitions": True}, _ctx(proj))
    def_files = {
        e["file"] for e in with_defs.data["results"] for r in e["references"] if r["definition"]
    }
    assert "src/app.py" in def_files


def test_search_hybrid_ranks_definitions_first(proj) -> None:
    result = search_hybrid({"query": "worker"}, _ctx(proj))
    assert result.ok
    results = result.data["results"]
    assert results, "expected hybrid hits"
    top = results[0]
    assert "definition" in top["reasons"]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_filename_bonus(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "service_worker.js").write_text(
        "export function ping() { return 1; }\n", encoding="utf-8"
    )
    ctx = ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=root.parent,
        profile=normalize_profile("workspace"),
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=10, max_output_bytes=1024 * 1024),
        artifact_root=root / "art",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
    )
    result = search_hybrid({"query": "service"}, ctx)
    assert result.ok
    assert any("filename" in r["reasons"] for r in result.data["results"])


def test_search_tools_run_through_policy_as_read(proj) -> None:
    registry = ToolRegistry()
    from rinari.tools.native import all_native_tools

    registry.register_all(all_native_tools())
    runtime = ToolRuntime(registry, PolicyEngine(), ApprovalEngine(prompt=lambda r: "n"))
    result = runtime.execute("search.regex", {"pattern": "handle"}, _ctx(proj))
    assert result.ok, result.error
    assert "src/app.py" in {m["file"] for m in result.data["matches"]}
