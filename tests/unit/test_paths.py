from pathlib import Path

from rinari.shared.paths import ENV_HOME, ensure_layout, layout_for, resolve_home


def test_resolve_home_default_is_dot_rinari(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_home() == tmp_path / ".rinari"


def test_resolve_home_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom"
    monkeypatch.setenv(ENV_HOME, str(custom))
    assert resolve_home() == custom


def test_resolve_home_explicit_wins_over_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "env"))
    assert resolve_home(explicit) == explicit


def test_layout_paths(tmp_path):
    layout = layout_for(tmp_path / "home")
    assert layout.config_file.name == "config.toml"
    assert layout.state_db.name == "state.db"
    assert layout.credentials_dir.name == "credentials"
    assert layout.artifacts_dir.name == "artifacts"


def test_ensure_layout_creates_standard_dirs(tmp_path):
    root = tmp_path / "rinari-home"
    layout = ensure_layout(root)
    for name in (
        "profiles",
        "policies",
        "skills",
        "agents",
        "plugins",
        "memory",
        "sessions",
        "artifacts",
        "cache",
        "logs",
        "credentials",
    ):
        assert (root / name).is_dir(), name
    assert layout.root == root.resolve()


def test_ensure_layout_is_idempotent(tmp_path):
    root = tmp_path / "rinari-home"
    ensure_layout(root)
    layout = ensure_layout(root)
    assert layout.root == root.resolve()


def test_rinari_home_is_not_home(tmp_path, monkeypatch):
    # Invariant guard: Rinari state lives in a dedicated dir, never in $HOME
    # directly, so $HOME can never become an implicit writable workspace.
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    home = Path.home()
    assert resolve_home() == tmp_path / "rinari"
    assert resolve_home() != home
