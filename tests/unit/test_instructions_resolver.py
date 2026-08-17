"""RINARI.md instruction resolver (phase 3: root/nested/override/chain)."""

from __future__ import annotations

from pathlib import Path

from rinari.instructions.resolver import (
    chain_dirs,
    provenance_for,
    resolve_project_instructions,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_chain_root_to_cwd_ordering(tmp_path) -> None:
    root = tmp_path / "proj"
    sub = root / "src" / "api"
    _write(root / "RINARI.md", "root rule\n")
    _write(root / "src" / "RINARI.md", "src rule\n")
    _write(sub / "RINARI.md", "api rule\n")

    entries = resolve_project_instructions(root, sub, global_path=tmp_path / "global.md")
    assert [e.scope for e in entries] == ["root", "dir:src", "dir:src/api"]
    assert [e.content for e in entries] == ["root rule", "src rule", "api rule"]
    # Provenance is stable and level-explicit:
    assert [provenance_for(e) for e in entries] == [
        "./RINARI.md",
        "src/RINARI.md",
        "src/api/RINARI.md",
    ]


def test_cwd_at_root_yields_single_entry(tmp_path) -> None:
    root = tmp_path / "proj"
    _write(root / "RINARI.md", "only\n")
    entries = resolve_project_instructions(root, root, global_path=None)
    assert len(entries) == 1
    assert entries[0].scope == "root"
    assert entries[0].relative == ""


def test_override_replaces_not_stacks(tmp_path) -> None:
    root = tmp_path / "proj"
    _write(root / "RINARI.md", "root\n")
    sub = root / "sub"
    _write(sub / "RINARI.md", "standard here\n")
    _write(sub / "RINARI.override.md", "override here\n")

    entries = resolve_project_instructions(root, sub, global_path=None)
    assert [e.content for e in entries] == ["root", "override here"]
    sub_entry = entries[1]
    assert sub_entry.kind == "override"
    assert sub_entry.scope == "dir:sub"
    assert provenance_for(sub_entry) == "sub/RINARI.override.md"


def test_chain_when_cwd_is_outside_root_falls_back_to_root(tmp_path) -> None:
    root = tmp_path / "proj"
    other = tmp_path / "elsewhere"
    other.mkdir()
    _write(root / "RINARI.md", "root\n")
    dirs = chain_dirs(root, other)
    assert dirs == [root]


def test_global_user_file_resolves_first_and_alone_for_chat(tmp_path) -> None:
    root = tmp_path / "proj"
    _write(root / "RINARI.md", "project\n")
    global_path = tmp_path / "home" / "RINARI.md"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("global rule\n", encoding="utf-8")

    # PROJECT: global first, then the chain.
    entries = resolve_project_instructions(root, root, global_path=global_path)
    assert [e.scope for e in entries] == ["global", "root"]
    assert entries[0].trust == "trusted"

    # CHAT (no project): only the global file.
    chat_entries = resolve_project_instructions(None, None, global_path=global_path)
    assert [e.scope for e in chat_entries] == ["global"]


def test_untrusted_project_yields_only_global(tmp_path) -> None:
    root = tmp_path / "proj"
    _write(root / "RINARI.md", "project\n")
    global_path = tmp_path / "global.md"
    global_path.write_text("g\n", encoding="utf-8")

    entries = resolve_project_instructions(root, root, global_path=global_path, trusted=False)
    assert [e.scope for e in entries] == ["global"]
    assert all(e.content != "project" for e in entries)


def test_readme_is_never_an_instruction(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("documentation only\n", encoding="utf-8")
    assert resolve_project_instructions(root, root, global_path=None) == []


def test_blank_files_are_skipped(tmp_path) -> None:
    root = tmp_path / "proj"
    sub = root / "sub"
    _write(root / "RINARI.md", "   \n\t\n")
    _write(sub / "RINARI.md", "real\n")
    entries = resolve_project_instructions(root, sub, global_path=None)
    assert [e.content for e in entries] == ["real"]


def test_files_are_bounded_to_max(tmp_path) -> None:
    from rinari.instructions.resolver import MAX_INSTRUCTION_BYTES

    root = tmp_path / "proj"
    _write(root / "RINARI.md", "x" * (MAX_INSTRUCTION_BYTES + 1000))
    entries = resolve_project_instructions(root, root, global_path=None)
    assert len(entries[0].content) <= MAX_INSTRUCTION_BYTES
    assert entries[0].size_bytes == MAX_INSTRUCTION_BYTES + 1000
