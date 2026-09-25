"""Policy engine: decides allow / ask / deny per tool action.

Policy is computed before each model/tool interaction (harness.md 74-79).
It is separated from the sandbox (what can technically execute) and from
approvals (what requires consent). Locked system rules are applied first
and can never be relaxed by a profile.

Permissions v3 (2026-09-25). The profiles are cut by what cannot be undone,
measured on the owner's history (92 prompts in two weeks, 92 approved):

    read-only    reads anything (local and internet) except system secrets;
                 never writes, runs commands or sends data out
    workspace    free inside the project/chat, on localhost/LAN and reading
                 the internet; asks once (or always, per project) to act
                 outside: writes/commands outside, sending to internet hosts,
                 external tools (MCP), git push
    full-access  asks for nothing but the hard list

Always asked, in every profile: force push, deleting outside the project,
and system secrets (SSH keys, OS/browser credential stores). Hard-list asks
cannot be granted "always".

Untrusted-content guard: once a turn has read external content (a web page,
an internet API, an MCP result), the next action that sends data out (POST,
MCP call, git push, curl with a body, browser upload) asks even in
full-access. A prompt injection can read; it cannot quietly exfiltrate.
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
# Peer messaging (Boards). Sending text to another agent session starts a
# turn there and may forward data to a different provider: always consent,
# bound exactly to one destination; PLAN/REVIEW/read-only never send.
CAPABILITY_SESSION_MESSAGE = "session.message"


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
    # This turn already read external (untrusted) content: sending data out
    # asks even in full-access (module docstring).
    external_content: bool = False


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
    # `capability`: a session grant covers later targets of the same
    # capability (legacy). `exact`: every grant is bound to this target only.
    binding_mode: str = "capability"


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
    "system secrets (SSH/GPG/cloud keys, credential stores) always require approval",
    "force push and deleting outside the project always require approval",
    "after reading external content, sending data out requires approval",
)

# Choices: hard-list asks never offer a lasting grant.
CHOICES_HARD = ("deny", "allow_once")
CHOICES_SESSION = ("deny", "allow_once", "allow_session")
CHOICES_LASTING = ("deny", "allow_once", "allow_session", "allow_project")


# System secrets: what a prompt injection would want to steal. Project
# config (.env, credentials.json) is the owner's everyday work and is not
# here; is_sensitive_file still hides those from previews and snapshots.
_PRIVATE_KEY = re.compile(
    r"(\.pem$|\.p12$|\.pfx$|\.ppk$|\.key$|(^|[\\/])id_(rsa|dsa|ecdsa|ed25519)(\.[^\\/]*)?$)",
    re.IGNORECASE,
)
_SECRET_FILE_NAMES = frozenset(
    {".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass"}
)
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"})
_SECRET_PATH_FRAGMENTS = (
    "/.config/gcloud/",
    "/google/chrome/user data/",
    "/microsoft/edge/user data/",
    "/bravesoftware/brave-browser/user data/",
    "/mozilla/firefox/profiles/",
    "/.mozilla/firefox/",
    "/.config/google-chrome/",
    "/microsoft/credentials/",
    "/microsoft/protect/",
    "/microsoft/vault/",
    "/library/keychains/",
)


def is_system_secret(path: Path) -> bool:
    """SSH/GPG/cloud keys and OS or browser credential stores."""
    text = "/" + str(path).replace("\\", "/").lower().lstrip("/") + "/"
    if path.name.lower() in _SECRET_FILE_NAMES or _PRIVATE_KEY.search(path.name):
        return True
    if any(part.lower() in _SECRET_DIRS for part in path.parts[:-1]):
        return True
    return any(fragment in text for fragment in _SECRET_PATH_FRAGMENTS)


_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")
_LOCAL_NETWORKS = tuple(
    __import__("ipaddress").ip_network(block)
    for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)


def is_local_host(host: str | None) -> bool:
    """Loopback, private LAN and single-label names (ssh aliases, NetBIOS)."""
    import ipaddress

    if not host:
        return False
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback or address.is_link_local:
        return True
    # RFC 1918, IPv6 ULA and 100.64/10 (Tailscale/CGNAT overlays). Not
    # `is_private`, which also covers documentation ranges.
    return any(
        address in network for network in _LOCAL_NETWORKS if network.version == address.version
    )


# Shell commands that send data out (a body, a form, an upload).
_SHELL_SENDS = re.compile(
    r"\b(curl\b[^|;&]*\s(-d|--data[\w-]*|-F|--form|-T|--upload-file|-X\s*(POST|PUT|PATCH|DELETE))\b"
    r"|wget\b[^|;&]*\s--post-(data|file)\b"
    r"|(invoke-webrequest|invoke-restmethod|iwr|irm)\b[^|;&]*-method\s+(post|put|patch|delete)\b"
    r"|scp\s|rsync\s[^|;&]*\S+:)",
    re.IGNORECASE,
)


def shell_sends_data(command: str) -> bool:
    return bool(_SHELL_SENDS.search(command or ""))


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
_CMD_SWITCH_VERBS = frozenset(
    {"copy", "move", "del", "erase", "md", "rd", "rmdir", "mkdir", "xcopy", "robocopy"}
)
_REDIRECT_TARGET = re.compile(r"(?<!>)>>?\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))")
_PATH_TOKEN = re.compile(r"(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))")


def _local_part(command: str) -> str:
    """What runs here: a quoted `ssh host "..."` payload runs remotely."""
    match = re.match(r"^\s*ssh\b[^\"']*", command)
    return match.group(0) if match else command


def classify_shell_risk(command: str, scope: SessionScope) -> ShellRisk:
    """Recognise obvious filesystem mutation targets across common shells."""

    command = _local_part(command)
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
            # cmd.exe verbs take /switches (move /y, del /q): not paths.
            cmd_verb = bool(tokens) and tokens[0].lower() in _CMD_SWITCH_VERBS
            candidates.extend(
                token
                for token in tokens[1:]
                if not token.startswith("-")
                and not (cmd_verb and re.fullmatch(r"/[A-Za-z?]{1,2}", token))
            )
    external: list[str] = []
    sensitive = False
    private = False
    for raw in candidates:
        value = raw.strip().rstrip(",)")
        if not value or value in {"-", "/dev/null"} or value.upper() in {"NUL", "NUL:"}:
            continue
        if value.startswith("~"):
            value = str(Path(value).expanduser())
        looks_path = (
            value.startswith((".", "/", "\\", "~"))
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
        sensitive = sensitive or is_system_secret(resolved)
        private = private or _inside_any(resolved, scope.private_roots)
        root = scope.root.resolve() if scope.root is not None else None
        home = scope.user_home.resolve() if scope.user_home is not None else None
        if root is not None and root == home:
            root = None  # a chat opened at $HOME has no implicit workspace
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
        target: str | None = None,
        network_mode: str = "read",
    ) -> PolicyDecision:
        read_only = scope.profile is PermissionProfile.READ_ONLY
        if capability == CAPABILITY_SESSION_MESSAGE:
            if read_only:
                return self._deny(
                    capability,
                    "PLAN, REVIEW and read-only sessions do not message other agents",
                    target=target,
                    risk=risk,
                    risk_class=risk_class,
                )
            if not target:
                return self._deny(
                    capability,
                    "peer message without a destination session",
                    risk=risk,
                    risk_class=risk_class,
                )
            return self._outbound(
                scope,
                capability,
                "sending a message to another agent session (consent per destination)",
                target=target,
                risk=risk,
                risk_class=risk_class,
                rule_id="peer-message",
                binding_mode="exact",
            )
        if read_only and capability in (CAPABILITY_FS_WRITE, CAPABILITY_SHELL):
            return self._deny(
                capability,
                "read-only execution cannot write files or execute commands",
                target=str(path) if path is not None else command,
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_NETWORK:
            return self._network_decision(scope, host, network_mode, risk, risk_class)
        if capability in (CAPABILITY_FS_READ, CAPABILITY_GIT_LOCAL):
            return self._fs_read(scope, path, risk, risk_class, capability)
        if capability in (CAPABILITY_PROCESS_LOCAL, CAPABILITY_STATE_READ, "state.mutate"):
            # Managing an approved process, reading harness state, or loading
            # tools into this session's exposure: no outside effect.
            return self._allow(capability, "local harness bookkeeping", risk, risk_class)
        if capability == CAPABILITY_STATE_WRITE:
            if read_only:
                return self._deny(
                    capability,
                    "read-only profile does not record validation state",
                    risk=risk,
                    risk_class=risk_class,
                )
            return self._allow(capability, "local validation evidence", risk, risk_class)
        if capability == CAPABILITY_FS_WRITE:
            return self._fs_write(scope, path, risk, risk_class)
        if capability in (CAPABILITY_BROWSER_READ, CAPABILITY_BROWSER_WRITE, CAPABILITY_MCP_READ):
            if read_only:
                return self._deny(
                    capability,
                    "read-only profile does not drive a browser or call MCP tools",
                    risk=risk,
                    risk_class=risk_class,
                )
            if capability == CAPABILITY_BROWSER_WRITE and risk_class == "communication":
                return self._outbound(
                    scope,
                    capability,
                    "uploading a local file through the browser",
                    risk=risk,
                    risk_class=risk_class,
                    rule_id="browser_upload",
                )
            if (
                capability == CAPABILITY_BROWSER_WRITE
                and risk_class == "remote-reversible"
                and scope.profile is PermissionProfile.WORKSPACE
            ):
                # Clicking, typing or running script in a page can act on a
                # site (submit, buy, post). Workspace asks once; full does not.
                return self._ask(
                    capability,
                    "interacting with a web page can act on the site",
                    risk=risk,
                    risk_class=risk_class,
                    rule_id="browser_interact",
                )
            return self._allow(
                capability,
                "browsing and read-only MCP calls (navigation is network-gated)",
                risk,
                risk_class,
            )
        if capability == CAPABILITY_MCP_WRITE:
            if read_only:
                return self._deny(
                    capability,
                    "read-only profile cannot call external MCP tools",
                    risk=risk,
                    risk_class=risk_class,
                )
            return self._outbound(
                scope,
                capability,
                "external MCP call can cause side effects",
                risk=risk,
                risk_class=risk_class,
                rule_id="mcp_call",
            )
        if capability in {"channel.send_attachment", "channel.reply", "channel.delivery_get"}:
            return PolicyDecision(
                action=PolicyAction.ALLOW
                if not read_only or capability == "channel.delivery_get"
                else PolicyAction.DENY,
                capability=capability,
                reason="Host-bound reply to the originating owner conversation",
                risk=risk,
                risk_class=risk_class,
            )
        if capability == CAPABILITY_SHELL:
            return self._shell(scope, command, risk, risk_class)
        if read_only:
            return self._deny(
                capability,
                f"capability {capability!r} is not available in read-only",
                risk=risk,
                risk_class=risk_class,
            )
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return self._allow(
                capability, f"full-access: {capability!r} has no stricter rule", risk, risk_class
            )
        return self._ask(
            capability,
            f"capability {capability!r} is not covered by a built-in rule",
            risk=risk,
            risk_class=risk_class,
            rule_id="unknown_capability",
        )

    # -- decision helpers ----------------------------------------------------

    @staticmethod
    def _allow(capability, reason, risk="low", risk_class="none", target=None) -> PolicyDecision:
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            capability=capability,
            reason=reason,
            target=target,
            risk=risk,
            risk_class=risk_class,
        )

    @staticmethod
    def _deny(capability, reason, *, target=None, risk="low", risk_class="none", rule_id="default"):
        return PolicyDecision(
            action=PolicyAction.DENY,
            capability=capability,
            reason=reason,
            target=target,
            risk=risk,
            risk_class=risk_class,
            rule_id=rule_id,
            reusable=False,
            choices=("deny",),
        )

    @staticmethod
    def _ask(
        capability,
        reason,
        *,
        target=None,
        risk="medium",
        risk_class="none",
        rule_id="default",
        choices=CHOICES_LASTING,
        binding_mode="capability",
    ) -> PolicyDecision:
        return PolicyDecision(
            action=PolicyAction.ASK,
            capability=capability,
            reason=reason,
            target=target,
            risk=risk,
            risk_class=risk_class,
            rule_id=rule_id,
            reusable=choices is not CHOICES_HARD,
            choices=choices,
            binding_mode=binding_mode,
        )

    def _hard(self, capability, reason, *, target, rule_id, risk="critical", risk_class="none"):
        """Hard list: asked in every profile, never granted for good."""
        return self._ask(
            capability,
            reason,
            target=target,
            risk=risk,
            risk_class=risk_class,
            rule_id=rule_id,
            choices=CHOICES_HARD,
        )

    def _secret(self, capability, resolved, *, write: bool) -> PolicyDecision:
        return self._ask(
            capability,
            "system secret (SSH/GPG/cloud keys or a credential store)",
            target=str(resolved),
            risk="critical" if write else "high",
            risk_class="credential",
            rule_id="system_secret",
            choices=CHOICES_SESSION,
            binding_mode="exact",
        )

    def _outbound(
        self,
        scope: SessionScope,
        capability: str,
        reason: str,
        *,
        target=None,
        risk="medium",
        risk_class="none",
        rule_id: str,
        binding_mode="capability",
    ) -> PolicyDecision:
        """An action that sends data or acts outside this machine."""
        if scope.external_content:
            return self._ask(
                capability,
                f"{reason}, after reading external content in this turn",
                target=target,
                risk="high",
                risk_class=risk_class,
                rule_id="external_content_send",
                choices=CHOICES_SESSION,
                binding_mode=binding_mode,
            )
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return self._allow(capability, f"full-access: {reason}", risk, risk_class, target)
        return self._ask(
            capability,
            reason,
            target=target,
            risk=risk,
            risk_class=risk_class,
            rule_id=rule_id,
            binding_mode=binding_mode,
        )

    # -- network ---------------------------------------------------------------

    def _network_decision(
        self, scope: SessionScope, host, mode: str, risk: str, risk_class: str
    ) -> PolicyDecision:
        from rinari.policy.network import MODE_ALLOW, MODE_ASK

        net = self._network.decide(host or "")
        if net.action is PolicyAction.DENY:
            return PolicyDecision(
                action=PolicyAction.DENY,
                capability=CAPABILITY_NETWORK,
                reason=net.reason,
                target=net.target or None,
                risk=risk if risk in ("high", "critical") else "high",
                risk_class=risk_class,
                reusable=False,
                choices=("deny",),
            )
        target = net.target or None
        if net.rule is not None or self._network.mode == MODE_ALLOW:
            return self._allow(CAPABILITY_NETWORK, net.reason, risk, risk_class, target)
        sending = mode == "send"
        if sending and scope.profile is PermissionProfile.READ_ONLY:
            return self._deny(
                CAPABILITY_NETWORK,
                "read-only profile does not send data out",
                target=target,
                risk=risk,
                risk_class=risk_class,
            )
        if self._network.mode == MODE_ASK:
            # Explicit strict mode chosen by the owner: every host asks once.
            return self._ask(
                CAPABILITY_NETWORK,
                "outbound network requires approval (network.mode=ask)",
                target=target,
                risk=risk,
                risk_class=risk_class,
                rule_id="network_host",
                binding_mode="exact",
            )
        if not sending or is_local_host(net.host):
            return self._allow(
                CAPABILITY_NETWORK,
                "local network" if is_local_host(net.host) else "reading from the internet",
                risk,
                risk_class,
                target,
            )
        return self._outbound(
            scope,
            CAPABILITY_NETWORK,
            f"sending data to {net.host}",
            target=target,
            risk=risk,
            risk_class=risk_class,
            rule_id="network_send",
            binding_mode="exact",
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
        if is_system_secret(resolved):
            return self._secret(CAPABILITY_FS_READ, resolved, write=False)
        return self._allow(
            CAPABILITY_FS_READ,
            f"inside the {scope.kind} session root"
            if self._inside_root(resolved, scope.root)
            else "reads are free outside the root too (system secrets excepted)",
            risk,
            risk_class,
            str(resolved),
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
        if is_system_secret(resolved):
            return self._secret(CAPABILITY_FS_WRITE, resolved, write=True)
        if scope.profile is PermissionProfile.READ_ONLY:
            return self._deny(
                CAPABILITY_FS_WRITE, "read-only profile cannot write", target=str(resolved)
            )
        full = scope.profile is PermissionProfile.FULL_ACCESS
        root = scope.root
        home_root = root is not None and self._is_home_itself(root, scope)
        # Dirty-worktree protection (workspace): overwriting uncommitted work
        # that predates the session asks once. Full access trusts the owner;
        # the turn changeset can still undo tool writes.
        guard = scope.worktree
        if guard is not None and not full:
            classification = guard.classify(resolved)
            if classification is not None:
                return self._ask(
                    CAPABILITY_FS_WRITE,
                    f"file has uncommitted user changes from before this session "
                    f"({classification})",
                    target=str(resolved),
                    risk="medium",
                    risk_class="local-destructive",
                    rule_id="preexisting_user_work",
                    choices=CHOICES_SESSION,
                )
        if self._inside_root(resolved, root) and not home_root:
            return self._allow(
                CAPABILITY_FS_WRITE,
                "inside the project root"
                if scope.kind == "PROJECT"
                else "inside the directory opened for this chat",
                risk,
                risk_class,
                str(resolved),
            )
        if full:
            return self._allow(
                CAPABILITY_FS_WRITE,
                "full-access: writes outside the root",
                risk,
                risk_class,
                str(resolved),
            )
        return self._ask(
            CAPABILITY_FS_WRITE,
            "write outside the project root",
            target=str(resolved),
            risk=risk,
            risk_class=risk_class,
            rule_id="write_outside_root",
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
            return self._hard(
                CAPABILITY_SHELL,
                "force push rewrites remote history",
                target=command,
                rule_id="git_force_push",
                risk_class="remote-destructive",
            )
        shell_risk = classify_shell_risk(command or "", scope)
        if shell_risk.private_target:
            return self._deny(
                CAPABILITY_SHELL,
                "command targets private restoration data",
                target=command,
                risk="critical",
                risk_class="private-runtime-state",
                rule_id="private_change_snapshot",
            )
        if shell_risk.sensitive_target:
            return self._ask(
                CAPABILITY_SHELL,
                "command targets a system secret",
                target=command,
                risk="critical",
                risk_class="credential",
                rule_id="system_secret",
                choices=CHOICES_SESSION,
                binding_mode="exact",
            )
        if scope.profile is PermissionProfile.READ_ONLY:
            return self._deny(
                CAPABILITY_SHELL, "read-only profile cannot execute commands", target=command
            )
        if shell_risk.destructive and shell_risk.external_path_targets:
            return self._hard(
                CAPABILITY_SHELL,
                "deletes outside the project: " + ", ".join(shell_risk.external_path_targets[:3]),
                target=command,
                rule_id="delete_outside_root",
                risk_class="local-destructive",
            )
        if remote == "remote":
            return self._outbound(
                scope,
                CAPABILITY_SHELL,
                "git push publishes to a remote",
                target=command,
                risk="high",
                risk_class="remote-reversible",
                rule_id="git_remote_mutation",
            )
        if scope.external_content and shell_sends_data(command or ""):
            return self._outbound(
                scope,
                CAPABILITY_SHELL,
                "command sends data out",
                target=command,
                risk="high",
                risk_class="communication",
                rule_id="shell_send",
            )
        if scope.profile is PermissionProfile.FULL_ACCESS:
            return self._allow(CAPABILITY_SHELL, "full-access: local command", risk, risk_class)
        if shell_risk.local_mutation and shell_risk.external_path_targets:
            return self._ask(
                CAPABILITY_SHELL,
                "command writes outside the workspace: "
                + ", ".join(shell_risk.external_path_targets[:3]),
                target=command,
                risk=risk,
                risk_class=risk_class,
                rule_id="shell_external_mutation",
            )
        return self._allow(CAPABILITY_SHELL, "local command in the workspace", risk, risk_class)

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

    def _is_home_itself(self, path: Path, scope: SessionScope) -> bool:
        if scope.user_home is None:
            return False
        return path.resolve() == scope.user_home.resolve()

    def _is_home(self, path: Path, scope: SessionScope) -> bool:
        if scope.user_home is None:
            return False
        home = scope.user_home.resolve()
        candidate = path.resolve()
        return candidate == home or home in candidate.parents
