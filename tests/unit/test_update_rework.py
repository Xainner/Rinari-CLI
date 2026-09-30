"""`rinari update`: inventory, release, plan and apply — no network, no installs."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from rinari.update import apply as update_apply
from rinari.update.inventory import CliInstall, DesktopInstall, detect_cli, detect_desktop
from rinari.update.plan import build_plan
from rinari.update.releases import Release, ReleaseError, fetch_latest

SHA = "0fdaeb276776bd641ab87437afc62fc2ad3bf1a9"
OTHER = "cf3ac0c392d23d9e3bf9e50c5fa6aa942f753e10"


def _release(tmp_path: Path, payload: bytes = b"setup bytes", version: str = "0.2.1") -> Path:
    feed = tmp_path / "feed"
    feed.mkdir()
    name = f"Rinari-Agent-Setup-{version}-x64.exe"
    (feed / name).write_bytes(payload)
    digest = base64.b64encode(hashlib.sha512(payload).digest()).decode()
    manifest = {
        "schema": 1,
        "version": version,
        "engine_repository": "Xainner/Rinari-CLI",
        "engine_git_sha": SHA,
        "protocol_version": 1,
        "unsigned": True,
        "setup": {"name": name, "sha512": digest, "size": len(payload)},
    }
    (feed / "rinari-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return feed


def _app(tmp_path: Path, version: str = "0.2.0") -> Path:
    app = tmp_path / "Rinari Agent"
    (app / "resources" / "engine-dist").mkdir(parents=True)
    (app / "rinari-agent.exe").write_bytes(b"")
    (app / "resources" / "engine-dist" / "python.exe").write_bytes(b"")
    (app / ".rinari-install.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return app


# -- inventory ------------------------------------------------------------------


def test_the_cli_says_how_it_was_installed(tmp_path) -> None:
    app = _app(tmp_path)
    bundled = detect_cli(app / "resources" / "engine-dist" / "python.exe", lambda: None)
    assert bundled == CliInstall("bundled", source=str(app.resolve()))

    git = json.dumps(
        {
            "url": "https://github.com/Xainner/Rinari-CLI",
            "vcs_info": {"vcs": "git", "commit_id": SHA},
        }
    )
    assert detect_cli(tmp_path / "python.exe", lambda: git) == CliInstall(
        "uv-git", commit=SHA, source="https://github.com/Xainner/Rinari-CLI"
    )
    editable = json.dumps({"url": "file:///C:/DEV/RInari-CLI", "dir_info": {"editable": True}})
    assert detect_cli(tmp_path / "python.exe", lambda: editable).kind == "editable"
    assert detect_cli(tmp_path / "python.exe", lambda: None).kind == "other"


def test_the_desktop_comes_from_its_uninstall_entry_or_rinari_agent_bin(tmp_path) -> None:
    app = _app(tmp_path, version="0.2.0")
    found = detect_desktop(lambda: [(app, "0.1.9")], lambda: True, environ={})
    # The install marker is the truth; the registry value only fills a gap.
    assert found == DesktopInstall(app, "0.2.0", app / "rinari-agent.exe", True)

    by_env = detect_desktop(
        lambda: [], lambda: False, environ={"RINARI_AGENT_BIN": str(app / "rinari-agent.exe")}
    )
    assert by_env is not None and by_env.install_dir == app and by_env.running is False
    assert detect_desktop(lambda: [(tmp_path / "gone", "0.2.0")], lambda: False, environ={}) is None


# -- release --------------------------------------------------------------------


def test_the_release_is_read_from_a_local_feed(tmp_path) -> None:
    feed = _release(tmp_path)
    with httpx.Client() as client:
        release = fetch_latest(client, feed=str(feed))
    assert release.version == "0.2.1" and release.engine_git_sha == SHA
    assert release.setup_url == str(feed / "Rinari-Agent-Setup-0.2.1-x64.exe")
    assert release.cli_requirement == f"rinari @ git+https://github.com/Xainner/Rinari-CLI@{SHA}"


def test_the_release_is_read_from_github(tmp_path) -> None:
    feed = _release(tmp_path)
    manifest = (feed / "rinari-release.json").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "html_url": "https://github.com/Xainner/Rinari-Agent/releases/tag/v0.2.1",
                    "assets": [
                        {
                            "name": "rinari-release.json",
                            "browser_download_url": "https://dl/m.json",
                        },
                        {
                            "name": "Rinari-Agent-Setup-0.2.1-x64.exe",
                            "browser_download_url": "https://dl/setup.exe",
                        },
                    ],
                },
            )
        if str(request.url) == "https://dl/m.json":
            return httpx.Response(200, text=manifest)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        release = fetch_latest(client, feed="")
    assert release.setup_url == "https://dl/setup.exe"
    assert release.page.endswith("/v0.2.1")

    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client,
        pytest.raises(ReleaseError, match="no release"),
    ):
        fetch_latest(client, feed="")


# -- plan -----------------------------------------------------------------------


def _fake_release(version: str = "0.2.1") -> Release:
    return Release(version, "Xainner/Rinari-CLI", SHA, "s.exe", "s.exe", "x", 1)


def test_the_plan_names_each_piece_and_why(tmp_path) -> None:
    old = DesktopInstall(tmp_path, "0.2.0", tmp_path / "rinari-agent.exe", False)
    new = DesktopInstall(tmp_path, "0.2.1", tmp_path / "rinari-agent.exe", False)
    release = _fake_release()

    def actions(cli, desktop):
        return [(s.target, s.action) for s in build_plan(cli, desktop, release)]

    assert actions(CliInstall("bundled"), old) == [("desktop", "update"), ("cli", "covered")]
    assert actions(CliInstall("bundled"), new) == [("desktop", "current"), ("cli", "current")]
    assert actions(CliInstall("uv-git", commit=OTHER), new)[1] == ("cli", "reinstall")
    assert actions(CliInstall("uv-git", commit=SHA), None) == [
        ("desktop", "not-installed"),
        ("cli", "current"),
    ]
    assert actions(CliInstall("editable"), new)[1] == ("cli", "switch")
    assert actions(CliInstall("other"), new)[1] == ("cli", "manual")
    only_cli = build_plan(CliInstall("editable"), old, release, cli_only=True)
    assert [s.target for s in only_cli] == ["cli"]


# -- apply ----------------------------------------------------------------------


def _feed_release(feed: Path) -> Release:
    with httpx.Client() as client:
        return fetch_latest(client, feed=str(feed))


def test_the_installer_is_verified_before_use(tmp_path) -> None:
    release = _feed_release(_release(tmp_path))
    cache = tmp_path / "cache"
    with httpx.Client() as client:
        path = update_apply.download_setup(client, release, cache)
        assert path.read_bytes() == b"setup bytes"
        forged = Release(
            release.version,
            release.engine_repository,
            release.engine_git_sha,
            "forged.exe",
            release.setup_url,
            base64.b64encode(hashlib.sha512(b"other").digest()).decode(),
            release.setup_size,
        )
        with pytest.raises(ReleaseError, match="does not match"):
            update_apply.download_setup(client, forged, cache)
    assert not (cache / "forged.exe").exists()
    assert not (cache / "forged.exe.part").exists()


def test_an_open_app_is_asked_and_a_closed_one_gets_the_installer(tmp_path) -> None:
    release = _feed_release(_release(tmp_path))
    app = _app(tmp_path)
    launched: list[list[str]] = []
    ran: list[list[str]] = []

    def launch(args, **_):
        launched.append(args)

    def run(args, **_):
        ran.append(args)
        return SimpleNamespace(returncode=0)

    with httpx.Client() as client:
        open_app = DesktopInstall(app, "0.2.0", app / "rinari-agent.exe", True)
        result = update_apply.update_desktop(
            client, open_app, release, tmp_path / "c", detach=False, launch=launch, run=run
        )
        assert result == {"status": "handed-to-app"}
        assert launched == [[str(app / "rinari-agent.exe"), "--update"]] and ran == []

        closed = DesktopInstall(app, "0.2.0", app / "rinari-agent.exe", False)
        result = update_apply.update_desktop(
            client, closed, release, tmp_path / "c", detach=False, launch=launch, run=run
        )
        assert result["status"] == "updated"
        assert ran[-1][1:] == ["--updated"] and ran[-1][0].endswith(
            "Rinari-Agent-Setup-0.2.1-x64.exe"
        )

        # The bundled CLI lives inside the app: the installer is left running.
        result = update_apply.update_desktop(
            client, closed, release, tmp_path / "c", detach=True, launch=launch, run=run
        )
        assert result["status"] == "started" and launched[-1][1:] == ["--updated"]

        def failing(args, **_):
            return SimpleNamespace(returncode=3)

        with pytest.raises(ReleaseError, match="exit 3"):
            update_apply.update_desktop(
                client, closed, release, tmp_path / "c", detach=False, launch=launch, run=failing
            )


def test_the_cli_is_reinstalled_after_it_exits_on_windows(tmp_path) -> None:
    release = _fake_release()
    launched: list[list[str]] = []
    result = update_apply.reinstall_cli(
        release,
        tmp_path / "cli.log",
        uv="C:/uv/uv.exe",
        launch=lambda args, **_: launched.append(args),
        pid=11,
        parent=22,
        platform="win32",
    )
    assert result["status"] == "scheduled"
    script = launched[0][-1]
    # uv cannot replace a running tool on Windows: wait for this process and its launcher.
    assert script.startswith("Wait-Process -Id 11,22")
    assert f"'rinari @ git+https://github.com/Xainner/Rinari-CLI@{SHA}'" in script
    assert "-Encoding utf8" in script

    ran: list[list[str]] = []

    def run(args, **_):
        ran.append(args)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    posix = update_apply.reinstall_cli(
        release, tmp_path / "cli.log", uv="uv", run=run, platform="linux"
    )
    assert posix["status"] == "updated"
    assert ran == [["uv", "tool", "install", "--force", release.cli_requirement]]


# -- command --------------------------------------------------------------------


def test_the_command_reports_both_pieces(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from rinari.cli import main as cli_main
    from rinari.cli.commands import update_cmd

    feed = _release(tmp_path)
    app = _app(tmp_path, version="0.2.0")
    monkeypatch.setenv("RINARI_UPDATE_FEED", str(feed))
    monkeypatch.setenv("RINARI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(update_cmd, "detect_cli", lambda: CliInstall("editable", source="dev"))
    monkeypatch.setattr(
        update_cmd,
        "detect_desktop",
        lambda: DesktopInstall(app, "0.2.0", app / "rinari-agent.exe", False),
    )
    result = CliRunner().invoke(cli_main.app, ["--json", "update", "--check"])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)["data"]
    assert data["latest"] == "0.2.1" and data["update_available"] is True
    assert [(s["target"], s["action"]) for s in data["steps"]] == [
        ("desktop", "update"),
        ("cli", "switch"),
    ]
