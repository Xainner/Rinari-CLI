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

import hashlib
import json
import re
from dataclasses import dataclass

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError

MEM_MAX_TEXT = 4096
MEM_MAX_SUMMARY = 400
MEM_MAX_PROVENANCE = 256
MEM_PROMPT_MAX_CHARS = 12000
MEM_SOURCE_QUOTE_MAX = 4096


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A bounded owner-message proposal before durable memory exists."""

    topic: str
    text: str
    kind: str
    confidence: float
    classification: str
    reason: str = ""


class MemoryConflictError(ConflictError, InvalidUsageError):
    """Optimistic concurrency failure with a protocol-stable error type."""


class MemoryNotFoundError(NotFoundError, InvalidUsageError):
    """Requested live memory record is absent."""


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
    re.compile(
        r"(?i)\b(?:contrase(?:ña|na)|password|passwd|token|api[ _-]?key|clave privada)\b"
        r"\s*(?:es|is|[:=])\s*[^\s,.;]{3,}"
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


def _suppression_hashes(topic: str, text: str) -> tuple[str, str]:
    """Return minimal hashes used to keep an explicitly forgotten pair out."""
    return (
        hashlib.sha256(_normalize_topic(topic).encode("utf-8")).hexdigest(),
        hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest(),
    )


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

    def _user_view(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        view = dict(row)
        view["sources"] = [
            {
                "session_id": source["session_id"],
                "message_id": source["message_id"],
                "source_hash": source["source_hash"],
                "created_at": source["created_at"],
                "revoked_at": source["revoked_at"],
            }
            for source in self.repo.sources_for_memory(row["id"])
        ]
        return view

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
        if not isinstance(text, str):
            raise InvalidUsageError(f"{field} must be a string")
        cleaned = text.strip()
        if not cleaned:
            raise InvalidUsageError(f"{field} is required")
        return cleaned

    def _bounded(self, value: str, field: str, maximum: int) -> str:
        cleaned = self._require(value, field)
        if len(cleaned) > maximum:
            raise InvalidUsageError(f"memory {field} exceeds {maximum} characters")
        return cleaned

    @staticmethod
    def _confidence(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidUsageError("confidence must be between 0 and 1")
        result = float(value)
        if not 0.0 <= result <= 1.0:
            raise InvalidUsageError("confidence must be between 0 and 1")
        return result

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
        source: dict | None = None,
    ) -> dict:
        """Shared remember path for user/project with conflict supersede."""
        topic_n = self._bounded(topic, "topic", 128)
        text = self._bounded(text, "text", MEM_MAX_TEXT)
        if provenance is None:
            provenance = "agent"
        provenance = self._bounded(provenance, "provenance", MEM_MAX_PROVENANCE)
        confidence = self._confidence(confidence)
        self._check_sensitive(topic_n, "topic")
        self._check_sensitive(text)
        self._check_sensitive(provenance, "provenance")
        _, text_hash = _suppression_hashes(topic_n, text)
        with self.repo._db.transaction():
            if store == "user" and self.repo.suppression_exists(text_hash):
                raise InvalidUsageError(
                    "this memory was explicitly forgotten and cannot be recreated automatically"
                )
            now = self._now()
            memory_id = self._ctx.ids.new(f"mem-{store}")
            if store == "user" and self.repo.record_suppression_exists(memory_id):
                raise InvalidUsageError(
                    "this memory record was explicitly forgotten and cannot be restored"
                )

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
                    if source is not None:
                        existing_sources = self.repo.sources_for_memory(row["id"], live_only=True)
                        if not any(
                            item["session_id"] == source["session_id"]
                            and item["message_id"] == source["message_id"]
                            for item in existing_sources
                        ):
                            self.repo.source_insert(
                                {
                                    "id": self._ctx.ids.new("mem-source"),
                                    "memory_id": row["id"],
                                    "session_id": source["session_id"],
                                    "message_id": source["message_id"],
                                    "source_hash": source["source_hash"],
                                    "quote": source["quote"][:MEM_SOURCE_QUOTE_MAX],
                                    "created_at": now,
                                }
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
                if source is not None:
                    self.repo.source_insert(
                        {
                            "id": self._ctx.ids.new("mem-source"),
                            "memory_id": memory_id,
                            "session_id": source["session_id"],
                            "message_id": source["message_id"],
                            "source_hash": source["source_hash"],
                            "quote": source["quote"][:MEM_SOURCE_QUOTE_MAX],
                            "created_at": now,
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
        source: dict | None = None,
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
            source=source,
        )

    def search_user(
        self, query: str = "", *, kind: str | None = None, limit: int = 20
    ) -> list[dict]:
        rows = self.repo.user_search(query, kind=kind, limit=limit)
        return [self._user_view(row) for row in rows]

    def list_user(self, *, limit: int = 100) -> list[dict]:
        return [self._user_view(row) for row in self.repo.user_live()[: max(1, min(limit, 500))]]

    def get_user(self, memory_id: str) -> dict | None:
        return self._user_view(self.repo.user_get(memory_id))

    def update_user(
        self,
        memory_id: str,
        *,
        text: str | None = None,
        topic: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
        expected_version: str | None = None,
        expected_revision: int | None = None,
        owner_consent: bool = False,
    ) -> dict:
        with self.repo._db.transaction():
            existing = self.repo.user_get(memory_id)
            if existing is None:
                raise MemoryNotFoundError(f"user memory not found: {memory_id}")
            if expected_version is not None and existing.get("updated_at") != expected_version:
                raise MemoryConflictError("memory version conflict; recall the current record")
            if (
                expected_revision is not None
                and int(existing.get("revision", 1)) != expected_revision
            ):
                raise MemoryConflictError("memory revision conflict; recall the current record")
            fields: dict = {}
            if text is not None:
                text = self._require(text, "text")
                if len(text) > MEM_MAX_TEXT:
                    raise InvalidUsageError("memory text exceeds 4096 characters")
                self._check_sensitive(text)
                fields["text"] = text
            if topic is not None:
                topic = self._require(topic, "topic")
                if len(topic) > 128:
                    raise InvalidUsageError("memory topic exceeds 128 characters")
                self._check_sensitive(topic, "topic")
                fields["topic"] = topic
            if confidence is not None:
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    raise InvalidUsageError("confidence must be between 0 and 1")
                if not 0.0 <= float(confidence) <= 1.0:
                    raise InvalidUsageError("confidence must be between 0 and 1")
                fields["confidence"] = float(confidence)
            if provenance is not None:
                provenance = self._bounded(provenance, "provenance", MEM_MAX_PROVENANCE)
                self._check_sensitive(provenance, "provenance")
                fields["provenance"] = provenance
            next_topic = fields.get("topic", existing["topic"])
            next_text = fields.get("text", existing["text"])
            if not owner_consent and self.requires_owner_consent(next_topic, next_text):
                raise InvalidUsageError(
                    "sensitive personal memory requires explicit owner approval"
                )
            _, text_hash = _suppression_hashes(next_topic, next_text)
            if self.repo.suppression_exists(text_hash):
                raise InvalidUsageError(
                    "this memory was explicitly forgotten and cannot be recreated automatically"
                )
            fields = {k: v for k, v in fields.items() if existing.get(k) != v}
            if fields:
                self.repo.user_update(memory_id, fields, self._now())
            return self.repo.user_get(memory_id)

    def forget_user(self, memory_id: str, *, expected_revision: int | None = None) -> bool:
        source_sessions: set[str] = set()
        with self.repo._db.transaction():
            existing = self.repo.user_get(memory_id)
            if existing is None:
                return False
            if (
                expected_revision is not None
                and int(existing.get("revision", 1)) != expected_revision
            ):
                raise MemoryConflictError("memory revision conflict; recall the current record")
            topic_hash, text_hash = _suppression_hashes(existing["topic"], existing["text"])
            self.repo.suppression_insert(
                topic_hash=topic_hash, text_hash=text_hash, created_at=self._now()
            )
            now = self._now()
            # A forgotten fact must not be extracted again from an owner
            # message, including when the wording is paraphrased later by a
            # deferred extractor. Keep only message ids and a hash; the quote
            # is not copied into the suppression ledger.
            normalized = _normalize_text(existing["text"])
            matching = [
                row for row in self.repo.user_live() if _normalize_text(row["text"]) == normalized
            ]
            lineage_ids: set[str] = set()
            for row in matching:
                lineage_ids.update(self.repo.user_lineage_ids(row["id"]))
            for lineage_id in lineage_ids:
                self.repo.record_suppression_insert(lineage_id, now)
                for source in self.repo.sources_for_memory(lineage_id, live_only=True):
                    source_sessions.add(source["session_id"])
                    self.repo.source_suppression_insert(
                        source["session_id"], source["message_id"], now
                    )
                    self.repo.source_revoke(source["id"], now)
            forgotten = False
            # A prior version allowed the same text under different topics.
            # Forgetting the content must remove every live user duplicate so
            # search and prompt retrieval cannot surface it through a label.
            for row in matching:
                forgotten = self.repo.user_delete(row["id"]) or forgotten
            self.repo.ledger_advance()
        for session_id in source_sessions:
            self.invalidate_compact_state(session_id)
        return forgotten

    # -- selective extraction and conversation privacy -------------------------

    @staticmethod
    def _candidate_from_text(text: str) -> MemoryCandidate | None:
        """Parse only explicit owner language; never infer a personal fact.

        The allow-list intentionally covers ordinary assistant preferences.
        Unknown topics become review candidates, so a later classifier or the
        owner can decide without silently persisting a sensitive inference.
        """
        clean = " ".join((text or "").strip().split())
        if not clean or len(clean) > MEM_SOURCE_QUOTE_MAX:
            return None
        lowered = clean.casefold()
        patterns = (
            (r"^(?:prefiero(?: que)?|i prefer)\s+(.+)$", "preference"),
            (r"^(?:me gusta|i like)\s+(.+)$", "preference"),
            (r"^(?:recuerda que|remember that)\s+(.+)$", "fact"),
            (r"^(?:siempre|always)\s+(.+)$", "rule"),
            (r"^(?:nunca|never)\s+(.+)$", "rule"),
        )
        match = None
        kind = "preference"
        for expression, candidate_kind in patterns:
            match = re.match(expression, clean, re.IGNORECASE)
            if match:
                kind = candidate_kind
                break
        if match is None:
            return None
        value = " ".join(match.group(1).strip().split())
        if not value:
            return None
        topic = "preferencias" if kind == "preference" else "reglas personales"
        sensitive_markers = (
            "salud",
            "médic",
            "medic",
            "diabet",
            "insulin",
            "enfermed",
            "terapia",
            "diagnóst",
            "diagnost",
            "cardió",
            "cardio",
            "salario",
            "sueldo",
            "deuda",
            "banco",
            "financ",
            "tarjeta",
            "dinero",
            "intim",
            "sexual",
            "pareja",
            "embaraz",
            "dirección",
            "direccion",
            "documento",
            "identidad",
            "pasaporte",
            "seguro social",
            "contraseña",
            "contrasena",
            "password",
            "passwd",
            "api key",
            "apikey",
            "token",
            "private key",
            "clave privada",
        )
        # This is intentionally a closed grammar. A substring match such as
        # ``usar mi insulina`` must remain review-gated even though it contains
        # a harmless-looking word like ``usar``.
        benign = (
            r"(?:respuestas?|mensajes?)\s+(?:breves?|cortos?|detallad[oa]s?|larg[oa]s?)",
            r"(?:que\s+)?respondas?\s+(?:en\s+)?(?:español|castellano|inglés|ingles|english)",
            r"(?:usar|usa|utiliza)\s+(?:español|castellano|inglés|ingles|english|markdown)",
            r"formato\s+(?:markdown|texto\s+plano|json|tabla)",
            r"tono\s+(?:formal|casual|amable|directo|profesional)",
            r"idioma\s+(?:español|castellano|inglés|ingles|english)",
            r"canal\s+(?:whatsapp|telegram|discord|panel)",
            r"(?:habla|hablemos)\s+(?:en\s+)?(?:español|castellano|inglés|ingles|english)",
            r"zona\s+horaria\s+UTC[+-](?:0\d|1[0-4])(?::[0-5]\d)?",
            r"timezone\s+UTC[+-](?:0\d|1[0-4])(?::[0-5]\d)?",
        )
        if any(marker in lowered for marker in sensitive_markers):
            return MemoryCandidate(topic, clean, kind, 0.55, "sensitive", "requires owner consent")
        if any(re.fullmatch(pattern, value, flags=re.IGNORECASE) for pattern in benign):
            if re.fullmatch(
                r"(?:respuestas?|mensajes?)\s+(?:breves?|cortos?|detallad[oa]s?|larg[oa]s?)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.longitud"
            elif re.fullmatch(
                r"(?:que\s+)?respondas?\s+(?:en\s+)?(?:español|castellano|inglés|ingles|english)"
                r"|(?:usar|usa|utiliza)\s+(?:español|castellano|inglés|ingles|english)",
                value,
                flags=re.IGNORECASE,
            ) or re.fullmatch(
                r"(?:habla|hablemos)\s+(?:en\s+)?(?:español|castellano|inglés|ingles|english)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.idioma"
            elif re.fullmatch(
                r"(?:usar|usa|utiliza)\s+markdown|formato\s+(?:markdown|texto\s+plano|json|tabla)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.formato"
            elif re.fullmatch(
                r"tono\s+(?:formal|casual|amable|directo|profesional)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.tono"
            elif re.fullmatch(
                r"canal\s+(?:whatsapp|telegram|discord|panel)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.canal"
            elif re.fullmatch(
                r"(?:zona\s+horaria\s+UTC[+-](?:0\d|1[0-4])(?::[0-5]\d)?|"
                r"timezone\s+UTC[+-](?:0\d|1[0-4])(?::[0-5]\d)?)",
                value,
                flags=re.IGNORECASE,
            ):
                topic = "preferencias.zona_horaria"
            return MemoryCandidate(topic, clean, kind, 0.95, "safe_preference")
        return MemoryCandidate(topic, clean, kind, 0.5, "review", "sensitivity is unknown")

    @staticmethod
    def _source_topic(session_id: str, message_id: str) -> str:
        source = hashlib.sha256(f"{session_id}\x00{message_id}".encode()).hexdigest()[:24]
        return f"source:{source}"

    def capture_owner_message(self, session_id: str, message_id: str, text: str) -> dict | None:
        """Capture an explicit owner preference or queue it for review.

        This is called after normal message persistence, so a crash before the
        call leaves a recoverable source message and a later retry is safe.
        """
        control = self.repo.control(session_id)
        if control is not None and control.get("mode") != "auto":
            return None
        if self.repo.source_suppressed(session_id, message_id):
            return None
        source_message = next(
            (row for row in self._ctx.message_repo.list(session_id) if row.id == message_id),
            None,
        )
        if (
            source_message is None
            or source_message.role != "user"
            or source_message.content != text
        ):
            return None
        candidate = self._candidate_from_text(text)
        if candidate is None:
            return None
        source_hash = hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()
        with self.repo._db.transaction():
            existing = self.repo._db.query_one(
                "SELECT id FROM memory_candidates WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            )
            if existing:
                return self.repo.candidate_get(str(existing["id"]))
            candidate_id = self._ctx.ids.new("mem-candidate")
            now = self._now()
            candidate_topic = candidate.topic
            if candidate.classification != "safe_preference":
                candidate_topic = self._source_topic(session_id, message_id)
                candidate = MemoryCandidate(
                    candidate_topic,
                    candidate.text,
                    candidate.kind,
                    candidate.confidence,
                    candidate.classification,
                    candidate.reason,
                )
            if candidate.classification == "safe_preference":
                result = self.remember_user(
                    candidate.text,
                    kind=candidate.kind,
                    topic=candidate.topic,
                    provenance=f"session:{session_id}/message:{message_id}",
                    confidence=candidate.confidence,
                    source={
                        "session_id": session_id,
                        "message_id": message_id,
                        "source_hash": source_hash,
                        "quote": "",
                    },
                )
                memory_id = result["id"]
                status = "accepted"
            else:
                memory_id = None
                status = "pending"
            self.repo.candidate_insert(
                {
                    "id": candidate_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "topic": candidate.topic,
                    # Pending sensitive/unknown text is resolved from the
                    # owner message after consent; do not duplicate it in a
                    # durable candidate row before approval.
                    "text": candidate.text if status == "accepted" else "",
                    "kind": candidate.kind,
                    "confidence": candidate.confidence,
                    "classification": candidate.classification,
                    "reason": candidate.reason,
                    "status": status,
                    "memory_id": memory_id,
                    "created_at": now,
                    "resolved_at": now if status == "accepted" else None,
                }
            )
            return self.repo.candidate_get(candidate_id)

    def list_candidates(self, *, status: str | None = "pending", limit: int = 100) -> list[dict]:
        rows = self.repo.candidates(status=status, limit=limit)
        # The owner panel may preview a pending candidate, but the durable
        # row keeps no copy of sensitive/unknown text before consent.
        for row in rows:
            if not row.get("text") and row.get("status") == "pending":
                source = next(
                    (
                        item
                        for item in self._ctx.message_repo.list(row["session_id"])
                        if item.id == row["message_id"]
                    ),
                    None,
                )
                row["text"] = source.content if source is not None else ""
        return rows

    def resolve_candidate(self, candidate_id: str, decision: str) -> dict:
        if decision not in ("allow_once", "deny"):
            raise InvalidUsageError("candidate decision must be allow_once or deny")
        with self.repo._db.transaction():
            candidate = self.repo.candidate_get(candidate_id)
            if candidate is None:
                raise MemoryNotFoundError(f"memory candidate not found: {candidate_id}")
            if candidate["status"] != "pending":
                return candidate
            if decision == "deny":
                self.repo.candidate_resolve(
                    candidate_id, status="denied", memory_id=None, resolved_at=self._now()
                )
                return self.repo.candidate_get(candidate_id)
            control = self.repo.control(candidate["session_id"])
            if control is not None and control.get("mode") != "auto":
                raise InvalidUsageError("memory extraction is excluded for this conversation")
            if self.repo.source_suppressed(candidate["session_id"], candidate["message_id"]):
                self.repo.candidate_resolve(
                    candidate_id, status="denied", memory_id=None, resolved_at=self._now()
                )
                return self.repo.candidate_get(candidate_id)
            source_row = self._ctx.message_repo.list(candidate["session_id"])
            source_message = next(
                (row for row in source_row if row.id == candidate["message_id"]), None
            )
            if source_message is None:
                raise MemoryNotFoundError("source message not found")
            parsed = self._candidate_from_text(source_message.content or "")
            if parsed is None:
                raise InvalidUsageError("source message is no longer an eligible memory candidate")
            quote = source_message.content or ""
            source_hash = hashlib.sha256(_normalize_text(quote).encode("utf-8")).hexdigest()
            result = self.remember_user(
                parsed.text,
                kind=parsed.kind,
                topic=(
                    candidate["topic"]
                    if candidate["topic"].startswith("source:")
                    else parsed.topic
                ),
                provenance=f"session:{candidate['session_id']}/message:{candidate['message_id']}",
                confidence=float(candidate["confidence"]),
                source={
                    "session_id": candidate["session_id"],
                    "message_id": candidate["message_id"],
                    "source_hash": source_hash,
                    "quote": "",
                },
            )
            self.repo.candidate_resolve(
                candidate_id,
                status="accepted",
                memory_id=result["id"],
                resolved_at=self._now(),
            )
            return self.repo.candidate_get(candidate_id)

    def exclude_conversation(self, session_id: str) -> dict:
        now = self._now()
        removed = 0
        with self.repo._db.transaction():
            self.repo.set_control(session_id, "excluded", now)
            source_rows = self.repo.sources_for_session(session_id, live_only=True)
            memory_ids = {row["memory_id"] for row in source_rows}
            self.repo.source_revoke_for_session(session_id, now)
            for memory_id in memory_ids:
                if not self.repo.sources_for_memory(memory_id, live_only=True):
                    removed = int(self.repo.user_delete(memory_id)) + removed
            self.repo._db.execute(
                "UPDATE memory_candidates SET status = 'denied', resolved_at = ? "
                "WHERE session_id = ? AND status = 'pending'",
                (now, session_id),
            )
            self.repo.ledger_advance()
        return {"session_id": session_id, "mode": "excluded", "memories_removed": removed}

    def conversation_control(self, session_id: str) -> dict:
        return self.repo.control(session_id) or {"session_id": session_id, "mode": "auto"}

    def extraction_allowed(self, session_id: str) -> bool:
        control = self.repo.control(session_id)
        return control is None or control.get("mode") == "auto"

    @staticmethod
    def requires_owner_consent(topic: str, text: str) -> bool:
        value = f"{topic} {text}".casefold()
        markers = (
            "salud",
            "médic",
            "medic",
            "diabet",
            "insulin",
            "enfermed",
            "diagnóst",
            "diagnost",
            "salario",
            "sueldo",
            "deuda",
            "banco",
            "financ",
            "tarjeta",
            "dinero",
            "intim",
            "sexual",
            "pareja",
            "embaraz",
            "dirección",
            "direccion",
            "documento",
            "pasaporte",
            "contraseña",
            "contrasena",
            "password",
            "token",
            "private key",
            "clave privada",
        )
        return any(marker in value for marker in markers)

    def delete_conversation(self, session_id: str) -> dict:
        """Remove conversation-derived data before the session rows disappear."""
        now = self._now()
        excluded = self.exclude_conversation(session_id)
        with self.repo._db.transaction():
            for message in self._ctx.message_repo.list(session_id):
                self.repo.source_suppression_insert(session_id, message.id, now)
            episodic = self.repo.delete_episodic_for_session(session_id)
            self.repo.set_control(session_id, "deleted", now)
            # Keep the minimal source suppression tombstones and deleted
            # conversation control so stale backups cannot re-enable deferred
            # extraction. Quotes/candidates/source rows are removed.
            self.repo._db.execute("DELETE FROM memory_sources WHERE session_id = ?", (session_id,))
            self.repo._db.execute(
                "DELETE FROM memory_candidates WHERE session_id = ?", (session_id,)
            )
        return {
            **excluded,
            "session_id": session_id,
            "mode": "deleted",
            "episodic_removed": episodic,
        }

    def redact_history(self, records: list) -> tuple[list, int]:
        suppressed = self.repo.suppressed_message_ids(records[0].session_id) if records else set()
        if not suppressed:
            return records, 0
        kept = []
        redacted = 0
        hidden_turns: set[str] = set()
        for record in records:
            if record.id in suppressed:
                redacted += 1
                if record.turn_id:
                    hidden_turns.add(record.turn_id)
                continue
            if record.turn_id in hidden_turns and record.role != "user":
                redacted += 1
                continue
            if record.role == "user":
                hidden_turns.discard(record.turn_id)
            kept.append(record)
        return kept, redacted

    def invalidate_compact_state(self, session_id: str) -> None:
        record = self._ctx.session_repo.get(session_id)
        if record is None or record.compact_state is None:
            return
        record.compact_state = None
        record.updated_at = self._now()
        self._ctx.session_repo.update(record)

    # -- backup/recovery privacy ledger ---------------------------------------

    def export_privacy_ledger(self) -> dict:
        """Return suppression metadata only; never export memory content."""
        ledger = {
            "version": 1,
            "watermark": self.repo.ledger_watermark(),
            "memory_suppressions": [
                dict(row)
                for row in self.repo._db.query(
                    "SELECT topic_hash, text_hash, created_at FROM memory_suppressions "
                    "ORDER BY topic_hash, text_hash"
                )
            ],
            "record_suppressions": [
                dict(row)
                for row in self.repo._db.query(
                    "SELECT memory_id, created_at FROM memory_record_suppressions "
                    "ORDER BY memory_id"
                )
            ],
            "source_suppressions": [
                dict(row)
                for row in self.repo._db.query(
                    "SELECT session_id, message_id, created_at FROM memory_source_suppressions "
                    "ORDER BY session_id, message_id"
                )
            ],
            "conversation_controls": [
                dict(row)
                for row in self.repo._db.query(
                    "SELECT session_id, mode, excluded_at, deleted_at "
                    "FROM memory_conversation_controls ORDER BY session_id"
                )
            ],
        }
        return ledger

    @staticmethod
    def privacy_ledger_digest(ledger: dict) -> str:
        return hashlib.sha256(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def import_privacy_ledger(self, ledger: dict, ledger_digest: str | None = None) -> dict:
        if not isinstance(ledger, dict) or ledger.get("version") != 1:
            raise InvalidUsageError("unsupported privacy ledger")
        if ledger_digest is not None and ledger_digest != self.privacy_ledger_digest(ledger):
            raise InvalidUsageError("privacy ledger digest mismatch")
        watermark = ledger.get("watermark")
        if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            raise InvalidUsageError("privacy ledger watermark is invalid")
        memory_rows = ledger.get("memory_suppressions", [])
        record_rows = ledger.get("record_suppressions", [])
        source_rows = ledger.get("source_suppressions", [])
        control_rows = ledger.get("conversation_controls", [])
        if not all(
            isinstance(item, dict)
            for item in (*memory_rows, *record_rows, *source_rows, *control_rows)
        ):
            raise InvalidUsageError("privacy ledger rows are invalid")
        input_digest = self.privacy_ledger_digest(ledger)
        source_sessions_to_refresh: set[str] = set()
        with self.repo._db.transaction():
            for row in memory_rows:
                if not all(
                    isinstance(row.get(key), str) and row.get(key)
                    for key in ("topic_hash", "text_hash", "created_at")
                ):
                    raise InvalidUsageError("privacy suppression row is invalid")
                self.repo.suppression_insert(
                    topic_hash=row["topic_hash"],
                    text_hash=row["text_hash"],
                    created_at=row["created_at"],
                )
            for row in record_rows:
                if not all(
                    isinstance(row.get(key), str) and row.get(key)
                    for key in ("memory_id", "created_at")
                ):
                    raise InvalidUsageError("record suppression row is invalid")
                self.repo.record_suppression_insert(row["memory_id"], row["created_at"])
            for row in source_rows:
                if not all(
                    isinstance(row.get(key), str) and row.get(key)
                    for key in ("session_id", "message_id", "created_at")
                ):
                    raise InvalidUsageError("source suppression row is invalid")
                self.repo.source_suppression_insert(
                    row["session_id"], row["message_id"], row["created_at"]
                )
                source_sessions_to_refresh.add(row["session_id"])
                affected_ids = {
                    item["memory_id"]
                    for item in self.repo.sources_for_message(
                        row["session_id"], row["message_id"], live_only=True
                    )
                }
                for source in self.repo.sources_for_message(
                    row["session_id"], row["message_id"], live_only=True
                ):
                    self.repo.source_revoke(source["id"], self._now())
                for memory_id in affected_ids:
                    if not self.repo.sources_for_memory(memory_id, live_only=True):
                        self.repo.user_delete(memory_id)
            for row in control_rows:
                session_id = row.get("session_id")
                mode = row.get("mode")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or mode not in ("auto", "excluded", "deleted")
                ):
                    raise InvalidUsageError("conversation control row is invalid")
                current = self.repo.control(session_id)
                rank = {"auto": 0, "excluded": 1, "deleted": 2}
                if current is None or rank[mode] >= rank.get(current.get("mode", "auto"), 0):
                    self.repo.set_control(
                        session_id,
                        mode,
                        row.get("excluded_at") or row.get("deleted_at") or self._now(),
                    )
                if mode in ("excluded", "deleted"):
                    messages = self._ctx.message_repo.list(session_id)
                    if mode == "deleted":
                        for message in messages:
                            self.repo.source_suppression_insert(session_id, message.id, self._now())
                    source_rows_for_session = self.repo.sources_for_session(
                        session_id, live_only=True
                    )
                    affected_ids = {item["memory_id"] for item in source_rows_for_session}
                    self.repo.source_revoke_for_session(session_id, self._now())
                    for memory_id in affected_ids:
                        if not self.repo.sources_for_memory(memory_id, live_only=True):
                            self.repo.user_delete(memory_id)
                    self.repo._db.execute(
                        "UPDATE memory_candidates SET status = 'denied', resolved_at = ? "
                        "WHERE session_id = ? AND status = 'pending'",
                        (self._now(), session_id),
                    )
                    if mode == "deleted":
                        self.repo.delete_episodic_for_session(session_id)
                        self.repo._db.execute(
                            "DELETE FROM memory_sources WHERE session_id = ?", (session_id,)
                        )
                        self.repo._db.execute(
                            "DELETE FROM memory_candidates WHERE session_id = ?", (session_id,)
                        )
                        self.repo._db.execute(
                            "DELETE FROM session_events WHERE session_id = ?", (session_id,)
                        )
                        self.repo._db.execute(
                            "DELETE FROM session_messages WHERE session_id = ?", (session_id,)
                        )
                        self.repo._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                    source_sessions_to_refresh.add(session_id)
            # A stale backup can contain a forgotten live or superseded row.
            # Purge by record id first (edited records have different text
            # hashes), then by normalized hash before any restored prompt can
            # see it.  Source rows are revoked before their memory is removed.
            record_ids = {row["memory_id"] for row in record_rows}
            text_hashes = {row.get("text_hash") for row in memory_rows}
            all_memories = [dict(row) for row in self.repo._db.query("SELECT * FROM user_memory")]
            for memory in all_memories:
                _, text_hash = _suppression_hashes(memory["topic"], memory["text"])
                if memory["id"] not in record_ids and text_hash not in text_hashes:
                    continue
                for source in self.repo.sources_for_memory(memory["id"], live_only=True):
                    self.repo.source_suppression_insert(
                        source["session_id"], source["message_id"], self._now()
                    )
                    source_sessions_to_refresh.add(source["session_id"])
                    self.repo.source_revoke(source["id"], self._now())
                self.repo.user_delete(memory["id"])
            applied = self.repo.ledger_set_max(watermark)
        for session_id in source_sessions_to_refresh:
            self.invalidate_compact_state(session_id)
        return {
            "ledger_digest": input_digest,
            "state_digest": self.privacy_ledger_digest(self.export_privacy_ledger()),
            "watermark": applied,
            "imported": True,
        }

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
        expected_version: str | None = None,
    ) -> dict:
        with self.repo._db.transaction():
            existing = self.repo.project_get(project_root, memory_id)
            if existing is None:
                raise InvalidUsageError(f"project memory not found: {memory_id}")
            if expected_version is not None and existing.get("updated_at") != expected_version:
                raise InvalidUsageError("memory version conflict; recall the current record")
            fields: dict = {}
            if text is not None:
                text = self._bounded(text, "text", MEM_MAX_TEXT)
                self._check_sensitive(text)
                fields["text"] = text
            if topic is not None:
                topic = self._bounded(topic, "topic", 128)
                self._check_sensitive(topic, "topic")
                fields["topic"] = topic
            if confidence is not None:
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    raise InvalidUsageError("confidence must be between 0 and 1")
                if not 0.0 <= float(confidence) <= 1.0:
                    raise InvalidUsageError("confidence must be between 0 and 1")
                fields["confidence"] = float(confidence)
            if provenance is not None:
                provenance = self._bounded(provenance, "provenance", MEM_MAX_PROVENANCE)
                self._check_sensitive(provenance, "provenance")
                fields["provenance"] = provenance
            fields = {k: v for k, v in fields.items() if existing.get(k) != v}
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
        with self.repo._db.transaction():
            existing = self.repo.episodic_match(session_id, project_root or "", summary, outcome)
            if existing:
                return {
                    "id": existing["id"],
                    "session_ref": session_id,
                    "summary_chars": len(summary),
                    "deduplicated": True,
                }
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

    @staticmethod
    def _prompt_tokens(query: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(re.findall(r"[\wÀ-ÿ]{2,}", query.lower())))

    @staticmethod
    def _prompt_rank(row: dict, tokens: tuple[str, ...]) -> tuple[int, int, str, str]:
        if not tokens:
            return (0, 0, str(row.get("updated_at", "")), str(row.get("id", "")))
        topic = _normalize_topic(row.get("topic", ""))
        text = _normalize_text(row.get("text", ""))
        score = sum((3 if token in topic else 0) + (1 if token in text else 0) for token in tokens)
        preference = 1 if row.get("kind") == "preference" else 0
        return (score, preference, str(row.get("updated_at", "")), str(row.get("id", "")))

    def _prompt_rows(self, rows: list[dict], query: str) -> list[dict]:
        tokens = self._prompt_tokens(query)
        ranked = sorted(rows, key=lambda row: self._prompt_rank(row, tokens), reverse=True)
        if not tokens:
            return ranked[:15]
        matched = [row for row in ranked if self._prompt_rank(row, tokens)[0] > 0]
        recent = [row for row in ranked if self._prompt_rank(row, tokens)[0] == 0]
        return (matched + recent)[:15]

    def prompt_segment(self, project_root: str | None, query: str = "") -> str | None:
        """Render the durable memory block for the system prompt (if any).

        Durable memory is injected so the agent starts aligned with stored
        preferences/facts; it is explicitly lower-authority than instructions
        and marked possibly stale.
        """
        tokens = self._prompt_tokens(query)
        user_rows = self._prompt_rows(self.repo.user_live(), query)
        project_rows = (
            self._prompt_rows(self.repo.project_live(project_root), query) if project_root else []
        )
        if tokens:
            # Rank both scopes together before applying the character budget;
            # unrelated user rows must not crowd a matching project fact out.
            candidates = [(row, False) for row in user_rows] + [(row, True) for row in project_rows]
            candidates.sort(
                key=lambda item: self._prompt_rank(item[0], tokens),
                reverse=True,
            )
        else:
            candidates = [(row, False) for row in user_rows] + [(row, True) for row in project_rows]
        lines = [
            f"- [{'project:' if is_project else ''}{row['kind']}] {row['text']}"
            for row, is_project in candidates
        ]
        if not lines:
            return None
        header = (
            "Durable memory (explicit records; lower authority than instructions; "
            "possibly stale — re-verify volatile facts before relying on them):"
        )
        rendered = header
        selected: list[str] = []
        for line in lines:
            addition = f"\n{line}"
            if len(rendered) + len(addition) <= MEM_PROMPT_MAX_CHARS:
                selected.append(line)
                rendered += addition
                continue
            remaining = MEM_PROMPT_MAX_CHARS - len(rendered) - 1
            if remaining > 3 and not selected:
                selected.append(f"{line[: remaining - 1]}…")
                rendered += f"\n{selected[-1]}"
            break
        return rendered


__all__ = [
    "MemoryConflictError",
    "MemoryNotFoundError",
    "MemoryService",
    "find_sensitive_match",
]
