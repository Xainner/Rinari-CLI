"""verify.plan reads the commands RINARI.md lists under «Cómo probar» (plan 0.4.0 #81)."""

from __future__ import annotations

from rinari.verify.instruction_commands import instruction_commands, merge_commands

RINARI = """# Tienda

## Qué es
Usa `npm` para todo.

## Cómo probar

- Pruebas: `pytest -q`
- Lint: `ruff check src`
- Tipos: `mypy src`

```bash
$ npm run build
```

## Convenciones
- No toques `vendor/`.
"""


def test_commands_from_the_section_only_by_kind():
    found = instruction_commands([RINARI])
    assert found["test"] == ["pytest -q"]
    assert found["lint"] == ["ruff check src"]
    assert found["typecheck"] == ["mypy src"]
    assert found["build"] == ["npm run build"]
    # Backticks outside the section are not commands.
    assert "npm" not in found["test"] and "vendor/" not in found["test"]


def test_english_heading_and_nested_subsections():
    text = "## How to test\n\n### Unit\n`go test ./...`\n\n## Layout\n`cmd/` holds binaries\n"
    assert instruction_commands([text])["test"] == ["go test ./..."]


def test_project_word_first_then_discovered_without_repeats():
    merged = merge_commands(
        {"test": ["pytest -q"], "lint": []},
        {"test": ["pytest", "pytest -q"], "build": ["make"]},
    )
    assert merged["test"] == ["pytest -q", "pytest"]
    assert merged["build"] == ["make"]


def test_plan_uses_them_when_the_project_is_trusted(app_ctx, tmp_path):
    from rinari.application.services import build_services

    home = tmp_path / "home"
    home.mkdir()
    services = build_services(app_ctx, user_home=home)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "RINARI.md").write_text(RINARI, encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    plan = services.verification.plan(root, ["app.py"], trusted=True)
    assert "pytest -q" in plan.test_commands
    untrusted = services.verification.plan(root, ["app.py"], trusted=False)
    assert "pytest -q" not in untrusted.test_commands
