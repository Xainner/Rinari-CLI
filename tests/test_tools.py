"""Tests para el tool registry del agente."""

import json
import time

import pytest

from rinari.agent.tools import (
    ToolRegistry,
    list_dir,
    read_file,
    run_command,
    search_files,
    write_file,
)


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "hola.txt").write_text("línea uno\nlínea dos\n", encoding="utf-8")
    (d / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return d


def test_run_command_stdout_and_exit(workdir):
    result = run_command({"command": "echo hola"}, cwd=str(workdir))
    assert result["exit_code"] == 0
    assert "hola" in result["stdout"]


def test_run_command_captures_stderr(workdir):
    result = run_command({"command": "echo err 1>&2"}, cwd=str(workdir))
    assert result["exit_code"] == 0
    assert "err" in result["stderr"]


def test_run_command_nonzero_exit(workdir):
    result = run_command({"command": "exit 3"}, cwd=str(workdir))
    assert result["exit_code"] == 3


def test_run_command_timeout(workdir):
    start = time.time()
    result = run_command({"command": "sleep 5", "timeout": 1}, cwd=str(workdir))
    elapsed = time.time() - start
    assert elapsed < 4
    assert "timeout" in result["error"].lower() or "timed out" in result["error"].lower()


def test_read_file_returns_content(workdir):
    result = read_file({"path": "hola.txt"}, cwd=str(workdir))
    assert "línea uno" in result["content"]
    assert result["exists"] is True


def test_read_file_missing(workdir):
    result = read_file({"path": "no-existe.txt"}, cwd=str(workdir))
    assert result["exists"] is False
    assert "error" in result


def test_write_file_creates(workdir):
    result = write_file({"path": "nuevo.txt", "content": "contenido"}, cwd=str(workdir))
    assert result["ok"] is True
    assert (workdir / "nuevo.txt").read_text(encoding="utf-8") == "contenido"


def test_write_file_overwrites(workdir):
    write_file({"path": "hola.txt", "content": "nuevo"}, cwd=str(workdir))
    assert (workdir / "hola.txt").read_text(encoding="utf-8") == "nuevo"


def test_search_files_regex(workdir):
    result = search_files({"pattern": "def main", "path": "."}, cwd=str(workdir))
    assert len(result["matches"]) == 1
    assert "main.py" in result["matches"][0]["file"]


def test_search_files_limit(workdir):
    for i in range(5):
        (workdir / f"f{i}.py").write_text("def main():\n    pass\n", encoding="utf-8")
    result = search_files({"pattern": "def main", "path": ".", "limit": 3}, cwd=str(workdir))
    assert len(result["matches"]) <= 3


def test_list_dir(workdir):
    result = list_dir({"path": "."}, cwd=str(workdir))
    names = {e["name"] for e in result["entries"]}
    assert "hola.txt" in names
    assert "main.py" in names


def test_tool_schemas_are_valid_openai_tools(workdir):
    registry = ToolRegistry()
    schemas = registry.openai_schemas()
    assert len(schemas) >= 5
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["name"]
        assert s["function"]["parameters"]["type"] == "object"
        # JSON-serializable
        json.dumps(s)


def test_registry_executes_by_name(workdir):
    registry = ToolRegistry()
    result = registry.execute("run_command", {"command": "echo x"}, cwd=str(workdir))
    assert result["exit_code"] == 0


def test_registry_unknown_tool_raises(workdir):
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="desconocida"):
        registry.execute("no_existe", {}, cwd=str(workdir))


def test_write_file_rejects_path_escape(workdir):
    with pytest.raises(ValueError, match="fuera del directorio"):
        write_file({"path": "../escape.txt", "content": "x"}, cwd=str(workdir))


def test_read_file_rejects_path_escape(workdir):
    with pytest.raises(ValueError, match="fuera del directorio"):
        read_file({"path": "../../etc/passwd"}, cwd=str(workdir))
