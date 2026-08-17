"""Network policy foundation + sandbox hooks (phase 4).

Contract: network policy is authoritative regardless of model request.
Mode off/ask/allow + persistent allow/deny rules decide per host; the
runtime gate enforces it for network.outbound tools and audits every
decision; the NetworkGuard hard-blocks DENYs at the tool level.
Deterministic: no sockets, no real network, FakeClock, fakes only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari.application.services import build_services
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import (
    CAPABILITY_NETWORK,
    PermissionProfile,
    PolicyAction,
    PolicyEngine,
    SessionScope,
)
from rinari.policy.network import (
    NetworkDecision,
    NetworkGuard,
    NetworkPolicy,
    NetworkRule,
    host_matches,
    normalize_host,
)
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError, SandboxViolationError
from rinari.tools.definition import (
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
)
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

runner = CliRunner()


# ---------------------------------------------------------------------------
# Host normalization and matching
# ---------------------------------------------------------------------------


def test_normalize_host_variants() -> None:
    assert normalize_host("https://api.github.com/repos") == "api.github.com"
    assert normalize_host("https://user:pw@example.com:8443/x") == "example.com"
    assert normalize_host("Example.COM.") == "example.com"
    assert normalize_host("example.com:443") == "example.com"
    assert normalize_host("  ftp://files.example.org ") == "files.example.org"
    assert normalize_host("::1") is None
    assert normalize_host("[::1]:80") is None
    assert normalize_host("no-colon-but-bad!") is None
    assert normalize_host("localhost") == "localhost"
    assert normalize_host("just-a-host") == "just-a-host"
    assert normalize_host("") is None
    assert normalize_host(None) is None


def test_host_matches_subdomain_semantics() -> None:
    assert host_matches("github.com", "api.github.com")
    assert host_matches("github.com", "github.com")
    assert not host_matches("github.com", "evilgithub.com")
    assert not host_matches("github.com", "notgithub.com")
    assert not host_matches("", "api.github.com")


# ---------------------------------------------------------------------------
# Decision core
# ---------------------------------------------------------------------------


def _decision(policy: NetworkPolicy, target: str) -> NetworkDecision:
    return policy.decide(target)


def test_decision_mode_ask_default_asks() -> None:
    d = _decision(NetworkPolicy(), "api.example.com")
    assert d.action is PolicyAction.ASK
    assert d.host == "api.example.com"


def test_decision_mode_allow_allows() -> None:
    assert _decision(NetworkPolicy(mode="allow"), "api.example.com").action is PolicyAction.ALLOW


def test_decision_mode_off_denies_even_with_allow_rule() -> None:
    policy = NetworkPolicy(mode="off", rules=[NetworkRule(host="example.com", decision="allow")])
    d = _decision(policy, "api.example.com")
    assert d.action is PolicyAction.DENY
    assert "disabled" in d.reason


def test_decision_deny_rule_beats_allow_rule_and_mode() -> None:
    policy = NetworkPolicy(
        mode="allow",
        rules=[
            NetworkRule(host="example.com", decision="allow"),
            NetworkRule(host="api.example.com", decision="deny"),
        ],
    )
    assert _decision(policy, "api.example.com").action is PolicyAction.DENY
    assert _decision(policy, "other.example.com").action is PolicyAction.ALLOW


def test_decision_allow_rule_short_circuits_ask() -> None:
    policy = NetworkPolicy(rules=[NetworkRule(host="example.com", decision="allow")])
    d = _decision(policy, "deep.api.example.com")
    assert d.action is PolicyAction.ALLOW
    assert d.rule is not None and d.rule.host == "example.com"


def test_decision_unresolvable_target_denies() -> None:
    d = _decision(NetworkPolicy(mode="allow"), "not-a-host-at-all!")
    assert d.action is PolicyAction.DENY
    assert d.host is None


def test_rules_provider_is_live() -> None:
    current: list[NetworkRule] = []
    policy = NetworkPolicy(mode="ask", rules_provider=lambda: tuple(current))
    assert _decision(policy, "example.com").action is PolicyAction.ASK
    current.append(NetworkRule(host="example.com", decision="allow"))
    assert _decision(policy, "example.com").action is PolicyAction.ALLOW


def test_guard_hard_blocks_deny_but_passes_consent() -> None:
    guard = NetworkGuard(NetworkPolicy(mode="off"))
    with pytest.raises(SandboxViolationError):
        guard.assert_reachable("api.example.com")
    guard_ask = NetworkGuard(NetworkPolicy(mode="ask"))
    # ASK is consented upstream by the runtime approval gate: not a violation.
    assert guard_ask.assert_reachable("https://example.com/x") == "example.com"
    guard_denied = NetworkGuard(
        NetworkPolicy(mode="allow", rules=[NetworkRule(host="bad.com", decision="deny")])
    )
    with pytest.raises(SandboxViolationError):
        guard_denied.assert_reachable("deep.bad.com")
    assert guard_denied.assert_reachable("ok.example.com") == "ok.example.com"


def test_engine_delegates_network_capability() -> None:
    scope = SessionScope(kind="CHAT", root=None, cwd=Path("/tmp"), user_home=None)
    engine = PolicyEngine()  # default mode = ask
    d = engine.decide(CAPABILITY_NETWORK, scope, host="api.example.com")
    assert d.capability == CAPABILITY_NETWORK
    assert d.action is PolicyAction.ASK
    off = PolicyEngine(network=NetworkPolicy(mode="off"))
    assert off.decide(CAPABILITY_NETWORK, scope, host="api.example.com").action is PolicyAction.DENY
    # Non-network capabilities are unaffected.
    assert engine.decide("fs.read", scope, path="x").capability == "fs.read"


# ---------------------------------------------------------------------------
# Service + repository
# ---------------------------------------------------------------------------


@pytest.fixture
def env(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


def test_service_rules_status_and_test(env) -> None:
    s = env
    before = s.network.status()
    assert before["mode"] == "ask"
    row = s.network.add_rule("https://api.github.com:443/x", "allow", reason="ci")
    assert row["host"] == "api.github.com"
    s.network.add_rule("evil.example", "deny")
    status = s.network.status()
    assert status["rules"] == before["rules"] + 2
    assert status["allow"] == before["allow"] + 1
    assert status["deny"] == before["deny"] + 1

    assert s.network.test("api.github.com")["action"] == "allow"
    assert s.network.test("www.evil.example")["action"] == "deny"
    assert s.network.test("other.example")["action"] == "ask"

    assert s.network.remove_rule("api.github.com", "allow") == 1
    assert s.network.test("api.github.com")["action"] == "ask"
    assert s.network.remove_rule("unknown.host") == 0

    with pytest.raises(InvalidUsageError):
        s.network.add_rule("???", "allow")


def test_service_event_audit(env) -> None:
    s = env
    assert s.network.events() == []
    for i, action in enumerate(("allow", "ask", "deny")):
        s.network.log_event("s1", "web.fetch", f"h{i}.example", action, "reason")
    events = s.network.events(limit=2)
    assert len(events) == 2
    assert events[0]["host"] == "h2.example"
    assert s.network.status()["events"] == 3
    assert all(e["session_id"] == "s1" for e in s.network.events(session_id="s1"))


# ---------------------------------------------------------------------------
# Tool Runtime gate for network.outbound tools
# ---------------------------------------------------------------------------


def _make_fake_web_tool():
    def handler(arguments: dict, ctx: ToolContext) -> ToolResult:
        host = ctx.network.assert_reachable(arguments["url"])
        return ToolResult(ok=True, data={"fetched": host})

    return ToolDefinition(
        name="web.fetch",
        description="Fetch a URL (fake for tests).",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        capabilities=("network.outbound",),
        namespace="web",
        risk="medium",
        side_effects="remote-reversible",
        handler=handler,
        classify=lambda i: ClassifiedAction("network.outbound", str(i.get("url") or "")),
    )


def _runtime_ctx(tmp_path: Path, network_guard) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="s-net",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        network=network_guard,
    )


def _build_runtime(services, tmp_path: Path, *, approval: str):
    registry = ToolRegistry()
    registry.register(_make_fake_web_tool())
    network_policy = services.network.policy()
    runtime = ToolRuntime(
        registry,
        PolicyEngine(network=network_policy),
        ApprovalEngine(prompt=lambda req: approval),
        clock=services.ctx.clock,
        network_event_log=lambda session_id, tool, host, action, reason: services.network.log_event(
            session_id, tool, host, action, reason
        ),
    )
    ctx = _runtime_ctx(tmp_path, NetworkGuard(network_policy))
    return runtime, ctx


def test_runtime_network_mode_off_denies_and_audits(env, tmp_path) -> None:
    s = env
    original_mode = s.network.mode
    s.network.mode = lambda: "off"  # type: ignore[method-assign]
    try:
        runtime, ctx = _build_runtime(s, tmp_path, approval="y")
        result = runtime.execute("web.fetch", {"url": "https://api.example.com"}, ctx)
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ToolErrorCode.POLICY_DENIED
        events = s.network.events()
        assert len(events) == 1
        assert events[0]["action"] == "deny"
        assert events[0]["host"] == "api.example.com"
        assert events[0]["tool"] == "web.fetch"
        assert events[0]["session_id"] == "s-net"
    finally:
        s.network.mode = original_mode  # type: ignore[method-assign]


def test_runtime_network_ask_denied_by_user(env, tmp_path) -> None:
    s = env
    runtime, ctx = _build_runtime(s, tmp_path, approval="n")
    result = runtime.execute("web.fetch", {"url": "https://api.example.com"}, ctx)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ToolErrorCode.APPROVAL_DENIED
    assert s.network.events()[-1]["action"] == "ask"


def test_runtime_network_allow_rule_skips_approval(env, tmp_path) -> None:
    s = env
    s.network.add_rule("api.example.com", "allow")
    runtime, ctx = _build_runtime(s, tmp_path, approval="n")
    result = runtime.execute("web.fetch", {"url": "https://api.example.com"}, ctx)
    assert result.ok is True
    assert result.data == {"fetched": "api.example.com"}
    assert s.network.events()[-1]["action"] == "allow"


def test_runtime_network_deny_rule_blocks_even_when_approved(env, tmp_path) -> None:
    s = env
    s.network.add_rule("api.example.com", "deny")
    runtime, ctx = _build_runtime(s, tmp_path, approval="y")
    result = runtime.execute("web.fetch", {"url": "https://api.example.com"}, ctx)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ToolErrorCode.POLICY_DENIED


# ---------------------------------------------------------------------------
# CLI surface (network status/test/rules/allow/deny/history)
# ---------------------------------------------------------------------------


def test_cli_network_round_trip(env) -> None:
    # The app_ctx fixture already points RINARI_HOME at the fixture home, so
    # the CLI opens the same state.db the service layer wrote to above.
    from rinari.cli import main as cli_main

    result = runner.invoke(cli_main.app, ["--json", "network", "status"])
    assert result.exit_code == 0, result.output
    payload = _json_payload(result.output)
    assert payload["data"]["mode"] == "ask"

    result = runner.invoke(cli_main.app, ["network", "allow", "https://api.github.com"])
    assert result.exit_code == 0, result.output
    assert "allowed api.github.com" in result.output

    result = runner.invoke(cli_main.app, ["network", "rules"])
    assert result.exit_code == 0
    assert "[allow] api.github.com" in result.output

    result = runner.invoke(cli_main.app, ["network", "test", "api.github.com"])
    assert result.exit_code == 0
    assert "ALLOW" in result.output

    result = runner.invoke(cli_main.app, ["network", "deny", "evil.example"])
    assert result.exit_code == 0
    assert "denied evil.example" in result.output

    result = runner.invoke(cli_main.app, ["network", "history"])
    assert result.exit_code == 0

    result = runner.invoke(cli_main.app, ["network", "remove", "api.github.com"])
    assert result.exit_code == 0
    assert "removed 1 rule(s)" in result.output
    remaining = env.network.rules()
    assert [r["host"] for r in remaining] == ["evil.example"]


def _json_payload(output: str) -> dict:
    import json

    return json.loads(output.strip())
