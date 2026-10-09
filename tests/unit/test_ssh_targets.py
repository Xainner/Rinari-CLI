import base64
import dataclasses
import io
import subprocess

import pytest

from rinari.application.ssh_targets import TargetStore
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import SystemClock
from rinari.tools.definition import ToolContext
from rinari.tools.native import ssh
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


def destination():
    key = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode()
    return {
        "id": "fixture",
        "name": "Fixture",
        "host": "192.0.2.10",
        "port": 22,
        "username": "test",
        "identity": "fixture",
        "host_key": "ssh-ed25519 " + key,
    }


def test_targets_immutable_and_persistent(tmp_path):
    store = TargetStore(tmp_path)
    target = store.add(destination())
    assert TargetStore(tmp_path).get("fixture") == target
    assert store.add(destination()) == target
    with pytest.raises(ValueError):
        store.add({**destination(), "host": "192.0.2.20"})
    for change in (
        {"host": "-oProxyCommand=bad"},
        {"identity": "../secret"},
        {"username": "test;id"},
        {"host_key": "ssh-ed25519 invalid"},
        {"port": True},
    ):
        with pytest.raises(ValueError):
            TargetStore(tmp_path / "invalid").add({**destination(), **change})


@pytest.fixture
def setup(tmp_path):
    store = TargetStore(tmp_path)
    target = store.add(destination())
    key = store.root / "identities" / "fixture"
    key.write_text("synthetic-test-placeholder", encoding="utf-8")
    key.chmod(0o600)
    policy = NetworkPolicy()
    ctx = ToolContext(
        session_id="s",
        kind="CHAT",
        cwd=tmp_path,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(tmp_path, write_roots=()),
        limits=ProcessLimits(),
        artifact_root=tmp_path / "artifacts",
        clock=SystemClock(),
        cancellation=CancellationToken(),
        network=NetworkGuard(policy),
    )
    registry = ToolRegistry()
    registry.register_all(ssh.ssh_tools(store, target))
    return store, ctx, registry, policy


@pytest.mark.parametrize(
    "mode,code",
    [
        ("ok", None),
        ("host-key-rejected", "AUTH_REQUIRED"),
        ("timeout", "TIMEOUT"),
        ("cancel", "CANCELLED"),
    ],
)
def test_pinned_transport_failures_and_cancellation(setup, monkeypatch, mode, code):
    _store, ctx, registry, policy = setup
    calls = []

    class Process:
        returncode = None
        stdout = io.BytesIO(b'{"cpus": 4}')
        stderr = io.BytesIO(
            b"Host key verification failed" if mode == "host-key-rejected" else b"test error"
        )

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            assert "StrictHostKeyChecking=yes" in argv
            assert "IdentityAgent=none" in argv
            assert "ForwardAgent=no" in argv
            assert argv[-2:] == ["192.0.2.10", ssh.COMMANDS["cpu"]]
            assert kwargs["shell"] is False
            hosts = next(v.split("=", 1)[1] for v in argv if v.startswith("UserKnownHostsFile="))
            from pathlib import Path

            assert Path(hosts).read_text().startswith("rinari-fixture ssh-ed25519 ")

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if self.returncode is not None:
                return self.returncode
            if mode == "timeout":
                raise subprocess.TimeoutExpired("ssh", timeout)
            if mode == "cancel":
                ctx.cancellation.cancel()
                return self.returncode
            self.returncode = 255 if mode == "host-key-rejected" else 0
            return self.returncode

    monkeypatch.setattr(ssh.subprocess, "Popen", Process)
    monkeypatch.setattr(ssh, "_kill_tree", lambda process: setattr(process, "returncode", -9))
    runtime = ToolRuntime(
        registry, PolicyEngine(network=policy), ApprovalEngine(prompt=lambda _: "y")
    )
    result = runtime.execute("ssh.inspect", {"target_id": "fixture", "section": "cpu"}, ctx)
    assert len(calls) == 1
    if code:
        assert not result.ok and result.error.code == code
    else:
        assert result.ok and result.data["target_id"] == "fixture"


