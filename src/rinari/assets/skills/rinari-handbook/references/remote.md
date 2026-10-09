# Working on a remote machine over SSH

Typed tools work on registered SSH destinations and static OpenSSH config aliases. They are on demand: `capability.search` with `query: "ssh"` and `load=true` exposes them.

## Read first, act second
- Hardware and OS facts: `ssh.inspect` (`section: "hardware"` gathers everything in one connection).
- Anything else: `ssh.run` with a `script`. The script is sent on stdin to `bash -s` (or `sh -s`), so write it exactly as you would type it on the remote shell: several lines, normal quotes, no escaping.
- Avoid `shell.exec` with `ssh host '...'`: the command passes through the local shell, then ssh, then the remote shell, and quotes break at every layer (worst on Windows, where cmd.exe is first).
- One script per step, not one call per command: group related commands and print what you need to decide the next step.
- Set `timeout_s` for long work (up to 600 s). A timeout or a cancellation does not prove the remote command stopped; check before repeating it.
- Start scripts that change things with `set -eu` so they stop at the first failure instead of continuing on a broken state.

## Changing remote state
- Back up before you edit configuration: copy the file next to itself with a timestamp (`cp -a file file.bak.$(date +%Y%m%d%H%M%S)`), then edit, so the change can be undone.
- Validate before applying when the service offers a check (`nginx -t`, `sshd -t`, `systemd-analyze verify`, `docker compose config`), and reload rather than restart when that is enough.
- Never lock yourself out: be especially careful with the SSH daemon, firewalls and network configuration; keep the current session working until a new connection has been verified.
- After changing a service, verify it from outside the machine as well (an HTTP request from here, a port check, a new SSH connection), not only with its local status: "active" on the server does not mean reachable for its users.

## Errors
- `AUTH_REQUIRED`: host key mismatch or failed authentication. Rinari never learns or replaces host keys; the owner has to verify and fix it.
- `NETWORK_ERROR`: the connection failed (DNS, route, port, firewall). Diagnose connectivity before retrying.
- A non-zero `exit_code` is the script's own result: read its stderr.
