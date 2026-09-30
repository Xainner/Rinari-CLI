"""What is published: the latest release of the desktop app."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

RELEASE_REPOSITORY = "Xainner/Rinari-Agent"
LATEST_URL = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
RELEASE_MANIFEST = "rinari-release.json"
#: A folder or URL with `rinari-release.json` and the setup next to it. Tests
#: and local rehearsals point here instead of GitHub.
FEED_ENV = "RINARI_UPDATE_FEED"


class ReleaseError(Exception):
    """The release could not be read; nothing is changed."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    engine_repository: str
    engine_git_sha: str
    setup_name: str
    setup_url: str
    setup_sha512: str
    setup_size: int
    page: str | None = None

    @property
    def cli_requirement(self) -> str:
        """uv requirement for this release's Engine, pinned to the exact commit."""
        return f"rinari @ git+https://github.com/{self.engine_repository}@{self.engine_git_sha}"


def parse_version(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version.split("+")[0].split("-")[0])
    return tuple(int(part) for part in parts) if parts else (0,)


def _manifest(data: object, setup_url: str, page: str | None) -> Release:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ReleaseError(f"{RELEASE_MANIFEST} has an unknown format")
    setup = data.get("setup")
    sha = data.get("engine_git_sha")
    if (
        not isinstance(data.get("version"), str)
        or not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or not isinstance(setup, dict)
        or not isinstance(setup.get("name"), str)
        or not isinstance(setup.get("sha512"), str)
        or not isinstance(setup.get("size"), int)
    ):
        raise ReleaseError(f"{RELEASE_MANIFEST} is incomplete")
    return Release(
        version=data["version"],
        engine_repository=str(data.get("engine_repository") or "Xainner/Rinari-CLI"),
        engine_git_sha=sha,
        setup_name=setup["name"],
        setup_url=setup_url.replace("{name}", setup["name"]),
        setup_sha512=setup["sha512"],
        setup_size=setup["size"],
        page=page,
    )


def fetch_latest(
    client: httpx.Client, *, feed: str | None = None, timeout_s: float = 15.0
) -> Release:
    """The latest published release, or ReleaseError. Never changes anything."""
    feed = feed if feed is not None else os.environ.get(FEED_ENV)
    try:
        if feed:
            return _from_feed(client, feed, timeout_s)
        response = client.get(
            LATEST_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout_s,
            follow_redirects=True,
        )
        if response.status_code == 404:
            raise ReleaseError("no release of Rinari Agent has been published yet")
        response.raise_for_status()
        release = response.json()
        assets = {
            asset.get("name"): asset.get("browser_download_url")
            for asset in release.get("assets", [])
            if isinstance(asset, dict)
        }
        manifest_url = assets.get(RELEASE_MANIFEST)
        if not isinstance(manifest_url, str):
            raise ReleaseError(f"the latest release has no {RELEASE_MANIFEST}")
        manifest = client.get(manifest_url, timeout=timeout_s, follow_redirects=True)
        manifest.raise_for_status()
        data = manifest.json()
        name = data.get("setup", {}).get("name") if isinstance(data, dict) else None
        setup_url = assets.get(name) if isinstance(name, str) else None
        if not isinstance(setup_url, str):
            raise ReleaseError("the latest release does not carry its installer")
        return _manifest(data, setup_url, release.get("html_url"))
    except httpx.HTTPError as exc:
        raise ReleaseError(f"could not reach GitHub: {exc}") from exc
    except ValueError as exc:
        raise ReleaseError(f"unreadable release data: {exc}") from exc


def _from_feed(client: httpx.Client, feed: str, timeout_s: float) -> Release:
    if feed.startswith(("http://", "https://")):
        base = feed.rstrip("/")
        response = client.get(f"{base}/{RELEASE_MANIFEST}", timeout=timeout_s)
        response.raise_for_status()
        return _manifest(response.json(), f"{base}/{{name}}", None)
    folder = Path(feed)
    try:
        data = json.loads((folder / RELEASE_MANIFEST).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"no {RELEASE_MANIFEST} in {folder}") from exc
    return _manifest(data, str(folder / "{name}"), None)
