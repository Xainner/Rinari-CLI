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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rinari.policy.network import NetworkPolicy


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
CAPABILITY_PROCESS_LOCAL = "process.local"
CAPABILITY_NETWORK = "network.outbound"
# Local harness bookkeeping (validation records, index/task state reads):
# no filesystem or network side effect, so it never asks for approval.
CAPABILITY_STATE_READ = "state.read"
CAPABILITY_STATE_WRITE = "state.write"
# Browser capabilities (phase 5). Navigation is gated as network.outbound on
# the target URL; the rest of the control surface maps to these two:
#   browser.read    reads state of the browser this session drives
#                   (no new dial: the navigation that reached the page was
#                   already network-gated)
#   browser.mutate  changes browser/page state; external side effects always
#                   require explicit consent (AGENTS.md 11)
CAPABILITY_BROWSER_READ = "browser.read"
CAPABILITY_BROWSER_WRITE = "browser.mutate"
# MCP capabilities (phase 5). MCP servers are external capability providers:
#   mcp.read  read-only MCP calls (server-declared readOnlyHint)
#   mcp.call  any other MCP tool call; external side effects require consent
CAPABILITY_MCP_READ = "mcp.read"
CAPABILITY_MCP_WRITE = "mcp.call"


@dataclass(frozen=True, slots=True)
class SessionScope:
    kind: str  # "CHAT" | "PROJECT"
    root: Path | None  # project root (PROJECT) or cwd (CHAT)
    cwd: Path
    profile: PermissionProfile = PermissionProfile.WORKSPACE
    user_home: Path | None = None
    # Optional WorktreeGuard: dirty-state baseline captured at session start.
    worktree: Any | None = None
    private_roots: tuple[Path, ...] = ()
    read_profile: PermissionProfile | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    capability: str
    reason: str
    target: str | None = None
    risk: str = "low"
    risk_class: str = "none"
    rule_id: str = "default"
    reusable: bool = True
    choices: tuple[str, ...] = ("deny", "allow_once", "allow_session")


@dataclass(frozen=True, slots=True)
class ShellRisk:
    """Best-effort shell intent classification, never a sandbox claim."""

    local_mutation: bool = False
    external_path_targets: tuple[str, ...] = ()
    remote_git_mutation: bool = False
    destructive: bool = False
    sensitive_target: bool = False
    private_target: bool = False
    confidence: str = "low"


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


def _inside_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root.resolve() or root.resolve() in path.parents for root in roots)


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


_MUTATING_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:cp|mv|rm|mkdir|touch|install|tee|"
    r"copy|move|del|erase|md|rd|rmdir|set-content|add-content|out-file|"
    r"new-item|remove-item|move-item|copy-item|rename-item)\b",
    re.IGNORECASE,
)
_DESTRUCTIVE_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|del|erase|rd|rmdir|remove-item)\b",
    re.IGNORECASE,
)
_REDIRECT_TARGET = re.compile(r"(?<!>)>>?\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))")
_PATH_TOKEN = re.compile(r"(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))")


def classify_shell_risk(command: str, scope: SessionScope) -> ShellRisk:
    """Recognise obvious filesystem mutation targets across common shells."""

    remote = classify_git_remote(command)
    mutation = bool(_MUTATING_SHELL.search(command) or _REDIRECT_TARGET.search(command))
    destructive = bool(_DESTRUCTIVE_SHELL.search(command))
    candidates: list[str] = []
    for match in _REDIRECT_TARGET.finditer(command):
        candidates.append(next(value for value in match.groups() if value is not None))
    if mutation:
        for segment in re.split(r"[;&|]+", command):
            if not _MUTATING_SHELL.search(segment):
                continue
            tokens = [
                next(value for value in match.groups() if value is not None)
                for match in _PATH_TOKEN.finditer(segment)
            ]
            candidates.extend(token for token in tokens[1:] if not token.startswith("-"))
    external: list[str] = []
    sensitive = False
    private = False
    for raw in candidates:
        value = raw.strip().rstrip(",)")
        if not value or value in {"-", "/dev/null", "NUL"}:
            continue
        looks_path = (
            value.startswith((".", "/", "\\"))
            or bool(re.match(r"^[A-Za-z]:[\\/]", value))
            or "/" in value
            or "\\" in value
        )
        if not looks_path:
            continue
        try:
            base = scope.root if scope.root is not None and scope.kind == "PROJECT" else scope.cwd
            candidate = Path(value)
            resolved = (base / candidate if not candidate.is_absolute() else candidate).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        sensitive = sensitive or is_sensitive_file(resolved)
        private = private or _inside_any(resolved, scope.private_roots)
        root = scope.root.resolve() if scope.root is not None else None
        if root is None or not (resolved == root or root in resolved.parents):
            rendered = str(resolved)
            if rendered not in external:
                external.append(rendered)
    return ShellRisk(
        local_mutation=mutation,
        external_path_targets=tuple(external),
        remote_git_mutation=remote is not None,
        destructive=destructive,
        sensitive_target=sensitive,
        private_target=private,
        confidence="high" if mutation and candidates else "low",
    )


