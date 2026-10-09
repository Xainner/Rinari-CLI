"""How much two skills overlap in purpose, to stop near-duplicates.

Real libraries do not grow copies so much as stale siblings: a skill written
for one way of doing a job stays around when a newer skill does the same job
another way (an image lab reached over HTTP, then over MCP). Word overlap does
not see that -- generic words like "test" or "verify" dominate it -- so the
score is TF-IDF cosine over the installed skills themselves: what two skills
share and few others have (a model name, a host, a format list) is what
counts. Name, description and triggers are weighted over the body because
they say what a skill is for.

Calibrated on a real library of 27 skills (10 learned, 17 packaged): the
superseded pair scored 0.41, a skill and its specialization 0.33, siblings of
one lab about 0.30, unrelated packaged skills below 0.24.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

#: A new skill this close to an existing one must update it or say why not.
#: Low on purpose: a false alarm costs the model one sentence of reasons.
PROPOSE_THRESHOLD = 0.25
#: Pairs worth showing the owner as possible duplicates of their own skills.
REVIEW_THRESHOLD = 0.30
#: At most this many existing skills are named when a proposal is stopped.
MAX_SIMILAR = 3

_STOP = frozenset(
    [
        "a",
        "al",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "con",
        "de",
        "del",
        "el",
        "en",
        "es",
        "for",
        "from",
        "in",
        "is",
        "it",
        "la",
        "las",
        "los",
        "o",
        "of",
        "on",
        "or",
        "para",
        "por",
        "que",
        "se",
        "the",
        "this",
        "to",
        "un",
        "una",
        "use",
        "used",
        "uses",
        "using",
        "when",
        "with",
        "y",
        "your",
        "skill",
        "skills",
        "no",
        "si",
        "lo",
        "le",
        "su",
        "sus",
        "mas",
        "cuando",
        "como",
        "este",
        "esta",
        "esto",
        "usa",
        "usala",
        "activala",
        "name",
        "description",
        "version",
        "triggers",
    ]
)
_HEAD_WEIGHT = 3  # name, description and triggers count three times
_SHARED_SHOWN = 5


@dataclass(frozen=True, slots=True)
class SkillText:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source: str = "user"


@dataclass(frozen=True, slots=True)
class Similar:
    name: str
    score: float
    shared: tuple[str, ...]
    source: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 3),
            "shared": list(self.shared),
            "source": self.source,
        }


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _fold(text)) if len(t) > 1 and t not in _STOP]


def _terms(skill: SkillText) -> Counter:
    head = " ".join([skill.name.replace("-", " "), skill.description, " ".join(skill.triggers)])
    counts = Counter(_tokens(skill.body))
    for token in _tokens(head):
        counts[token] += _HEAD_WEIGHT
    return counts


class SimilarityIndex:
    """TF-IDF vectors of a set of skills; IDF comes from the same set."""

    def __init__(self, skills: Iterable[SkillText]) -> None:
        self._skills = {skill.name: skill for skill in skills}
        self._terms = {name: _terms(skill) for name, skill in self._skills.items()}
        self._df: Counter = Counter()
        for counts in self._terms.values():
            self._df.update(set(counts))
        self._vectors = {name: self._vector(counts) for name, counts in self._terms.items()}

    def _vector(self, counts: Counter) -> dict[str, float]:
        total = len(self._terms) + 1
        weights = {
            term: (1 + math.log(count)) * math.log(total / (self._df.get(term, 0) + 1))
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {term: w / norm for term, w in weights.items() if w > 0}

    def _compare(self, a: dict[str, float], b: dict[str, float]) -> tuple[float, tuple[str, ...]]:
        common = set(a) & set(b)
        score = sum(a[t] * b[t] for t in common)
        shared = tuple(t for t in sorted(common, key=lambda t: -(a[t] * b[t]))[:_SHARED_SHOWN])
        return score, shared

    def similar_to(
        self,
        candidate: SkillText,
        *,
        threshold: float = PROPOSE_THRESHOLD,
        limit: int = MAX_SIMILAR,
        exclude: Iterable[str] = (),
    ) -> list[Similar]:
        """Installed skills close to `candidate`, best first.

        The candidate is scored against the index as if it were one more
        skill, so its own rare words count as rare.
        """
        skipped = {candidate.name, *exclude}
        vector = self._vector(_terms(candidate))
        found = []
        for name, other in self._vectors.items():
            if name in skipped:
                continue
            score, shared = self._compare(vector, other)
            if score >= threshold:
                found.append(Similar(name, score, shared, self._skills[name].source))
        found.sort(key=lambda s: (-s.score, s.name))
        return found[:limit]

    def pairs(
        self, *, threshold: float = REVIEW_THRESHOLD, among: Iterable[str] | None = None
    ) -> list[tuple[Similar, Similar]]:
        """Pairs of skills above the threshold, both in `among` when given."""
        names = sorted(self._vectors if among is None else set(among) & set(self._vectors))
        out = []
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                score, shared = self._compare(self._vectors[a], self._vectors[b])
                if score >= threshold:
                    out.append(
                        (
                            Similar(a, score, shared, self._skills[a].source),
                            Similar(b, score, shared, self._skills[b].source),
                        )
                    )
        out.sort(key=lambda pair: (-pair[0].score, pair[0].name, pair[1].name))
        return out


__all__ = [
    "MAX_SIMILAR",
    "PROPOSE_THRESHOLD",
    "REVIEW_THRESHOLD",
    "Similar",
    "SimilarityIndex",
    "SkillText",
]
