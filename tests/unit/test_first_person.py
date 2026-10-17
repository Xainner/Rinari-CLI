"""Rinari speaks of herself in first person (plan 0.4.0 #95).

Everything the model reads about itself is written to it ("your engine",
"your skills"), never about a third party called Rinari. Product names,
commands, repositories and things called Rinari that are not her stay.
"""

from __future__ import annotations

import re
from pathlib import Path

from rinari.browser.human_check import message as human_check_message
from rinari.engine_protocol.orphans import MESSAGE
from rinari.tools.native import all_native_tools

ROOT = Path(__file__).parents[2] / "src" / "rinari"
THIRD_PERSON = re.compile(
    r"Rinari's|\bby Rinari\b"
    r"|\bRinari (?:stores|keeps|uses|checks|says|records|saves|reads|runs|is running)\b"
)


def _model_facing_texts() -> dict[str, str]:
    constitution = (ROOT / "assets" / "constitution.md").read_text(encoding="utf-8")
    # The rule itself quotes what not to say.
    rule = constitution.index("## Yourself")
    texts = {"constitution": constitution[:rule] + constitution[constitution.index("---", rule) :]}
    for path in (ROOT / "assets" / "skills").rglob("*.md"):
        texts[str(path.relative_to(ROOT))] = path.read_text(encoding="utf-8")
    for tool in all_native_tools():
        texts[f"tool:{tool.name}"] = tool.description
    texts["orphans"] = MESSAGE
    texts["human_check"] = human_check_message(
        {"name": "Cloudflare Turnstile", "kind": "turnstile"}
    )
    return texts


def test_what_the_model_reads_about_itself_is_in_second_person() -> None:
    found = [
        f"{name}: {match.group(0)}"
        for name, text in _model_facing_texts().items()
        for match in THIRD_PERSON.finditer(text)
    ]
    assert found == []


def test_the_rule_is_in_the_constitution_whatever_the_soul() -> None:
    constitution = (ROOT / "assets" / "constitution.md").read_text(encoding="utf-8")
    assert "The app (Rinari Agent), the Engine and the CLI are you" in constitution
