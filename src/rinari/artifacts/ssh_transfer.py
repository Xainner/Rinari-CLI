"""Pinned OpenSSH file retrieval; no media decoding or generation."""

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_REMOTE = r"""
import os,sys,stat,json,hashlib
p=sys.argv[1]
assert os.path.isabs(p) and os.path.realpath(p)==p
with open(p,'rb') as f:
 s=os.fstat(f.fileno());assert stat.S_ISREG(s.st_mode) and s.st_size<=int(sys.argv[2])
 o=sys.stdout.buffer;o.write((json.dumps({'size':s.st_size})+'\n').encode());o.flush()
 h=hashlib.sha256()
 while b:=f.read(1048576):h.update(b);o.write(b)
 a=os.fstat(f.fileno())
 assert (s.st_ino,s.st_size,s.st_mtime_ns)==(a.st_ino,a.st_size,a.st_mtime_ns)
 o.write(h.hexdigest().encode());o.flush()
"""


def retrieve(store, target, path, ctx, maximum=None):
    from rinari.artifacts.limits import limit

    maximum = maximum if maximum is not None else limit("quota_bytes", 10 * 1024**3)
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("Remote path must be absolute")
    if ctx.network is None:
        raise ValueError("Missing network guard")
    ctx.network.assert_reachable(target["host"], source="artifact.import")
    key = store.root / "identities" / target["identity"]
    if (
        key.is_symlink()
        or not key.is_file()
        or key.resolve().parent != (store.root / "identities").resolve()
    ):
        raise ValueError("Missing installation SSH identity")
    if os.name != "nt" and key.stat().st_mode & 0o077:
        raise ValueError("SSH identity requires mode 0600")
    temporary = tempfile.TemporaryDirectory(dir=store.root)
    folder = Path(temporary.name)
    alias = "rinari-" + target["id"]
    (folder / "known_hosts").write_text(alias + " " + target["host_key"] + "\n")
    (folder / "config").write_text("")
    command = "python3 -c " + shlex.quote(_REMOTE) + " " + shlex.quote(path) + " " + str(maximum)
    argv = [
        "ssh",
        "-F",
        str(folder / "config"),
        "-T",
        "-n",
        "-i",
        str(key),
        "-p",
        str(target["port"]),
        "-l",
        target["username"],
    ]
    for option in [
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "ConnectTimeout=10",
        "ConnectionAttempts=1",
        "HostKeyAlias=" + alias,
        "UserKnownHostsFile=" + str(folder / "known_hosts"),
        "GlobalKnownHostsFile=" + os.devnull,
    ]:
        argv.extend(["-o", option])
    argv.extend([target["host"], command])
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),
    )
    finished = threading.Event()
    deadline = min(ctx.deadline_at or float("inf"), time.time() + 120)

    def watchdog():
        while not finished.wait(0.05):
            try:
                if ctx.cancellation:
                    ctx.cancellation.throw_if_cancelled()
                if time.time() < deadline:
                    continue
            except Exception:
                pass
            process.kill()
            return

    worker = threading.Thread(target=watchdog, daemon=True)
    worker.start()
    try:
        header = process.stdout.readline(4096)
        count = json.loads(header)["size"]
        if type(count) is not int or not 0 <= count <= maximum:
            raise ValueError("Remote file exceeds limit")
        (folder / "payload").mkdir()
        result = folder / "payload" / Path(path).name
        digest = hashlib.sha256()
        with result.open("wb") as output:
            remaining = count
            while remaining:
                chunk = process.stdout.read(min(1024**2, remaining))
                if not chunk:
                    raise ValueError("Remote transfer interrupted")
                remaining -= len(chunk)
                digest.update(chunk)
                output.write(chunk)
        expected = process.stdout.read(65)
        if expected != digest.hexdigest().encode() or process.wait(timeout=5) != 0:
            raise ValueError("Remote file changed or failed integrity check")
        return temporary, result
    except BaseException:
        temporary.cleanup()
        raise
    finally:
        finished.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        worker.join(timeout=1)
