"""Memory service (phase 4): user, project, episodic, and pattern memory.

Design rules (AGENTS.md 21, harness.md section 69):

* The four stores stay separate; nothing auto-promotes a record between them.
* Records are created only through explicit stores/tools — the harness never
  auto-persists model inferences.
* Every record carries provenance; confidence is informational metadata.
* Stale handling: a new record with the same (kind, topic) supersedes the
  previous one, which is kept as history but never returned by reads.
  Consumers must treat durable memory as possibly stale and re-verify
  volatile facts.
* No secrets: a sensitivity filter rejects text that looks like credentials
  before anything touches storage.
"""

from __future__ import annotations

import re

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.shared.errors import InvalidUsageError

MEM_MAX_TEXT = 4096
MEM_MAX_SUMMARY = 400
MEM_MAX_PROVENANCE = 256

# Heuristic secret detection. These are recall-oriented patterns (catch real
# secrets, tolerate some false positives: a rejected memory is cheap, a
# stored secret is not). The known-secrets list from provider credentials
# is matched verbatim in addition to the patterns.
_SENSITIVE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\b[os]k[a-z]{4}-[A-Za-z0-9-]{10,}"),  # OpenAI service/API keys
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(
        r"(?i)(api[_-]?key|secret|access[_-]?key|token|password|passwd|bearer)\s*[:=]\s*['\"]?"
        r"[A-Za-z0-9_\-./+=]{12,}"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*(bearer|basic)\s+[A-Za-z0-9_\-./+=]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
)

# Generic long-blob candidates: matched with a character-diversity check so
# repeated-character filler (test input, padding) does not trip the filter,
# while a real 64+ char token/blob does. 48+ hex: 40-char git SHAs remain
# legitimate memory content.
_BLOB_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"[A-Za-z0-9+/]{64,}={0,2}"),
    re.compile(r"\b[0-9a-f]{48,}\b"),
)


def _normalize_topic(topic: str) -> str:
    return " ".join(topic.strip().lower().split())


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def find_sensitive_match(text: str, known_secrets: list[str] | tuple[str, ...] = ()) -> str | None:
    """Return a short reason if text looks secret-bearing, else None."""
    if not text:
        return None
    for secret in known_secrets:
        if secret and secret in text:
            return "contains a known credential value"
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return "matches a credential pattern"
    for pattern in _BLOB_PATTERNS:
        match = pattern.search(text)
        if match is not None and len(set(match.group(0))) >= 20:
            return "matches a credential pattern"
    return None


