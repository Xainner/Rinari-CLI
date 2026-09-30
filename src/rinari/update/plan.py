"""What `rinari update` would do, piece by piece, with the reason."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from rinari.update.inventory import CliInstall, DesktopInstall
from rinari.update.releases import Release, parse_version

Action = Literal[
    "current",  # nothing to do
    "update",  # the app: install the release's setup
    "reinstall",  # a uv CLI: reinstall at the release's Engine commit
    "switch",  # an editable CLI: offer to replace it with a normal install
    "covered",  # the bundled CLI: it moves with the app
    "not-installed",
    "manual",  # installed in a way this command does not manage
]


@dataclass(frozen=True, slots=True)
class Step:
    target: Literal["desktop", "cli"]
    action: Action
    reason: str
    current: str | None = None
    target_version: str | None = None

    @property
    def pending(self) -> bool:
        return self.action in {"update", "reinstall", "switch"}

    def to_dict(self) -> dict:
        return {**asdict(self), "pending": self.pending}


def build_plan(
    cli: CliInstall,
    desktop: DesktopInstall | None,
    release: Release,
    *,
    desktop_only: bool = False,
    cli_only: bool = False,
) -> list[Step]:
    steps: list[Step] = []
    desktop_updates = False
    if desktop is None:
        desktop_step = Step("desktop", "not-installed", "Rinari Agent is not installed")
    elif desktop.version and parse_version(desktop.version) >= parse_version(release.version):
        desktop_step = Step("desktop", "current", "up to date", desktop.version, release.version)
    else:
        desktop_updates = True
        desktop_step = Step(
            "desktop", "update", "a newer release is published", desktop.version, release.version
        )
    if not cli_only:
        steps.append(desktop_step)

    short = release.engine_git_sha[:7]
    if cli.kind == "bundled":
        cli_step = (
            Step("cli", "covered", "bundled with the app: updates with it", None, short)
            if desktop_updates
            else Step("cli", "current", "bundled with the app", None, short)
        )
    elif cli.kind == "uv-git" and cli.commit == release.engine_git_sha:
        cli_step = Step("cli", "current", "at the release's Engine", short, short)
    elif cli.kind == "uv-git":
        cli_step = Step(
            "cli", "reinstall", "installed at another Engine commit", (cli.commit or "?")[:7], short
        )
    elif cli.kind == "editable":
        cli_step = Step(
            "cli",
            "switch",
            f"development checkout ({cli.source or 'editable'}); "
            "can switch to a normal install at the release's Engine",
            "editable",
            short,
        )
    else:
        cli_step = Step(
            "cli",
            "manual",
            f'not installed with uv; run: uv tool install --force "{release.cli_requirement}"',
            None,
            short,
        )
    if not desktop_only:
        steps.append(cli_step)
    return steps
