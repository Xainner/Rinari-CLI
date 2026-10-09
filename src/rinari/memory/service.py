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

import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError
from rinari.shared.redaction import REDACTED, redact_text
from rinari.storage.repositories.memory import PROJECT_KINDS, USER_KINDS

MEM_MAX_TEXT = 4096
MEM_MAX_SUMMARY = 400
MEM_MAX_PROVENANCE = 256
MEM_PROMPT_MAX_CHARS = 12000
MEM_SOURCE_QUOTE_MAX = 4096

# Learned facts (memory.propose): what Rinari learns while working, not what
# the owner dictated. One short fact per record keeps the prompt list compact.
LEARNED_FACTS_KEY = "memory.learned_facts"
LEARNED_FACTS_MODES = ("ask", "auto")
LEARNED_KINDS = ("environment", "workflow", "preference", "fact")
LEARNED_PROVENANCE_PREFIX = "learned:"
MEM_LEARNED_MAX_TEXT = 600
# Facts the model needs before it acts (hosts, ports, how to start things)
# ride in their own short list so ranking against the message never drops them.
CONTEXT_KINDS = ("environment", "workflow")
MEM_CONTEXT_MAX_ITEMS = 20
MEM_CONTEXT_MAX_CHARS = 2500
MEM_CONTEXT_LINE_MAX = 300
_SIMILAR_RATIO = 0.9
# Candidates the model wrote itself: their text lives in the row, so the owner
# can approve them without re-reading a message (and during a turn).
MODEL_CANDIDATE_CLASSES = ("agent_sensitive", "learned", "learned_sensitive")
_SENSITIVE_CLASSES = ("sensitive", "agent_sensitive", "learned_sensitive")


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


def _fold(value: str) -> str:
    """Casefold and drop accents, so `configuración` finds `configuracion`."""
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Words that carry no meaning for recall in the two languages Rinari is used
# in most; without them "el servidor de la casa" ranks by "servidor", "casa".
_STOPWORD_TEXT = (
    "a al como con de del el en es la las lo los para por que se su un una y "
    "an and are as at be by for from how in is it of on or that the this to was what with"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_MAX_QUERY_TERMS = 12


def query_terms(query: str) -> tuple[str, ...]:
    tokens = re.findall(r"\w{2,}", _fold(query))
    return tuple(dict.fromkeys(t for t in tokens if t not in _STOPWORDS))[:_MAX_QUERY_TERMS]


def _term_in(term: str, haystack: str) -> bool:
    # Short terms (ip, db, ssh) match at a word start only: as substrings
    # they would hit half the corpus ("ip" in "script").
    if len(term) <= 3:
        return re.search(rf"(?<!\w){re.escape(term)}", haystack) is not None
    return term in haystack


def rank_by_terms(
    rows: list[dict], query: str, fields: dict[str, int], *, limit: int
) -> list[dict]:
    """Rows that match the query terms, best first; recency breaks ties.

    `rows` arrive newest first. Ranking is by how many distinct terms a row
    contains, then by the weighted fields they hit, then by recency. A query
    with no meaningful terms keeps plain recency order. A whole-query match
    was the only thing the old LIKE found, so it still earns a bonus.
    """
    terms = query_terms(query)
    if not terms:
        return rows[:limit]
    phrase = " ".join(_fold(query).split())
    scored: list[tuple[int, int, int, dict]] = []
    for index, row in enumerate(rows):
        hits = 0
        score = 0
        haystacks = {name: _fold(str(row.get(name) or "")) for name in fields}
        for term in terms:
            matched = False
            for name, weight in fields.items():
                if _term_in(term, haystacks[name]):
                    score += weight
                    matched = True
            hits += int(matched)
        if not hits:
            continue
        if len(terms) > 1 and any(phrase in haystacks[name] for name in fields):
            score += 2
        scored.append((-hits, -score, index, row))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:limit]]


def _similar(a: str, b: str) -> bool:
    left, right = _fold(_normalize_text(a)), _fold(_normalize_text(b))
    if left == right:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= _SIMILAR_RATIO


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


