import hashlib

import pytest

from rinari.runtime.identity import (
    SOURCE_PACKAGED,
    SOURCE_USER,
    load_constitution,
    load_soul,
)
from rinari.shared.errors import ConfigurationError


def test_packaged_soul_loads(app_ctx):
    asset = load_soul(app_ctx.home)
    assert asset.source == SOURCE_PACKAGED
    assert asset.version == "2.0"
    assert asset.text.startswith("<!-- rinari-asset:")
    assert "# Canonical Soul" in asset.text
    assert "You are **Rinari**" in asset.text


def test_packaged_soul_excludes_extended_identity(app_ctx):
    asset = load_soul(app_ctx.home)
    assert "# Extended Identity Reference" not in asset.text
    assert "Maintainer Notes" not in asset.text
    assert "Hair" not in asset.text


def test_packaged_constitution_loads(app_ctx):
    asset = load_constitution(app_ctx.home)
    assert asset.source == SOURCE_PACKAGED
    assert asset.version == "1.0"
    assert "# Rinari Harness Constitution" in asset.text
    for section in ("Execution", "Engineering", "Tool Discipline", "Context", "Completion"):
        assert section in asset.text
    # The constitution carries no persona content.
    assert "20-year-old" not in asset.text


def test_user_soul_override_wins(app_ctx):
    override = app_ctx.layout.soul_file
    override.write_text(
        "<!-- rinari-asset: id=soul version=9.9 -->\n\nCustom soul body.\n", encoding="utf-8"
    )
    asset = load_soul(app_ctx.home)
    assert asset.source == SOURCE_USER
    assert asset.version == "9.9"
    assert "Custom soul body." in asset.text


def test_override_without_header_has_unknown_version(app_ctx):
    app_ctx.layout.soul_file.write_text("Just some custom text.\n", encoding="utf-8")
    asset = load_soul(app_ctx.home)
    assert asset.source == SOURCE_USER
    assert asset.version == "unknown"


def test_override_constitution_wins(app_ctx):
    app_ctx.layout.constitution_file.write_text(
        "<!-- rinari-asset: id=constitution version=2.0 -->\n\nCustom constitution.\n",
        encoding="utf-8",
    )
    asset = load_constitution(app_ctx.home)
    assert asset.source == SOURCE_USER
    assert asset.version == "2.0"
    assert "Custom constitution." in asset.text


def test_fallback_when_no_override(app_ctx):
    assert not app_ctx.layout.soul_file.exists()
    assert not app_ctx.layout.constitution_file.exists()
    assert load_soul(app_ctx.home).source == SOURCE_PACKAGED
    assert load_constitution(app_ctx.home).source == SOURCE_PACKAGED


def test_sha256_tracks_content(app_ctx):
    asset = load_constitution(app_ctx.home)
    assert asset.sha256 == hashlib.sha256(asset.text.encode("utf-8")).hexdigest()

    other = load_soul(app_ctx.home)
    assert other.sha256 != asset.sha256


def test_empty_override_rejected(app_ctx):
    app_ctx.layout.soul_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_soul(app_ctx.home)


def test_home_layout_exposes_asset_paths(app_ctx):
    assert app_ctx.layout.soul_file == app_ctx.home / "soul.md"
    assert app_ctx.layout.constitution_file == app_ctx.home / "constitution.md"
