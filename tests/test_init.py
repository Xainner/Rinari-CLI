"""Tests de /init: genera RINARI.md analizando el repo (estilo codex /init)."""

import pytest

from rinari.repl import _cmd_init


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["requests>=2"]\n', encoding="utf-8"
    )
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "src" / "demo" / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\nUn proyecto de prueba.\n", encoding="utf-8")
    return tmp_path


def test_init_creates_rinari_md(repo):
    result = _cmd_init(str(repo))
    assert "RINARI.md" in result
    assert (repo / "RINARI.md").exists()


def test_init_detects_python_stack(repo):
    _cmd_init(str(repo))
    content = (repo / "RINARI.md").read_text(encoding="utf-8")
    assert "Python" in content
    assert "pytest" in content
    assert "requests" in content
    assert "demo" in content


def test_init_detects_readme_purpose(repo):
    _cmd_init(str(repo))
    content = (repo / "RINARI.md").read_text(encoding="utf-8")
    assert "prueba" in content  # del README


def test_init_detects_node_stack(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name": "webapp", "scripts": {"test": "jest"}}\n', encoding="utf-8"
    )
    result = _cmd_init(str(tmp_path))
    content = (tmp_path / "RINARI.md").read_text(encoding="utf-8")
    assert "Node" in content or "JavaScript" in content
    assert "jest" in content


def test_init_no_overwrite_without_force(repo):
    (repo / "RINARI.md").write_text("manual\n", encoding="utf-8")
    _cmd_init(str(repo))
    content = (repo / "RINARI.md").read_text(encoding="utf-8")
    assert content.strip() == "manual"  # no se sobrescribió


def test_init_no_rinari_md_means_empty_repo(tmp_path):
    result = _cmd_init(str(tmp_path))
    # repo vacío: crea un archivo mínimo
    assert "RINARI.md" in result
    assert (tmp_path / "RINARI.md").exists()
