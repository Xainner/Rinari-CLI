"""Built-in team templates («Usar una plantilla», vision 10.4).

A template is data shipped with the Engine (`assets/agency_templates/*.json`):
a team's name and purpose and its agents' names, jobs, instructions and
permission mode, in every language it is written in. Never connectors,
accounts, budgets nor models: those are the owner's to give once the team
exists. `agency.team.from_template` turns one into agents and a team.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from rinari.agency.service import MAX_INSTRUCTIONS, MAX_NAME, MAX_ROLE, PERMISSION_PROFILES

#: The language a template is shown in when the caller names none or one it lacks.
DEFAULT_LANGUAGE = "en"


class TemplateError(ValueError):
    """Invalid input for a template; the message is safe to show."""


@cache
def _load() -> tuple[dict[str, Any], ...]:
    folder = files("rinari").joinpath("assets/agency_templates")
    loaded = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".json"):
            continue
        data = json.loads(entry.read_text(encoding="utf-8"))
        _check(data, entry.name)
        loaded.append(data)
    return tuple(loaded)


def _check(data: dict[str, Any], source: str) -> None:
    """A template must make a valid team of valid cards in each of its languages."""
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ValueError(f"{source}: a template needs its id")
    texts = data.get("texts")
    if not isinstance(texts, dict) or DEFAULT_LANGUAGE not in texts:
        raise ValueError(f"{source}: a template needs its texts in {DEFAULT_LANGUAGE}")
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ValueError(f"{source}: a template needs its agents")
    for agent in agents:
        if agent.get("permission_profile") not in PERMISSION_PROFILES:
            raise ValueError(f"{source}: unknown permission_profile")
    for language, text in texts.items():
        cards = text.get("agents") or []
        if len(cards) != len(agents) or not text.get("name"):
            raise ValueError(f"{source}: the {language} texts do not match its agents")
        for card in cards:
            if not 0 < len(card.get("name") or "") <= MAX_NAME:
                raise ValueError(f"{source}: an agent's name is too long or empty")
            if len(card.get("role") or "") > MAX_ROLE:
                raise ValueError(f"{source}: an agent's role is too long")
            if len(card.get("instructions") or "") > MAX_INSTRUCTIONS:
                raise ValueError(f"{source}: an agent's instructions are too long")


def _language(raw: Any, texts: dict[str, Any]) -> str:
    if raw is None:
        return DEFAULT_LANGUAGE
    if not isinstance(raw, str) or not raw.strip():
        raise TemplateError("Param 'language' must be a language code such as 'es' or 'en'.")
    code = raw.strip().lower().replace("_", "-").split("-")[0]
    return code if code in texts else DEFAULT_LANGUAGE


def _render(data: dict[str, Any], language: Any) -> dict[str, Any]:
    texts = data["texts"]
    chosen = _language(language, texts)
    text = texts[chosen]
    agents = [
        {
            "name": card["name"],
            "role": card.get("role") or "",
            "instructions": card.get("instructions") or "",
            "permission_profile": base["permission_profile"],
            **({"icon": base["icon"]} if base.get("icon") else {}),
        }
        for base, card in zip(data["agents"], text["agents"], strict=True)
    ]
    return {
        "id": data["id"],
        "language": chosen,
        "languages": sorted(texts),
        "team": {"name": text["name"], "purpose": text.get("purpose") or ""},
        "agents": agents,
    }


def templates(language: Any = None) -> list[dict[str, Any]]:
    """Every built-in template, in `language` when it is written in it (else English)."""
    return [_render(data, language) for data in _load()]


def template(template_id: Any, language: Any = None) -> dict[str, Any]:
    if not isinstance(template_id, str) or not template_id:
        raise TemplateError("Param 'template_id' must be a non-empty string.")
    for data in _load():
        if data["id"] == template_id:
            return _render(data, language)
    raise LookupError(template_id)


__all__ = ["DEFAULT_LANGUAGE", "TemplateError", "template", "templates"]
