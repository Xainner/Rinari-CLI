"""Servers a trusted project folder brings, asked about once (plan de conectores C4).

Like Claude Code with `.mcp.json`: Rinari never uses the servers a folder
declares on its own. When the folder is trusted, `list` returns the ones
the owner has not answered yet; `decide` records each answer (accepted or
rejected) per project, so the question comes once per server. An accepted
server is created with project scope, tied to that folder
(`mcp_servers.project_root`), with its secrets in the credential store. A
definition that changed since the answer (another command, URL, env or
header names) is offered again, marked `changed`. A folder without trust
offers nothing and accepts nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rinari.shared.clock import now_iso
from rinari.storage.repositories.mcp_project_offers import McpProjectOfferRepository

from .config import McpConfigError
from .importers import ImportCandidate, row_fingerprint, scan_project, unique_name
from .service import canonical_root

ACCEPTED = "accepted"
REJECTED = "rejected"


class ProjectNotTrustedError(PermissionError):
    """Offers are only made, and answered, in a trusted folder."""


def existing_by_fingerprint(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """`fingerprint → name` of the saved servers (first one wins)."""
    found: dict[str, str] = {}
    for row in rows:
        found.setdefault(row_fingerprint(row), str(row.get("name")))
    return found


def create_server(
    mcp: Any,
    candidate: ImportCandidate,
    name: str,
    *,
    scope: str = "global",
    project_root: str | None = None,
) -> dict:
    """Create the server a candidate describes; refuses to replace one."""
    if not candidate.importable:
        raise McpConfigError(f"{candidate.name} cannot be imported ({candidate.reason})")
    if mcp.show(name) is not None or mcp.show(name, "project") is not None:
        raise McpConfigError(f"an MCP server named {name!r} already exists")
    params = dict(candidate.params)
    return mcp.add(
        name,
        params.pop("command", None),
        scope=scope,
        transport=params.pop("transport", None),
        url=params.pop("url", None),
        env=params.pop("env", None),
        auth=params.pop("auth", None),
        headers=params.pop("headers", None),
        project_root=project_root,
    )


class ProjectOffers:
    def __init__(self, ctx: Any, mcp: Any, trust: Any) -> None:
        self._ctx = ctx
        self._mcp = mcp
        self._trust = trust
        self._repo = McpProjectOfferRepository(ctx.db)

    def _root(self, project: str | Path) -> tuple[Path, str]:
        path = Path(project).expanduser().resolve()
        return path, canonical_root(path)

    def list(self, project: str | Path) -> dict[str, Any]:
        """`{project, trusted, offers, answered, files}`; empty for an untrusted folder."""
        path, root = self._root(project)
        trusted = path.is_dir() and self._trust.is_trusted(path)
        if not trusted:
            return {
                "project": str(path),
                "trusted": False,
                "offers": [],
                "answered": 0,
                "files": [],
            }
        candidates, reports = scan_project(path)
        decisions = self._repo.for_project(root)
        existing = existing_by_fingerprint(self._mcp.list())
        taken = [row["name"] for row in self._mcp.list()]
        offers = []
        answered = 0
        for candidate in candidates:
            decision = decisions.get(candidate.key)
            if decision is not None and decision["fingerprint"] == candidate.fingerprint:
                answered += 1
                continue
            view = candidate.view()
            view["changed"] = decision is not None
            view["previous_decision"] = decision["decision"] if decision is not None else None
            view["suggested_name"] = unique_name(candidate.name, taken)
            view["existing"] = existing.get(candidate.fingerprint)
            offers.append(view)
        return {
            "project": str(path),
            "trusted": True,
            "offers": offers,
            "answered": answered,
            "files": [report.view() for report in reports if report.found],
        }

    def decide(self, project: str | Path, decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Record answers `[{key, accept, name?}]`; accepted servers are created.

        Each answer is applied on its own: one that fails (a name taken, a
        server that cannot be imported) is reported and left unanswered, so
        it is offered again. Raises ProjectNotTrustedError for an untrusted
        folder and LookupError when a key is not in the folder any more.
        """
        path, root = self._root(project)
        if not (path.is_dir() and self._trust.is_trusted(path)):
            raise ProjectNotTrustedError(str(path))
        candidates = {c.key: c for c in scan_project(path)[0]}
        answers = list(decisions)
        for answer in answers:
            if answer.get("key") not in candidates:
                raise LookupError(str(answer.get("key")))
        created: list[dict[str, Any]] = []
        rejected: list[str] = []
        failed: list[dict[str, Any]] = []
        taken = [row["name"] for row in self._mcp.list()]
        for answer in answers:
            candidate = candidates[answer["key"]]
            if not answer.get("accept"):
                self._record(root, candidate, REJECTED, "")
                rejected.append(candidate.key)
                continue
            name = answer.get("name") or unique_name(candidate.name, taken)
            try:
                row = create_server(
                    self._mcp, candidate, name, scope="project", project_root=str(path)
                )
            except McpConfigError as exc:
                failed.append({"key": candidate.key, "message": str(exc)})
                continue
            taken.append(row["name"])
            self._record(root, candidate, ACCEPTED, row["name"])
            created.append({"key": candidate.key, "row": row})
        return {"project": str(path), "created": created, "rejected": rejected, "failed": failed}

    def _record(self, root: str, candidate: ImportCandidate, decision: str, name: str) -> None:
        self._repo.record(
            root,
            candidate.key,
            source=candidate.path or "",
            fingerprint=candidate.fingerprint,
            decision=decision,
            server_name=name,
            decided_at=now_iso(self._ctx.clock),
        )


__all__ = [
    "ACCEPTED",
    "REJECTED",
    "ProjectNotTrustedError",
    "ProjectOffers",
    "create_server",
    "existing_by_fingerprint",
]
