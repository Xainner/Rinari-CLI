"""ApiService: registered OpenAPI specs + generated tools + invocations.

Application-level facade over the `api_specs` table. Specs are loaded from
JSON files (or URLs); generated tools attach a handler that reconstructs the
HTTP request, resolves `env://` secrets from the process environment, passes
the target through the NetworkGuard, and returns a bounded payload.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.trust import TrustService

from .spec import (
    ApiSpecError,
    Operation,
    SpecDocument,
    load_spec_file,
    load_spec_url,
)
from .tools import spec_tool_definitions


class ApiCallError(Exception):
    """Structured invocation failure (code + message)."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        # AUTH_REQUIRED | INVALID_ARGUMENT | NETWORK_ERROR | TIMEOUT
        # | UPSTREAM_ERROR | SPEC_STALE | SERVER_NOT_FOUND
        self.code = code
        self.message = message
        self.retryable = retryable


def _now(ctx: AppContext) -> str:
    return now_iso(ctx.clock)


def _spec_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class _InvocationEnv:
    client: Any
    network_guard: Any = None
    timeout_s: float = 30.0


class ApiService:
    def __init__(self, ctx: AppContext, trust: TrustService) -> None:
        self._ctx = ctx
        self._trust = trust

    # -- registry --------------------------------------------------------------

    def add(
        self,
        name: str,
        path: Path,
        *,
        scope: str = "global",
        overrides: dict[str, Any] | None = None,
        auth: dict[str, str] | None = None,
    ) -> dict:
        doc = load_spec_file(path)
        raw = (Path(path).expanduser()).read_text(encoding="utf-8")
        auth_json = _validate_auth_refs(auth or {})
        base_url = doc.base_url
        row = self._ctx.api_spec_repo.add(
            self._ctx.ids.new("api"),
            name=name,
            origin="file",
            path=str(Path(path).expanduser().resolve()),
            url=base_url,
            scope=scope,
            auth_json=json.dumps(auth_json, sort_keys=True),
            overrides_json=json.dumps(overrides or {}, sort_keys=True),
            spec_hash=_spec_hash(raw),
            created_at=_now(self._ctx),
        )
        return row

    def add_url(
        self,
        name: str,
        url: str,
        client: Any,
        *,
        scope: str = "global",
        overrides: dict[str, Any] | None = None,
        auth: dict[str, str] | None = None,
    ) -> dict:
        load_spec_url(url, client)  # validate reachability + shape before storing
        auth_json = _validate_auth_refs(auth or {})
        row = self._ctx.api_spec_repo.add(
            self._ctx.ids.new("api"),
            name=name,
            origin="url",
            url=url,
            scope=scope,
            auth_json=json.dumps(auth_json, sort_keys=True),
            overrides_json=json.dumps(overrides or {}, sort_keys=True),
            spec_hash="",
            created_at=_now(self._ctx),
        )
        return row

    def remove(self, name: str, scope: str = "global") -> bool:
        return self._ctx.api_spec_repo.delete(name, scope)

    def enable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.api_spec_repo.set_enabled(name, scope, True, _now(self._ctx))
        return self._ctx.api_spec_repo.find(name, scope) if ok else None

    def disable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.api_spec_repo.set_enabled(name, scope, False, _now(self._ctx))
        return self._ctx.api_spec_repo.find(name, scope) if ok else None

    def list(self, scope: str | None = None) -> list[dict]:
        rows = self._ctx.api_spec_repo.list(scope)
        for row in rows:
            try:
                row["overrides"] = json.loads(row.get("overrides_json") or "{}")
            except json.JSONDecodeError:
                row["overrides"] = {}
        return rows

    def show(self, name: str, scope: str = "global") -> dict | None:
        row = self._ctx.api_spec_repo.find(name, scope)
        if row is None:
            return None
        try:
            row["auth"] = json.loads(row.get("auth_json") or "{}")
            row["overrides"] = json.loads(row.get("overrides_json") or "{}")
        except json.JSONDecodeError:
            row["auth"], row["overrides"] = {}, {}
        return row

    # -- spec handling -----------------------------------------------------------

    def _load(self, row: dict, http_client: Any | None = None) -> SpecDocument:
        if row["origin"] == "file":
            doc = load_spec_file(Path(row["path"]))
            raw = Path(row["path"]).read_text(encoding="utf-8")
            current = _spec_hash(raw)
            if row.get("spec_hash") and row["spec_hash"] != current:
                self._ctx.api_spec_repo.set_hash(
                    row["name"], row["scope"] or "global", current, _now(self._ctx)
                )
            return doc
        if http_client is None:
            raise ApiCallError("SPEC_STALE", "URL-backed spec needs an HTTP client to refresh")
        return load_spec_url(row["url"], http_client)

    def validate(self, name: str, scope: str = "global") -> dict:
        row = self._ctx.api_spec_repo.find(name, scope)
        if row is None:
            raise LookupError(f"api spec not found: {name}")
        try:
            doc = self._load(row)
        except ApiSpecError as exc:
            return {"ok": False, "code": exc.code, "message": exc.message, "operations": 0}
        return {"ok": True, "operations": len(doc.operations), "title": doc.title}

    def refresh(self, name: str, scope: str = "global", http_client: Any | None = None) -> dict:
        row = self._ctx.api_spec_repo.find(name, scope)
        if row is None:
            raise LookupError(f"api spec not found: {name}")
        doc = self._load(row, http_client)
        overrides = json.loads(row.get("overrides_json") or "{}")
        defs = spec_tool_definitions(row["name"], doc, overrides)
        return {"name": row["name"], "operations": len(doc.operations), "tools": len(defs)}

    # -- tools -------------------------------------------------------------------

    def tool_definitions(
        self,
        name: str,
        scope: str = "global",
        http_client: Any | None = None,
    ) -> list:
        row = self._ctx.api_spec_repo.find(name, scope)
        if row is None:
            raise LookupError(f"api spec not found: {name}")
        if not row.get("enabled"):
            return []
        doc = self._load(row, http_client)
        overrides = json.loads(row.get("overrides_json") or "{}")
        auth = json.loads(row.get("auth_json") or "{}")
        return spec_tool_definitions(
            row["name"],
            doc,
            overrides,
            make_handler=lambda spec, operation: self._make_handler(spec, operation, auth),
        )

    # -- invocation -----------------------------------------------------------------

    def _make_handler(self, spec_name: str, operation: Operation, auth: dict[str, str]):
        def handler(arguments: dict, ctx) -> object:

            factory = ctx.web if callable(getattr(ctx, "web", None)) else None
            client = factory() if factory is not None else None
            owns_client = client is None
            if owns_client:
                client = _new_client()
            env = _InvocationEnv(
                client=client,
                network_guard=getattr(ctx, "network", None),
            )
            try:
                return self._invoke_result(spec_name, operation, arguments or {}, env)
            finally:
                if owns_client and client is not None:
                    with contextlib.suppress(Exception):
                        client.close()

        return handler

    def _invoke_result(
        self, spec_name: str, operation: Operation, arguments: dict, env: _InvocationEnv
    ) -> object:
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        try:
            payload = self.invoke(spec_name, operation, arguments, env)
        except ApiCallError as exc:
            code = {
                "AUTH_REQUIRED": ToolErrorCode.AUTH_REQUIRED,
                "INVALID_ARGUMENT": ToolErrorCode.INVALID_ARGUMENT,
                "NETWORK_ERROR": ToolErrorCode.NETWORK_ERROR,
                "TIMEOUT": ToolErrorCode.TIMEOUT,
                "UPSTREAM_ERROR": ToolErrorCode.UNKNOWN,
                "SPEC_STALE": ToolErrorCode.DEPENDENCY_ERROR,
                "SERVER_NOT_FOUND": ToolErrorCode.NOT_FOUND,
            }.get(exc.code, ToolErrorCode.UNKNOWN)
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(code, exc.message, retryable=exc.retryable),
                origin="openapi",
            )
        except Exception as exc:  # defensive
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.UNKNOWN, str(exc)),
                origin="openapi",
            )
        if not payload.get("ok", True):
            return ToolResult(
                ok=False,
                data=payload,
                error=ToolErrorInfo(
                    ToolErrorCode.UNKNOWN,
                    f"upstream returned HTTP {payload.get('status')}",
                ),
                origin="openapi",
            )
        return ToolResult(ok=True, data=payload, origin="openapi")

    def invoke(
        self,
        spec_name: str,
        operation: Operation,
        arguments: dict,
        env: _InvocationEnv,
    ) -> dict:
        row = self._ctx.api_spec_repo.find(spec_name, "global") or self._ctx.api_spec_repo.find(
            spec_name, "project"
        )
        if row is None:
            raise ApiCallError("SERVER_NOT_FOUND", f"api spec not found: {spec_name}")
        doc = self._load(row, env.client)
        url, headers, query = _build_request(operation, arguments, doc, row, self._ctx)
        _apply_auth(operation, doc, url, headers, query, json.loads(row.get("auth_json") or "{}"))
        if env.network_guard is not None:
            from rinari.shared.errors import SandboxViolationError

            try:
                env.network_guard.assert_reachable(url)
            except SandboxViolationError as exc:
                raise ApiCallError("NETWORK_ERROR", str(exc)) from exc

        response = _request(env.client, operation.method, url, headers, query, arguments, env)
        return _summarize_response(response)


