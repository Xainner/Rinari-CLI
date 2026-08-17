"""Network service (phase 4): rules, mode, decisions, and the audit trail.

Application-level facade over the `network_rules` / `network_events` tables
and the pure `NetworkPolicy` decision core. The Tool Runtime consults the
same policy per tool call, so a `rinari network allow` applies to a running
session on its next network action (rules are read lazily).
"""

from __future__ import annotations

from rinari.application.context import AppContext
from rinari.policy.network import (
    MODE_ASK,
    VALID_MODES,
    NetworkDecision,
    NetworkPolicy,
    NetworkRule,
    normalize_host,
)
from rinari.shared.errors import InvalidUsageError

RULE_DECISIONS = ("allow", "deny")


class NetworkService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._repo = ctx.network_repo

    # -- state ----------------------------------------------------------------

    def mode(self) -> str:
        mode = str(self._ctx.config.value("network.mode") or MODE_ASK)
        if mode not in VALID_MODES:
            return MODE_ASK
        return mode

    def policy(self) -> NetworkPolicy:
        return NetworkPolicy(
            mode=self.mode(),
            rules_provider=self._rules_live,
        )

    def _rules_live(self) -> list[NetworkRule]:
        return [
            NetworkRule(
                host=row["host"],
                decision=row["decision"],
                scope=row.get("scope") or "global",
                reason=row.get("reason") or "",
            )
            for row in self._repo.list_rules()
        ]

    # -- rules ----------------------------------------------------------------

    def status(self) -> dict:
        rules = self._repo.list_rules()
        return {
            "mode": self.mode(),
            "rules": len(rules),
            "allow": sum(1 for r in rules if r["decision"] == "allow"),
            "deny": sum(1 for r in rules if r["decision"] == "deny"),
            "events": self._repo.count_events(),
        }

    def add_rule(
        self, host: str, decision: str, *, reason: str = "", scope: str = "global"
    ) -> dict:
        if decision not in RULE_DECISIONS:
            raise InvalidUsageError(
                f"decision must be one of {', '.join(RULE_DECISIONS)}",
                hint="Use `rinari network allow <host>` or `rinari network deny <host>`.",
            )
        if scope not in ("global", "project"):
            raise InvalidUsageError("scope must be 'global' or 'project'")
        normalized = normalize_host(host)
        if normalized is None:
            raise InvalidUsageError(
                f"could not resolve a host from {host!r}",
                hint="Pass a bare host (api.github.com) or a URL (https://api.github.com).",
            )
        return self._repo.add_rule(
            self._ctx.ids.new("nr"),
            normalized,
            decision,
            scope=scope,
            reason=reason,
            created_at=self._now(),
        )

    def remove_rule(self, host: str, decision: str | None = None, scope: str = "global") -> int:
        normalized = normalize_host(host)
        if normalized is None:
            raise InvalidUsageError(f"could not resolve a host from {host!r}")
        removed = 0
        targets = [decision] if decision in RULE_DECISIONS else list(RULE_DECISIONS)
        for dec in targets:
            if self._repo.delete_rule(normalized, dec, scope=scope):
                removed += 1
        return removed

    def rules(self) -> list[dict]:
        return self._repo.list_rules()

    # -- decisions / audit -------------------------------------------------------

    def test(self, target: str) -> dict:
        decision = self.policy().decide(target)
        return decision_dict(decision)

    def events(self, limit: int = 20, session_id: str | None = None) -> list[dict]:
        return self._repo.list_events(limit=limit, session_id=session_id)

    def log_event(
        self,
        session_id: str,
        tool: str,
        host: str,
        action: str,
        reason: str,
    ) -> None:
        self._repo.add_event(
            self._ctx.ids.new("ne"),
            host=host,
            action=action,
            reason=reason,
            session_id=session_id,
            tool=tool,
            created_at=self._now(),
        )

    def _now(self) -> str:
        from rinari.shared.clock import now_iso

        return now_iso(self._ctx.clock)


def decision_dict(decision: NetworkDecision) -> dict:
    return {
        "action": decision.action.value,
        "host": decision.host,
        "reason": decision.reason,
        "target": decision.target,
        "rule": decision.rule.host if decision.rule is not None else None,
    }


__all__ = ["RULE_DECISIONS", "NetworkService", "decision_dict"]