def test_policy_and_bound_target_prevent_dial(setup, monkeypatch):
    _, ctx, registry, policy = setup
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not dial"))
    runtime = ToolRuntime(
        registry, PolicyEngine(network=policy), ApprovalEngine(prompt=lambda _: "n")
    )
    assert not runtime.execute("ssh.inspect", {"target_id": "fixture", "section": "cpu"}, ctx).ok
    assert not runtime.execute("ssh.inspect", {"target_id": "other", "section": "cpu"}, ctx).ok
    assert not runtime.execute(
        "ssh.inspect", {"target_id": "fixture", "section": "cpu", "command": "id"}, ctx
    ).ok
    assert not runtime.execute(
        "shell.exec",
        {"command": "id"},
        dataclasses.replace(ctx, profile=PermissionProfile.FULL_ACCESS),
    ).ok


def test_hardware_defaults_to_one_connection_with_partial_sections(setup, monkeypatch):
    _, ctx, registry, policy = setup
    calls = []
    output = "".join(
        f"RINARI_SECTION_{name}\n{name} fixture\nRINARI_EXIT_{127 if name == 'gpu' else 0}\n"
        for name in ssh.HARDWARE_SECTIONS
    )

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append(argv)
            assert argv[-1] == ssh.COMMANDS["hardware"]
            self.stdout = io.BytesIO(output.encode())
            self.stderr = io.BytesIO(b"GPU utility absent")

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(ssh.subprocess, "Popen", Process)
    runtime = ToolRuntime(
        registry, PolicyEngine(network=policy), ApprovalEngine(prompt=lambda _: "y")
    )
    result = runtime.execute("ssh.inspect", {"target_id": "fixture"}, ctx)
    assert result.ok and len(calls) == 1
    assert result.data["sections"]["cpu"]["ok"]
    assert not result.data["sections"]["gpu"]["ok"]
    assert result.data["sections"]["gpu"]["exit_code"] == 127


def test_an_unreadable_ssh_folder_does_not_break_building_the_store(tmp_path, monkeypatch):
    """Card 07: the `.rinari/ssh` folder closed to this account failed every turn."""
    from pathlib import Path

    from rinari.application.ssh_targets import TargetStoreUnavailable

    def denied(self, *args, **kwargs):
        raise PermissionError(5, "Access is denied", str(self))

    monkeypatch.setattr(Path, "mkdir", denied)
    store = TargetStore(tmp_path)  # what every turn does: must not raise
    tool = next(t for t in ssh.ssh_tools(store) if t.name == "ssh.inspect")
    with pytest.raises(TargetStoreUnavailable, match="give this account ownership"):
        store.list()
    result = tool.handler({"target_id": "fixture", "section": "cpu"}, None)
    assert not result.ok
    assert result.error.code.value == "PERMISSION_DENIED"
    assert str(tmp_path / "ssh") in result.error.message


# -- ssh.run -------------------------------------------------------------------


class _Stdin(io.BytesIO):
    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def close(self):
        self._sink.append(self.getvalue())
        super().close()


def _remote(calls, sent, *, exit_code=0, stdout=b"ok\n", stderr=b"", hang=False):
    class Process:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            self.returncode = None
            self.stdin = _Stdin(sent)
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if self.returncode is None and hang:
                raise subprocess.TimeoutExpired("ssh", timeout)
            if self.returncode is None:
                self.returncode = exit_code
            return self.returncode

    return Process


def _run_runtime(store, prompt="y"):
    registry = ToolRegistry()
    registry.register_all(ssh.ssh_tools(store))
    return ToolRuntime(registry, PolicyEngine(), ApprovalEngine(prompt=lambda _: prompt))


def test_run_sends_the_script_on_stdin_with_pinned_host_keys(setup, monkeypatch):
    store, ctx, _registry, _policy = setup
    calls, sent = [], []
    monkeypatch.setattr(ssh.subprocess, "Popen", _remote(calls, sent, stdout=b"active\n"))
    script = "set -e\r\nsystemctl is-active 'web' \"$UNIT\"\r\necho done"
    result = _run_runtime(store).execute("ssh.run", {"target_id": "Fixture", "script": script}, ctx)
    assert result.ok and result.data["exit_code"] == 0
    argv, kwargs = calls[0]
    # The script never touches a local or remote command line: no quoting.
    assert argv[-2:] == ["192.0.2.10", "bash -s"] and script not in " ".join(argv)
    assert "-n" not in argv and kwargs["stdin"] == subprocess.PIPE and kwargs["shell"] is False
    assert "StrictHostKeyChecking=yes" in argv and "IdentityAgent=none" in argv
    # CRLF from a Windows editor would reach bash as part of each command.
    assert sent == [b"set -e\nsystemctl is-active 'web' \"$UNIT\"\necho done\n"]
    text = result.to_model_text("ssh.run")
    assert "--- stdout ---\nactive" in text and "systemctl" not in text


