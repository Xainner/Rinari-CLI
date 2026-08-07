"""Tests para la vista de inicio (banner/dashboard) de Rinari."""

import subprocess

from rinari.ui import (
    build_welcome,
    check_endpoint_health,
    git_info,
    render_logo,
)


def test_render_logo_contains_rinari():
    logo = render_logo()
    assert logo.strip()  # no vacío
    assert len(logo.splitlines()) >= 3  # ASCII art multlínea
    assert any(c in logo for c in "|/_\\")  # tiene trazos de figlet


def test_build_welcome_contains_profile_info():
    welcome = build_welcome(
        profile="casa",
        model="qwen3.6-27b",
        base_url="http://192.168.0.3:8020/v1",
        repo_name="mi-repo",
        git={"branch": "feature-ui", "clean": True, "commit": "abc1234"},
        endpoint_ok=True,
        version="0.1.0",
        sessions_count=5,
    )
    assert "casa" in welcome
    assert "qwen3.6-27b" in welcome
    assert "feature-ui" in welcome
    assert "0.1.0" in welcome


def test_build_welcome_shows_endpoint_status():
    ok = build_welcome(
        profile="casa", model="m", base_url="u", repo_name="r",
        git={}, endpoint_ok=True, version="v", sessions_count=0,
    )
    assert "conectado" in ok.lower() or "vivo" in ok.lower() or "online" in ok.lower() or "✓" in ok

    down = build_welcome(
        profile="casa", model="m", base_url="u", repo_name="r",
        git={}, endpoint_ok=False, version="v", sessions_count=0,
    )
    assert "caído" in down.lower() or "sin conexión" in down.lower() or "offline" in down.lower() or "✗" in down


def test_build_welcome_shows_repo_dirty_state():
    welcome = build_welcome(
        profile="casa", model="m", base_url="u", repo_name="r",
        git={"branch": "main", "clean": False, "commit": "abc1234"},
        endpoint_ok=True, version="v", sessions_count=0,
    )
    assert "modificado" in welcome.lower() or "cambios" in welcome.lower() or "dirty" in welcome.lower()


def test_git_info_returns_empty_for_non_repo(tmp_path):
    info = git_info(tmp_path)
    assert info == {}


def test_git_info_detects_branch_and_clean(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("hola\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    info = git_info(tmp_path)
    assert info["branch"] == "master" or info["branch"] == "main"
    assert info["clean"] is True
    assert len(info["commit"]) >= 7


def test_git_info_detects_dirty(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("hola\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("cambio\n", encoding="utf-8")

    info = git_info(tmp_path)
    assert info["clean"] is False


def test_check_endpoint_health_ok():
    """Endpoint responde → True (con transport mock)."""
    import httpx

    from rinari.client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "qwen3.6-27b"}]})

    client = LLMClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert check_endpoint_health(client) is True


def test_check_endpoint_health_down():
    """Endpoint caído/error → False (sin colgar)."""
    import httpx

    from rinari.client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = LLMClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert check_endpoint_health(client) is False


def test_check_endpoint_health_http_error():
    import httpx

    from rinari.client import LLMClient, LLMError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = LLMClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert check_endpoint_health(client) is False
