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
