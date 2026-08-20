"""OpenAPI spec loading, validation, and operation extraction (phase 5)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class ApiSpecError(Exception):
    """Structured spec failure (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # SPEC_NOT_FOUND | SPEC_INVALID | FORMAT_UNSUPPORTED | NO_OPERATIONS
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    operation_id: str
    description: str = ""
    summary: str = ""
    parameters: tuple[dict[str, Any], ...] = ()
    request_body: dict[str, Any] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return self.operation_id


def load_spec_file(path: Path) -> SpecDocument:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ApiSpecError("SPEC_NOT_FOUND", f"spec file not found: {p}")
    suffix = p.suffix.lower()
    if suffix not in (".json",):
        raise ApiSpecError(
            "FORMAT_UNSUPPORTED",
            f"{p.name}: v1 loads JSON OpenAPI specs (convert YAML first; "
            "no YAML dependency is added)",
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiSpecError("SPEC_INVALID", f"cannot parse spec: {exc}") from exc
    errors = validate_spec(data)
    if errors:
        raise ApiSpecError("SPEC_INVALID", "; ".join(errors))
    return _extract(data)


def load_spec_url(url: str, client: Any) -> SpecDocument:
    import httpx

    try:
        response = client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        raise ApiSpecError("SPEC_NOT_FOUND", f"cannot fetch spec: {exc}") from exc
    if response.status_code != 200:
        raise ApiSpecError("SPEC_NOT_FOUND", f"spec fetch failed: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiSpecError("SPEC_INVALID", "spec is not valid JSON") from exc
    errors = validate_spec(data)
    if errors:
        raise ApiSpecError("SPEC_INVALID", "; ".join(errors))
    return _extract(data)


def validate_spec(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["spec must be a JSON object"]
    version = data.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        errors.append(f"openapi must be a 3.x string (got {version!r})")
    if not isinstance(data.get("paths"), dict):
        errors.append("missing 'paths' object")
    servers = data.get("servers")
    if servers is not None and (
        not isinstance(servers, list)
        or not all(isinstance(s, dict) and s.get("url") for s in servers)
    ):
        errors.append("'servers' must be a list of {url} entries")
    return errors


def _extract(data: dict) -> SpecDocument:
    base_url = str((data.get("servers") or [{"url": ""}])[0].get("url", ""))
    security_schemes: dict[str, Any] = {}
    components = data.get("components") or {}
    if isinstance(components, dict):
        security_schemes = dict(components.get("securitySchemes") or {})
    operations: list[Operation] = []
    for raw_path, item in (data.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        # Path-level parameters apply to every operation under this path.
        path_params = tuple(p for p in item.get("parameters") or [] if isinstance(p, dict))
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            operation_id = str(op.get("operationId") or "").strip()
            if not operation_id:
                operation_id = f"{method}_{_slug(raw_path)}"
            params = list(path_params)
            for p in op.get("parameters") or []:
                if isinstance(p, dict):
                    params.append(p)
            operations.append(
                Operation(
                    method=method,
                    path=raw_path,
                    operation_id=_safe(operation_id),
                    description=str(op.get("description") or op.get("summary") or ""),
                    summary=str(op.get("summary") or ""),
                    parameters=tuple(params),
                    request_body=op.get("requestBody")
                    if isinstance(op.get("requestBody"), dict)
                    else None,
                    tags=tuple(t for t in op.get("tags") or [] if isinstance(t, str)),
                )
            )
    if not operations:
        raise ApiSpecError("NO_OPERATIONS", "spec defines no operations")
    title = str(data.get("info", {}).get("title", "")) if isinstance(data.get("info"), dict) else ""
    return SpecDocument(
        base_url=base_url,
        title=title,
        security_schemes=security_schemes,
        global_security=tuple(data.get("security") or []),
        operations=tuple(operations),
    )


def _slug(text: str) -> str:
    return _NAME_RE.sub("_", text).strip("_") or "root"


def _safe(operation_id: str) -> str:
    return _NAME_RE.sub("_", operation_id)[:63] or "operation"


@dataclass(frozen=True, slots=True)
class SpecDocument:
    base_url: str
    title: str
    security_schemes: dict[str, Any]
    global_security: tuple[dict, ...]
    operations: tuple[Operation, ...]

    def operation_keys(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for op in self.operations:
            if op.key not in seen:
                seen.add(op.key)
                ordered.append(op.key)
        return ordered


__all__ = [
    "METHODS",
    "ApiSpecError",
    "Operation",
    "SpecDocument",
    "load_spec_file",
    "load_spec_url",
    "validate_spec",
]
