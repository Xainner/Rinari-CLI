"""Repository state detection (phase 3: languages, frameworks, commands)."""

from __future__ import annotations

from pathlib import Path

from rinari.repo.state import analyze_repository


def _py_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = \'demo\'\ndependencies = [\n  "fastapi>=0.110",\n  "pytest>=8",\n]\n\n'
        '[tool.ruff]\nline-length = 100\n\n[build-system]\nrequires = ["hatchling"]\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("lock\n", encoding="utf-8")
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "node_modules").mkdir()


def _node_project(root: Path) -> None:
    (root / "package.json").write_text(
        '{\n "name": "demo",\n "dependencies": {"react": "^18"},\n'
        ' "devDependencies": {"typescript": "^5"},\n'
        ' "scripts": {"build": "tsc", "test": "vitest", "typecheck": "tsc --noEmit"}\n}\n',
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.tsx").write_text("export {}\n", encoding="utf-8")
    (root / "src" / "util.js").write_text("export 1\n", encoding="utf-8")


def _rust_project(root: Path) -> None:
    (root / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")


def test_python_project_detection(tmp_path) -> None:
    _py_project(tmp_path)
    s = analyze_repository(tmp_path)
    assert s.languages[:1] == ["python"]
    assert "fastapi" in s.frameworks
    assert "uv" in s.package_managers
    assert any(h.command == "uv run pytest" for h in s.test)
    assert any(h.command == "uv run ruff check ." for h in s.lint)
    assert any(h.command == "uv build" for h in s.build)
    # Generated dirs are hinted, not scanned deeply.
    assert "dist" in s.generated
    assert "node_modules" in s.generated
    prompt = s.to_prompt_dict()
    assert prompt["test_command"] == "uv run pytest"
    assert prompt["languages"].startswith("python")


def test_node_project_detection(tmp_path) -> None:
    _node_project(tmp_path)
    s = analyze_repository(tmp_path)
    assert "typescript" in s.languages
    assert "javascript" in s.languages
    assert "react" in s.frameworks
    assert "npm" in s.package_managers
    assert any(h.command == "npm run build" for h in s.build)
    assert any(h.command == "npm run test" for h in s.test)
    assert any(h.command == "npm run typecheck" for h in s.typecheck)


def test_rust_project_detection(tmp_path) -> None:
    _rust_project(tmp_path)
    s = analyze_repository(tmp_path)
    assert s.languages == ["rust"]
    assert "cargo" in s.package_managers
    assert any(h.command == "cargo build" for h in s.build)
    assert any(h.command == "cargo test" for h in s.test)


def test_empty_directory_is_safe(tmp_path) -> None:
    s = analyze_repository(tmp_path)
    assert s.languages == []
    assert s.package_managers == []
    assert s.scanned_files == 0


def test_missing_root_returns_empty_summary(tmp_path) -> None:
    s = analyze_repository(tmp_path / "does-not-exist")
    assert s.languages == []
    assert s.markers == []


def test_generated_dirs_are_not_scanned_for_languages(tmp_path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x\n", encoding="utf-8")
    (tmp_path / "real.js").write_text("x\n", encoding="utf-8")
    s = analyze_repository(tmp_path)
    # Only the real file counts.
    assert s.languages == ["javascript"]
    assert "node_modules" in s.generated


def test_language_ranking_by_count(tmp_path) -> None:
    (tmp_path / "a.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 1\n", encoding="utf-8")
    s = analyze_repository(tmp_path)
    assert s.languages == ["go", "python"]
