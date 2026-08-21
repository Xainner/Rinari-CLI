"""`rinari models pick` — interactive provider+model picker (hermes model-style).

Covers the manual fallback path (discovery unreachable -> type a custom ID) end
to end through the CLI, plus the numbered-list selection via the helper with a
stubbed discovery.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from rinari.application.model_service import ModelService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.application.services import build_services
from rinari.cli.main import app
from rinari.providers.adapters.base import DiscoveredModel
from rinari.shared.paths import ENV_HOME, ensure_layout

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    ensure_layout(tmp_path / "rinari-home")
    return tmp_path / "rinari-home"


def test_pick_custom_id_manual_fallback(home) -> None:
    # provider points at an unreachable endpoint -> discovery fails -> manual ID
    res = runner.invoke(
        app,
        [
            "--json",
            "providers",
            "add",
            "custom",
            "--name",
            "fake",
            "--endpoint",
            "http://127.0.0.1:9/v1",
            "--no-auth",
        ],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(
        app,
        ["models", "pick", "--provider", "fake", "--name", "wanted"],
        input="qwen-real-model\n",
    )
    assert res.exit_code == 0, res.output
    assert "saved and active on 'fake'" in res.output
    status = runner.invoke(app, ["--json", "model", "current"])
    assert status.exit_code == 0, status.output
    assert "qwen-real-model" in status.output


def test_pick_numbered_selection(app_ctx, monkeypatch) -> None:
    from rich.console import Console

    from rinari.cli.commands.models import _select_model

    s = build_services(app_ctx, user_home=app_ctx.home / "home")
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="custom",
            endpoint="http://127.0.0.1:9/v1",
            auth_method="none",
        )
    )
    record = providers.get("fake")

    monkeypatch.setattr(
        ModelService,
        "available",
        lambda self, ref=None: {
            "fake": [
                DiscoveredModel(provider_model_id="alpha", capabilities={"chat": True}),
                DiscoveredModel(provider_model_id="beta", capabilities={"tools": True}),
            ]
        },
    )
    monkeypatch.setattr("rinari.cli.commands.models.typer.prompt", lambda *a, **k: "2")
    console = Console(record=True, no_color=True, width=100)
    chosen = _select_model(s, record, console, False)
    assert chosen == "beta"


def test_add_provider_from_catalog(app_ctx, monkeypatch) -> None:
    from rich.console import Console

    from rinari.application.services import build_services
    from rinari.cli.commands.models import _add_provider

    s = build_services(app_ctx, user_home=app_ctx.home / "home")
    # catalog: openai(1) anthropic(2) openrouter(3) ...
    script = iter(["3", "", "env", "OPENROUTER_API_KEY"])
    monkeypatch.setattr("rinari.cli.commands.models.typer.prompt", lambda *a, **k: next(script))
    console = Console(record=True, no_color=True, width=100)
    rec = _add_provider(s, console)
    assert rec.alias == "openrouter"
    assert rec.type == "custom"
    assert rec.endpoint == "https://openrouter.ai/api/v1"
    assert rec.auth_method == "api-key"


def test_opencode_presets_have_real_endpoints() -> None:
    from rinari.providers.catalog import PROVIDER_CATALOG

    by_key = {p.key: p for p in PROVIDER_CATALOG}
    zen = by_key["opencode-zen"]
    assert zen.base_url == "https://opencode.ai/zen/v1"
    assert zen.default_env == "OPENCODE_API_KEY"
    go = by_key["opencode-go"]
    assert go.base_url == "https://opencode.ai/zen/go/v1"
    assert go.default_env == "OPENCODE_GO_API_KEY"
