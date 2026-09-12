"""Native browser tools (phase 5): `browser.*` over the session's BrowserManager.

Capability model (phase 5 "Browser engine" decision, AGENTS.md 11):

- `browser.open` / `browser.navigate` with an http(s) URL classify as
  `network.outbound` on that URL: the network policy/guard is the authority
  on *what the browser may reach* (hard deny in code, ask via approval).
- Reading the browser's own state (tabs, snapshot, a11y, screenshot, console,
  network log, cookies, status) is `browser.read`: no new dial, allowed
  outside read-only profiles.
- Anything that changes browser/page state is `browser.mutate`: external
  side effects always require explicit consent (approval), never read-only.
- Uploads resolve through the session sandbox (upload provenance: path,
  bytes, sha256 returned); downloads only ever land in the session artifact
  directory (download policy), with suggested name + sha256 provenance.
- Cookie values are redacted in `browser.cookies` results (credentials).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from rinari.browser.manager import BrowserError, BrowserManager
from rinari.shared.errors import PermissionDeniedError, SandboxViolationError
from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_COMMUNICATION,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    ArtifactRef,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.native.fs import _resolve_read
from rinari.tools.native.web import _guard, _ok, _validate_url

_CODE_MAP: dict[str, ToolErrorCode] = {
    "BROWSER_DEPENDENCY": ToolErrorCode.DEPENDENCY_ERROR,
    "BROWSER_DISCONNECTED": ToolErrorCode.DEPENDENCY_ERROR,
    "BROWSER_LAUNCH_FAILED": ToolErrorCode.DEPENDENCY_ERROR,
    "BROWSER_TIMEOUT": ToolErrorCode.TIMEOUT,
    "BROWSER_PROTOCOL": ToolErrorCode.UNKNOWN,
    "JS_ERROR": ToolErrorCode.UNKNOWN,
    "TARGET_NOT_FOUND": ToolErrorCode.NOT_FOUND,
    "RESOURCE_EXHAUSTED": ToolErrorCode.RESOURCE_EXHAUSTED,
    "INVALID_ARGUMENT": ToolErrorCode.INVALID_ARGUMENT,
    "CANCELLED": ToolErrorCode.CANCELLED,
}
_RETRYABLE = {ToolErrorCode.TIMEOUT, ToolErrorCode.DEPENDENCY_ERROR}


def _manager(ctx: ToolContext) -> BrowserManager | None:
    manager = getattr(ctx, "browser", None)
    return manager if isinstance(manager, BrowserManager) else None


def _cancelled_fn(ctx: ToolContext) -> Callable[[], bool] | None:
    token = getattr(ctx, "cancellation", None)
    return lambda: (
        bool(token and token.cancelled)
        or (ctx.deadline_at is not None and time.time() >= ctx.deadline_at)
    )


def _need_manager(ctx: ToolContext) -> tuple[BrowserManager | None, ToolResult | None]:
    manager = _manager(ctx)
    if manager is None:
        return None, ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.DEPENDENCY_ERROR,
                message="browser runtime is not available in this session",
            ),
        )
    return manager, None


def _run(ctx: ToolContext, fn: Callable[[], Any]) -> ToolResult:
    _manager_instance, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        return _ok(fn())
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            data={"browser_error": exc.code,
                  "diagnostics": _manager_instance.diagnostics() if _manager_instance else {}},
            error=ToolErrorInfo(
                code=code, message=exc.message, retryable=exc.retryable or code in _RETRYABLE
            ),
        )
    except SandboxViolationError as exc:
        return ToolResult(
            ok=False, error=ToolErrorInfo(code=ToolErrorCode.SANDBOX_VIOLATION, message=exc.message)
        )
    except PermissionDeniedError as exc:
        return ToolResult(
            ok=False, error=ToolErrorInfo(code=ToolErrorCode.PERMISSION_DENIED, message=exc.message)
        )
    except Exception as exc:  # defensive: a crash must never look like success
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.UNKNOWN, message=f"{exc.__class__.__name__}: {exc}"
            ),
        )


def _nav_url_ok(value: Any) -> str | None:
    """http(s) URLs (web validation) or the local about:blank page."""
    if isinstance(value, str) and value.strip() == "about:blank":
        return "about:blank"
    return _validate_url(value)


def _nav_guard(ctx: ToolContext, url: str) -> ToolResult | None:
    """Network-gate http(s) navigations; local schemes are not a dial."""
    if url.startswith(("http://", "https://")):
        return _guard(ctx, url)
    return None


def _nav_classify(input: dict[str, Any]) -> ClassifiedAction:
    url = str(input.get("url") or "")
    if url.startswith(("http://", "https://")):
        return ClassifiedAction("network.outbound", url)
    return ClassifiedAction("browser.mutate")


def _read_classify(_input: dict[str, Any]) -> ClassifiedAction:
    return ClassifiedAction("browser.read")


def _write_classify(_input: dict[str, Any]) -> ClassifiedAction:
    return ClassifiedAction("browser.mutate")


def _upload_classify(input: dict[str, Any]) -> ClassifiedAction:
    return ClassifiedAction("fs.read", str(input.get("path") or ""))


# -- artifacts / provenance ------------------------------------------------------


def _artifact_dir(ctx: ToolContext) -> Path:
    directory = ctx.artifact_root / ctx.session_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_artifacts(data: Any, artifacts: tuple[ArtifactRef, ...]) -> ToolResult:
    return ToolResult(ok=True, data=data, artifacts=artifacts)


# -- handlers ---------------------------------------------------------------------


def browser_status(input: dict, ctx: ToolContext) -> ToolResult:
    return _run(ctx, lambda: _manager(ctx).status())  # type: ignore[union-attr]


def browser_launch(input: dict, ctx: ToolContext) -> ToolResult:
    return _run(
        ctx,
        lambda: {
            "launched": _manager(ctx).launch(  # type: ignore[union-attr]
                input.get("command"), input.get("port")
            ),
            "diagnostics": _manager(ctx).diagnostics(),
        },
    )


def browser_connect(input: dict, ctx: ToolContext) -> ToolResult:
    endpoint = input.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("ws://"):
        return _run_invalid("endpoint must be a ws:// URL")
    return _run(ctx, lambda: {
        "connected": _manager(ctx).connect(endpoint),
        "diagnostics": _manager(ctx).diagnostics(),
    })


def browser_close(input: dict, ctx: ToolContext) -> ToolResult:
    return _run(ctx, lambda: _manager(ctx).close())  # type: ignore[union-attr]


def _run_invalid(message: str) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=ToolErrorCode.INVALID_ARGUMENT, message=message)
    )


def browser_tabs(input: dict, ctx: ToolContext) -> ToolResult:
    return _run(ctx, lambda: {"tabs": _manager(ctx).targets()})  # type: ignore[union-attr]


def browser_tabs_close(input: dict, ctx: ToolContext) -> ToolResult:
    return _run(
        ctx,
        lambda: _manager(ctx).close_page(str(input.get("target_id") or "")),  # type: ignore[union-attr]
    )


def _page_target(input: dict, ctx: ToolContext) -> str | ToolResult | None:
    target = input.get("target_id")
    if target is None:
        return None
    if not isinstance(target, str) or not target:
        return _run_invalid("target_id must be a non-empty string")
    return target


def browser_open(input: dict, ctx: ToolContext) -> ToolResult:
    url = input.get("url")
    if not _nav_url_ok(url):
        return _run_invalid("url must be a valid http(s) URL or about:blank")
    guard_error = _nav_guard(ctx, str(url))
    if guard_error is not None:
        return guard_error
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    manager, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        created = manager.new_page(str(url))
        return _ok(created)
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
        )


def browser_navigate(input: dict, ctx: ToolContext) -> ToolResult:
    url = input.get("url")
    if not _nav_url_ok(url):
        return _run_invalid("url must be a valid http(s) URL or about:blank")
    guard_error = _nav_guard(ctx, str(url))
    if guard_error is not None:
        return guard_error
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).navigate(target, str(url), cancelled=_cancelled_fn(ctx)),  # type: ignore[union-attr]
    )


def browser_snapshot(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    manager, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        if input.get("format") == "semantic":
            snap = manager.a11y_tree(target, cancelled=_cancelled_fn(ctx))
            import json

            snap["snapshot_id"] = hashlib.sha256(
                json.dumps(snap, sort_keys=True).encode()
            ).hexdigest()
            snap["target_id"] = target
            return _ok(snap)
        snap = manager.snapshot(target, cancelled=_cancelled_fn(ctx))
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
        )
    data: dict[str, Any] = {"bytes": snap["bytes"], "truncated": snap["truncated"]}
    artifacts: list[ArtifactRef] = []
    if snap["truncated"]:
        path = _artifact_dir(ctx) / f"snapshot-{int(time.time())}.html"
        path.write_text(snap["html"], encoding="utf-8")
        artifacts.append(ArtifactRef(uri=f"file://{path}", name=path.name, kind="browser-snapshot"))
        data["html"] = snap["html"][:8000]
        data["artifact"] = f"file://{path}"
    else:
        data["html"] = snap["html"]
    return _with_artifacts(data, tuple(artifacts))


def browser_a11y(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).a11y_tree(target, cancelled=_cancelled_fn(ctx)),  # type: ignore[union-attr]
    )


def browser_screenshot(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    manager, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        png = manager.screenshot(target, cancelled=_cancelled_fn(ctx))
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
        )
    path = _artifact_dir(ctx) / f"screenshot-{int(time.time())}.png"
    path.write_bytes(png)
    ref = ArtifactRef(uri=f"file://{path}", name=path.name, kind="screenshot")
    return _with_artifacts(
        {"artifact": ref.uri, "bytes": len(png), "sha256": _sha256(path)}, (ref,)
    )


def browser_click(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    selector = input.get("selector")
    if selector is not None and (not isinstance(selector, str) or not selector):
        return _run_invalid("selector must be a non-empty string")

    def action() -> dict:
        return _manager(ctx).click(  # type: ignore[union-attr]
            target,
            selector=selector,
            x=input.get("x"),
            y=input.get("y"),
            cancelled=_cancelled_fn(ctx),
        )

    return _run(ctx, action)


def browser_fill(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).fill(  # type: ignore[union-attr]
            target,
            str(input.get("selector") or ""),
            str(input.get("value") or ""),
            cancelled=_cancelled_fn(ctx),
        ),
    )


def browser_type(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).type_text(  # type: ignore[union-attr]
            target,
            str(input.get("selector") or ""),
            str(input.get("text") or ""),
            press_enter=bool(input.get("press_enter")),
            cancelled=_cancelled_fn(ctx),
        ),
    )


def browser_select(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).select_option(  # type: ignore[union-attr]
            target,
            str(input.get("selector") or ""),
            str(input.get("value") or ""),
            cancelled=_cancelled_fn(ctx),
        ),
    )


def browser_check(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    checked = input.get("checked")
    if not isinstance(checked, bool):
        return _run_invalid("checked must be a boolean")
    return _run(
        ctx,
        lambda: _manager(ctx).set_checked(  # type: ignore[union-attr]
            target, str(input.get("selector") or ""), checked, cancelled=_cancelled_fn(ctx)
        ),
    )


def browser_scroll(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: _manager(ctx).scroll(  # type: ignore[union-attr]
            target,
            dx=float(input.get("dx") or 0),
            dy=float(input.get("dy") or 0),
            x=input.get("x"),
            y=input.get("y"),
            cancelled=_cancelled_fn(ctx),
        ),
    )


def browser_drag(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    to_x, to_y = input.get("to_x"), input.get("to_y")
    if not isinstance(to_x, (int, float)) or not isinstance(to_y, (int, float)):
        return _run_invalid("to_x and to_y (numbers) are required")
    from_selector = input.get("from_selector")
    if from_selector is not None and (not isinstance(from_selector, str) or not from_selector):
        return _run_invalid("from_selector must be a non-empty string")

    def action() -> dict:
        return _manager(ctx).drag(  # type: ignore[union-attr]
            target,
            from_selector=from_selector,
            from_x=input.get("from_x"),
            from_y=input.get("from_y"),
            to_x=float(to_x),
            to_y=float(to_y),
            cancelled=_cancelled_fn(ctx),
        )

    return _run(ctx, action)


def browser_evaluate(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    expression = input.get("expression")
    if not isinstance(expression, str) or not expression:
        return _run_invalid("expression must be a non-empty string")
    manager, error = _need_manager(ctx)
    if error is not None:
        return error

    def action() -> dict:
        return manager.evaluate(
            target,
            expression,
            await_promise=bool(input.get("await_promise")),
            max_chars=int(input.get("max_chars") or 8000),
            cancelled=_cancelled_fn(ctx),
        )

    return _run(ctx, action)


def browser_console(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: {
            "events": _manager(ctx).console_events(  # type: ignore[union-attr]
                target, limit=int(input.get("limit") or 100), cancelled=_cancelled_fn(ctx)
            )
        },
    )


def browser_network_log(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: {
            "requests": _manager(ctx).network_events(  # type: ignore[union-attr]
                target, limit=int(input.get("limit") or 100), cancelled=_cancelled_fn(ctx)
            )
        },
    )


def browser_cookies(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    return _run(
        ctx,
        lambda: {
            "cookies": _manager(ctx).cookies(target, cancelled=_cancelled_fn(ctx))  # type: ignore[union-attr]
        },
    )


def browser_set_cookie(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    name = input.get("name")
    if not isinstance(name, str) or not name:
        return _run_invalid("name must be a non-empty string")
    url = input.get("url")
    if url is not None and not _validate_url(url):
        return _run_invalid("url must be a valid URL")
    return _run(
        ctx,
        lambda: _manager(ctx).set_cookie(  # type: ignore[union-attr]
            target,
            name,
            str(input.get("value") or ""),
            str(url) if url else None,
            cancelled=_cancelled_fn(ctx),
        ),
    )


def browser_upload(input: dict, ctx: ToolContext) -> ToolResult:
    target = _page_target(input, ctx)
    if isinstance(target, ToolResult):
        return target
    selector = input.get("selector")
    if not isinstance(selector, str) or not selector:
        return _run_invalid("selector must be a non-empty string")
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error is not None:
        return error
    if not resolved.is_file():
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=ToolErrorCode.NOT_FOUND, message=f"Not a file: {resolved}"),
        )
    manager, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        out = manager.set_file_input(target, selector, resolved, cancelled=_cancelled_fn(ctx))
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
        )
    out.update(
        {
            "provenance": {
                "path": str(resolved),
                "bytes": resolved.stat().st_size,
                "sha256": _sha256(resolved),
            }
        }
    )
    return _ok(out)


def browser_download(input: dict, ctx: ToolContext) -> ToolResult:
    manager, error = _need_manager(ctx)
    if error is not None:
        return error
    try:
        directory = manager.begin_download(_artifact_dir(ctx))
        ref = manager.poll_download(
            timeout_s=float(input.get("wait_s") or 15.0), cancelled=_cancelled_fn(ctx)
        )
    except BrowserError as exc:
        code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
        )
    if ref is None:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.TIMEOUT,
                message=(
                    "No download appeared within the wait window. Trigger it first "
                    "(e.g. browser.click on the download link) and retry."
                ),
                retryable=True,
            ),
        )
    path = Path(ref.path)
    digest = _sha256(path)
    artifact_ref = ArtifactRef(uri=f"file://{path}", name=ref.suggested_name, kind="download")
    return _with_artifacts(
        {
            "path": ref.path,
            "bytes": ref.bytes,
            "suggested_name": ref.suggested_name,
            "sha256": digest,
            "artifact": artifact_ref.uri,
            "downloads_dir": str(directory),
        },
        (artifact_ref,),
    )


# -- registry -----------------------------------------------------------------------


def browse_tools() -> list[ToolDefinition]:
    optional_target = {
        "type": "object",
        "properties": {"target_id": {"type": "string"}},
    }
    target_schema = {
        "type": "object",
        "properties": {"target_id": {"type": "string"}},
        "required": ["target_id"],
    }
    # Nivel C (Etapa B): browser tools are on-demand, discovered via
    # capability.search and exposed via capability.activate.
    tools = [
        ToolDefinition(
            name="browser.status",
            description=(
                "Browser session state: connected/disconnected, endpoint, managed, open tabs."
            ),
            input_schema={"type": "object", "properties": {}},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_status,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.launch",
            description=(
                "Launch a managed headless Chromium-family browser with an isolated "
                "per-session profile, or connect when RINARI_BROWSER_CDP is set."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=_write_classify,
            handler=browser_launch,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.connect",
            description="Connect the session to an existing CDP endpoint (ws://host:port).",
            input_schema={
                "type": "object",
                "properties": {"endpoint": {"type": "string"}},
                "required": ["endpoint"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=_write_classify,
            handler=browser_connect,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.close",
            description=(
                "Close the CDP session and stop the browser only if Rinari launched it "
                "(subprocess ownership)."
            ),
            input_schema={"type": "object", "properties": {}},
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=lambda i: ClassifiedAction("process.local"),
            handler=browser_close,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.tabs",
            description="List open page targets (tabs): target_id, title, url.",
            input_schema={"type": "object", "properties": {}},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_tabs,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.tabs_close",
            description="Close the tab with the given target_id.",
            input_schema=target_schema,
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=_write_classify,
            handler=browser_tabs_close,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.open",
            description="Open a new tab at a URL (network-gated for http/https).",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_nav_classify,
            handler=browser_open,
            namespace="browser",
            capabilities=("network.outbound", "browser.mutate"),
        ),
        ToolDefinition(
            name="browser.navigate",
            description=(
                "Navigate the current (or given target) tab to a URL (network-gated "
                "for http/https)."
            ),
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}, "target_id": {"type": "string"}},
                "required": ["url"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_nav_classify,
            handler=browser_navigate,
            namespace="browser",
            capabilities=("network.outbound", "browser.mutate"),
        ),
        ToolDefinition(
            name="browser.snapshot",
            description=(
                "Bounded DOM snapshot (outer HTML) of the page; truncated output is "
                "spilled to an artifact with the first 8 KiB returned. Prefer format=semantic "
                "for compact roles/names and element_ids usable as click selectors."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["html", "semantic"], "default": "html"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_snapshot,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.a11y",
            description="Accessibility tree of the page (bounded, flattened fields).",
            input_schema=optional_target,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_a11y,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.screenshot",
            description="Screenshot the page as PNG; saved to the session artifact dir.",
            input_schema=optional_target,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=_read_classify,
            handler=browser_screenshot,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.click",
            description=(
                "Click at the center of a selector's bounding box, or at x/y coordinates."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_click,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.fill",
            description=(
                "Set the value of an input/textarea and dispatch input+change events "
                "(framework-safe value setter)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_fill,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.type",
            description=(
                "Focus a field and type text as key events (max 200 chars); optionally press Enter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "press_enter": {"type": "boolean"},
                },
                "required": ["selector", "text"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_type,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.select",
            description="Select an option in a <select> element.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_select,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.check",
            description="Check or uncheck a checkbox/radio.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "checked": {"type": "boolean"},
                },
                "required": ["selector", "checked"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_check,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.scroll",
            description="Scroll the page (mouse wheel) by dx/dy at x/y or the viewport center.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "dx": {"type": "number"},
                    "dy": {"type": "number"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_scroll,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.drag",
            description=(
                "Drag from a selector's center (or from_x/from_y) to to_x/to_y with "
                "interpolated mouse moves."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "from_selector": {"type": "string"},
                    "from_x": {"type": "number"},
                    "from_y": {"type": "number"},
                    "to_x": {"type": "number"},
                    "to_y": {"type": "number"},
                },
                "required": ["to_x", "to_y"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_drag,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.evaluate",
            description=(
                "Run a bounded JavaScript expression in the page (returnByValue; result "
                "JSON-capped). Use for reading page state no other tool exposes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "expression": {"type": "string"},
                    "await_promise": {"type": "boolean"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 8000},
                },
                "required": ["expression"],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_evaluate,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.console",
            description="Drain captured console messages (console.* calls) of the page.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_console,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.network",
            description="Drain observed network requests of the page (url, method, status).",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_network_log,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.cookies",
            description=(
                "Cookies visible to the page. Values are redacted (credentials); names/"
                "domains are returned."
            ),
            input_schema=optional_target,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_read_classify,
            handler=browser_cookies,
            namespace="browser",
            capabilities=("browser.read",),
        ),
        ToolDefinition(
            name="browser.set_cookie",
            description="Set a cookie for the page/URL (the value never comes back out).",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["name", "value"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
            classify=_write_classify,
            handler=browser_set_cookie,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
        ToolDefinition(
            name="browser.upload",
            description=(
                "Set a local file on a file input. The path must be inside the session "
                "sandbox; provenance (path, bytes, sha256) is returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["selector", "path"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_COMMUNICATION,
            classify=_upload_classify,
            handler=browser_upload,
            namespace="browser",
            capabilities=("fs.read", "browser.mutate"),
        ),
        ToolDefinition(
            name="browser.download",
            description=(
                "Wait for a download triggered on the page (e.g. by browser.click) and "
                "capture it in the session artifact dir with name/size/sha256 provenance."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "wait_s": {"type": "number", "minimum": 1, "maximum": 60},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            classify=_write_classify,
            handler=browser_download,
            namespace="browser",
            capabilities=("browser.mutate",),
        ),
    ]
    non_repeatable = {
        "browser.click",
        "browser.type",
        "browser.drag",
        "browser.evaluate",
        "browser.open",
        "browser.upload",
        "browser.download",
    }
    return [
        replace(
            tool,
            always_loaded=False,
            idempotent=tool.idempotent and tool.name not in non_repeatable,
        )
        for tool in tools
    ]


__all__ = ["browse_tools"]
