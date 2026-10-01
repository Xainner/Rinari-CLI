"""Official Claude Code CLI as an authenticated inference transport.

Rinari stays the agent: this module only resolves the binary, asks it who it
is authenticated as, discovers the models the account announces, and runs one
request-scoped process per model call. It never reads `~/.claude`, never
copies an OAuth token and never calls an Anthropic endpoint itself; the
subscription belongs to the Claude CLI.

Three invariants hold here (plan section 95):

1. Rinari remains the agent — the child starts with every built-in tool,
   skill and settings source disabled, in a throwaway directory.
2. The Claude CLI owns its authentication — we only read `claude auth status`.
3. A subscription provider never silently becomes API billing — the auth
   source is verified before every request and the child environment is
   stripped of anything that could reroute billing. Unknown means blocked,
   never connected.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.providers.errors import ProviderError, ProviderErrorCode

# Oldest CLI whose `auth status --json` and `--print --output-format
# stream-json` contracts match what this transport parses. Verified against
# 2.1.286 on Windows (see docs/providers/claude-subscription.md).
CLAUDE_CLI_MIN_VERSION = (2, 1, 0)

# Versions with a reproducible print-mode regression. Keep this table empty
# unless there is evidence; a guess here blocks working installs.
CLAUDE_CLI_KNOWN_BAD: frozenset[tuple[int, ...]] = frozenset()

# Environment variables that can move the child off the subscription and onto
# API/cloud billing. They are dropped from the child environment only; the
# user's own shell keeps them (plan section 11).
BILLING_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    # `--bare` mode reads ANTHROPIC_API_KEY or apiKeyHelper and never OAuth:
    # a helper configured here would silently bill the API.
    "ANTHROPIC_API_KEY_HELPER",
)

ENV_COMMAND_OVERRIDE = "RINARI_CLAUDE_COMMAND"

# Provider states surfaced to the UI (plan section 9).
STATE_MISSING_CLI = "missing_cli"
STATE_UNSUPPORTED_CLI = "unsupported_cli"
STATE_LOGGED_OUT = "logged_out"
STATE_CONNECTED = "connected"
STATE_NON_SUBSCRIPTION_AUTH = "non_subscription_auth"
STATE_ERROR = "error"

# Separate budgets: an HTTP read timeout means nothing to a child process
# (plan section 24).
TIMEOUT_VERSION_S = 15.0
TIMEOUT_AUTH_S = 20.0
TIMEOUT_DISCOVERY_S = 45.0
TIMEOUT_FIRST_TOKEN_S = 120.0
TIMEOUT_IDLE_STREAM_S = 120.0
TIMEOUT_HARD_TURN_S = 1800.0
TERMINATE_GRACE_S = 5.0
# How often the reader loop re-checks cancellation and the deadlines above.
_POLL_INTERVAL_S = 0.1

_WINDOWS = os.name == "nt"


def _creation_flags() -> int:
    """New process group so cancellation kills the whole tree, not just the shim."""
    if _WINDOWS:
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    return 0


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"creationflags": _creation_flags()} if _WINDOWS else {}
    if not _WINDOWS:
        kwargs["start_new_session"] = True
    return kwargs


@dataclass(frozen=True, slots=True)
class ClaudeCliBinary:
    path: str
    source: str  # override | env | path | well-known


@dataclass(frozen=True, slots=True)
class ClaudeCliVersion:
    raw: str
    parts: tuple[int, ...]

    @property
    def supported(self) -> bool:
        if not self.parts:
            return False
        if self.parts[:3] in CLAUDE_CLI_KNOWN_BAD:
            return False
        return self.parts >= CLAUDE_CLI_MIN_VERSION[: len(self.parts)] and (
            self.parts >= CLAUDE_CLI_MIN_VERSION
        )


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """What `claude auth status` reports, plus Rinari's own verdict."""

    installed: bool
    logged_in: bool
    auth_method: str | None
    subscription_type: str | None
    api_provider: str | None
    account_hint: str | None
    safe_for_subscription: bool
    state: str
    detail: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaudeModel:
    provider_model_id: str
    label: str | None = None
    source: str = "claude-cli"
    pinned: bool = True


@dataclass(frozen=True, slots=True)
class ClaudeRunResult:
    text: str
    usage: dict[str, Any] | None
    model: str | None
    stop_reason: str | None
    raw_result: dict[str, Any] | None


