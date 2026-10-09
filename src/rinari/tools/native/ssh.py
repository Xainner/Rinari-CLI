"""Typed SSH on registered destinations over pinned OpenSSH, through the common runtime.

`ssh.inspect` reads fixed hardware facts; `ssh.run` runs a script on the
destination. Both share one connection builder: host keys are pinned (never
learned or replaced), no agent, no forwarding, no local commands, bounded
output and an explicit deadline.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from rinari.tools.definition import (
    RISK_HIGH,
    SIDE_EFFECT_REMOTE_DESTRUCTIVE,
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
)
from rinari.tools.native.shell import (
    MAX_OUTPUT_BYTES,
    _BoundedBuffer,
    _drain,
    _ExecutionCapture,
    _fail,
    _kill_tree,
    effective_timeout,
    timeout_message,
)

COMMANDS = {
    "system": "/usr/bin/uname -a",
    "gpu": (
        "/usr/bin/nvidia-smi --query-gpu=name,memory.total,driver_version "
        "--format=csv,noheader,nounits"
    ),
    "cpu": "/usr/bin/lscpu --json",
    "memory": "/usr/bin/free --bytes",
    "disks": "/usr/bin/lsblk --json --bytes --output NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS",
}
HARDWARE_SECTIONS = tuple(COMMANDS)
COMMANDS["hardware"] = "; ".join(
    f"printf '\\nRINARI_SECTION_{name}\\n'; {command}; printf '\\nRINARI_EXIT_%s\\n' \"$?\""
    for name, command in COMMANDS.items()
)

# ssh.run: the script travels on stdin to `<shell> -s`, so it is never parsed
# by a local shell and needs no quoting at all (cmd -> ssh -> bash nesting was
# the main source of broken remote commands).
REMOTE_SHELLS = ("bash", "sh")
MAX_SCRIPT_BYTES = 64 * 1024
RUN_DEFAULT_TIMEOUT_S = 60.0
RUN_MAX_TIMEOUT_S = 600.0
# OpenSSH reports its own failures with exit status 255. A script may also
# exit 255, so the status alone is not enough: these diagnostics are ssh's.
_SSH_DIAGNOSTIC = re.compile(
    r"^ssh: |host key verification failed|remote host identification has changed|"
    r"permission denied \(|connection (refused|timed out|closed|reset)|"
    r"could not resolve hostname|no route to host|network is unreachable",
    re.IGNORECASE | re.MULTILINE,
)


def hardware_results(output: str) -> dict:
    sections = {}
    for name in HARDWARE_SECTIONS:
        marker = f"RINARI_SECTION_{name}\n"
        if marker not in output:
            sections[name] = {"ok": False, "error": "missing_output"}
            continue
        block = output.split(marker, 1)[1].split("RINARI_SECTION_", 1)[0]
        body, separator, status = block.rpartition("RINARI_EXIT_")
        try:
            code = int(status.strip()) if separator else None
        except ValueError:
            code = None
        sections[name] = {"ok": code == 0, "exit_code": code, "output": body.strip()}
    return sections


def _identity(store, destination) -> Path | ToolResult:
    is_alias = destination.get("source") == "openssh-config"
    key = (
        Path(destination["identity_path"])
        if is_alias
        else store.root / "identities" / destination["identity"]
    )
    if (
        key.is_symlink()
        or not key.is_file()
        or (not is_alias and key.resolve().parent != (store.root / "identities").resolve())
    ):
        return _fail(ToolErrorCode.AUTH_REQUIRED, "Provision the installation SSH identity first")
    if os.name != "nt" and key.stat().st_mode & 0o077:
        return _fail(ToolErrorCode.AUTH_REQUIRED, "SSH identity requires mode 0600")
    return key


def _argv(folder: Path, destination: dict, key: Path, remote_command: str, *, stdin: bool):
    """Pinned OpenSSH argv; writes the empty config and pinned known_hosts into `folder`."""
    is_alias = destination.get("source") == "openssh-config"
    (folder / "config").write_text("", encoding="utf-8")
    alias = "rinari-" + destination["id"]
    if not is_alias:
        (folder / "known_hosts").write_text(
            alias + " " + destination["host_key"] + "\n", encoding="utf-8"
        )
    host_key_alias = destination.get("host_alias") if is_alias else alias
    host_key_options = ["-o", "HostKeyAlias=" + host_key_alias] if host_key_alias else []
    return [
        "ssh",
        "-F",
        str(folder / "config"),
        "-T",
        # -n closes stdin; ssh.run needs it open to send the script.
        *([] if stdin else ["-n"]),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile="
        + (destination["known_hosts"] if is_alias else str(folder / "known_hosts")),
        "-o",
        "GlobalKnownHostsFile=" + os.devnull,
        *host_key_options,
        "-o",
        "HostKeyAlgorithms="
        + (
            "ssh-ed25519,rsa-sha2-512,rsa-sha2-256,ecdsa-sha2-nistp256"
            if is_alias
            else "ssh-ed25519"
        ),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(key),
        "-p",
        str(destination["port"]),
        "-l",
        destination["username"],
        destination["host"],
        remote_command,
    ]


def _execute(argv, ctx, timeout, stdout, stderr, *, script=None, sink=None, capture=None):
    """Run ssh to completion, timeout or cancellation; returns (process, timed_out)."""
    kwargs = {
        "stdin": subprocess.PIPE if script is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    threads = [
        threading.Thread(target=_drain, args=(stream, buffer, name, sink, capture), daemon=True)
        for stream, buffer, name in (
            (process.stdout, stdout, "stdout"),
            (process.stderr, stderr, "stderr"),
        )
    ]
    if script is not None:

        def feed():
            # A writer thread: a large script must not block the wait (and
            # its timeout) when the remote side stops reading.
            with contextlib.suppress(OSError, ValueError):
                process.stdin.write(script)
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()

        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    remove = (
        ctx.cancellation.on_cancel(lambda: _kill_tree(process) if process.poll() is None else None)
        if ctx.cancellation
        else None
    )
    timed_out = False
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        if remove:
            remove()
        if process.poll() is None:
            _kill_tree(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        for stream in (process.stdout, process.stderr, getattr(process, "stdin", None)):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
    return process, timed_out


def _transport_failure(stderr_text: str) -> tuple[ToolErrorCode, str] | None:
    diagnostic = stderr_text.lower()
    if (
        "host key verification failed" in diagnostic
        or "host identification has changed" in diagnostic
    ):
        return (
            ToolErrorCode.AUTH_REQUIRED,
            "Verify the host key in known_hosts; Rinari will not replace it",
        )
    if "permission denied" in diagnostic:
        return (
            ToolErrorCode.AUTH_REQUIRED,
            "SSH authentication failed; verify the configured user and identity",
        )
    return None


def ssh_tools(store, bound=None):
    def target(args):
        if bound is not None:
            return bound if args.get("target_id") == bound["id"] else None
        if store is None:
            return None
        value = args.get("target_id")
        exact = store.get(value)
        if exact is not None:
            return exact
        matches = [row for row in store.list() if row.get("name") == value]
        if matches:
            return matches[0] if len(matches) == 1 else None
        from rinari.application.ssh_aliases import resolve_alias

        try:
            return resolve_alias(str(value or ""), Path.home())
        except (OSError, ValueError):
            return None

    def connect(args, ctx, source):
        """The destination and its identity, or the failure that stops the call."""
        from rinari.application.ssh_targets import TargetStoreUnavailable

        try:
            destination = target(args)
        except TargetStoreUnavailable as exc:
            return None, None, _fail(ToolErrorCode.PERMISSION_DENIED, str(exc))
        if destination is None:
            return None, None, _fail(ToolErrorCode.INVALID_ARGUMENT, UNKNOWN_TARGET)
        if ctx.network is None:
            return None, None, _fail(ToolErrorCode.POLICY_DENIED, "Missing network guard")
        ctx.network.assert_reachable(destination["host"], source=source)
        if ctx.cancellation:
            ctx.cancellation.throw_if_cancelled()
        key = _identity(store, destination)
        if isinstance(key, ToolResult):
            return None, None, key
        return destination, key, None

    def inspect(args, ctx):
        args = {"section": "hardware", **args}
        if args.get("section") not in COMMANDS:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, UNKNOWN_TARGET)
        destination, key, failure = connect(args, ctx, "ssh.inspect")
        if failure is not None:
            return failure
        timeout = min(30.0, ctx.limits.timeout_s)
        if ctx.deadline_at is not None:
            timeout = min(timeout, max(0.01, ctx.deadline_at - time.time()))
        output = _BoundedBuffer(min(ctx.limits.max_output_bytes, 65536))
        errors = _BoundedBuffer(8192)
        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            argv = _argv(Path(directory), destination, key, COMMANDS[args["section"]], stdin=False)
            try:
                process, timed_out = _execute(argv, ctx, timeout, output, errors)
            except OSError:
                return _fail(ToolErrorCode.DEPENDENCY_ERROR, "OpenSSH could not start")
        partial = {
            "target_id": destination["id"],
            "revision": destination["revision"],
            "section": args["section"],
            "output": output.text(),
            "stderr": errors.text(),
            "exit_code": process.returncode,
            **(
                {"sections": hardware_results(output.text())}
                if args["section"] == "hardware"
                else {}
            ),
        }
        if ctx.cancellation and ctx.cancellation.cancelled:
            return _fail(
                ToolErrorCode.CANCELLED,
                "SSH inspection cancelled; remote completion not guaranteed",
                data=partial,
            )
        if timed_out:
            return _fail(ToolErrorCode.TIMEOUT, "SSH inspection timed out", data=partial)
        if process.returncode != 0:
            known = _transport_failure(errors.text())
            if known is not None:
                code, message = known
            elif process.returncode == 127:
                code = ToolErrorCode.DEPENDENCY_ERROR
                message = "The remote inspection utility is unavailable"
            else:
                code = ToolErrorCode.NETWORK_ERROR
                message = "SSH connection failed; inspect stderr and destination connectivity"
            return _fail(code, message, data=partial)
        return ToolResult(
            ok=True,
            data={
                "target_id": destination["id"],
                "revision": destination["revision"],
                "section": args["section"],
                "output": output.text(),
                **(
                    {"sections": hardware_results(output.text())}
                    if args["section"] == "hardware"
                    else {}
                ),
            },
            truncated=output.truncated,
        )

    def run(args, ctx):
        script = args.get("script")
        if not isinstance(script, str) or not script.strip():
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "script is required")
        # A script written on Windows arrives with CRLF; bash would read
        # `\r` as part of every command (`command not found: ls\r`).
        payload = script.replace("\r\n", "\n").encode("utf-8")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        if len(payload) > MAX_SCRIPT_BYTES:
            return _fail(
                ToolErrorCode.INVALID_ARGUMENT,
                f"script exceeds {MAX_SCRIPT_BYTES} bytes; copy large files with another tool "
                "and keep the script short",
            )
        shell = args.get("shell") or "bash"
        if shell not in REMOTE_SHELLS:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "shell must be bash or sh")
        destination, key, failure = connect(args, ctx, "ssh.run")
        if failure is not None:
            return failure
        timeout = effective_timeout(args.get("timeout_s"), RUN_DEFAULT_TIMEOUT_S, ctx)
        max_bytes = min(ctx.limits.max_output_bytes, MAX_OUTPUT_BYTES)
        stdout = _BoundedBuffer(max_bytes)
        stderr = _BoundedBuffer(max_bytes)
        capture = _ExecutionCapture()
        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            argv = _argv(Path(directory), destination, key, f"{shell} -s", stdin=True)
            try:
                process, timed_out = _execute(
                    argv,
                    ctx,
                    timeout["effective_s"],
                    stdout,
                    stderr,
                    script=payload,
                    sink=ctx.output_sink,
                    capture=capture,
                )
            except OSError:
                return _fail(ToolErrorCode.DEPENDENCY_ERROR, "OpenSSH could not start")
        label = destination.get("name") or destination["id"]
        data = {
            "target_id": destination["id"],
            "revision": destination["revision"],
            "shell": shell,
            # Display form for the activity card; the model already has it.
            "command": f"ssh {label} {shell} -s <<'EOF'\n{payload.decode('utf-8').rstrip()}\nEOF",
            "exit_code": process.returncode if process.returncode is not None else -1,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "truncated": stdout.truncated or stderr.truncated,
            "timeout": timeout,
        }
        captured = capture.result() if data["truncated"] else None
        if ctx.cancellation and ctx.cancellation.cancelled:
            result = _fail(
                ToolErrorCode.CANCELLED,
                "SSH command cancelled; remote completion not guaranteed",
                data=data,
            )
        elif timed_out:
            result = _fail(
                ToolErrorCode.TIMEOUT,
                timeout_message(timeout) + "; the remote side may still be running it",
                data=data,
            )
        elif process.returncode == 255 and _SSH_DIAGNOSTIC.search(data["stderr"]):
            code, message = _transport_failure(data["stderr"]) or (
                ToolErrorCode.NETWORK_ERROR,
                "SSH connection failed; inspect stderr and destination connectivity",
            )
            result = _fail(code, message, data=data)
        else:
            # Like shell.exec, a non-zero exit is the script's answer, not a
            # tool failure: the model reads exit_code and stderr.
            result = ToolResult(ok=True, data=data, truncated=data["truncated"])
            missing = rf"\b{shell}: (command )?not found|\b{shell}: no such file"
            if process.returncode == 127 and re.search(missing, data["stderr"], re.IGNORECASE):
                data["hint"] = "The remote shell is missing; retry with shell=sh"
        from dataclasses import replace

        return replace(result, captured_output=captured)

    def classify_run(args):
        try:
            host = (target(args) or {}).get("host", "")
        except OSError:  # an unreadable store fails in the handler, precisely
            host = ""
        return [
            # Running commands on another machine is acting, not reading.
            ClassifiedAction("network.outbound", host, "send"),
            # And it is a command: every shell rule applies (read-only asks,
            # peer turns are denied), never less than `shell.exec "ssh ..."`.
            # The quoted payload runs remotely, as policy reads `ssh host '...'`.
            ClassifiedAction("shell.exec", f"ssh {host} '" + str(args.get("script") or "")),
        ]

    def precheck_run(args, ctx):
        # Reject an unknown destination before asking for an approval it
        # could never use.
        from rinari.application.ssh_targets import TargetStoreUnavailable

        try:
            known = target(args) is not None
        except TargetStoreUnavailable:
            return None  # the handler reports the store failure precisely
        return None if known else _fail(ToolErrorCode.INVALID_ARGUMENT, UNKNOWN_TARGET)

    target_schema = {"type": "string", **({"enum": [bound["id"]]} if bound else {})}
    tools = [
        ToolDefinition(
            name="ssh.inspect",
            namespace="ssh",
            description=(
                "Read Linux hardware on a registered SSH destination (ID or unique name). "
                "Use section=hardware for system, CPU, memory, disks and GPU in ONE connection. "
                "Section markers and exit codes report partial failures. "
                "Also resolves static ~/.ssh/config aliases using existing keys and known_hosts; "
                "never learns or replaces host keys."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": target_schema,
                    "section": {"type": "string", "enum": list(COMMANDS), "default": "hardware"},
                },
                "required": ["target_id"],
                "additionalProperties": False,
            },
            capabilities=("network.outbound",),
            risk="medium",
            timeout_ms=35000,
            # Running commands on another machine is acting, not reading:
            # free on the LAN, asks for an internet host (workspace).
            classify=lambda args: ClassifiedAction(
                "network.outbound", (target(args) or {}).get("host", ""), "send"
            ),
            handler=inspect,
            # A session bound to one destination has nothing else to show.
            always_loaded=bound is not None,
        )
    ]
    if bound is None:
        # A bound remote operation keeps its read-only surface: running
        # arbitrary scripts there is a separate decision for its owner.
        tools.append(
            ToolDefinition(
                name="ssh.run",
                namespace="ssh",
                description=(
                    "Run a shell script on a registered SSH destination (ID or unique name, or a "
                    "static ~/.ssh/config alias). The script is sent on stdin to `bash -s` (or "
                    "`sh -s`): write it exactly as on the remote shell, multi-line, with no extra "
                    "quoting. Returns exit code and bounded stdout/stderr. Prefer this to "
                    "shell.exec with `ssh host '...'`. Never learns or replaces host keys."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_id": target_schema,
                        "script": {"type": "string", "minLength": 1},
                        "shell": {"type": "string", "enum": list(REMOTE_SHELLS)},
                        "timeout_s": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": RUN_MAX_TIMEOUT_S,
                            "description": "Seconds; default 60, max 600.",
                        },
                    },
                    "required": ["target_id", "script"],
                    "additionalProperties": False,
                },
                capabilities=("network.outbound",),
                risk=RISK_HIGH,
                side_effects=SIDE_EFFECT_REMOTE_DESTRUCTIVE,
                idempotent=False,
                timeout_ms=int(RUN_MAX_TIMEOUT_S * 1000) + 15_000,
                classify_many=classify_run,
                precheck=precheck_run,
                handler=run,
                always_loaded=False,
            )
        )
    return tools


UNKNOWN_TARGET = (
    "Unknown registered SSH target or inspection. This tool uses Rinari's "
    "registered targets and static OpenSSH aliases (no Match/Include/proxy). "
    "Configure the target "
    "or use an authorized shell connection; repeating this call will not resolve it."
)
