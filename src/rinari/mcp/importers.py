"""Import MCP server definitions from other tools (plan de conectores C4).

Like Claude Code, nothing is imported on its own: a scan lists candidates
and only the ones the owner marks are created. Sources:

- other apps on this PC: Claude Desktop, Claude Code, Cursor, VS Code and
  Codex (paths for Windows, macOS and Linux, from the home folder and the
  environment of the process; nothing specific to any owner);
- a pasted `mcpServers` JSON (also VS Code's `servers` and a bare map);
- a trusted project's `.mcp.json`, `.rinari/mcp.json`, `.vscode/mcp.json`
  and `.cursor/mcp.json` (`project_offers.py`).

A candidate's public view carries the server's shape and the *names* of the
secrets it brings, never a value. Values only live in `ImportCandidate.params`
until `McpService.add` stores them in the credential store: env values,
header values, the token of `Authorization: Bearer …`, user and password of
a URL (as a Basic header) and secret query values of a URL (`url_ref`).
`${VAR}` and `${env:VAR}` become `env://VAR` references; any other variable
(`${input:…}`, `${workspaceFolder}`, a variable inside a longer value) cannot
be resolved here and is reported. A command line that carries what looks
like a secret is not importable: argv lands in the row and the views.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import (
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    McpConfigError,
    draft_from_row,
    redact_url,
    validate_url,
)

SOURCE_CLAUDE_DESKTOP = "claude_desktop"
SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_CURSOR = "cursor"
SOURCE_VSCODE = "vscode"
SOURCE_CODEX = "codex"
SOURCE_PASTED = "pasted"
SOURCE_PROJECT = "project"
APP_SOURCES = (
    SOURCE_CLAUDE_DESKTOP,
    SOURCE_CLAUDE_CODE,
    SOURCE_CURSOR,
    SOURCE_VSCODE,
    SOURCE_CODEX,
)
SOURCE_LABELS = {
    SOURCE_CLAUDE_DESKTOP: "Claude Desktop",
    SOURCE_CLAUDE_CODE: "Claude Code",
    SOURCE_CURSOR: "Cursor",
    SOURCE_VSCODE: "VS Code",
    SOURCE_CODEX: "Codex",
    SOURCE_PASTED: "JSON",
    SOURCE_PROJECT: "Project",
}

#: Files a project folder may bring, in the order that wins on a repeated name.
PROJECT_FILES = (".mcp.json", ".rinari/mcp.json", ".vscode/mcp.json", ".cursor/mcp.json")

#: Largest config file read (a `~/.claude.json` with a long history is big).
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PASTED_CHARS = 512 * 1024

_VAR_FULL_RE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}$")
_BEARER_VAR_RE = re.compile(
    r"^Bearer\s+\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}$", re.IGNORECASE
)
_VAR_ANY_RE = re.compile(r"\$\{[^}]*\}")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Command-line arguments that look like a secret.
_SECRET_FLAG_RE = re.compile(
    r"^--?(?:api[-_]?key|access[-_]?token|token|secret|password|passwd|auth(?:[-_]token)?|"
    r"client[-_]secret|bearer)(?:=.+)?$",
    re.IGNORECASE,
)
_URL_PASSWORD_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]*:[^@\s/]+@", re.IGNORECASE)
_TOKEN_SHAPE_RE = re.compile(
    r"^(?:sk-|ghp_|gho_|github_pat_|xox[abpr]-|glpat-|AKIA)[A-Za-z0-9_\-]{8,}", re.IGNORECASE
)
_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class ImportCandidate:
    """One server found in a source. `params` holds values: never in a view."""

    key: str
    name: str
    source: str
    path: str | None
    transport: str | None
    command: list[str] = field(default_factory=list)
    url: str = ""
    secrets: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None
    project_root: str | None = None
    params: dict[str, Any] = field(default_factory=dict, repr=False)
    fingerprint: str = ""

    @property
    def importable(self) -> bool:
        return self.reason is None

    def view(self) -> dict[str, Any]:
        """Presentation-safe: shape and secret names, never a value."""
        return {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "path": self.path,
            "project_root": self.project_root,
            "transport": self.transport,
            "command": list(self.command),
            "url": self.url,
            "secrets": [dict(item) for item in self.secrets],
            "warnings": list(self.warnings),
            "importable": self.importable,
            "reason": self.reason,
        }


@dataclass
class SourceReport:
    """A place a scan looked at, and what it found there (no content)."""

    source: str
    path: str
    found: bool
    servers: int = 0
    error: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": SOURCE_LABELS.get(self.source, self.source),
            "path": self.path,
            "found": self.found,
            "servers": self.servers,
            "error": self.error,
        }


# -- where other apps keep their configuration --------------------------------------


def app_config_paths(
    home: Path, env: Mapping[str, str], platform: str | None = None
) -> list[tuple[str, Path, str]]:
    """`(source, path, flavor)` of every place another app may keep MCP servers.

    `flavor`: `mcpServers` (Claude Desktop, Cursor), `claude_code`
    (`~/.claude.json`), `vscode_mcp` (`servers`), `vscode_settings`
    (`settings.json` → `mcp.servers`) or `codex` (TOML).
    """
    platform = platform or sys.platform
    paths: list[tuple[str, Path, str]] = []
    if platform.startswith("win"):
        appdata = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
        local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
        config_dirs = [appdata]
        paths.append(
            (SOURCE_CLAUDE_DESKTOP, appdata / "Claude" / "claude_desktop_config.json", "mcpServers")
        )
        # The Microsoft Store build keeps its roaming folder in its package.
        packages = local / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("Claude_*")):
                paths.append(
                    (
                        SOURCE_CLAUDE_DESKTOP,
                        package
                        / "LocalCache"
                        / "Roaming"
                        / "Claude"
                        / "claude_desktop_config.json",
                        "mcpServers",
                    )
                )
    elif platform == "darwin":
        support = home / "Library" / "Application Support"
        config_dirs = [support]
        paths.append(
            (SOURCE_CLAUDE_DESKTOP, support / "Claude" / "claude_desktop_config.json", "mcpServers")
        )
    else:
        xdg = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
        config_dirs = [xdg]
        paths.append(
            (SOURCE_CLAUDE_DESKTOP, xdg / "Claude" / "claude_desktop_config.json", "mcpServers")
        )
    claude_dir = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
    if claude_dir is not None:
        paths.append((SOURCE_CLAUDE_CODE, claude_dir / ".claude.json", "claude_code"))
    paths.append((SOURCE_CLAUDE_CODE, home / ".claude.json", "claude_code"))
    paths.append((SOURCE_CURSOR, home / ".cursor" / "mcp.json", "mcpServers"))
    for base in config_dirs:
        for product in ("Code", "Code - Insiders"):
            user = base / product / "User"
            paths.append((SOURCE_VSCODE, user / "mcp.json", "vscode_mcp"))
            paths.append((SOURCE_VSCODE, user / "settings.json", "vscode_settings"))
    codex_home = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    paths.append((SOURCE_CODEX, codex_home / "config.toml", "codex"))
    seen: set[str] = set()
    unique = []
    for source, path, flavor in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append((source, path, flavor))
    return unique


# -- reading files -------------------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (VS Code) → plain JSON."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read(path: Path, flavor: str) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    text = path.read_text(encoding="utf-8-sig")
    if flavor == "codex":
        return tomllib.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(text))


def _servers_in(document: Any, flavor: str) -> list[tuple[str, Any, str | None]]:
    """`(name, spec, project_root)` of every server a parsed file declares."""
    if not isinstance(document, dict):
        return []
    found: list[tuple[str, Any, str | None]] = []

    def take(block: Any, project: str | None = None) -> None:
        if isinstance(block, dict):
            found.extend((str(name), spec, project) for name, spec in block.items())

    if flavor == "codex":
        take(document.get("mcp_servers"))
    elif flavor == "vscode_mcp":
        take(document.get("servers"))
    elif flavor == "vscode_settings":
        mcp = document.get("mcp")
        if isinstance(mcp, dict):
            take(mcp.get("servers"))
        take(document.get("mcp.servers"))
    elif flavor == "claude_code":
        take(document.get("mcpServers"))
        projects = document.get("projects")
        if isinstance(projects, dict):
            for root, data in sorted(projects.items()):
                if isinstance(data, dict):
                    take(data.get("mcpServers"), str(root))
    else:  # mcpServers, or a pasted document of any of the JSON shapes
        if isinstance(document.get("mcpServers"), dict):
            take(document["mcpServers"])
        elif isinstance(document.get("servers"), dict):
            take(document["servers"])
        elif isinstance(document.get("mcp"), dict) and isinstance(
            document["mcp"].get("servers"), dict
        ):
            take(document["mcp"]["servers"])
        elif document and all(
            isinstance(v, dict) and ("command" in v or "url" in v) for v in document.values()
        ):
            take(document)  # a bare {name: spec} map
    return found


# -- one server → McpService.add arguments ---------------------------------------------


def _secret_input(value: Any) -> tuple[Any, bool]:
    """A config value as a secret input: literal, env:// reference, or unresolvable."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    match = _VAR_FULL_RE.match(value.strip())
    if match:
        return f"env://{match.group(1)}", True
    if _VAR_ANY_RE.search(value):
        return None, False
    return value, True