class PolicyEngine:
    def __init__(self, *, network: NetworkPolicy | None = None) -> None:
        from rinari.policy.network import NetworkPolicy  # local: avoid import cycle

        # Default is the safe mode ("ask"): a plain PolicyEngine() without a
        # configured policy still gates network tools behind approval.
        self._network = network if network is not None else NetworkPolicy()

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network

    def decide(
        self,
        capability: str,
        scope: SessionScope,
        *,
        path: str | Path | None = None,
        command: str | None = None,
        host: str | None = None,
        risk: str = "low",
        risk_class: str = "none",
    ) -> PolicyDecision:
        if scope.profile is PermissionProfile.READ_ONLY and capability in (
            CAPABILITY_FS_WRITE,
            CAPABILITY_SHELL,
        ):
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=capability,
                reason="read-only execution cannot write files or execute commands",
                target=str(path) if path is not None else command,
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_NETWORK:
            return self._network_decision(host, risk, risk_class)
        if capability in (CAPABILITY_FS_READ, CAPABILITY_GIT_LOCAL):
            return self._fs_read(scope, path, risk, risk_class, capability)
        if capability == CAPABILITY_PROCESS_LOCAL:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason="managing a process already approved at start in this session",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_STATE_READ:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason="reading local harness state (no filesystem or network effect)",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_STATE_WRITE:
            if scope.profile is PermissionProfile.READ_ONLY:
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    capability=capability,
                    reason="read-only profile does not record validation state",
                    risk=risk,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason="writing local validation evidence (no external side effect)",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_FS_WRITE:
            return self._fs_write(scope, path, risk, risk_class)
        if capability == CAPABILITY_BROWSER_READ:
            if scope.profile is PermissionProfile.READ_ONLY:
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    capability=capability,
                    reason="read-only profile does not read browser state",
                    risk=risk,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason=(
                    "reading the state of the browser this session drives "
                    "(navigation is network-gated)"
                ),
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_BROWSER_WRITE:
            if scope.profile is PermissionProfile.READ_ONLY:
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    capability=capability,
                    reason="read-only profile does not control a browser",
                    risk=risk,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=capability,
                reason="browser control can cause external side effects (consent required)",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_MCP_READ:
            if scope.profile is PermissionProfile.READ_ONLY:
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    capability=capability,
                    reason="read-only profile does not call external MCP servers",
                    risk=risk,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=capability,
                reason="read-only call to a trusted external capability provider",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_MCP_WRITE:
            if scope.profile is PermissionProfile.READ_ONLY:
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    capability=capability,
                    reason="read-only profile cannot call external MCP tools",
                    risk=risk,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=capability,
                reason="external MCP call can cause side effects (consent required)",
                risk=risk,
                risk_class=risk_class,
            )
        if capability in {"channel.send_attachment", "channel.reply", "channel.delivery_get"}:
            return PolicyDecision(
                action=PolicyAction.ALLOW if scope.profile is not PermissionProfile.READ_ONLY
                or capability == "channel.delivery_get" else PolicyAction.DENY,
                capability=capability, reason="Host-bound reply to the originating owner conversation",
                risk=risk, risk_class=risk_class,
            )
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

    # -- network -------------------------------------------------------------

    def _network_decision(self, host, risk: str, risk_class: str) -> PolicyDecision:
        net = self._network.decide(host or "")
        risk_level = risk
        if net.action is PolicyAction.DENY and risk_level not in ("high", "critical"):
            risk_level = "high"
        return PolicyDecision(
            action=net.action,
            capability=CAPABILITY_NETWORK,
            reason=net.reason,
            target=net.target or None,
            risk=risk_level,
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
        if _inside_any(resolved, scope.private_roots):
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_READ,
                reason="private restoration data is never model-readable",
                target=str(resolved),
                risk="critical",
                risk_class="private-runtime-state",
                rule_id="private_change_snapshot",
                reusable=False,
                choices=("deny",),
            )
        if is_sensitive_file(resolved):
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_FS_READ,
                reason="sensitive credential file (locked system rule)",
                target=str(resolved),
                risk="high",
                risk_class="credential",
                rule_id="sensitive_credential",
                reusable=False,
                choices=("deny", "allow_once"),
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
        read_profile = scope.read_profile or scope.profile
        if read_profile is PermissionProfile.FULL_ACCESS:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_READ,
                reason="full-access profile reads outside the root are allowed",
                target=str(resolved),
                risk=risk,
                risk_class=risk_class,
            )
        if read_profile is PermissionProfile.READ_ONLY:
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
        if _inside_any(resolved, scope.private_roots):
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_WRITE,
                reason="private restoration data cannot be modified by tools",
                target=str(resolved),
                risk="critical",
                risk_class="private-runtime-state",
                rule_id="private_change_snapshot",
                reusable=False,
                choices=("deny",),
            )
        if is_sensitive_file(resolved):
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_FS_WRITE,
                reason="sensitive credential file (locked system rule)",
                target=str(resolved),
                risk="critical",
                risk_class="credential",
                rule_id="sensitive_credential",
                reusable=False,
                choices=("deny", "allow_once"),
            )
        root = scope.root
        home_root = root is not None and self._is_home(root, scope)
        if (
            self._is_home(resolved, scope)
            and not home_root
            and scope.profile is not PermissionProfile.FULL_ACCESS
        ):
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_FS_WRITE,
                reason="$HOME is never an implicit writable root (locked system rule)",
                target=str(resolved),
                risk="high",
                risk_class="local-destructive",
                rule_id="implicit_home_write",
                reusable=False,
                choices=("deny",),
            )
        # Dirty-worktree protection: overwriting a file the user already had
        # uncommitted modifies work that predates the session, so it always
        # goes through the approval gate even inside the project root.
        guard = scope.worktree
        if guard is not None:
            classification = guard.classify(resolved)
            if classification is not None:
                return PolicyDecision(
                    action=PolicyAction.ASK,
                    capability=CAPABILITY_FS_WRITE,
                    reason=(
                        f"file has uncommitted user changes from before this "
                        f"session ({classification}); overwriting requires "
                        "explicit approval"
                    ),
                    target=str(resolved),
                    risk="medium",
                    risk_class="local-destructive",
                    rule_id="preexisting_user_work",
                    reusable=False,
                    choices=("deny", "allow_once"),
                )
        inside = self._inside_root(resolved, scope.root)
        inside_allow_reason: str | None = None
        if inside and scope.profile is not PermissionProfile.READ_ONLY:
            if scope.kind == "PROJECT":
                inside_allow_reason = (
                    "workspace/full-access profile may write inside the project root"
                )
            elif not home_root:
                # CHAT candidate workspace: the directory the user explicitly
                # opened. Narrow and intentional (harness.md 76); creating a
                # project marker there promotes the session in place. A
                # candidate rooted at $HOME keeps the locked behavior.
                inside_allow_reason = (
                    "candidate project workspace: the directory explicitly opened "
                    "for this session ($HOME stays locked)"
                )
        if inside_allow_reason is not None:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_WRITE,
                reason=inside_allow_reason,
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
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_FS_WRITE,
                reason="full-access profile permits ordinary local writes",
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
                rule_id="git_force_push",
                reusable=False,
                choices=("deny", "allow_once"),
            )
        if remote == "remote":
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_SHELL,
                reason="remote Git mutation requires approval",
                target=command,
                risk="high",
                risk_class="remote-reversible",
                rule_id="git_remote_mutation",
                reusable=False,
                choices=("deny", "allow_once"),
            )
        shell_risk = classify_shell_risk(command or "", scope)
        if shell_risk.private_target:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_SHELL,
                reason="command targets private restoration data",
                target=command,
                risk="critical",
                risk_class="private-runtime-state",
                rule_id="private_change_snapshot",
                reusable=False,
                choices=("deny",),
            )
        if shell_risk.sensitive_target:
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_SHELL,
                reason="command targets a sensitive credential file (locked system rule)",
                target=command,
                risk="critical",
                risk_class="credential",
                rule_id="sensitive_credential",
                reusable=False,
                choices=("deny", "allow_once"),
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
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_SHELL,
                reason="full-access profile permits ordinary local shell execution",
                target=command,
                risk=risk,
                risk_class=risk_class,
            )
        if shell_risk.local_mutation and shell_risk.external_path_targets:
            return PolicyDecision(
                action=PolicyAction.ASK,
                capability=CAPABILITY_SHELL,
                reason=(
                    "workspace profile: command has an explicit write target outside the workspace"
                ),
                target=command,
                risk="high" if shell_risk.destructive else risk,
                risk_class="local-destructive" if shell_risk.destructive else risk_class,
                rule_id="shell_external_mutation",
            )
        if scope.root is not None:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                capability=CAPABILITY_SHELL,
                reason="local execution inside the selected workspace",
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
