"""Approval scopes follow the conversation (plan 0.4.0 #56).

A loose chat offers «in this chat», never «always»: what is allowed in one
chat does not reach the others. «In this chat» / «in this session» is kept
with the conversation, so it survives a restart, and old «always in chats»
grants no longer apply.
"""

from __future__ import annotations

from dataclasses import replace

import httpx

from rinari.policy.approval_store import ProjectGrantStore
from rinari.policy.approvals import ApprovalEngine, ApprovalGrant, ApprovalRequest, GrantScope
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkPolicy
from rinari.policy.session_grants import SessionGrantRegistry, SessionGrants, drop_peer_grants
from rinari.shared.clock import FakeClock
from rinari.storage.db import Database
from rinari.storage.migrations.runner import MigrationRunner
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

from .test_permissions_v3 import _ctx

POST = {"url": "https://api.example.com/x", "method": "POST", "body": "hola"}


def _runtime(asked: list[ApprovalRequest], ctx) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    return ToolRuntime(
        registry,
        PolicyEngine(network=NetworkPolicy()),
        ApprovalEngine(prompt=lambda r: (asked.append(r), "n")[1]),
        clock=ctx.clock,
    )


def _client():
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))


def test_a_loose_chat_offers_this_chat_and_a_project_offers_always(tmp_path) -> None:
    project = _ctx(tmp_path, profile=PermissionProfile.WORKSPACE, client=_client)
    asked: list[ApprovalRequest] = []
    _runtime(asked, project).execute("http.request", POST, project)
    assert asked[-1].choices == ("deny", "allow_once", "allow_session", "allow_project")

    chat = replace(project, kind="CHAT", project_root=None)
    _runtime(asked, chat).execute("http.request", POST, chat)
    assert asked[-1].choices == ("deny", "allow_once", "allow_session")
    assert asked[-1].project_id == "chats"


def test_old_always_in_chats_grants_no_longer_apply(tmp_path) -> None:
    store = ProjectGrantStore(tmp_path / "project_grants.json")
    store.add(
        scope_key="chats",
        capability="network.outbound",
        rule_id="network_send",
        target="api.example.com",
    )
    request = ApprovalRequest(
        capability="network.outbound",
        description="",
        target="api.example.com",
        session_id="s1",
        project_id="chats",
        rule_id="network_send",
        choices=("deny", "allow_once", "allow_session"),
        binding_mode="exact",
    )
    engine = ApprovalEngine(prompt=lambda _r: "p", project_store=store)
    # Neither the old grant nor a new «always» answer: a chat has no project.
    assert not engine.check(request).granted
    assert len(store.list()) == 1


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "state.db")
    MigrationRunner(db, FakeClock()).migrate()
    for sid in ("s1", "s2"):
        db.execute(
            "INSERT INTO sessions (id, kind, created_cwd, current_cwd, provider_id, model_id, "
            "state, created_at, updated_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "CHAT", "/w", "/w", "prov", "mdl", "active", "t0", "t0", "t0"),
        )
    return db


def test_this_chat_grants_survive_a_restart_and_go_with_the_chat(tmp_path) -> None:
    db = _db(tmp_path)
    request = ApprovalRequest(
        capability="shell.exec",
        description="",
        target="npm test",
        session_id="s1",
        project_id="chats",
        rule_id="shell_send",
        choices=("deny", "allow_once", "allow_session"),
    )
    engine = ApprovalEngine(prompt=lambda _r: "s", session_grants=SessionGrants(db, "s1"))
    assert engine.check(request).grant.scope is GrantScope.SESSION

    # A new Engine reads it back for this chat, not for another one.
    after = ApprovalEngine(prompt=lambda _r: "n", session_grants=SessionGrants(db, "s1"))
    assert after.check(request).granted
    other = ApprovalEngine(prompt=lambda _r: "n", session_grants=SessionGrants(db, "s2"))
    assert not other.check(replace(request, session_id="s2")).granted

    # A peer group's revocation takes back only its own grants.
    drop_peer_grants(db, "s1")
    assert len(SessionGrants(db, "s1")) == 1
    # Deleting the chat deletes them.
    db.execute("DELETE FROM sessions WHERE id = 's1'")
    assert len(SessionGrants(db, "s1")) == 0


def test_one_grant_list_per_conversation_outside_the_peer_broker(tmp_path) -> None:
    """The «in this chat» grants do not depend on Boards' peer messaging."""
    db = _db(tmp_path)
    registry = SessionGrantRegistry(db)
    grants = registry.for_session("s1")
    assert registry.for_session("s1") is grants
    grants.append(
        ApprovalGrant(
            capability="shell.exec",
            scope=GrantScope.SESSION,
            target="npm test",
            session_id="s1",
            project_id="chats",
        )
    )
    # Rows written underneath (a child inheriting its mother's) show after forget.
    db.execute(
        "INSERT INTO session_approval_grants "
        "(session_id, capability, rule_id, target, project_id, granted_at) "
        "VALUES ('s1', 'net.send', NULL, 'example.com', 'chats', '2026-10-17T00:00:00')"
    )
    assert len(registry.for_session("s1")) == 1
    registry.forget("s1")
    fresh = registry.for_session("s1")
    assert fresh is not grants
    assert {grant.capability for grant in fresh} == {"shell.exec", "net.send"}