def candidate_view(row: dict | None) -> dict | None:
    """Candidate as the desktop reads it: store scope and sensitivity are
    explicit, so a card never has to decode the classification."""
    if row is None:
        return None
    view = dict(row)
    view["scope"] = view.get("scope") or "user"
    view["sensitive"] = view.get("classification") in _SENSITIVE_CLASSES
    return view


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
        view["scope"] = "user"
        return view

    @staticmethod
    def _project_view(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {**row, "scope": "project"}

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
        if kind not in USER_KINDS:
            raise InvalidUsageError(f"user memory kind must be one of: {', '.join(USER_KINDS)}")
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
        rows = [row for row in self.repo.user_live() if kind is None or row["kind"] == kind]
        ranked = rank_by_terms(rows, query, {"topic": 3, "text": 2}, limit=max(1, min(limit, 200)))
        return [self._user_view(row) for row in ranked]

    def list_user(self, *, limit: int = 100, kind: str | None = None) -> list[dict]:
        rows = [row for row in self.repo.user_live() if kind is None or row["kind"] == kind]
        return [self._user_view(row) for row in rows[: max(1, min(limit, 500))]]

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
        # The model already kept something from this message in its own words
        # (`remember_for_owner`): extracting the literal sentence as well
        # would store the same memory twice under another topic.
        has_candidate = self.repo._db.query_one(
            "SELECT 1 FROM memory_candidates WHERE session_id = ? AND message_id = ?",
            (session_id, message_id),
        )
        if not has_candidate and self.repo.sources_for_message(session_id, message_id):
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

    def remember_for_owner(
        self,
        session_id: str,
        message_id: str,
        owner_text: str,
        *,
        text: str,
        topic: str,
        kind: str,
        confidence: float,
    ) -> dict:
        """Personal memory the model writes during the owner's turn.

        The model words the entry; provenance is the owner message that
        started the turn, never a claim of the model. Sensitive personal data
        becomes a pending proposal with its text, because only the owner can
        approve it and the review needs to show what would be kept. Secrets
        are rejected here, as in every other write.
        """
        control = self.repo.control(session_id)
        if control is not None and control.get("mode") != "auto":
            raise InvalidUsageError("memory is excluded for this conversation.")
        if self.repo.source_suppressed(session_id, message_id):
            raise InvalidUsageError("the owner withdrew this message from memory.")
        text = self._require(text, "text")
        topic = self._require(topic, "topic")
        self._check_sensitive(text)
        self._check_sensitive(topic, "topic")
        source = {
            "session_id": session_id,
            "message_id": message_id,
            "source_hash": hashlib.sha256(_normalize_text(owner_text).encode("utf-8")).hexdigest(),
            "quote": "",
        }
        if self.requires_owner_consent(topic, text):
            now = self._now()
            candidate_id = self._ctx.ids.new("mem-candidate")
            self.repo.candidate_insert(
                {
                    "id": candidate_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "topic": topic,
                    "text": text,
                    "kind": kind,
                    "confidence": confidence,
                    "classification": "agent_sensitive",
                    "reason": "sensitive personal data requires owner consent",
                    "status": "pending",
                    "memory_id": None,
                    "created_at": now,
                    "resolved_at": None,
                }
            )
            return {"pending": True, "candidate": self.repo.candidate_get(candidate_id)}
        return self.remember_user(
            text,
            kind=kind,
            topic=topic,
            provenance=f"session:{session_id}/message:{message_id}",
            confidence=confidence,
            source=source,
        )

    def list_candidates(self, *, status: str | None = "pending", limit: int = 100) -> list[dict]:
        if status == "resolved":
            rows = self.repo.candidates(status=("accepted", "denied"), limit=limit)
        else:
            rows = self.repo.candidates(status=None if status == "all" else status, limit=limit)
        rows = [candidate_view(row) for row in rows]
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

    def resolve_candidate(
        self,
        candidate_id: str,
        decision: str,
        *,
        text: str | None = None,
        topic: str | None = None,
    ) -> dict:
        """Approve or decline a pending candidate.

        With `text`/`topic` the owner approves an edited version: it goes
        through the same secret checks as anything the model writes.
        """
        if decision not in ("allow_once", "deny"):
            raise InvalidUsageError("candidate decision must be allow_once or deny")
        if decision == "deny" and (text is not None or topic is not None):
            raise InvalidUsageError("text and topic only apply when approving a candidate")
        if text is not None:
            text = self._bounded(text, "text", MEM_MAX_TEXT)
            self._check_learned_secret(text, "text")
        if topic is not None:
            topic = self._bounded(topic, "topic", 128)
            self._check_learned_secret(topic, "topic")
        return candidate_view(
            self._resolve_candidate_row(candidate_id, decision, text=text, topic=topic)
        )

    def _resolve_candidate_row(
        self, candidate_id: str, decision: str, *, text: str | None, topic: str | None
    ) -> dict:
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
            if candidate.get("classification") in ("learned", "learned_sensitive"):
                return self._accept_learned(candidate, text=text, topic=topic)
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
            quote = source_message.content or ""
            source = {
                "session_id": candidate["session_id"],
                "message_id": candidate["message_id"],
                "source_hash": hashlib.sha256(_normalize_text(quote).encode("utf-8")).hexdigest(),
                "quote": "",
            }
            provenance = f"session:{candidate['session_id']}/message:{candidate['message_id']}"
            if candidate.get("classification") == "agent_sensitive":
                # A proposal the model wrote in the owner's turn: the owner
                # approves exactly the text shown (or their edit of it), not a
                # re-parse of the message.
                result = self.remember_user(
                    text if text is not None else candidate["text"],
                    kind=candidate["kind"],
                    topic=topic if topic is not None else candidate["topic"],
                    provenance=provenance,
                    confidence=float(candidate["confidence"]),
                    source=source,
                )
            else:
                parsed = self._candidate_from_text(quote)
                if parsed is None:
                    raise InvalidUsageError(
                        "source message is no longer an eligible memory candidate"
                    )
                default_topic = (
                    candidate["topic"] if candidate["topic"].startswith("source:") else parsed.topic
                )
                result = self.remember_user(
                    text if text is not None else parsed.text,
                    kind=parsed.kind,
                    topic=topic if topic is not None else default_topic,
                    provenance=provenance,
                    confidence=float(candidate["confidence"]),
                    source=source,
                )
            self.repo.candidate_resolve(
                candidate_id,
                status="accepted",
                memory_id=result["id"],
                resolved_at=self._now(),
            )
            return self.repo.candidate_get(candidate_id)

    # -- learned facts ---------------------------------------------------------

    def learned_facts_mode(self) -> str:
        """`ask` (default): learned facts wait for approval. `auto`: saved."""
        value = self._ctx.config_repo.get(LEARNED_FACTS_KEY)
        return value if value in LEARNED_FACTS_MODES else "ask"

    def set_learned_facts_mode(self, mode: str) -> str:
        from rinari.storage.records import ConfigValue

        if mode not in LEARNED_FACTS_MODES:
            raise InvalidUsageError("learned_facts must be ask or auto")
        self._ctx.config_repo.set(
            ConfigValue(key=LEARNED_FACTS_KEY, value=mode, updated_at=self._now())
        )
        return mode

    def _check_learned_secret(self, text: str, field: str = "text") -> None:
        """Memory's credential patterns plus the history redactor's.

        A learned command is the likeliest place for a secret to slip in
        (`mysql -p...`, `user:pass@host`, `--token x`): whatever the redactor
        would hide is refused instead of stored.
        """
        reason = find_sensitive_match(text)
        if reason is None and (REDACTED in text or redact_text(text) != text):
            reason = "contains a credential"
        if reason is not None:
            raise InvalidUsageError(
                f"memory {field} {reason}; secrets must never be stored in memory. "
                "Name where the secret lives (an environment variable, the credential "
                "store) instead of its value",
            )

    @staticmethod
    def learned_needs_consent(topic: str, text: str) -> bool:
        """Personal-data check for learned facts.

        The owner-consent markers include `dirección`, which in Spanish is
        also how an IP or server address is named, and `documento`, the
        Documents folder: those infrastructure phrasings are not personal data.
        """
        value = f"{topic} {text}".casefold()
        value = re.sub(
            r"direcci[oó]n(?:es)?\s+(?:ip|mac|de\s+red|del?\s+(?:servidor|host|equipo|"
            r"api|repositorio|servicio|proxy|gateway))",
            " ",
            value,
        )
        value = re.sub(r"documentos?[\\/]|[\\/]documentos?\b", " ", value)
        return MemoryService.requires_owner_consent("", value)

    def _learned_store_rows(self, scope: str, project_root: str | None) -> list[dict]:
        rows = list(self.repo.user_live())
        if scope == "project" and project_root:
            rows += self.repo.project_live(project_root)
        return rows

    def propose_learned(
        self,
        session_id: str,
        *,
        text: str,
        topic: str,
        kind: str = "fact",
        scope: str = "user",
        project_root: str | None = None,
        trusted: bool = True,
        untrusted_reason: str = "",
    ) -> dict:
        """Keep a fact learned while working, or propose it to the owner.

        `ask` mode, sensitive personal data and untrusted turns (external
        content read, not started by the owner) produce a pending candidate;
        `auto` mode saves the rest at once with `learned:` provenance, which
        the owner can undo. Duplicates, declined and forgotten facts are
        reported, never stored again.
        """
        if not self.extraction_allowed(session_id):
            raise InvalidUsageError("memory is excluded for this conversation")
        if kind not in LEARNED_KINDS:
            raise InvalidUsageError(f"kind must be one of: {', '.join(LEARNED_KINDS)}")
        if scope not in ("user", "project"):
            raise InvalidUsageError("scope must be user or project")
        if scope == "project" and not project_root:
            raise InvalidUsageError("project scope needs a project")
        text = self._bounded(text, "text", MEM_LEARNED_MAX_TEXT)
        topic = self._bounded(topic, "topic", 128)
        self._check_learned_secret(text)
        self._check_learned_secret(topic, "topic")
        _, text_hash = _suppression_hashes(topic, text)
        if self.repo.suppression_exists(text_hash):
            return {"status": "forgotten"}
        for row in self._learned_store_rows(scope, project_root):
            if _similar(row["text"], text):
                view = (
                    self._project_view(row)
                    if row.get("project_root") is not None
                    else self._user_view(row)
                )
                return {"status": "already_known", "memory": view}
        for row in self.repo.candidates(status=("pending", "denied"), limit=500):
            if row.get("classification") not in MODEL_CANDIDATE_CLASSES or not row.get("text"):
                continue
            if (row.get("scope") or "user") != scope or (
                scope == "project" and row.get("project_root") != project_root
            ):
                continue
            if _similar(row["text"], text):
                status = "already_proposed" if row["status"] == "pending" else "declined"
                return {"status": status, "candidate": candidate_view(row)}
        sensitive = self.learned_needs_consent(topic, text)
        provenance = f"{LEARNED_PROVENANCE_PREFIX}session/{session_id}"
        if self.learned_facts_mode() == "auto" and not sensitive and trusted:
            if scope == "project":
                result = self.remember_project(
                    str(project_root),
                    text,
                    kind=kind if kind in PROJECT_KINDS else "fact",
                    topic=topic,
                    provenance=provenance,
                    confidence=0.9,
                )
                record = self._project_view(self.repo.project_get(str(project_root), result["id"]))
            else:
                result = self.remember_user(
                    text, kind=kind, topic=topic, provenance=provenance, confidence=0.9
                )
                record = self.get_user(result["id"])
            return {"status": "saved", "action": result["action"], "memory": record}
        if sensitive:
            reason = "sensitive personal data requires owner approval"
        elif not trusted:
            reason = untrusted_reason or "learned in a turn the owner did not start"
        else:
            reason = "learned facts wait for the owner's approval"
        candidate_id = self._ctx.ids.new("mem-candidate")
        self.repo.candidate_insert(
            {
                "id": candidate_id,
                "session_id": session_id,
                # Not the owner message: a candidate row on that message would
                # stop the end-of-turn capture of what the owner stated.
                "message_id": "",
                "topic": topic,
                "text": text,
                "kind": kind,
                "confidence": 0.9,
                "classification": "learned_sensitive" if sensitive else "learned",
                "reason": reason,
                "status": "pending",
                "memory_id": None,
                "created_at": self._now(),
                "resolved_at": None,
                "scope": scope,
                "project_root": project_root if scope == "project" else None,
            }
        )
        return {
            "status": "pending",
            "candidate": candidate_view(self.repo.candidate_get(candidate_id)),
        }

    def _accept_learned(self, candidate: dict, *, text: str | None, topic: str | None) -> dict:
        text = text if text is not None else candidate["text"]
        topic = topic if topic is not None else candidate["topic"]
        provenance = f"{LEARNED_PROVENANCE_PREFIX}session/{candidate['session_id']}"
        if candidate.get("scope") == "project" and candidate.get("project_root"):
            kind = candidate["kind"] if candidate["kind"] in PROJECT_KINDS else "fact"
            result = self.remember_project(
                candidate["project_root"],
                text,
                kind=kind,
                topic=topic,
                provenance=provenance,
                confidence=float(candidate["confidence"]),
            )
        else:
            result = self.remember_user(
                text,
                kind=candidate["kind"],
                topic=topic,
                provenance=provenance,
                confidence=float(candidate["confidence"]),
            )
        self.repo.candidate_resolve(
            candidate["id"], status="accepted", memory_id=result["id"], resolved_at=self._now()
        )
        return self.repo.candidate_get(candidate["id"])

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

    def session_exists(self, session_id: str) -> bool:
        return self._ctx.session_repo.get(session_id) is not None

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
            # Only the owner's messages are memory sources: a tombstone per
            # assistant/tool message only grew the ledger (thousands of rows).
            for message in self._ctx.message_repo.list(session_id):
                if message.role == "user":
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
                            if message.role == "user":
                                self.repo.source_suppression_insert(
                                    session_id, message.id, self._now()
                                )
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
        if kind not in PROJECT_KINDS:
            raise InvalidUsageError(
                f"project memory kind must be one of: {', '.join(PROJECT_KINDS)}"
            )
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
        rows = [
            row
            for row in self.repo.project_live(project_root)
            if kind is None or row["kind"] == kind
        ]
        ranked = rank_by_terms(rows, query, {"topic": 3, "text": 2}, limit=max(1, min(limit, 200)))
        return [self._project_view(row) for row in ranked]

    def list_project(
        self, project_root: str | None, *, limit: int = 100, kind: str | None = None
    ) -> list[dict]:
        """Live project records; `project_root=None` lists every project."""
        rows = (
            self.repo.project_live(project_root) if project_root else self.repo.project_all_live()
        )
        rows = [row for row in rows if kind is None or row["kind"] == kind]
        return [self._project_view(row) for row in rows[: max(1, min(limit, 500))]]

    def get_any(self, memory_id: str) -> dict | None:
        """A live user or project record by id, with its `scope`."""
        row = self.get_user(memory_id)
        if row is not None:
            return row
        return self._project_view(self.repo.project_get_by_id(memory_id))

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
        expected_revision: int | None = None,
    ) -> dict:
        with self.repo._db.transaction():
            existing = self.repo.project_get(project_root, memory_id)
            if existing is None:
                raise MemoryNotFoundError(f"project memory not found: {memory_id}")
            if expected_version is not None and existing.get("updated_at") != expected_version:
                raise MemoryConflictError("memory version conflict; recall the current record")
            if (
                expected_revision is not None
                and int(existing.get("revision", 1)) != expected_revision
            ):
                raise MemoryConflictError("memory revision conflict; recall the current record")
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
            return self._project_view(self.repo.project_get(project_root, memory_id))

    def forget_project(
        self, project_root: str, memory_id: str, *, expected_revision: int | None = None
    ) -> bool:
        with self.repo._db.transaction():
            existing = self.repo.project_get(project_root, memory_id)
            if existing is None:
                return False
            if (
                expected_revision is not None
                and int(existing.get("revision", 1)) != expected_revision
            ):
                raise MemoryConflictError("memory revision conflict; recall the current record")
            if str(existing.get("provenance") or "").startswith(LEARNED_PROVENANCE_PREFIX):
                # Undoing a learned fact must stick: the model would otherwise
                # learn it again in the next session that runs into it.
                topic_hash, text_hash = _suppression_hashes(existing["topic"], existing["text"])
                self.repo.suppression_insert(
                    topic_hash=topic_hash, text_hash=text_hash, created_at=self._now()
                )
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
        # Ranked by the query in both paths: without a project the old code
        # ignored the query and returned the newest rows.
        rows = self.repo.episodic_window(project_root or None)
        return rank_by_terms(
            rows, query, {"summary": 2, "outcome": 1}, limit=max(1, min(limit, 100))
        )

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
        rows = self.repo.pattern_list(scope=scope, limit=200)
        return rank_by_terms(rows, query, {"topic": 3, "text": 2}, limit=max(1, min(limit, 100)))

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

    def _context_lines(self, project_root: str | None, query: str) -> tuple[list[str], set[str]]:
        """Compact "known environment" list: environment/workflow records.

        These are what the model needs before acting (where a server lives,
        how a project starts), so they are listed whatever the message says,
        relevant first, and bounded in count and characters.
        """
        rows = [(row, False) for row in self.repo.user_live() if row["kind"] in CONTEXT_KINDS]
        if project_root:
            rows += [
                (row, True)
                for row in self.repo.project_live(project_root)
                if row["kind"] in CONTEXT_KINDS
            ]
        tokens = self._prompt_tokens(query)
        rows.sort(key=lambda item: self._prompt_rank(item[0], tokens), reverse=True)
        lines: list[str] = []
        used: set[str] = set()
        size = 0
        for row, is_project in rows[:MEM_CONTEXT_MAX_ITEMS]:
            text = " ".join(str(row["text"]).split())
            if len(text) > MEM_CONTEXT_LINE_MAX:
                text = text[: MEM_CONTEXT_LINE_MAX - 1] + "…"
            line = f"- {'[project] ' if is_project else ''}{row['topic']}: {text}"
            if size + len(line) + 1 > MEM_CONTEXT_MAX_CHARS:
                break
            lines.append(line)
            used.add(str(row["id"]))
            size += len(line) + 1
        return lines, used

    def prompt_segment(self, project_root: str | None, query: str = "") -> str | None:
        """Render the durable memory block for the system prompt (if any).

        Durable memory is injected so the agent starts aligned with stored
        preferences/facts; it is explicitly lower-authority than instructions
        and marked possibly stale.
        """
        tokens = self._prompt_tokens(query)
        context_lines, listed = self._context_lines(project_root, query)
        user_rows = self._prompt_rows(
            [row for row in self.repo.user_live() if row["id"] not in listed], query
        )
        project_rows = (
            self._prompt_rows(
                [row for row in self.repo.project_live(project_root) if row["id"] not in listed],
                query,
            )
            if project_root
            else []
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
        if not lines and not context_lines:
            return None
        header = (
            "Durable memory (explicit records; lower authority than instructions; "
            "possibly stale — re-verify volatile facts before relying on them):"
        )
        rendered = header
        if context_lines:
            rendered += "\nKnown environment and workflows:\n" + "\n".join(context_lines)
            if lines:
                rendered += "\nOther records:"
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
    "LEARNED_FACTS_MODES",
    "LEARNED_KINDS",
    "MemoryConflictError",
    "MemoryNotFoundError",
    "MemoryService",
    "candidate_view",
    "find_sensitive_match",
    "rank_by_terms",
]