def _looks_secret_arg(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if _URL_PASSWORD_RE.search(arg) or _TOKEN_SHAPE_RE.match(arg):
            return True
        if _SECRET_FLAG_RE.match(arg) and ("=" in arg or index + 1 < len(args)):
            return True
    return False


def _fingerprint(
    transport: str | None, command: list[str], url: str, env: Iterable[str], headers: Iterable[str]
) -> str:
    """Non-secret shape: what a definition is, without any value."""
    parts = urlsplit(url) if url else None
    base = (
        urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        if parts
        else ""
    )
    shape = {
        "transport": transport,
        "command": list(command),
        "url": base.rstrip("/"),
        "env": sorted(env),
        "headers": sorted(h.lower() for h in headers),
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()


def row_fingerprint(row: Mapping[str, Any]) -> str:
    """The same fingerprint for a saved server (to tell «ya está en Rinari»)."""
    draft = draft_from_row(row)
    if draft.transport == TRANSPORT_STDIO:
        return _fingerprint(TRANSPORT_STDIO, draft.argv, "", draft.env, ())
    headers = list(draft.headers)
    if draft.auth_kind == "bearer":
        headers.append("authorization")
    return _fingerprint(TRANSPORT_HTTP, [], draft.url, (), headers)


def candidate_from_spec(
    name: str,
    spec: Any,
    *,
    source: str,
    key: str,
    path: str | None = None,
    project_root: str | None = None,
    flavor: str = "mcpServers",
) -> ImportCandidate:
    """Normalize one server of any supported format."""
    candidate = ImportCandidate(
        key=key,
        name=name,
        source=source,
        path=path,
        transport=None,
        project_root=project_root,
    )
    if not isinstance(spec, dict):
        candidate.reason = "invalid_definition"
        return candidate
    kind = str(spec.get("type") or spec.get("transport") or "").lower()
    if spec.get("disabled") is True or spec.get("enabled") is False:
        candidate.warnings.append("disabled_in_source")
    if kind == "sse" or (
        not kind
        and isinstance(spec.get("url"), str)
        and urlsplit(spec["url"]).path.rstrip("/").endswith("/sse")
    ):
        candidate.transport = TRANSPORT_HTTP
        candidate.url = redact_url(str(spec.get("url") or ""))
        candidate.reason = "legacy_sse_unsupported"
        return _with_fingerprint(candidate)
    if spec.get("command"):
        _stdio(candidate, spec, flavor)
    elif spec.get("url") or spec.get("serverUrl"):
        _http(candidate, spec, flavor)
    else:
        candidate.reason = "invalid_definition"
    return _with_fingerprint(candidate)


def _with_fingerprint(candidate: ImportCandidate) -> ImportCandidate:
    """A server that cannot be imported still has a shape to tell when it changes."""
    if not candidate.fingerprint:
        candidate.fingerprint = _fingerprint(
            candidate.transport, candidate.command, candidate.url, (), ()
        )
    return candidate


def _stdio(candidate: ImportCandidate, spec: dict, flavor: str) -> None:
    candidate.transport = TRANSPORT_STDIO
    command = spec.get("command")
    args = spec.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list):
        candidate.reason = "invalid_definition"
        return
    argv = [command, *(str(a) for a in args)]
    candidate.command = argv
    if _looks_secret_arg(argv):
        # argv is kept in the row and shown: a secret there would be visible.
        candidate.command = [argv[0], *("…" for _ in argv[1:])]
        candidate.reason = "secret_in_command"
        return
    if any(_VAR_ANY_RE.search(a) for a in argv):
        candidate.warnings.append("unresolved_variable:command")
    env: dict[str, Any] = {}
    raw_env = spec.get("env") or {}
    if not isinstance(raw_env, dict):
        candidate.reason = "invalid_definition"
        return
    for env_name, value in raw_env.items():
        if not _ENV_NAME_RE.match(str(env_name)):
            candidate.warnings.append("invalid_env_name")
            continue
        secret, ok = _secret_input(value)
        if not ok:
            candidate.warnings.append(f"unresolved_variable:env.{env_name}")
            continue
        if secret == "":
            continue
        env[str(env_name)] = secret
        candidate.secrets.append({"where": "env", "name": str(env_name), "kind": _kind(secret)})
    if spec.get("cwd"):
        candidate.warnings.append("cwd_ignored")
    if spec.get("envFile") or spec.get("env_file"):
        candidate.warnings.append("env_file_ignored")
    candidate.params = {"transport": TRANSPORT_STDIO, "command": argv, "env": env or None}
    candidate.fingerprint = _fingerprint(TRANSPORT_STDIO, argv, "", env, ())


def _kind(secret: Any) -> str:
    return "env_ref" if isinstance(secret, str) and secret.startswith("env://") else "value"


def _http(candidate: ImportCandidate, spec: dict, flavor: str) -> None:
    candidate.transport = TRANSPORT_HTTP
    raw_url = str(spec.get("url") or spec.get("serverUrl") or "").strip()
    if _VAR_ANY_RE.search(raw_url):
        candidate.url = redact_url(_VAR_ANY_RE.sub("…", raw_url))
        candidate.reason = "unresolved_variable"
        return
    headers: dict[str, Any] = {}
    auth: dict[str, Any] | None = None
    parts = urlsplit(raw_url)
    if parts.username or parts.password:
        # user:password@host → a Basic header in the store; the URL goes without it.
        user = parts.username or ""
        password = parts.password or ""
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        candidate.secrets.append({"where": "url", "name": "userinfo", "kind": "value"})
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        raw_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    try:
        url = validate_url(raw_url)
    except McpConfigError:
        candidate.url = redact_url(raw_url) if raw_url.startswith("http") else ""
        candidate.reason = "invalid_url"
        return
    display = redact_url(url)
    candidate.url = display
    if display != url:
        candidate.secrets.append({"where": "url", "name": "query", "kind": "value"})
    raw_headers = spec.get("headers") or spec.get("http_headers") or {}
    if not isinstance(raw_headers, dict):
        candidate.reason = "invalid_definition"
        return
    for header, value in raw_headers.items():
        header = str(header)
        text = "" if value is None else str(value)
        bearer = _BEARER_VAR_RE.match(text.strip())
        if header.lower() == "authorization" and bearer:
            auth = {"kind": "bearer", "token": f"env://{bearer.group(1)}"}
            candidate.secrets.append({"where": "header", "name": header, "kind": "env_ref"})
            continue
        secret, ok = _secret_input(text)
        if not ok:
            candidate.warnings.append(f"unresolved_variable:headers.{header}")
            continue
        if secret == "":
            continue
        # Every imported header value is kept as a secret: the source cannot say.
        headers[header] = {"value": secret, "secret": True}
        candidate.secrets.append({"where": "header", "name": header, "kind": _kind(secret)})
    # Codex: bearer token and headers read from the environment.
    bearer_env = spec.get("bearer_token_env_var")
    if isinstance(bearer_env, str) and _ENV_NAME_RE.match(bearer_env):
        auth = {"kind": "bearer", "token": f"env://{bearer_env}"}
        candidate.secrets.append({"where": "header", "name": "Authorization", "kind": "env_ref"})
    env_headers = spec.get("env_http_headers") or {}
    if isinstance(env_headers, dict):
        for header, var in env_headers.items():
            if isinstance(var, str) and _ENV_NAME_RE.match(var):
                headers[str(header)] = {"value": f"env://{var}", "secret": True}
                candidate.secrets.append(
                    {"where": "header", "name": str(header), "kind": "env_ref"}
                )
    for found in [h for h in headers if h.lower() == "authorization"]:
        if auth is not None:  # an env bearer wins over a literal Authorization
            headers.pop(found)
    if "Authorization" in headers and isinstance(headers["Authorization"], str):
        headers["Authorization"] = {"value": headers["Authorization"], "secret": True}
    params: dict[str, Any] = {"transport": TRANSPORT_HTTP, "url": url}
    if auth is not None:
        params["auth"] = auth
    if headers:
        params["headers"] = headers
    candidate.params = params
    header_names = list(headers) + (["authorization"] if auth is not None else [])
    candidate.fingerprint = _fingerprint(TRANSPORT_HTTP, [], display, (), header_names)


# -- scans -----------------------------------------------------------------------------


def _key(source: str, name: str, project_root: str | None = None) -> str:
    if project_root:
        digest = hashlib.sha256(project_root.encode("utf-8")).hexdigest()[:8]
        return f"{source}:{digest}:{name}"
    return f"{source}:{name}"


def scan_apps(
    home: Path,
    env: Mapping[str, str],
    *,
    platform: str | None = None,
    sources: Iterable[str] | None = None,
) -> tuple[list[ImportCandidate], list[SourceReport]]:
    """Every server other apps on this PC declare (reads only, never writes)."""
    wanted = set(sources) if sources is not None else set(APP_SOURCES)
    candidates: list[ImportCandidate] = []
    reports: list[SourceReport] = []
    keys: set[str] = set()
    for source, path, flavor in app_config_paths(home, env, platform):
        if source not in wanted:
            continue
        report = SourceReport(source=source, path=str(path), found=path.is_file())
        reports.append(report)
        if not report.found:
            continue
        try:
            document = _read(path, flavor)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            report.error = "file_too_large" if str(exc) == "file_too_large" else "unreadable"
            continue
        for name, spec, project in _servers_in(document, flavor):
            key = _key(source, name, project)
            if key in keys:
                continue  # the same server in a second file of the same app
            keys.add(key)
            report.servers += 1
            candidates.append(
                candidate_from_spec(
                    name,
                    spec,
                    source=source,
                    key=key,
                    path=str(path),
                    project_root=project,
                    flavor=flavor,
                )
            )
    return candidates, reports


def parse_pasted(text: Any) -> list[ImportCandidate]:
    """Servers of a pasted JSON (`mcpServers`, `servers` or a bare map)."""
    if not isinstance(text, str) or not text.strip():
        raise McpConfigError("Param 'json' must be a non-empty string")
    if len(text) > MAX_PASTED_CHARS:
        raise McpConfigError("Param 'json' is too long")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            document = json.loads(strip_jsonc(text))
        except json.JSONDecodeError as exc:
            # Position only: the text may carry secrets.
            raise McpConfigError(
                f"Param 'json' is not valid JSON (line {exc.lineno}, column {exc.colno})"
            ) from None
    servers = _servers_in(document, "mcpServers")
    if not servers:
        raise McpConfigError("Param 'json' has no MCP servers (mcpServers or servers)")
    return [
        candidate_from_spec(name, spec, source=SOURCE_PASTED, key=_key(SOURCE_PASTED, name))
        for name, spec, _project in servers
    ]


def scan_project(root: Path) -> tuple[list[ImportCandidate], list[SourceReport]]:
    """Servers a project folder brings; a name repeated in a later file is skipped."""
    candidates: list[ImportCandidate] = []
    reports: list[SourceReport] = []
    names: set[str] = set()
    for relative in PROJECT_FILES:
        path = root / relative
        report = SourceReport(source=SOURCE_PROJECT, path=relative, found=path.is_file())
        reports.append(report)
        if not report.found:
            continue
        flavor = "vscode_mcp" if relative == ".vscode/mcp.json" else "mcpServers"
        try:
            document = _read(path, flavor)
        except (OSError, ValueError) as exc:
            report.error = "file_too_large" if str(exc) == "file_too_large" else "unreadable"
            continue
        if flavor == "vscode_mcp" and not (
            isinstance(document, dict) and isinstance(document.get("servers"), dict)
        ):
            flavor = "mcpServers"
        for name, spec, _project in _servers_in(document, flavor):
            if name in names:
                continue
            names.add(name)
            report.servers += 1
            candidates.append(
                candidate_from_spec(
                    name, spec, source=SOURCE_PROJECT, key=name, path=relative, flavor=flavor
                )
            )
    return candidates, reports


# -- names ---------------------------------------------------------------------------------


def clean_name(name: str) -> str:
    cleaned = _NAME_CLEAN_RE.sub("-", name.strip()).strip("-_")
    return cleaned[:48] or "server"


def unique_name(name: str, taken: Iterable[str]) -> str:
    """`name` cleaned, with `-2`, `-3`… when Rinari already has it."""
    used = {t.lower() for t in taken}
    base = clean_name(name)
    if base.lower() not in used:
        return base
    index = 2
    while f"{base}-{index}".lower() in used:
        index += 1
    return f"{base}-{index}"


__all__ = [
    "APP_SOURCES",
    "PROJECT_FILES",
    "ImportCandidate",
    "SourceReport",
    "app_config_paths",
    "candidate_from_spec",
    "clean_name",
    "parse_pasted",
    "row_fingerprint",
    "scan_apps",
    "scan_project",
    "strip_jsonc",
    "unique_name",
]
