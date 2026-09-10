import http.client
import json
from urllib.parse import urlsplit

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.server import EngineServer


@pytest.fixture
def preview(app_ctx, tmp_path):
    services = build_services(app_ctx, user_home=tmp_path)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="fake-test-secret",
        )
    )
    services.models.add("fake", "test-model", "test-model")
    services.providers.use("fake")
    server = EngineServer(services, user_home=tmp_path)
    session = server._session_create({"chat": True})["session"]
    from pathlib import Path

    root = Path(session["current_cwd"])
    (root / "juego ñ.html").write_text(
        '<html><script src="/game.js"></script></html>', encoding="utf-8"
    )
    (root / "game.js").write_text('window.game = "Snake"', encoding="utf-8")
    params = {"session_id": session["id"], "path": "juego ñ.html"}
    yield server, root, params
    server.close()


def request(url, path=None, host=None, method="GET"):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
    try:
        connection.request(method, path or parsed.path, headers={"Host": host or parsed.netloc})
        response = connection.getresponse()
        return response.status, dict(response.headers), response.read()
    finally:
        connection.close()


def test_static_html_assets_revision_and_stop(preview):
    server, root, params = preview
    result = server._previews.start(params)
    assert result["ready"] and result["kind"] == "static"
    status, headers, html = request(result["url"])
    assert status == 200 and b"<script src=" in html
    assert b"/_rinari/revision" in html
    assert "sandbox" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert request(result["url"], "/game.js")[2] == b'window.game = "Snake"'
    lookup = {"session_id": params["session_id"], "preview_id": result["preview_id"]}
    (root / "game.js").write_text("window.game = 'Updated'", encoding="utf-8")
    assert server._previews.status(lookup)["revision"] == 1
    assert json.loads(request(result["url"], "/_rinari/revision")[2])["revision"] == 1
    (root / "game.js").unlink()
    assert server._previews.status(lookup)["revision"] == 2
    assert request(result["url"], "/game.js")[0] == 404
    server._previews.stop(lookup)
    with pytest.raises(EngineProtocolError):
        server._previews.status(lookup)


def test_server_rejects_other_hosts_paths_secrets_and_writes(preview, monkeypatch):
    server, root, params = preview
    (root / ".env").write_text("SECRET=hidden", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    result = server._previews.start(params)
    assert request(result["url"], host="evil.example")[0] == 403
    for path in ("/.env", "/../outside.html", "/%2e%2e/outside.html", "/C:%5csecret.html"):
        assert request(result["url"], path)[0] == 403
    assert request(result["url"], method="POST")[0] == 501
    monkeypatch.setattr("rinari.engine_protocol.preview.MAX_ASSET", 3)
    assert request(result["url"], "/game.js")[0] == 413
    with pytest.raises(EngineProtocolError):
        server._previews.status({"session_id": "another", "preview_id": result["preview_id"]})


def test_vite_requires_explicit_start_and_reuses_process_backend(preview, monkeypatch):
    server, root, params = preview
    (root / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    with pytest.raises(EngineProtocolError) as required:
        server._previews.start(params)
    assert required.value.code == "PREVIEW_DEV_REQUIRED"
    calls = []
    monkeypatch.setattr(
        server._previews.processes,
        "start",
        lambda command, cwd: calls.append((command, cwd)) or "proc_test",
    )
    result = server._previews.start({**params, "run_dev": True})
    assert result["kind"] == "development"
    assert calls[0][0].startswith("npm run dev -- --host 127.0.0.1 --port ")
    assert calls[0][1] == str(root)
    assert result["ready"] is False


def test_preview_keeps_historical_root_and_rejects_remote_servers(preview, tmp_path):
    server, _root, params = preview
    for url in (
        "https://example.com",
        "http://evil.localhost:8000",
        "http://localhost:bad",
        "http://user@localhost:8000",
    ):
        with pytest.raises(EngineProtocolError):
            server._previews.start({**params, "dev_url": url})
    result = server._previews.start(params)
    destination = tmp_path / "destination"
    destination.mkdir()
    project = server._services.projects.upsert(destination)
    server._desktop.move({"session_id": params["session_id"], "project_id": project.id})
    assert request(result["url"])[0] == 200
    server._previews.stop_session(params["session_id"])
    assert not server._previews._items


def test_other_framework_requests_existing_server_and_plan_cannot_spawn(preview):
    server, root, params = preview
    package = root / "package.json"
    package.write_text(json.dumps({"scripts": {"start": "react-scripts start"}}), encoding="utf-8")
    with pytest.raises(EngineProtocolError) as required:
        server._previews.start(params)
    assert required.value.code == "PREVIEW_SERVER_REQUIRED"
    package.write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    server._services.sessions.set_mode(params["session_id"], "plan")
    with pytest.raises(EngineProtocolError) as denied:
        server._previews.start({**params, "run_dev": True})
    assert denied.value.code == "PERMISSION_DENIED"
    assert not server._previews.processes.list()
