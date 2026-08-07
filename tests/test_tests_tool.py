"""Tests para run_tests: detección y ejecución de la suite de tests del repo."""

import pytest

from rinari.agent.tools import ToolRegistry, run_tests


@pytest.fixture
def py_project(tmp_path):
    """Proyecto Python con pytest detectable."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def npm_project(tmp_path):
    """Proyecto Node con package.json y test script."""
    (tmp_path / "package.json").write_text(
        '{"name": "x", "scripts": {"test": "node -e \'console.log(\\"ok\\")\'"}}\n',
        encoding="utf-8",
    )
    return tmp_path


def test_run_tests_detects_pytest(py_project):
    result = run_tests({}, cwd=str(py_project))
    assert result["ok"] is True
    assert result["framework"] == "pytest"
    assert result["exit_code"] == 0
    assert "passed" in result["output"].lower() or "1 passed" in result["output"]


def test_run_tests_detects_npm(npm_project):
    result = run_tests({}, cwd=str(npm_project))
    assert result["ok"] is True
    assert result["framework"] == "npm"
    assert result["exit_code"] == 0


def test_run_tests_no_framework(tmp_path):
    (tmp_path / "hola.txt").write_text("x", encoding="utf-8")
    result = run_tests({}, cwd=str(tmp_path))
    assert result["ok"] is False
    assert "framework" in result["error"].lower() or "no se encontró" in result["error"].lower()


def test_run_tests_failing_test_reports_failure(py_project):
    (py_project / "tests" / "test_x.py").write_text(
        "def test_fail():\n    assert 1 == 2\n", encoding="utf-8"
    )
    result = run_tests({}, cwd=str(py_project))
    assert result["ok"] is False
    assert result["exit_code"] != 0
    assert "failed" in result["output"].lower()


def test_run_tests_respects_custom_command(py_project):
    """--command personalizado overridea la detección."""
    result = run_tests({"command": "python -c 'print(42)'"}, cwd=str(py_project))
    assert result["ok"] is True
    assert "42" in result["output"]


def test_run_tests_limits_output(py_project):
    result = run_tests({"max_output": 200}, cwd=str(py_project))
    assert result["ok"] is True
    assert len(result["output"]) <= 200 or result["truncated"] is True


def test_registry_exposes_run_tests():
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    assert "run_tests" in names


def test_run_tests_ignores_venv_dirs(py_project):
    """No detecta pytest dentro de .venv/ o node_modules/."""
    (py_project / ".venv").mkdir()
    (py_project / ".venv" / "pyproject.toml").write_text("[x]\n", encoding="utf-8")
    result = run_tests({}, cwd=str(py_project))
    assert result["framework"] == "pytest"  # usa el del root, no el del venv