def test_run_reports_the_script_exit_code_and_classifies_ssh_failures(setup, monkeypatch):
    store, ctx, _registry, _policy = setup
    rt = _run_runtime(store)
    monkeypatch.setattr(
        ssh.subprocess, "Popen", _remote([], [], exit_code=3, stderr=b"no such unit\n")
    )
    failed_script = rt.execute("ssh.run", {"target_id": "fixture", "script": "false"}, ctx)
    assert failed_script.ok and failed_script.data["exit_code"] == 3
    assert "exit_code: 3 (failed" in failed_script.to_model_text("ssh.run")

    rejected = b"Host key verification failed.\r\n"
    monkeypatch.setattr(ssh.subprocess, "Popen", _remote([], [], exit_code=255, stderr=rejected))
    host_key = rt.execute("ssh.run", {"target_id": "fixture", "script": "true"}, ctx)
    assert not host_key.ok and host_key.error.code == "AUTH_REQUIRED"

    # A script may exit 255 itself; without an ssh diagnostic it is its answer.
    monkeypatch.setattr(ssh.subprocess, "Popen", _remote([], [], exit_code=255))
    assert rt.execute("ssh.run", {"target_id": "fixture", "script": "exit 255"}, ctx).ok

    monkeypatch.setattr(ssh.subprocess, "Popen", _remote([], [], hang=True))
    monkeypatch.setattr(ssh, "_kill_tree", lambda process: setattr(process, "returncode", -9))
    timed_out = rt.execute(
        "ssh.run", {"target_id": "fixture", "script": "sleep 99", "timeout_s": 1}, ctx
    )
    assert not timed_out.ok and timed_out.error.code == "TIMEOUT"
    assert "timeout_s=1s" in timed_out.error.message


def test_run_is_never_freer_than_a_shell_ssh_command(setup, monkeypatch):
    store, ctx, _registry, _policy = setup
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not dial"))
    arguments = {"target_id": "fixture", "script": "rm -rf /srv/app"}
    rt = _run_runtime(store, prompt="n")
    # Read-only (PLAN/REVIEW): no remote actions at all.
    read_only = dataclasses.replace(ctx, profile=PermissionProfile.READ_ONLY)
    denied = rt.execute("ssh.run", arguments, read_only)
    assert denied.error.code == "POLICY_DENIED"
    # A turn started by another agent's message cannot run commands anywhere.
    peer = dataclasses.replace(ctx, origin_kind="peer")
    assert rt.execute("ssh.run", arguments, peer).error.code == "POLICY_DENIED"
    # Workspace: acting on an internet host asks, and a refusal stops it.
    assert rt.execute("ssh.run", arguments, ctx).error.code == "APPROVAL_DENIED"


def test_run_rejects_an_unknown_destination_before_asking(setup, monkeypatch):
    store, ctx, _registry, _policy = setup
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not dial"))
    asked = []
    registry = ToolRegistry()
    registry.register_all(ssh.ssh_tools(store))
    rt = ToolRuntime(
        registry, PolicyEngine(), ApprovalEngine(prompt=lambda request: asked.append(1) or "y")
    )
    result = rt.execute("ssh.run", {"target_id": "nowhere", "script": "id"}, ctx)
    assert result.error.code == "INVALID_ARGUMENT" and not asked


def test_bound_remote_operations_keep_their_read_only_surface(setup):
    store, _ctx, _registry, _policy = setup
    bound = ssh.ssh_tools(store, store.get("fixture"))
    assert [tool.name for tool in bound] == ["ssh.inspect"]
    assert bound[0].always_loaded
    # In ordinary sessions both are on demand (capability.search finds them).
    assert {tool.name: tool.always_loaded for tool in ssh.ssh_tools(store)} == {
        "ssh.inspect": False,
        "ssh.run": False,
    }
