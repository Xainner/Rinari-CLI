"""Policy engine: decides allow / ask / deny per tool action.

Policy is computed before each model/tool interaction (harness.md 74-79).
It is separated from the sandbox (what can technically execute) and from
approvals (what requires consent). Locked system rules are applied first
and can never be relaxed by a profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PermissionProfile(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE = "workspace"
    FULL_ACCESS = "full-access"


def normalize_profile(name: str | None) -> PermissionProfile:
    if name in ("read-only", "safe"):
        return PermissionProfile.READ_ONLY
    if name == "full-access":
        return PermissionProfile.FULL_ACCESS
    return PermissionProfile.WORKSPACE


class PolicyAction(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


CAPABILITY_FS_READ = "fs.read"
CAPABILITY_FS_WRITE = "fs.write"
CAPABILITY_SHELL = "shell.exec"
CAPABILITY_GIT_LOCAL = "git.local"


@dataclass(frozen=True, slots=True)
class SessionScope:
    kind: str  # "CHAT" | "PROJECT"
    root: Path | None  # project root (PROJECT) or cwd (CHAT)
    cwd: Path
    profile: PermissionProfile = PermissionProfile.WORKSPACE
    user_home: Path | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    capability: str
    reason: str
    target: str | None = None
    risk: str = "low"
    risk_class: str = "none"


# Locked system rules (harness.md 74-79; AGENTS.md 13, 31). They fire
# before profile evaluation and no config layer may relax them.
SENSITIVE_FILE = re.compile(
    r"(\.env(\..+)?$|(^|/)\.env\..+$|\.pem$|\.p12$|\.pfx$|\.key$|id_rsa.*|"
    r"\.netrc$|\.npmrc$|credentials(\.json)?|secrets?\.(json|ya?ml|toml)$)",
    re.IGNORECASE,
)

SYSTEM_RULES: tuple[str, ...] = (
    "$HOME is never an implicit writable project root",
    "sensitive credential files always require approval",
    "remote Git mutations always require approval",
)


def is_sensitive_file(path: Path) -> bool:
    name = path.name
    return bool(SENSITIVE_FILE.search(name)) or bool(SENSITIVE_FILE.search(str(path)))


def classify_git_remote(command: str) -> str | None:
    """Return 'force' | 'remote' if any shell segment mutates a remote.

    Splitting on shell separators avoids false positives on strings that
    merely mention `git push` (echo git push, echo "git push").
    """
    for segment in re.split(r"[;|&]+|\|\|", command):
        segment = segment.strip()
        if not re.match(r"^\s*(env\s+)?git\s+push\b", segment):
            continue
        if re.search(r"\s(--force\b|-f\b|--force-with-lease\b)", segment):
            return "force"
        return "remote"
    return None


class PolicyEngine:
    def decide(
        self,
        capability: str,
        scope: SessionScope,
        *,
        path: str | Path | None = None,
        command: str | None = None,
        risk: str = "low",
        risk_class: str = "none",
    ) -> PolicyDecision:
        if capability in (CAPABILITY_FS_READ, CAPABILITY_GIT_LOCAL):
            return self._fs_read(scope, path, risk, risk_class, capability)
        if capability == CAPABILITY_FS_WRITE:
            return self._fs_write(scope, path, risk, risk_class)
        if capability == CAPABILITY_SHELL:
            return self._shell(scope, command, risk, risk_class)
        return PolicyDecision(
            action=PolicyAction.ASK,
            capability=capability,
            reason=f"capability {capability!r} is not covered by a built-in rule yet",
            target=None,
            risk=risk,
            risk_class=risk_class,
        )

    # -- filesystem read ----------------------------------------------------

    def _fs_read(
        self,
        scope: SessionScope,
        path: str | Path | None,
        risk: str,
        risk_class: str,
        capability: str = CAPABILITY_FS_READ,
    ) -> PolicyDecision:
        if capability == CAPABILITY_GIT_LOCAL and path is None:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason="local Git inspection on the session repository",
                risk=risk,
                risk_class=risk_class,
            )
        if path is None:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_READ,
                reason="read without an explicit path is not allowed",
                risk=risk,
                risk_class=risk_class,
            )
        resolved = self._resolve(scope, path)
        if is_sensitive_file(resolved):
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_FS_READ,
                reason="sensitive credential file (locked system rule)",
                target=str(resolved),
                risk="high",
                risk_class="credential",
            )
        if self._inside_root(resolved, scope.root):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_READ,
                reason=f"inside the {scope.kind} session root",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_READ,
                reason="full-access profile reads outside the root are allowed",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        if scope.profile is PermissionProfile.READ_ONLY:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_READ,
                reason="read-only profile is limited to the session root",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        return PolicyDecision(
            action=PolicyAction.ASK,
            capability=CAPABILITY_FS_READ,
            reason="workspace profile: reads outside the session root require approval",
            target=str(resolved),
            risk=risk,
            risk_class=risk_class,
        )

    # -- filesystem write ---------------------------------------------------

    def _fs_write(
        self, scope: SessionScope, path: str | Path | None, risk: str, risk_class: str
    ) -> PolicyDecision:
        if path is None:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_WRITE,
                reason="write without an explicit path is not allowed",
                risk=risk,
                risk_class=risk_class,
            )
        resolved = self._resolve(scope, path)
        if is_sensitive_file(resolved):
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_FS_WRITE,
                reason="sensitive credential file (locked system rule)",
                target=str(resolved),
                risk="critical",
                risk_class="credential",
            )
        root = scope.root
        home_root = root is not None and self._is_home(root, scope)
        if self._is_home(resolved, scope) and not home_root:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_WRITE,
                reason="$HOME is never an implicit writable root (locked system rule)",
                target=str(resolved),
                risk="high",
                risk_class="local-destructive",
            )
        inside = self._inside_root(resolved, scope.root)
        if inside and scope.profile is not PermissionProfile.READ_ONLY and scope.kind == "PROJECT":
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_WRITE,
                reason="workspace/full-access profile may write inside the project root",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        if scope.profile is PermissionProfile.READ_ONLY:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_WRITE,
                reason="read-only profile cannot write",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        if scope.profile is PermissionProfile.FULL_ACCESS and scope.kind == "CHAT":
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_WRITE,
                reason="full-access profile in a chat session",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        return PolicyDecision(
            action=PolicyAction.ASK,
            capability=CAPABILITY_FS_WRITE,
            reason=(
                "chat session writes require approval (no implicit workspace)"
                if scope.kind == "CHAT"
                else "write outside the project root requires approval"
            ),
            target=str(resolved),
            risk=risk,
            risk_class=risk_class,
        )

    # -- shell ---------------------------------------------------------------

    def _shell(
        self,
        scope: SessionScope,
        command: str | None,
        risk: str,
        risk_class: str,
    ) -> PolicyDecision:
        remote = classify_git_remote(command or "")
        if remote == "force":
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_SHELL,
                reason="force push mutates remote history (locked system rule)",
                target=command,
                risk="critical",
                risk_class="remote-destructive",
            )
        if remote == "remote":
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_SHELL,
                reason="remote Git mutation requires approval",
                target=command,
                risk="high",
                risk_class="remote-reversible",
            )
        if scope.profile is PermissionProfile.READ_ONLY:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_SHELL,
                reason="read-only profile cannot execute commands",
                target=command,
                risk=risk,
                risk_class=risk_class,
            )
        if scope.kind == "PROJECT" and scope.root is not None:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_SHELL,
                reason="project-local execution inside the workspace",
                target=command,
                risk=risk,
                risk_class=risk_class,
            )
        return PolicyDecision(
            action=PolicyAction.ASK,
            capability=CAPABILITY_SHELL,
            reason="chat session shell execution requires approval",
            target=command,
            risk=risk,
            risk_class=risk_class,
        )

    # -- helpers --------------------------------------------------------------

    def _resolve(self, scope: SessionScope, path: str | Path) -> Path:
        base = scope.root if scope.root is not None and scope.kind == "PROJECT" else scope.cwd
        candidate = Path(path)
        return (base / candidate if not candidate.is_absolute() else candidate).resolve()

    def _inside_root(self, resolved: Path, root: Path | None) -> bool:
        if root is None:
            return False
        root = root.resolve()
        return resolved == root or root in resolved.parents

    def _is_home(self, path: Path, scope: SessionScope) -> bool:
        if scope.user_home is None:
            return False
        home = scope.user_home.resolve()
        candidate = path.resolve()
        return candidate == home or home in candidate.parents
