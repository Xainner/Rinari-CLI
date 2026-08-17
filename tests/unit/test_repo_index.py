"""Repository index: incremental build, queries, test mapping, doctor (phase 3).

Regression contract:
- project-scoped store keyed by canonical root;
- unchanged files (same sha256) keep their cached symbols and are not reparsed;
- references exclude definitions;
- test mapping links test files to the source files they import/reference;
- doctor reports drift (new/removed/stale) without reparsing;
- semantic layer is declared but 'none' by default (stack.md 68).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari.application.services import build_services
from rinari.shared.errors import InvalidUsageError

runner = CliRunner()


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        "class Greeter:\n"
        "    def greet(self, name):\n"
        '        return f"hi {name}"\n'
        "\n"
        "\n"
        "def make_greeter():\n"
        "    return Greeter()\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        'from app import Greeter\n\n\ndef main():\n    return Greeter().greet("world")\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_app.py").write_text(
        "from app import Greeter\n"
        "\n"
        "\n"
        "def test_greet():\n"
        '    assert Greeter().greet("x") == "hi x"\n',
        encoding="utf-8",
    )
    return root


def test_status_before_build_is_not_indexed(services, tmp_path) -> None:
    proj = _project(tmp_path)
    status = services.index.status(proj)
    assert status["indexed"] is False
    assert status["meta"] is None


def test_build_indexes_symbols_references_and_tests(services, tmp_path) -> None:
    proj = _project(tmp_path)
    result = services.index.build(proj)
    assert result.files_added == 3
    assert result.total_files == 3
    assert result.total_symbols >= 5
    assert result.total_references > 0
    assert result.semantic_layer == "none"

    status = services.index.status(proj)
    assert status["indexed"] is True
    assert status["meta"]["files"] == 3
    assert status["meta"]["symbols"] == result.total_symbols

    query = services.index.search(proj, "Greeter")
    assert any(s["rel_path"] == "app.py" for s in query["symbols"])
    assert any(s["qualified_name"] == "Greeter" for s in query["symbols"])
    ref_files = {r["rel_path"] for r in query["references"]}
    assert "main.py" in ref_files
    # Definitions (app.py class line) are not stored as references of themselves
    assert "tests/test_app.py" in query["test_files"]


def test_incremental_update_skips_unchanged_files(services, tmp_path) -> None:
    proj = _project(tmp_path)
    services.index.build(proj)
    (proj / "extra.py").write_text("def extra_fn():\n    return 1\n", encoding="utf-8")
    (proj / "app.py").write_text(
        "class Greeter:\n"
        "    def greet(self, name):\n"
        '        return f"hi {name}"\n'
        "\n"
        "\n"
        "def make_greeter():\n"
        "    return Greeter()\n"
        "\n"
        "\n"
        "def another():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    result = services.index.build(proj)
    assert result.files_added == 1
    assert result.files_changed == 1
    assert result.files_unchanged == 2

    query = services.index.search(proj, "another")
    assert query["symbols"][0]["rel_path"] == "app.py"


def test_removed_files_are_dropped(services, tmp_path) -> None:
    proj = _project(tmp_path)
    services.index.build(proj)
    (proj / "main.py").unlink()
    result = services.index.build(proj)
    assert result.files_removed == 1
    assert result.total_files == 2


def test_rebuild_forces_full_reparse(services, tmp_path) -> None:
    proj = _project(tmp_path)
    services.index.build(proj)
    result = services.index.rebuild(proj)
    assert result.files_added == 0
    assert result.files_changed == result.total_files
    assert result.files_unchanged == 0


def test_search_requires_an_index(services, tmp_path) -> None:
    proj = _project(tmp_path)
    with pytest.raises(InvalidUsageError):
        services.index.search(proj, "Greeter")


def test_doctor_reports_drift(services, tmp_path) -> None:
    proj = _project(tmp_path)
    services.index.build(proj)
    report = services.index.doctor(proj)
    assert report["indexed"] is True
    assert report["consistent"] is True
    assert report["stale"] == []
    assert report["new_on_disk"] == []

    with open(proj / "app.py", "a", encoding="utf-8") as handle:
        handle.write("\n\ndef later():\n    return 3\n")
    (proj / "brand_new.py").write_text("def new_thing():\n    return 1\n", encoding="utf-8")

    report = services.index.doctor(proj)
    assert report["consistent"] is False
    assert "app.py" in report["stale"]
    assert "brand_new.py" in report["new_on_disk"]

    services.index.update(proj)
    report = services.index.doctor(proj)
    assert report["consistent"] is True
    assert report["meta"]["files"] == 4


def test_clear_removes_all_state(services, tmp_path) -> None:
    proj = _project(tmp_path)
    services.index.build(proj)
    assert services.index.clear(proj) is True
    assert services.index.status(proj)["indexed"] is False
    assert services.index.clear(proj) is False
    # Rebuild after clear works from scratch.
    result = services.index.build(proj)
    assert result.files_added == 3


def test_cli_build_status_search_clear(tmp_path, monkeypatch) -> None:
    import rinari.cli.deps as cli_deps
    from rinari.cli.main import app as cli_app
    from rinari.shared.paths import ENV_HOME

    home = tmp_path / "rinari-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    proj = _project(tmp_path)

    result = runner.invoke(cli_app, ["index", "build", str(proj)])
    assert result.exit_code == 0, result.output
    assert "index build complete" in result.output

    # The real entrypoint strips `--json` from argv and sets the global flag
    # (main.py); CliRunner bypasses that, so emulate it for the JSON asserts.
    monkeypatch.setattr(cli_deps, "_global_json", True)
    result = runner.invoke(cli_app, ["index", "status", str(proj)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["indexed"] is True
    assert payload["data"]["meta"]["files"] == 3

    result = runner.invoke(cli_app, ["index", "search", "Greeter", str(proj)])
    payload = json.loads(result.output)
    assert any(s["rel_path"] == "app.py" for s in payload["data"]["symbols"])
    assert "tests/test_app.py" in payload["data"]["test_files"]

    result = runner.invoke(cli_app, ["index", "clear", str(proj)])
    payload = json.loads(result.output)
    assert payload["data"]["cleared"] is True

    result = runner.invoke(cli_app, ["index", "search", "Greeter", str(proj)])
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "not indexed" in payload["error"]["message"].lower()
