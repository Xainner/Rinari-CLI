"""Tests para edit_file: edición quirúrgica de archivos (reemplazo de bloques)."""

import pytest

from rinari.agent.tools import ToolRegistry, edit_file


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "app.py"
    f.write_text(
        "def saludar(nombre):\n"
        "    return f'Hola {nombre}'\n"
        "\n"
        "def despedir(nombre):\n"
        "    return f'Adiós {nombre}'\n",
        encoding="utf-8",
    )
    return tmp_path


def test_edit_file_replaces_block(sample_file):
    result = edit_file(
        {
            "path": "app.py",
            "old": "return f'Hola {nombre}'",
            "new": "return f'Hola, {nombre}!'",
        },
        cwd=str(sample_file),
    )
    assert result["ok"] is True
    content = (sample_file / "app.py").read_text(encoding="utf-8")
    assert "Hola, {nombre}!" in content
    assert "Adiós" in content  # el resto intacto


def test_edit_file_requires_old_text(sample_file):
    result = edit_file({"path": "app.py", "new": "x"}, cwd=str(sample_file))
    assert result["ok"] is False
    assert "old" in result["error"].lower()


def test_edit_file_old_not_found(sample_file):
    result = edit_file(
        {"path": "app.py", "old": "no existe esto", "new": "x"},
        cwd=str(sample_file),
    )
    assert result["ok"] is False
    assert "no se encontró" in result["error"].lower()


def test_edit_file_ambiguous_match_fails(sample_file):
    """old aparece 2+ veces → error pidiendo más contexto."""
    (sample_file / "app.py").write_text(
        "print('hola')\nprint('hola')\n", encoding="utf-8"
    )
    result = edit_file(
        {"path": "app.py", "old": "print('hola')", "new": "print('adiós')"},
        cwd=str(sample_file),
    )
    assert result["ok"] is False
    assert "ambig" in result["error"].lower()


def test_edit_file_uses_count_for_duplicates(sample_file):
    """Con count=2 reemplaza ambas ocurrencias."""
    (sample_file / "app.py").write_text(
        "print('hola')\nprint('hola')\n", encoding="utf-8"
    )
    result = edit_file(
        {"path": "app.py", "old": "print('hola')", "new": "print('adiós')", "count": 2},
        cwd=str(sample_file),
    )
    assert result["ok"] is True
    content = (sample_file / "app.py").read_text(encoding="utf-8")
    assert content.count("adiós") == 2


def test_edit_file_multiline_block(sample_file):
    """Reemplaza un bloque multilínea completo."""
    result = edit_file(
        {
            "path": "app.py",
            "old": "def saludar(nombre):\n    return f'Hola {nombre}'",
            "new": "def saludar(nombre, tono='normal'):\n    return f'Hola {nombre} ({tono})'",
        },
        cwd=str(sample_file),
    )
    assert result["ok"] is True
    content = (sample_file / "app.py").read_text(encoding="utf-8")
    assert "tono='normal'" in content


def test_edit_file_rejects_escape(sample_file):
    with pytest.raises(ValueError, match="fuera del directorio"):
        edit_file({"path": "../x.py", "old": "a", "new": "b"}, cwd=str(sample_file))


def test_edit_file_reports_line_numbers(sample_file):
    result = edit_file(
        {"path": "app.py", "old": "return f'Adiós {nombre}'", "new": "return 'chao'"},
        cwd=str(sample_file),
    )
    assert result["ok"] is True
    assert result["line"] >= 1


def test_registry_exposes_edit_file():
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    assert "edit_file" in names