def _new_client() -> Any:
    import httpx

    return httpx.Client(timeout=30.0)


def _request(
    client: Any, method: str, url: str, headers: dict, query: dict, args: dict, env: _InvocationEnv
) -> Any:
    body = args.get("body") if isinstance(args, dict) else None
    try:
        if method in ("get", "head", "delete"):
            return client.request(
                method, url, headers=headers, params=_clean_query(query), timeout=env.timeout_s
            )
        return client.request(
            method,
            url,
            headers=headers,
            params=_clean_query(query),
            json=body,
            timeout=env.timeout_s,
        )
    except Exception as exc:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            raise ApiCallError("TIMEOUT", "upstream timed out", retryable=True) from exc
        raise ApiCallError("NETWORK_ERROR", f"request failed: {exc}") from exc


def _clean_query(query: dict) -> dict:
    return {k: v for k, v in query.items() if v is not None}


def _build_request(
    operation: Operation, arguments: dict, doc: SpecDocument, row: dict, ctx: AppContext
) -> tuple[str, dict, dict]:
    base = (row.get("url") or doc.base_url or "").rstrip("/")
    path = operation.path
    headers: dict[str, str] = {}
    query: dict[str, Any] = {}
    for parameter in operation.parameters:
        if not isinstance(parameter, dict):
            continue
        name, in_ = str(parameter.get("name") or ""), parameter.get("in")
        value = arguments.get(name)
        if in_ == "path":
            if value is None:
                raise ApiCallError("INVALID_ARGUMENT", f"missing path parameter: {name}")
            path = path.replace(f"{{{name}}}", quote(str(value), safe=""))
        elif in_ == "query":
            if value is not None:
                query[name] = value
        elif in_ == "header":
            if value is not None:
                headers[name] = str(value)
    if "{ " in path or "}" in path:
        raise ApiCallError("INVALID_ARGUMENT", f"unresolved path parameters in {operation.path}")
    url = f"{base}{path}" if base else path
    return url, headers, query


