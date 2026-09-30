"""Carry out the plan. Each function does one piece and says what happened."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from rinari.update.inventory import DesktopInstall
from rinari.update.releases import Release, ReleaseError

_DETACHED = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32"
    else 0
)


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def download_setup(client: httpx.Client, release: Release, cache: Path) -> Path:
    """The release's installer, verified by size and SHA-512 before use."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / release.setup_name
    if (
        target.is_file()
        and target.stat().st_size == release.setup_size
        and _sha512(target) == release.setup_sha512
    ):
        return target
    partial = target.with_name(target.name + ".part")
    source = release.setup_url
    try:
        if source.startswith(("http://", "https://")):
            with client.stream("GET", source, follow_redirects=True, timeout=60.0) as response:
                response.raise_for_status()
                with partial.open("wb") as out:
                    for chunk in response.iter_bytes():
                        out.write(chunk)
        else:
            shutil.copyfile(source, partial)
    except (httpx.HTTPError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise ReleaseError(f"could not download the installer: {exc}") from exc
    if partial.stat().st_size != release.setup_size or _sha512(partial) != release.setup_sha512:
        partial.unlink(missing_ok=True)
        raise ReleaseError("the downloaded installer does not match the release (size or SHA-512)")
    os.replace(partial, target)
    return target


def update_desktop(
    client: httpx.Client,
    desktop: DesktopInstall,
    release: Release,
    cache: Path,
    *,
    detach: bool,
    launch=subprocess.Popen,
    run=subprocess.run,
) -> dict:
    """Update the app.

    - Open: hand the request to the app, which asks with its own «Restart and
      update» dialog. Nothing closes without the owner.
    - Closed: verify the installer and run it in updater mode. `detach` when
      this very process lives inside the app folder (the bundled CLI): the
      installer waits for it to exit before replacing files.
    """
    if desktop.running:
        launch([str(desktop.executable), "--update"], creationflags=_DETACHED, close_fds=True)
        return {"status": "handed-to-app"}
    setup = download_setup(client, release, cache)
    command = [str(setup), "--updated"]
    if detach:
        launch(command, creationflags=_DETACHED, close_fds=True)
        return {"status": "started", "setup": str(setup)}
    result = run(command, check=False)
    if result.returncode != 0:
        raise ReleaseError(f"the installer failed (exit {result.returncode})")
    return {"status": "updated", "setup": str(setup)}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def reinstall_cli(
    release: Release,
    log: Path,
    *,
    uv: str | None = None,
    launch=subprocess.Popen,
    run=subprocess.run,
    pid: int | None = None,
    parent: int | None = None,
    platform: str = sys.platform,
) -> dict:
    """Reinstall this CLI with uv at the release's Engine commit.

    On Windows uv cannot replace a tool while it runs (its venv is in use:
    «Access is denied»), so a detached helper waits for this process and its
    launcher to exit, then reinstalls and writes the log.
    """
    uv = uv or shutil.which("uv")
    if not uv:
        raise ReleaseError(
            f'uv is not on PATH; run: uv tool install --force "{release.cli_requirement}"'
        )
    log.parent.mkdir(parents=True, exist_ok=True)
    if platform != "win32":
        result = run(
            [uv, "tool", "install", "--force", release.cli_requirement],
            capture_output=True,
            text=True,
            check=False,
        )
        log.write_text(
            f"{result.stdout}{result.stderr}\nexit={result.returncode}\n", encoding="utf-8"
        )
        if result.returncode != 0:
            raise ReleaseError(f"uv failed (exit {result.returncode}); see {log}")
        return {"status": "updated", "log": str(log)}
    waits = ",".join(str(p) for p in (pid or os.getpid(), parent or os.getppid()))
    log_path = _ps_quote(str(log))
    script = (
        f"Wait-Process -Id {waits} -ErrorAction SilentlyContinue; "
        f"& {_ps_quote(uv)} tool install --force {_ps_quote(release.cli_requirement)} 2>&1 "
        f"| Out-File -LiteralPath {log_path} -Encoding utf8; "
        f"Add-Content -LiteralPath {log_path} -Value ('exit=' + $LASTEXITCODE)"
    )
    launch(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=_DETACHED,
        close_fds=True,
    )
    return {"status": "scheduled", "log": str(log)}
