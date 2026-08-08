"""Tests de apply_patch: edición por diff unificado (estilo codex apply_patch)."""

import pytest

from rinari.agent.tools import apply_patch, edit_file, read_file


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "app.py").write_text(
        "def saludo():\n"
        "    return \"hola\"\n"
        "\n"
        "def despedida():\n"
        "    return \"chao\"\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_apply_patch_simple_hunk(workdir, tmp_path):
    patch_text = """*** Begin Patch
*** Update File: app.py
@@
 def saludo():
-    return "hola"
+    return "¡hola!"
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is True
    content = (tmp_path / "app.py").read_text(encoding="utf-8")
    assert 'return "¡hola!"' in content
    assert 'return "chao"' in content  # el resto intacto


def test_apply_patch_multiple_hunks(workdir, tmp_path):
    patch_text = """*** Begin Patch
*** Update File: app.py
@@
 def saludo():
-    return "hola"
+    return "¡hola!"
@@
 def despedida():
-    return "chao"
+    return "¡chao!"
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is True
    content = (tmp_path / "app.py").read_text(encoding="utf-8")
    assert 'return "¡hola!"' in content
    assert 'return "¡chao!"' in content


def test_apply_patch_requires_markers(workdir):
    result = apply_patch({"patch": "sin marcadores"}, cwd=workdir)
    assert result["ok"] is False


def test_apply_patch_unknown_file(workdir):
    patch_text = """*** Begin Patch
*** Update File: noexiste.py
@@
-x
+y
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is False
    assert "noexiste" in result["error"]


def test_apply_patch_creates_file(workdir, tmp_path):
    patch_text = """*** Begin Patch
*** Add File: nuevo.py
+def extra():
+    return 42
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is True
    content = (tmp_path / "nuevo.py").read_text(encoding="utf-8")
    assert "def extra():" in content


def test_apply_patch_blocks_secrets(workdir, tmp_path):
    patch_text = """*** Begin Patch
*** Add File: .env
+SECRET=1
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is False
    assert "sensible" in result["error"].lower()


def test_apply_patch_edits_are_undoable(workdir, tmp_path):
    patch_text = """*** Begin Patch
*** Update File: app.py
@@
-    return "hola"
+    return "hola mundo"
*** End Patch
"""
    result = apply_patch({"patch": patch_text}, cwd=workdir)
    assert result["ok"] is True
    # read_file sigue funcionando y el backup de /undo existe
    r = read_file({"path": "app.py"}, cwd=workdir)
    assert "hola mundo" in r["content"]