def _apply_auth(
    operation: Operation,
    doc: SpecDocument,
    url: str,
    headers: dict[str, str],
    query: dict[str, Any],
    auth: dict[str, str],
) -> None:
    for requirement in _requirements(operation, doc):
        kind = requirement["kind"]
        ref = auth.get(requirement["scheme"])
        if kind == "bearer":
            secret = _resolve_secret(requirement["scheme"], ref)
            headers["Authorization"] = f"Bearer {secret}"
        else:
            secret = _resolve_secret(requirement["scheme"], ref)
            param = requirement.get("name") or requirement["scheme"]
            location = requirement.get("in") or "header"
            if location == "query":
                query[param] = secret
            else:
                headers[param] = secret


def _requirements(operation: Operation, doc: SpecDocument) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for group in doc.global_security or []:
        if not isinstance(group, dict):
            continue
        for scheme_name in group:
            if scheme_name in seen:
                continue
            seen.add(scheme_name)
            scheme = doc.security_schemes.get(scheme_name)
            if not isinstance(scheme, dict):
                continue
            stype = str(scheme.get("type") or "")
            if stype == "http" and str(scheme.get("scheme")) == "bearer":
                out.append({"scheme": scheme_name, "kind": "bearer"})
            elif stype == "apiKey":
                out.append(
                    {
                        "scheme": scheme_name,
                        "kind": "apiKey",
                        "name": str(scheme.get("name") or ""),
                        "in": str(scheme.get("in") or "header"),
                    }
                )
    return out


def _resolve_secret(scheme: str, ref: str | None) -> str:
    if not ref or not str(ref).startswith("env://"):
        raise ApiCallError(
            "AUTH_REQUIRED",
            f"spec has no env:// secret configured for scheme {scheme!r} "
            "(rinari api add --auth scheme=env://VAR)",
        )
    variable = str(ref).split("://", 1)[1]
    value = os.environ.get(variable)
    if value is None:
        raise ApiCallError("AUTH_REQUIRED", f"secret {variable} is not set in this process")
    return value


def _summarize_response(response: Any) -> dict:
    data: dict[str, Any] = {"ok": response.status_code < 400, "status": response.status_code}
    try:
        data["body"] = response.json()
    except ValueError:
        text = response.text or ""
        data["body"] = text[:4000]
        if len(text) > 4000:
            data["truncated"] = True
    return data


def _validate_auth_refs(auth: dict[str, str]) -> dict[str, str]:
    for scheme, ref in auth.items():
        if not isinstance(ref, str) or not ref.startswith("env://"):
            raise ApiSpecError(
                "SPEC_INVALID",
                f"auth for {scheme!r} must be an 'env://VAR' reference (secrets never stored)",
            )
    return dict(auth)


__all__ = ["ApiCallError", "ApiService"]
