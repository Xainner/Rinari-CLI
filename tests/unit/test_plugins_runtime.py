"""Plugin subsystem tests (phase 5).

Covers manifest validation, install/update/enable/disable/remove lifecycle,
capability-request enforcement on contribution, namespacing, trust gating for
project plugins, and diagnostics (doctor). Deterministic: tmp_path only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rinari.application.services import build_services
from rinari.plugins import load_manifest
from rinari.plugins.loader import load_plugin
from rinari.plugins.manifest import PluginError

PLUGIN_PY = """
from rinari.tools.definition import ToolDefinition


def contribute(api):
    api.add_tool(
        ToolDefinition(
            name="{name}.hello",
            description="says hi",
            input_schema={{"type": "object"}},
            namespace="plugin",
            manifest={{"source": "plugin", "plugin": "{name}"}},
        )
    )
""".strip()


def make_plugin_dir(
    root: Path,
    name: str = "demo",
    version: str | None = "1.0.0",
    *,
    capabilities: list[str] | None = None,
    entrypoint_body: str | None = None,
):
    plugin = root / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "entrypoint": "plugin.py",
                "description": "demo plugin",
                "capabilities": capabilities if capabilities is not None else ["tools"],
            }
        ),
        encoding="utf-8",
    )
    (plugin / "plugin.py").write_text(
        entrypoint_body or PLUGIN_PY.format(name=name), encoding="utf-8"
    )
    return plugin


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_load_manifest_ok(tmp_path):
    plugin = make_plugin_dir(tmp_path)
    manifest = load_manifest(plugin)
    assert manifest.name == "demo"
    assert manifest.version == "1.0.0"
    assert manifest.capabilities == ("tools",)


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(PluginError) as excinfo:
        load_manifest(tmp_path / "nope")
    assert excinfo.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("name", "Bad Name"),
        lambda m: m.__setitem__("version", "one-point-zero"),
        lambda m: m.pop("entrypoint"),
        lambda m: m.__setitem__("entrypoint", "main.js"),
        lambda m: m.__setitem__("capabilities", ["teleport"]),
        lambda m: m.__setitem__("capabilities", "tools"),
    ],
    ids=["bad-name", "bad-version", "no-entrypoint", "bad-entrypoint", "bad-cap", "cap-not-list"],
)
def test_load_manifest_invalid(tmp_path, mutate):
    plugin = make_plugin_dir(tmp_path)
    data = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    mutate(data)
    (plugin / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PluginError):
        load_manifest(plugin)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def services(app_ctx):
    return build_services(app_ctx)


def test_install_list_show_permissions(services, tmp_path):
    plugin = make_plugin_dir(tmp_path / "src-plugins", "alpha", "0.2.0")
    row = services.plugins.install(plugin, source="user")
    assert row["name"] == "alpha"
    assert row["version"] == "0.2.0"
    listed = services.plugins.list()
    assert [r["name"] for r in listed] == ["alpha"]
    shown = services.plugins.show("alpha", "user")
    assert shown["manifest"]["entrypoint"] == "plugin.py"
    perms = services.plugins.permissions("alpha", "user")
    assert perms["capabilities"] == ["tools"]


def test_install_rejects_duplicate(services, tmp_path):
    plugin = make_plugin_dir(tmp_path / "src", "dup")
    services.plugins.install(plugin)
    with pytest.raises(PermissionError):
        services.plugins.install(plugin)


def test_update_bumps_version(services, tmp_path):
    plugin = make_plugin_dir(tmp_path / "src", "upg", "1.0.0")
    services.plugins.install(plugin)
    make_plugin_dir(tmp_path / "src", "upg", "1.1.0")
    row = services.plugins.update(tmp_path / "src" / "upg", "upg")
    assert row["version"] == "1.1.0"


def test_update_name_mismatch(services, tmp_path):
    plugin = make_plugin_dir(tmp_path / "src", "orig", "1.0.0")
    services.plugins.install(plugin)
    other = make_plugin_dir(tmp_path / "other", "renamed")
    with pytest.raises(ValueError):
        services.plugins.update(other, "orig")


def test_enable_disable(services, tmp_path):
    services.plugins.install(make_plugin_dir(tmp_path / "src", "t"))
    row = services.plugins.disable("t")
    assert row["enabled"] in (0, False)
    row = services.plugins.enable("t")
    assert row["enabled"] in (1, True)


def test_remove(services, tmp_path):
    services.plugins.install(make_plugin_dir(tmp_path / "src", "bye"))
    assert services.plugins.remove("bye") is True
    assert services.plugins.list() == []


# ---------------------------------------------------------------------------
# Loading + contribution
# ---------------------------------------------------------------------------


def _trusted_trust(services, project: Path):
    services.trust.add(project)


def test_load_all_contributes_namespaced_tools(services, tmp_path):
    services.plugins.install(make_plugin_dir(tmp_path / "src", "alpha"))
    loaded = services.plugins.load_all()
    assert len(loaded) == 1
    assert loaded[0].ok(), loaded[0].diagnostics
    assert [t.name for t in loaded[0].tools] == ["alpha.hello"]


def test_unrequested_contribution_is_rejected(tmp_path):
    body = "def contribute(api):\n    api.add_hook('PostToolUse', lambda p: None)\n"
    plugin = make_plugin_dir(tmp_path, "nolimits", capabilities=[], entrypoint_body=body)
    manifest = load_manifest(plugin)
    loaded = load_plugin(manifest, plugin, "user", project_trusted=True)
    assert loaded.tools == []
    assert any(d["code"] in ("UNSUPPORTED_CONTRIBUTION", "LOAD_FAILED") for d in loaded.diagnostics)


def test_plugin_tool_namespacing_enforced(tmp_path):
    body = (
        "import rinari.tools.definition as td\n"
        "def contribute(api):\n"
        "    api.add_tool(td.ToolDefinition(name='not-namespaced', description='x', "
        "input_schema={'type': 'object'}))\n"
    )
    plugin = make_plugin_dir(tmp_path, "badname", entrypoint_body=body)
    manifest = load_manifest(plugin)
    loaded = load_plugin(manifest, plugin, "user", project_trusted=True)
    assert loaded.tools == []
    assert any(
        "namespaced" in d["message"] or d["code"] == "LOAD_FAILED" for d in loaded.diagnostics
    )


def test_project_plugin_requires_trust(services, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    src = make_plugin_dir(tmp_path / "src", "projplug")
    services.plugins.install(src, source="project", project=project)
    # Not trusted yet: loading yields the TRUST_REQUIRED diagnostic, no tools.
    loaded_untrusted = services.plugins.load_all(project=project)
    plug = next(pl for pl in loaded_untrusted if pl.path.name == "projplug")
    assert plug.tools == []
    assert any(d["code"] == "TRUST_REQUIRED" for d in plug.diagnostics)
    # Trust and re-load.
    _trusted_trust(services, project)
    loaded_trusted = services.plugins.load_all(project=project)
    plug = next(pl for pl in loaded_trusted if pl.path.name == "projplug")
    assert plug.ok()
    assert [t.name for t in plug.tools] == ["projplug.hello"]


def test_doctor_reports_clean(services, tmp_path):
    services.plugins.install(make_plugin_dir(tmp_path / "src", "okplug"))
    report = services.plugins.doctor()
    assert report[0]["diagnostics"][0]["code"] == "OK"


def test_doctor_reports_missing_path(services, tmp_path):
    services.plugins.install(make_plugin_dir(tmp_path / "src", "ghost"))
    # Remove files but keep the DB row.
    path = Path(services.plugins.list()[0]["path"])
    import shutil

    shutil.rmtree(path)
    report = services.plugins.doctor()
    assert any(d["code"] == "PATH_MISSING" for entry in report for d in entry["diagnostics"])