class MemoryService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def repo(self):
        return self._ctx.memory_repo

    # -- internals -------------------------------------------------------------

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

    def _check_sensitive(self, text: str, field: str = "text") -> None:
        reason = find_sensitive_match(text)
        if reason is not None:
            raise InvalidUsageError(
                f"memory {field} {reason}; secrets must never be stored in memory",
                hint="Remove the secret and re-store the memory without it.",
            )

    @staticmethod
    def _require(text: str, field: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            raise InvalidUsageError(f"{field} is required")
        return cleaned

    def _remember(
        self,
        *,
        store: str,
        root: str | None,
        kind: str,
        topic: str,
        text: str,
        provenance: str,
        confidence: float,
    ) -> dict:
        """Shared remember path for user/project with conflict supersede."""
        topic_n = self._require(topic, "topic")[:128]
        text = self._require(text, "text")
        if len(text) > MEM_MAX_TEXT:
            raise InvalidUsageError(
                "memory text exceeds 4096 characters",
                hint="Store a shorter fact; keep details in artifacts.",
            )
        provenance = (provenance or "agent").strip()[:MEM_MAX_PROVENANCE]
        self._check_sensitive(text)
        self._check_sensitive(provenance, "provenance")
        now = self._now()
        memory_id = self._ctx.ids.new(f"mem-{store}")

        prefix = _normalize_topic(topic_n)
        if store == "user":
            active = [
                row
                for row in self.repo.user_live()
                if row["kind"] == kind and _normalize_topic(row["topic"]) == prefix
            ]
        else:
            active = [
                row
                for row in self.repo.project_live(root)
                if row["kind"] == kind and _normalize_topic(row["topic"]) == prefix
            ]

        action = "created"
        normalized = _normalize_text(text)
        for row in active:
            if _normalize_text(row["text"]) == normalized:
                # Idempotent re-statement: refresh, do not duplicate.
                if store == "user":
                    self.repo.user_update(
                        row["id"],
                        {"provenance": provenance, "confidence": confidence},
                        now,
                    )
                else:
                    self.repo.project_update(
                        root,
                        row["id"],
                        {"provenance": provenance, "confidence": confidence},
                        now,
                    )
                action = "refreshed"
                return {"id": row["id"], "store": store, "kind": kind, "action": action}
            if store == "user":
                self.repo.user_supersede(row["id"], memory_id, now)
            else:
                self.repo.project_supersede(root, row["id"], memory_id, now)
            action = "superseded"

        if store == "user":
            self.repo.user_insert(
                {
                    "id": memory_id,
                    "kind": kind,
                    "topic": topic_n,
                    "text": text,
                    "provenance": provenance,
                    "confidence": confidence,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        else:
            self.repo.project_insert(
                {
                    "id": memory_id,
                    "project_root": root,
                    "kind": kind,
                    "topic": topic_n,
                    "text": text,
                    "provenance": provenance,
                    "confidence": confidence,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return {
            "id": memory_id,
            "store": store,
            "kind": kind,
            "topic": topic_n,
            "action": action,
        }

    # -- user memory -------------------------------------------------------------

    def remember_user(
        self,
        text: str,
        *,
        kind: str = "preference",
        topic: str,
        provenance: str = "agent",
        confidence: float = 1.0,
    ) -> dict:
        if kind not in ("preference", "rule", "fact"):
            raise InvalidUsageError("user memory kind must be preference, rule, or fact")
        return self._remember(
            store="user",
            root=None,
            kind=kind,
            topic=topic,
            text=text,
            provenance=provenance,
            confidence=confidence,
        )

    def search_user(
        self, query: str = "", *, kind: str | None = None, limit: int = 20
    ) -> list[dict]:
        return self.repo.user_search(query, kind=kind, limit=limit)

    def list_user(self, *, limit: int = 100) -> list[dict]:
        return self.repo.user_live()[: max(1, min(limit, 500))]

    def get_user(self, memory_id: str) -> dict | None:
        return self.repo.user_get(memory_id)

    def update_user(
        self,
        memory_id: str,
        *,
        text: str | None = None,
        topic: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
    ) -> dict:
        existing = self.repo.user_get(memory_id)
        if existing is None:
            raise InvalidUsageError(f"user memory not found: {memory_id}")
        fields: dict = {}
        if text is not None:
            text = self._require(text, "text")
            self._check_sensitive(text)
            fields["text"] = text
        if topic is not None:
            topic = self._require(topic, "topic")
            fields["topic"] = topic[:128]
        if confidence is not None:
            if not 0.0 <= float(confidence) <= 1.0:
                raise InvalidUsageError("confidence must be between 0 and 1")
            fields["confidence"] = float(confidence)
        if provenance is not None:
            provenance = provenance.strip()[:MEM_MAX_PROVENANCE]
            self._check_sensitive(provenance, "provenance")
            fields["provenance"] = provenance
        if fields:
            self.repo.user_update(memory_id, fields, self._now())
        return self.repo.user_get(memory_id)

    def forget_user(self, memory_id: str) -> bool:
        return self.repo.user_delete(memory_id)

    # -- project memory ------------------------------------------------------------

    def remember_project(
        self,
        project_root: str,
        text: str,
        *,
        kind: str = "fact",
        topic: str,
        provenance: str = "agent",
        confidence: float = 1.0,
    ) -> dict:
        if kind not in ("fact", "rule", "convention"):
            raise InvalidUsageError("project memory kind must be fact, rule, or convention")
        root = self._require(project_root, "project_root")
        return self._remember(
            store="project",
            root=root,
            kind=kind,
            topic=topic,
            text=text,
            provenance=provenance,
            confidence=confidence,
        )

    def search_project(
        self,
        project_root: str,
        query: str = "",
        *,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        return self.repo.project_search(project_root, query, kind=kind, limit=limit)

    def list_project(self, project_root: str, *, limit: int = 100) -> list[dict]:
        return self.repo.project_live(project_root)[: max(1, min(limit, 500))]

    def update_project(
        self,
        project_root: str,
        memory_id: str,
        *,
        text: str | None = None,
        topic: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
    ) -> dict:
        existing = self.repo.project_get(project_root, memory_id)
        if existing is None:
            raise InvalidUsageError(f"project memory not found: {memory_id}")
        fields: dict = {}
        if text is not None:
            text = self._require(text, "text")
            self._check_sensitive(text)
            fields["text"] = text
        if topic is not None:
            fields["topic"] = self._require(topic, "topic")[:128]
        if confidence is not None:
            if not 0.0 <= float(confidence) <= 1.0:
                raise InvalidUsageError("confidence must be between 0 and 1")
            fields["confidence"] = float(confidence)
        if provenance is not None:
            provenance = provenance.strip()[:MEM_MAX_PROVENANCE]
            self._check_sensitive(provenance, "provenance")
            fields["provenance"] = provenance
        if fields:
            self.repo.project_update(project_root, memory_id, fields, self._now())
        return self.repo.project_get(project_root, memory_id)

    def forget_project(self, project_root: str, memory_id: str) -> bool:
        return self.repo.project_delete(project_root, memory_id)

    # -- episodic memory -------------------------------------------------------------

    def record_episodic(
        self,
        session_id: str,
        project_root: str,
        summary: str,
        *,
        outcome: str = "",
        provenance: str = "agent",
    ) -> dict:
        summary = self._require(summary, "summary")[:MEM_MAX_SUMMARY]
        outcome = outcome.strip()[:MEM_MAX_SUMMARY]
        provenance = (provenance or "agent").strip()[:MEM_MAX_PROVENANCE]
        self._check_sensitive(summary, "summary")
        self._check_sensitive(outcome, "outcome")
        memory_id = self._ctx.ids.new("mem-episo")
        self.repo.episodic_insert(
            {
                "id": memory_id,
                "session_ref": session_id,
                "project_root": project_root or "",
                "summary": summary,
                "outcome": outcome,
                "provenance": provenance,
                "created_at": self._now(),
            }
        )
        return {"id": memory_id, "session_ref": session_id, "summary_chars": len(summary)}

    def search_episodic(
        self, project_root: str = "", query: str = "", *, limit: int = 10
    ) -> list[dict]:
        if not project_root:
            return self.repo.episodic_list(limit=limit)
        return self.repo.episodic_search(project_root, query, limit=limit)

    def list_episodic(self, project_root: str | None = None, *, limit: int = 20) -> list[dict]:
        return self.repo.episodic_list(project_root, limit=limit)

    # -- pattern memory -------------------------------------------------------------

    def remember_pattern(
        self,
        topic: str,
        text: str,
        *,
        scope: str = "global",
        provenance: str = "agent",
    ) -> dict:
        if scope not in ("global", "user"):
            raise InvalidUsageError("pattern scope must be global or user")
        topic_n = self._require(topic, "topic")[:128]
        text = self._require(text, "text")
        if len(text) > MEM_MAX_TEXT:
            raise InvalidUsageError("pattern text exceeds 4096 characters")
        provenance = (provenance or "agent").strip()[:MEM_MAX_PROVENANCE]
        self._check_sensitive(text)
        self._check_sensitive(provenance, "provenance")
        for row in self.repo.pattern_list(scope=scope, limit=200):
            if _normalize_topic(row["topic"]) == _normalize_topic(topic_n) and _normalize_text(
                row["text"]
            ) == _normalize_text(text):
                return {"id": row["id"], "scope": scope, "action": "exists"}
        memory_id = self._ctx.ids.new("mem-pat")
        self.repo.pattern_insert(
            {
                "id": memory_id,
                "scope": scope,
                "topic": topic_n,
                "text": text,
                "provenance": provenance,
                "created_at": self._now(),
            }
        )
        return {"id": memory_id, "scope": scope, "topic": topic_n, "action": "created"}

    def forget_pattern(self, memory_id: str) -> bool:
        return self.repo.pattern_delete(memory_id)

    def search_pattern(
        self, query: str = "", *, scope: str | None = None, limit: int = 10
    ) -> list[dict]:
        return self.repo.pattern_search(query, scope=scope, limit=limit)

    def list_pattern(self, *, scope: str | None = None, limit: int = 20) -> list[dict]:
        return self.repo.pattern_list(scope=scope, limit=limit)

    # -- prompt segment ------------------------------------------------------------------

    def prompt_segment(self, project_root: str | None) -> str | None:
        """Render the durable memory block for the system prompt (if any).

        Durable memory is injected so the agent starts aligned with stored
        preferences/facts; it is explicitly lower-authority than instructions
        and marked possibly stale.
        """
        lines: list[str] = []
        user_rows = self.repo.user_live()[:15]
        for row in user_rows:
            lines.append(f"- [{row['kind']}] {row['text']}")
        if project_root:
            for row in self.repo.project_live(project_root)[:15]:
                lines.append(f"- [project:{row['kind']}] {row['text']}")
        if not lines:
            return None
        return (
            (
                "Durable memory (explicit records; lower authority than "
                "instructions; possibly stale — re-verify volatile facts before "
                "relying on them):"
            )
            + "\n"
            + "\n".join(lines)
        )


__all__ = ["MemoryService", "find_sensitive_match"]
