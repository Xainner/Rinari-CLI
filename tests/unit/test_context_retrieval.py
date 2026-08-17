from pathlib import Path

import pytest

from rinari.artifacts.store import ArtifactStore
from rinari.context.retrieval import ContextRetrievalService
from rinari.memory import MemoryService
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.prompts.segments import SegmentKind


@pytest.fixture
def retrieval(app_ctx):
    services = {
        "memory": MemoryService(app_ctx),
        "artifacts": ArtifactStore(app_ctx),
    }
    return ContextRetrievalService(
        app_ctx, artifacts=services["artifacts"], memory=services["memory"]
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


def _index_project(app_ctx, root: Path) -> None:
    rep = app_ctx.index_repo
    with app_ctx.db.transaction():
        rep.replace_index_files(
            app_ctx.db,
            str(root),
            [
                {
                    "rel_path": "src/app.py",
                    "hash": "h1",
                    "language": "python",
                    "size_bytes": 30,
                    "symbols_json": "[]",
                    "imports_json": "[]",
                    "indexed_at": "2026-08-17T00:00:00.000Z",
                },
                {
                    "rel_path": "README.md",
                    "hash": "h2",
                    "language": "markdown",
                    "size_bytes": 6,
                    "symbols_json": "[]",
                    "imports_json": "[]",
                    "indexed_at": "2026-08-17T00:00:00.000Z",
                },
            ],
        )
        rep.replace_symbols(
            app_ctx.db,
            str(root),
            [
                {
                    "name": "add",
                    "kind": "function",
                    "qualified_name": "app.add",
                    "rel_path": "src/app.py",
                    "line": 1,
                }
            ],
        )


def test_pin_unpin_list(retrieval, app_ctx):
    row = retrieval.pin("ses_1", "file", "src/app.py", label="entry")
    assert row["source"] == "file"
    # re-pin refreshes label, no duplicate
    retrieval.pin("ses_1", "file", "src/app.py", label="entry2")
    pins = retrieval.list_pins("ses_1")
    assert len(pins) == 1
    assert pins[0]["label"] == "entry2"

    assert retrieval.unpin("ses_1", "file", "src/app.py") is True
    assert retrieval.unpin("ses_1", "file", "src/app.py") is False
    assert retrieval.list_pins("ses_1") == []


def test_pin_validates(app_ctx):
    service = ContextRetrievalService(app_ctx)
    with pytest.raises(ValueError):
        service.pin("ses_1", "bogus", "x")
    with pytest.raises(ValueError):
        service.pin("ses_1", "file", "   ")


def test_retrieve_ranks_and_dedups(retrieval, app_ctx, proj):
    _index_project(app_ctx, proj)
    mem = MemoryService(app_ctx)
    mem.remember_project(str(proj), "The add function lives in app.py", kind="fact", topic="add")

    results = retrieval.retrieve("ses_1", "add function", str(proj), limit=10)
    assert results, "expected candidates"
    sources = [r["source"] for r in results]
    # the symbol matches 'add' most specifically
    assert sources[0] == "symbol"
    refs = [(r["source"], r["ref"]) for r in results]
    assert len(refs) == len(set(refs)), "no duplicates"

    # pinned items rank first regardless of score
    retrieval.pin("ses_1", "file", "README.md", label="readme")
    results = retrieval.retrieve("ses_1", "add function", str(proj), limit=10)
    assert results[0]["pinned"] is True
    assert results[0]["ref"] == "README.md"


def test_retrieve_artifacts(retrieval, app_ctx, proj):
    store = ArtifactStore(app_ctx)
    store.create("ses_1", "tests", "pytest.log", b"418 passed\n", project_root=str(proj))
    results = retrieval.retrieve("ses_1", "pytest", str(proj), limit=5)
    assert any(
        r["source"] == "artifact" and r["ref"] == "artifact://ses_1/tests/pytest.log"
        for r in results
    )


def test_pinned_block_memory_and_file(retrieval, app_ctx, proj):
    mem = MemoryService(app_ctx)
    stored = mem.remember_user("Always run ruff after edits", topic="lint")
    retrieval.pin("ses_1", "memory", stored["id"])
    retrieval.pin("ses_1", "file", "src/app.py", label="entrypoint")

    block = retrieval.pinned_block("ses_1", str(proj))
    assert block is not None
    assert "Always run ruff after edits" in block
    assert "app.add" not in block
    assert "def add(a, b):" in block
    assert '<untrusted source="src/app.py">' in block
    assert "</untrusted>" in block

    # no pins -> None
    assert retrieval.pinned_block("ses_none", str(proj)) is None


def test_pinned_block_symbol_and_budget(retrieval, app_ctx, proj, tmp_path):
    _index_project(app_ctx, proj)
    retrieval.pin("ses_1", "symbol", "add")
    block = retrieval.pinned_block("ses_1", str(proj))
    assert block is not None
    assert "app.py:1" in block

    # a big pinned file must be bounded (many lines trigger the excerpt cap)
    big = proj / "BIG.txt"
    big.write_text("\n".join("x" * 20 for _ in range(5000)), encoding="utf-8")
    _index_project(app_ctx, proj)
    with app_ctx.db.transaction():
        app_ctx.index_repo.replace_index_files(
            app_ctx.db,
            str(proj),
            [
                {
                    "rel_path": "BIG.txt",
                    "hash": "h3",
                    "language": "text",
                    "size_bytes": 50_000,
                    "symbols_json": "[]",
                    "imports_json": "[]",
                    "indexed_at": "2026-08-17T00:00:00.000Z",
                }
            ],
        )
    retrieval.pin("ses_1", "file", "BIG.txt")
    block = retrieval.pinned_block("ses_1", str(proj))
    assert "truncated" in block
    assert len(block) <= 6000 + 64


def test_pinned_block_term(retrieval, app_ctx, proj):
    _index_project(app_ctx, proj)
    retrieval.pin("ses_1", "term", "app", label="app search")
    block = retrieval.pinned_block("ses_1", str(proj))
    assert block is not None
    assert "src/app.py" in block


def test_assembler_pinned_segment():
    bundle = PromptAssembler().build(
        AssemblerContext(pinned_context="Pinned context...\n[file:src/app.py]\nbody")
    )
    kinds = [seg.kind for seg in bundle.segments]
    assert SegmentKind.PINNED_CONTEXT in kinds
    assert "pinned-context" in [seg.id for seg in bundle.segments]
