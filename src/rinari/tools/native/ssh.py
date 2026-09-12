"""Bounded Linux inspection over pinned OpenSSH, through the common runtime."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from rinari.tools.definition import ClassifiedAction, ToolDefinition, ToolErrorCode, ToolResult
from rinari.tools.native.shell import _BoundedBuffer, _drain, _fail, _kill_tree

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

    def inspect(args, ctx):
        args = {"section": "hardware", **args}
        destination = target(args)
        if destination is None or args.get("section") not in COMMANDS:
            return _fail(
                ToolErrorCode.INVALID_ARGUMENT,
                "Unknown registered SSH target or inspection. This tool uses Rinari's "
                "registered targets and static OpenSSH aliases (no Match/Include/proxy). "
                "Configure the target "
                "or use an authorized shell connection; repeating this call will not resolve it.",
            )
        if ctx.network is None:
            return _fail(ToolErrorCode.POLICY_DENIED, "Missing network guard")
        ctx.network.assert_reachable(destination["host"], source="ssh.inspect")
        if ctx.cancellation:
            ctx.cancellation.throw_if_cancelled()
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
            return _fail(
                ToolErrorCode.AUTH_REQUIRED, "Provision the installation SSH identity first"
            )
        if os.name != "nt" and key.stat().st_mode & 0o077:
            return _fail(ToolErrorCode.AUTH_REQUIRED, "SSH identity requires mode 0600")
        timeout = min(30.0, ctx.limits.timeout_s)
        if ctx.deadline_at is not None:
            timeout = min(timeout, max(0.01, ctx.deadline_at - time.time()))
        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            folder = Path(directory)
            (folder / "config").write_text("", encoding="utf-8")
            alias = "rinari-" + destination["id"]
            if not is_alias:
                (folder / "known_hosts").write_text(
                    alias + " " + destination["host_key"] + "\n", encoding="utf-8"
                )
            host_key_alias = destination.get("host_alias") if is_alias else alias
            host_key_options = ["-o", "HostKeyAlias=" + host_key_alias] if host_key_alias else []
            argv = [
                "ssh",
                "-F",
                str(folder / "config"),
                "-T",
                "-n",
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
                COMMANDS[args["section"]],
            ]
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(argv, **kwargs)
            except OSError:
                return _fail(ToolErrorCode.DEPENDENCY_ERROR, "OpenSSH could not start")
            output = _BoundedBuffer(min(ctx.limits.max_output_bytes, 65536))
            errors = _BoundedBuffer(8192)
            readers = [
                threading.Thread(target=_drain, args=(stream, buffer, name), daemon=True)
                for stream, buffer, name in (
                    (process.stdout, output, "stdout"),
                    (process.stderr, errors, "stderr"),
                )
            ]
            for reader in readers:
                reader.start()
            remove = (
                ctx.cancellation.on_cancel(
                    lambda: _kill_tree(process) if process.poll() is None else None
                )
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
                for reader in readers:
                    reader.join(timeout=1)
                for stream in (process.stdout, process.stderr):
                    stream.close()
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
                diagnostic = errors.text().lower()
                if (
                    "host key verification failed" in diagnostic
                    or "host identification has changed" in diagnostic
                ):
                    code = ToolErrorCode.AUTH_REQUIRED
                    message = "Verify the host key in known_hosts; Rinari will not replace it"
                elif "permission denied" in diagnostic:
                    code = ToolErrorCode.AUTH_REQUIRED
                    message = "SSH authentication failed; verify the configured user and identity"
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

    return [
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
                    "target_id": {"type": "string", **({"enum": [bound["id"]]} if bound else {})},
                    "section": {"type": "string", "enum": list(COMMANDS), "default": "hardware"},
                },
                "required": ["target_id"],
                "additionalProperties": False,
            },
            capabilities=("network.outbound",),
            risk="medium",
            timeout_ms=35000,
            classify=lambda args: ClassifiedAction(
                "network.outbound", (target(args) or {}).get("host", "")
            ),
            handler=inspect,
        )
    ]
