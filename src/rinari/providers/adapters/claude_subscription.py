"""Claude Subscription: the official Claude Code CLI as a transport.

The adapter owns no credential and opens no socket. It asks
`ClaudeCliRuntime` who the CLI is authenticated as, refuses to run unless the
answer is a claude.ai subscription, and turns one `ModelRequest` into one
request-scoped child process (plan sections 6.2, 6.3, 9, 12).

Tools are deliberately absent: until the Rinari tool bridge lands (plan phase
D) the child runs with every built-in tool disabled and the adapter announces
`tool_calls = False`, so the model runtime never offers it work it cannot do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    Usage,
)
from rinari.providers.adapters.base import (
    AuthStatus,
    DiscoveredModel,
    ProviderAdapter,
    ProviderHealth,
)
from rinari.providers.claude_cli import (
    STATE_CONNECTED,
    ClaudeCliRuntime,
    ClaudeCliStream,
    ClaudeRunRequest,
    platform_install_hint,
)
from rinari.providers.errors import ProviderError, ProviderErrorCode

#: Aliases the CLI documents for `--model`. They are a snapshot, not a claim
#: about the account: availability stays "unknown" until a call resolves one,
#: because no print-mode command lists the models a plan actually includes.
PINNED_ALIASES: tuple[tuple[str, str], ...] = (
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
)


class ClaudeSubscriptionAdapter(ProviderAdapter):
    type = "claude-subscription"

    def __init__(self, runtime: ClaudeCliRuntime | None = None, **_: Any) -> None:
        super().__init__(client=None)
        self._runtime = runtime or ClaudeCliRuntime()

    # -- identity ----------------------------------------------------------

    @property
    def runtime(self) -> ClaudeCliRuntime:
        return self._runtime

    def auth_methods(self) -> tuple[str, ...]:
        return ("external-cli",)

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        # An identity for routing and diagnostics. Nothing may send it to an
        # HTTP client.
        return "process://claude"

    def client(self):  # pragma: no cover - a process transport has no client
        raise ProviderError(
            "Claude Subscription does not use an HTTP client.",
            code=ProviderErrorCode.SERVER_ERROR,
        )

    # -- auth --------------------------------------------------------------

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        if secret:
            # Rinari never holds a Claude credential; a secret here means a
            # caller tried to attach one.
            raise ProviderError(
                "Claude Subscription does not take a credential.",
                code=ProviderErrorCode.AUTH,
                hint="Authentication belongs to the Claude Code CLI.",
            )
        status = self._runtime.auth_status()
        detail = status.detail or (
            f"Claude subscription ({status.subscription_type})"
            if status.subscription_type
            else "Claude subscription"
        )
        return AuthStatus(connected=status.safe_for_subscription, detail=detail)

    def require_subscription(self) -> None:
        """Gate every call on a freshly read, positively identified subscription.

        Checking only at connect time would let an external `claude auth
        login --console` silently move the account onto API billing, so this
        runs before each request (plan sections 3.3, 66, 69).
        """
        status = self._runtime.auth_status()
        if status.safe_for_subscription:
            return
        if not status.installed:
            raise ProviderError(
                "Claude Code is not installed, or Rinari cannot find it.",
                code=ProviderErrorCode.AUTH,
                hint=platform_install_hint(),
            )
        if not status.logged_in:
            raise ProviderError(
                "Claude Code is not signed in.",
                code=ProviderErrorCode.AUTH,
                hint="Run `claude auth login` and check again.",
            )
        raise ProviderError(
            status.detail or "Claude Code is not using a subscription for authentication.",
            code=ProviderErrorCode.AUTH,
            hint="Claude Subscription only runs when Claude Code is signed in through claude.ai.",
        )

    # -- models ------------------------------------------------------------

    def list_models(self, secret: str | None, endpoint: str | None = None) -> list[DiscoveredModel]:
        self.require_subscription()
        version = self._runtime.version()
        if not version.supported:
            raise ProviderError(
                f"Claude Code {version.raw or 'unknown'} is older than this integration supports.",
                code=ProviderErrorCode.SERVER_ERROR,
                hint="Run `claude update` and check again.",
            )
        return [
            DiscoveredModel(
                provider_model_id=alias,
                availability="unknown",
                capabilities={
                    "label": label,
                    "source": "claude-cli-aliases",
                    "transport": "claude-cli",
                    # Unverified capabilities stay unknown, never a guessed
                    # False (plan section 30).
                    "tools": False,
                    "streaming": True,
                    "vision": None,
                    "reasoning": None,
                    "max_context_window": None,
                },
            )
            for alias, label in PINNED_ALIASES
        ]

    def health(self, secret: str | None, endpoint: str | None = None) -> ProviderHealth:
        status = self._runtime.auth_status()
        if status.state != STATE_CONNECTED:
            return ProviderHealth(connected=False, detail=status.detail or status.state)
        models = self.list_models(secret, endpoint)
        plan = f" ({status.subscription_type})" if status.subscription_type else ""
        return ProviderHealth(
            connected=True,
            detail=f"Claude subscription{plan}",
            models_discovered=len(models),
            models=models,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            # Phase D (the Rinari tool bridge) turns this on; announcing it
            # now would hand the model work this transport cannot route.
            tool_calls=False,
            structured_output=False,
            max_context_tokens=None,
            reasoning_effort=True,
            vision=None,
        )

    # -- inference ---------------------------------------------------------

    def invoke(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None = None,
        *,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        return self.invoke_stream(
            request, secret, endpoint, lambda _delta: None, tool_aliases=tool_aliases
        )

    def invoke_stream(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None,
        on_delta: Callable[[str], None],
        *,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        self.require_subscription()
        if request.tools:
            raise ProviderError(
                "Claude Subscription cannot run tools yet.",
                code=ProviderErrorCode.SERVER_ERROR,
                hint="Use a provider with tool support, or pick a mode without tools.",
            )
        system, messages = _split_history(request)
        result = ClaudeCliStream(self._runtime).run(
            ClaudeRunRequest(
                model=request.model or None,
                system=system,
                messages=messages,
                effort=request.reasoning_effort,
            ),
            on_delta=on_delta,
            cancellation=request.cancellation,
        )
        return ModelResponse(
            content=result.text,
            usage=_usage(result.usage),
            stop_reason=StopReason.END_TURN,
            # The resolved model id travels as transport metadata so history
            # records what actually ran, not just the alias the user picked
            # (plan section 20).
            provider_state=(
                {"resolved_model": result.model, "transport": "claude-cli"}
                if result.model
                else {"transport": "claude-cli"}
            ),
        )


def _split_history(request: ModelRequest) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Rinari history -> Claude stream-json input.

    The system prompt travels as `--system-prompt`, which replaces Claude
    Code's own agentic prompt instead of stacking on top of it (plan section
    35). Everything else is replayed from the Rinari session, which stays the
    only source of truth (plan section 16).
    """
    system_parts = [m.content or "" for m in request.messages if m.role == "system" and m.content]
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            continue
        text = message.content or ""
        if message.role == "assistant":
            # Replayed as context for the next generation; the CLI accepts a
            # user turn per line, so prior answers are labelled inline.
            if text:
                messages.append(_user_block(f"[assistant]\n{text}"))
            continue
        if message.role == "tool":
            if text:
                messages.append(_user_block(f"[tool result: {message.name or 'tool'}]\n{text}"))
            continue
        if text:
            messages.append(_user_block(text))
    if not messages:
        messages.append(_user_block(""))
    return ("\n\n".join(system_parts) or None), tuple(messages)


def _user_block(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _usage(payload: dict[str, Any] | None) -> Usage:
    if not payload:
        return Usage(source="unavailable")
    cache_read = payload.get("cache_read_input_tokens")
    return Usage(
        input_tokens=_int(payload.get("input_tokens")),
        output_tokens=_int(payload.get("output_tokens")),
        cached_input_tokens=_int(cache_read),
        source="complete",
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
