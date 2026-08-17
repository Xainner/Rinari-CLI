"""Network policy (phase 4): the authoritative gate for outbound connections.

Network policy remains authoritative regardless of model request
(commands.md #40). It is computed per connection target:

    1. unresolvable target      -> deny (the destination cannot be verified)
    2. matching DENY rule       -> deny (explicit rules always beat the mode)
    3. mode = "off"             -> deny
    4. matching ALLOW rule      -> allow (the allowlist short-circuits "ask")
    5. mode = "allow"           -> allow
    6. mode = "ask" (default)   -> ask (approval gate)

Host matching is exact or subdomain: a rule for `github.com` covers
`api.github.com` but not `evil-github.com`.

The `NetworkGuard` is the sandbox-side technical hook: network-capable tools
(web/http/browser land in phase 5; connectors and plugins too) pass every
connection target through it before opening a socket, so a deny is enforced
in code, never left to the model (AGENTS.md 11).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from rinari.policy.engine import PolicyAction
from rinari.shared.errors import SandboxViolationError

MODE_OFF = "off"
MODE_ASK = "ask"
MODE_ALLOW = "allow"
VALID_MODES = (MODE_OFF, MODE_ASK, MODE_ALLOW)

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"

_NETWORK_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-")


def normalize_host(value: object) -> str | None:
    """Reduce a URL, host:port, or user@host to a bare lowercase host.

    Returns None when no verifiable host can be extracted; callers must
    treat that as deny, not allow.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "://" in text:
        netloc = urlparse(text).netloc
        if not netloc:
            return None
        text = netloc
    if "@" in text:
        text = text.rpartition("@")[2]
    text = text.strip().rstrip(".")
    if "[" in text or "]" in text:
        return None  # IPv6 literals: no rule semantics defined for them yet
    if text.count(":") == 1:
        text = text.partition(":")[0]
    text = text.lower()
    if not text or any(ch not in _NETWORK_CHARS for ch in text):
        return None
    return text


def host_matches(rule_host: str, target_host: str) -> bool:
    rule = normalize_host(rule_host) or ""
    target = normalize_host(target_host) or ""
    if not rule or not target:
        return False
    return target == rule or target.endswith("." + rule)


@dataclass(frozen=True, slots=True)
class NetworkRule:
    host: str
    decision: str  # DECISION_ALLOW | DECISION_DENY
    scope: str = "global"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NetworkDecision:
    action: PolicyAction
    host: str | None  # normalized target (None = unresolvable)
    reason: str
    rule: NetworkRule | None = None
    target: str = ""  # normalized identifier for policy events


class NetworkPolicy:
    """Decides allow/ask/deny for outbound network targets.

    `rules_provider` (when given) is consulted on every decision so rule
    changes made via `rinari network allow|deny` apply to a running session
    without a restart.
    """

    def __init__(
        self,
        *,
        mode: str = MODE_ASK,
        rules: Sequence[NetworkRule] = (),
        rules_provider=None,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"unknown network mode: {mode!r}")
        self._mode = mode
        self._rules = tuple(rules)
        self._rules_provider = rules_provider

    @property
    def mode(self) -> str:
        return self._mode

    def rules(self) -> tuple[NetworkRule, ...]:
        if self._rules_provider is not None:
            return tuple(self._rules_provider())
        return self._rules

    def decide(self, target: object, *, source: str = "") -> NetworkDecision:
        host = normalize_host(target)
        if host is None:
            return NetworkDecision(
                action=PolicyAction.DENY,
                host=None,
                reason="network target could not be resolved to a host",
                target=str(target or "")[:200],
            )
        for rule in self.rules():
            if rule.decision == DECISION_DENY and host_matches(rule.host, host):
                return NetworkDecision(
                    action=PolicyAction.DENY,
                    host=host,
                    reason=(
                        f"host {host} is denied by rule ({rule.host})"
                        + (f": {rule.reason}" if rule.reason else "")
                    ),
                    rule=rule,
                    target=host,
                )
        if self._mode == MODE_OFF:
            return NetworkDecision(
                action=PolicyAction.DENY,
                host=host,
                reason="network is disabled (network.mode=off)",
                target=host,
            )
        for rule in self.rules():
            if rule.decision == DECISION_ALLOW and host_matches(rule.host, host):
                return NetworkDecision(
                    action=PolicyAction.ALLOW,
                    host=host,
                    reason=f"host {host} matches allow rule ({rule.host})",
                    rule=rule,
                    target=host,
                )
        if self._mode == MODE_ALLOW:
            return NetworkDecision(
                action=PolicyAction.ALLOW,
                host=host,
                reason="network.mode=allow (no matching deny rule)",
                target=host,
            )
        return NetworkDecision(
            action=PolicyAction.ASK,
            host=host,
            reason="outbound network requires approval (network.mode=ask)",
            target=host,
        )


class NetworkGuard:
    """Sandbox-side network hook (AGENTS.md 11: enforce in code).

    Network-capable tools call `assert_reachable` before every connection.
    It enforces the hard DENYs (mode off, deny rules, unresolvable targets)
    technically, so they cannot be bypassed by a tool handler. An ASK
    verdict is passed through: by the time the handler runs, the Tool
    Runtime's approval gate already handled consent for this call.
    """

    def __init__(self, policy: NetworkPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> NetworkPolicy:
        return self._policy

    def assert_reachable(self, target: object, *, source: str = "") -> str:
        decision = self._policy.decide(target, source=source)
        if decision.action is PolicyAction.DENY:
            raise SandboxViolationError(
                f"network blocked: {decision.reason}",
                hint=(
                    "Allow the host with `rinari network allow <host>` or "
                    "change network.mode in the config."
                ),
            )
        return decision.host or ""


__all__ = [
    "DECISION_ALLOW",
    "DECISION_DENY",
    "MODE_ALLOW",
    "MODE_ASK",
    "MODE_OFF",
    "VALID_MODES",
    "NetworkDecision",
    "NetworkGuard",
    "NetworkPolicy",
    "NetworkRule",
    "host_matches",
    "normalize_host",
]