def _parse_version(raw: str) -> tuple[int, ...]:
    head = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    parts: list[int] = []
    for chunk in head.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _well_known_paths(home: Path | None = None) -> list[Path]:
    """Where the official installers put `claude` when PATH does not have it.

    Electron on macOS inherits a GUI PATH without the user's shell additions,
    and the Windows native installer writes to ~/.local/bin without touching
    PATH, so an installed CLI is routinely invisible to `which`.
    """
    home = Path.home() if home is None else home
    candidates = [
        home / ".local" / "bin" / ("claude.exe" if _WINDOWS else "claude"),
        home / ".claude" / "local" / ("claude.exe" if _WINDOWS else "claude"),
    ]
    if _WINDOWS:
        candidates += [
            home / ".local" / "bin" / "claude.cmd",
            home / "AppData" / "Roaming" / "npm" / "claude.cmd",
            home / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
        ]
    else:
        candidates += [
            Path("/usr/local/bin/claude"),
            Path("/opt/homebrew/bin/claude"),
            home / ".npm-global" / "bin" / "claude",
            home / ".bun" / "bin" / "claude",
        ]
    return candidates


class ClaudeCliRuntime:
    """Process-level operations on the official CLI. Holds no credentials."""

    def __init__(
        self,
        *,
        command_override: str | None = None,
        env: dict[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        home: Path | None = None,
    ) -> None:
        self._override = command_override
        self._env = dict(os.environ if env is None else env)
        self._run = runner or subprocess.run
        self._home = home
        self._resolved: ClaudeCliBinary | None = None

    # -- binary ------------------------------------------------------------

    def resolve(self) -> ClaudeCliBinary | None:
        """First match wins; never a shell string, always an argv[0]."""
        if self._resolved is not None:
            return self._resolved
        sources = ((self._override, "override"), (self._env.get(ENV_COMMAND_OVERRIDE), "env"))
        for raw, source in sources:
            if raw and Path(raw).exists():
                self._resolved = ClaudeCliBinary(str(Path(raw)), source)
                return self._resolved
        found = shutil.which("claude", path=self._env.get("PATH"))
        if found:
            self._resolved = ClaudeCliBinary(found, "path")
            return self._resolved
        for candidate in _well_known_paths(self._home):
            if candidate.exists():
                self._resolved = ClaudeCliBinary(str(candidate), "well-known")
                return self._resolved
        return None

    def require_binary(self) -> ClaudeCliBinary:
        binary = self.resolve()
        if binary is None:
            raise ProviderError(
                "Claude Code is not installed, or Rinari cannot find it.",
                code=ProviderErrorCode.AUTH,
                hint="Install the official Claude Code CLI, then check again.",
            )
        return binary

    # -- environment -------------------------------------------------------

    def child_env(self) -> tuple[dict[str, str], list[str]]:
        """Child environment with every billing-source override removed.

        Returns the environment and the names that were dropped so diagnostics
        can say so out loud. The user's own shell is never modified.
        """
        env = dict(self._env)
        dropped = [name for name in BILLING_ENV_VARS if name in env]
        for name in dropped:
            env.pop(name, None)
        # Keep the child out of the user's Claude Code telemetry/session noise
        # without touching their config.
        env["CLAUDE_CODE_ENTRYPOINT"] = "rinari"
        return env, dropped

    # -- probes ------------------------------------------------------------

    def _capture(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        binary = self.require_binary()
        env, _ = self.child_env()
        try:
            return self._run(
                [binary.path, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                shell=False,
                **_popen_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"Claude Code did not answer `{' '.join(args)}` in {timeout:.0f}s.",
                code=ProviderErrorCode.TIMEOUT,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"Could not run Claude Code: {exc}",
                code=ProviderErrorCode.SERVER_ERROR,
            ) from exc

    def version(self) -> ClaudeCliVersion:
        completed = self._capture(["--version"], TIMEOUT_VERSION_S)
        raw = (completed.stdout or "").strip() or (completed.stderr or "").strip()
        return ClaudeCliVersion(raw=raw, parts=_parse_version(raw))

    def auth_status(self) -> ClaudeAuthStatus:
        """Read the CLI's own view of its authentication. Fails closed.

        Anything we cannot positively identify as a claude.ai subscription
        leaves `safe_for_subscription` false, so an unknown CLI version blocks
        inference instead of risking API billing.
        """
        binary = self.resolve()
        if binary is None:
            return ClaudeAuthStatus(
                installed=False,
                logged_in=False,
                auth_method=None,
                subscription_type=None,
                api_provider=None,
                account_hint=None,
                safe_for_subscription=False,
                state=STATE_MISSING_CLI,
                detail="Claude Code is not installed, or Rinari cannot find it.",
            )
        try:
            completed = self._capture(["auth", "status", "--json"], TIMEOUT_AUTH_S)
        except ProviderError as exc:
            return ClaudeAuthStatus(
                installed=True,
                logged_in=False,
                auth_method=None,
                subscription_type=None,
                api_provider=None,
                account_hint=None,
                safe_for_subscription=False,
                state=STATE_ERROR,
                detail=str(exc),
            )
        payload = _first_json_object(completed.stdout or "")
        if payload is None:
            return ClaudeAuthStatus(
                installed=True,
                logged_in=False,
                auth_method=None,
                subscription_type=None,
                api_provider=None,
                account_hint=None,
                safe_for_subscription=False,
                state=STATE_ERROR,
                detail="Claude Code did not report a readable authentication status.",
                raw={},
            )
        logged_in = payload.get("loggedIn") is True
        auth_method = payload.get("authMethod")
        api_provider = payload.get("apiProvider")
        subscription = payload.get("subscriptionType") or payload.get("subscription")
        # `subscriptionType` differs between CLI versions, so it labels the
        # plan but never decides the verdict (plan section 3.3).
        safe = bool(logged_in) and auth_method == "claude.ai"
        if safe and api_provider is not None and api_provider != "firstParty":
            safe = False
        if not logged_in:
            state = STATE_LOGGED_OUT
            detail = "Claude Code is installed but not signed in."
        elif safe:
            state = STATE_CONNECTED
            detail = None
        else:
            state = STATE_NON_SUBSCRIPTION_AUTH
            detail = (
                "Claude Code is authenticated with a non-subscription source "
                f"({auth_method or 'unknown'}"
                + (f", {api_provider}" if api_provider else "")
                + ")."
            )
        return ClaudeAuthStatus(
            installed=True,
            logged_in=logged_in,
            auth_method=auth_method if isinstance(auth_method, str) else None,
            subscription_type=subscription if isinstance(subscription, str) else None,
            api_provider=api_provider if isinstance(api_provider, str) else None,
            account_hint=_account_hint(payload),
            safe_for_subscription=safe,
            state=state,
            detail=detail,
            raw=_sanitized(payload),
        )


def _account_hint(payload: dict[str, Any]) -> str | None:
    """A label for the signed-in account, never an identifier we store."""
    for key in ("email", "accountEmail", "organizationName", "organization"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# Keys of `auth status` that are safe to echo into diagnostics. Anything else
# (paths that may embed a user name, future token-ish fields) is dropped.
_SAFE_AUTH_KEYS = frozenset(
    {"loggedIn", "authMethod", "apiProvider", "subscriptionType", "subscription"}
)


def _sanitized(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k in _SAFE_AUTH_KEYS}


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object in stdout, ignoring banner noise."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            return None
        decoder = json.JSONDecoder()
        try:
            parsed, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def platform_install_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return "Run: irm https://claude.ai/install.ps1 | iex"
    if system == "Darwin":
        return "Run: curl -fsSL https://claude.ai/install.sh | bash"
    return "Run: curl -fsSL https://claude.ai/install.sh | bash"


def _iter_json_lines(stream: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in stream:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _deadline(seconds: float) -> float:
    return time.monotonic() + seconds


@dataclass
class _Admission:
    """Single-request admission (plan section 13/36).

    The CLI of 2.1.286 has no `--max-turns`, so nothing upstream guarantees a
    single generation. Rinari admits the first assistant generation of a run
    and rejects every later one locally, which is what keeps one Rinari turn
    from becoming several subscription calls.
    """

    admitted: int = 0
    rejected: int = 0

    def admit(self) -> bool:
        self.admitted += 1
        if self.admitted > 1:
            self.rejected += 1
            return False
        return True


def _isolation_args() -> list[str]:
    """Start the child as a bare inference transport, not as an agent.

    Every flag is verified against 2.1.286: built-in tools off, no skills, no
    settings files, no MCP server the request did not pass, and no session
    written to disk. `--bare` is deliberately absent: it forces
    ANTHROPIC_API_KEY/apiKeyHelper auth and never reads the subscription.
    """
    return [
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--tools",
        "",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
    ]


class ClaudeCliRun:
    """One request-scoped child process, with its own process group."""

    def __init__(self, process: subprocess.Popen, workdir: Path | None) -> None:
        self._process = process
        self._workdir = workdir
        self._cancelled = False

    @property
    def pid(self) -> int:
        return self._process.pid

    def cancel(self) -> None:
        """Kill the whole tree: the shim, node, and anything they spawned."""
        self._cancelled = True
        terminate_tree(self._process)

    def cleanup(self) -> None:
        terminate_tree(self._process)
        if self._workdir is not None:
            import shutil as _shutil

            _shutil.rmtree(self._workdir, ignore_errors=True)


def terminate_tree(process: subprocess.Popen) -> None:
    """Terminate a process and its children. Never leaves an orphan."""
    if process.poll() is not None:
        return
    try:
        if _WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=TERMINATE_GRACE_S,
                shell=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), 15)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        with _suppress_os_error():
            process.kill()
    except OSError:
        pass


class _suppress_os_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


@dataclass(frozen=True, slots=True)
class ClaudeRunRequest:
    """What one model call needs. Built from Rinari history, never from Claude's."""

    model: str | None
    system: str | None
    messages: tuple[dict[str, Any], ...]
    effort: str | None = None


def _stream_json_user(message: dict[str, Any]) -> str:
    return json.dumps({"type": "user", "message": message}, ensure_ascii=False)


class ClaudeCliStream:
    """Runs one request and yields normalized events.

    Emitted values are Rinari's own shape, never Claude's: the model runtime
    and the UI stay unaware that the transport is a subprocess.
    """

    def __init__(self, runtime: ClaudeCliRuntime) -> None:
        self._runtime = runtime

    def run(
        self,
        request: ClaudeRunRequest,
        *,
        on_delta: Callable[[str], None] | None = None,
        cancellation: Any = None,
    ) -> ClaudeRunResult:
        import tempfile

        binary = self._runtime.require_binary()
        env, _ = self._runtime.child_env()
        args = [binary.path, *_isolation_args()]
        if request.model:
            args += ["--model", request.model]
        if request.system:
            args += ["--system-prompt", request.system]
        if request.effort:
            args += ["--effort", request.effort]

        # Isolated cwd: even with built-in tools off, this keeps Claude Code
        # from discovering the repo's CLAUDE.md, .claude/, hooks or MCP config
        # (plan section 33). The real workspace is reached only via Rinari.
        cwd = Path(tempfile.mkdtemp(prefix="rinari-claude-"))
        try:
            process = subprocess.Popen(  # argv list, never a shell string
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(cwd),
                env=env,
                shell=False,
                **_popen_kwargs(),
            )
        except OSError as exc:
            shutil.rmtree(cwd, ignore_errors=True)
            raise ProviderError(
                f"Could not start Claude Code: {exc}", code=ProviderErrorCode.SERVER_ERROR
            ) from exc

        run = ClaudeCliRun(process, cwd)
        try:
            return self._pump(run, process, request, on_delta, cancellation)
        finally:
            run.cleanup()

    def _pump(
        self,
        run: ClaudeCliRun,
        process: subprocess.Popen,
        request: ClaudeRunRequest,
        on_delta: Callable[[str], None] | None,
        cancellation: Any,
    ) -> ClaudeRunResult:
        admission = _Admission()
        if process.stdin is None or process.stdout is None:
            raise ProviderError(
                "Claude Code did not expose its streams.", code=ProviderErrorCode.SERVER_ERROR
            )
        try:
            for message in request.messages:
                process.stdin.write(_stream_json_user(message) + "\n")
            process.stdin.flush()
            process.stdin.close()
        except OSError as exc:
            raise ProviderError(
                "Claude Code closed its input before the request was sent.",
                code=ProviderErrorCode.STREAM_INTERRUPTED,
                retryable=True,
            ) from exc

        text_parts: list[str] = []
        usage: dict[str, Any] | None = None
        resolved_model: str | None = None
        stop_reason: str | None = None
        result_payload: dict[str, Any] | None = None
        hard_deadline = _deadline(TIMEOUT_HARD_TURN_S)

        for event in _pumped_events(process.stdout, run, cancellation, hard_deadline, text_parts):
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                resolved_model = event.get("model") or resolved_model
                continue
            if kind == "stream_event":
                delta = _text_delta(event.get("event") or {})
                if delta:
                    text_parts.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                continue
            if kind == "assistant":
                if not admission.admit():
                    # A second generation would be a second subscription call
                    # for one Rinari turn: stop the child, keep the first.
                    run.cancel()
                    break
                message = event.get("message") or {}
                resolved_model = message.get("model") or resolved_model
                stop_reason = message.get("stop_reason") or stop_reason
                if not text_parts:
                    whole = _message_text(message)
                    if whole:
                        text_parts.append(whole)
                        if on_delta is not None:
                            on_delta(whole)
                continue
            if kind == "result":
                result_payload = event
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if event.get("subtype") not in (None, "success"):
                    raise _result_error(event)
                if not text_parts and isinstance(event.get("result"), str):
                    text_parts.append(event["result"])
                break

        if process.poll() is None:
            try:
                process.wait(timeout=TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                run.cancel()
        stderr = (process.stderr.read() if process.stderr else "") or ""
        if not text_parts and result_payload is None:
            raise _startup_error(process.returncode, stderr)
        return ClaudeRunResult(
            text="".join(text_parts),
            usage=usage,
            model=resolved_model,
            stop_reason=stop_reason,
            raw_result=result_payload,
        )


def _cancelled(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    for attr in ("is_cancelled", "cancelled", "is_set"):
        probe = getattr(cancellation, attr, None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # a broken token never blocks a turn
                return False
        if isinstance(probe, bool):
            return probe
    return False


def _text_delta(event: dict[str, Any]) -> str | None:
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
        return delta["text"]
    return None


def _message_text(message: dict[str, Any]) -> str:
    blocks = message.get("content")
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _result_error(event: dict[str, Any]) -> ProviderError:
    subtype = str(event.get("subtype") or "error")
    detail = event.get("result") if isinstance(event.get("result"), str) else subtype
    lowered = f"{subtype} {detail}".lower()
    if "rate" in lowered or "429" in lowered:
        return ProviderError(
            "Claude rate-limited this request.",
            code=ProviderErrorCode.RATE_LIMIT,
            retryable=True,
        )
    if "usage limit" in lowered or "quota" in lowered:
        return ProviderError(
            "The Claude subscription reached its usage limit.",
            code=ProviderErrorCode.RATE_LIMIT,
            hint="Subscription limits are managed by Claude.",
        )
    if "context" in lowered and ("long" in lowered or "exceed" in lowered):
        return ProviderError(
            "The request exceeded the model's context window.",
            code=ProviderErrorCode.CONTEXT_OVERFLOW,
        )
    return ProviderError(f"Claude Code failed: {detail}", code=ProviderErrorCode.SERVER_ERROR)


def _startup_error(returncode: int | None, stderr: str) -> ProviderError:
    """Map a failed child to a normalized error; raw stderr never reaches the UI."""
    lowered = (stderr or "").lower()
    if "not logged in" in lowered or "authentication" in lowered or "unauthorized" in lowered:
        return ProviderError(
            "Claude Code is not signed in.",
            code=ProviderErrorCode.AUTH,
            hint="Run `claude auth login` and check again.",
        )
    if returncode == 0:
        return ProviderError(
            "Claude Code exited without producing a response.",
            code=ProviderErrorCode.STREAM_INTERRUPTED,
            retryable=True,
            hint="This Claude Code version may have a print-mode regression.",
        )
    return ProviderError(
        f"Claude Code exited with status {returncode}.",
        code=ProviderErrorCode.SERVER_ERROR,
        retryable=True,
    )


def _pumped_events(
    stdout: Any,
    run: ClaudeCliRun,
    cancellation: Any,
    hard_deadline: float,
    produced: list[str],
) -> Iterator[dict[str, Any]]:
    """Yield the child's events while staying responsive to cancel and clocks.

    The child is read on a thread and the caller polls, because a blocking
    readline cannot notice a cancelled turn or a stalled stream: a silent
    child would hold the turn open until it felt like exiting. The separate
    budgets of plan section 24 are enforced here — a child that never speaks
    fails on the first-token deadline, one that stops mid-answer fails on the
    idle deadline, and either way the process tree is killed.
    """
    import queue
    import threading

    lines: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for line in stdout:
                lines.put(line)
        except (OSError, ValueError):  # stream closed under us by a kill
            pass
        finally:
            lines.put(None)

    thread = threading.Thread(target=_reader, name="claude-cli-stdout", daemon=True)
    thread.start()

    last_activity = time.monotonic()
    while True:
        if _cancelled(cancellation):
            run.cancel()
            raise ProviderError(
                "The turn was cancelled.", code=ProviderErrorCode.STREAM_INTERRUPTED
            )
        now = time.monotonic()
        if now > hard_deadline:
            run.cancel()
            raise ProviderError(
                "Claude Code exceeded the turn time budget.",
                code=ProviderErrorCode.TIMEOUT,
                retryable=True,
            )
        budget = TIMEOUT_IDLE_STREAM_S if produced else TIMEOUT_FIRST_TOKEN_S
        if now - last_activity > budget:
            run.cancel()
            raise ProviderError(
                "Claude Code stopped sending output."
                if produced
                else "Claude Code did not start answering in time.",
                code=ProviderErrorCode.TIMEOUT,
                retryable=True,
            )
        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            continue
        if line is None:
            return
        last_activity = time.monotonic()
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Banner or progress noise on stdout is not a protocol error.
            continue
        if isinstance(parsed, dict):
            yield parsed
