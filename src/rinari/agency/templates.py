"""Built-in team templates («Usar una plantilla», vision 10.4).

A template is data shipped with the Engine (`assets/agency_templates/*.json`):
a team's name and purpose and its agents' names, jobs, instructions and
permission mode, in every language it is written in; and (v2) its manual and
its flow. Never connectors, accounts, budgets nor models: those are the
owner's to give once the team exists. Each template is also rendered as a
proposal (`plan`, the format of «Arma tu equipo con Rinari»), which the owner
reviews and creates with `agency.team.apply`; `agency.team.from_template`
still turns one into agents and a team directly.
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
        steps = data.get("flow") or []
        texts_steps = (text.get("flow") or {}).get("steps") or []
        if len(texts_steps) != len(steps):
            raise ValueError(f"{source}: the {language} flow does not match its steps")
    for step in data.get("flow") or []:
        who = step.get("agents")
        if not who or not all(isinstance(i, int) and 0 <= i < len(agents) for i in who):
            raise ValueError(f"{source}: a flow step names an agent that is not there")


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
        "plan": _plan(data, text, agents),
    }


def _plan(data: dict[str, Any], text: dict[str, Any], agents: list[dict[str, Any]]) -> dict:
    """The template as a proposal: what the owner reviews before creating it. The
    first agent leads (each template's first agent is written as its lead)."""
    keys = [f"agent-{index + 1}" for index in range(len(agents))]
    flow_text = text.get("flow") or {}
    steps = [
        {
            "name": words.get("name") or str(index + 1),
            "agents": [keys[i] for i in base["agents"]],
            "instructions": words.get("instructions") or "",
            "approval": base.get("approval") or "auto",
        }
        for index, (base, words) in enumerate(
            zip(data.get("flow") or [], flow_text.get("steps") or [], strict=True)
        )
    ]
    return {
        "team": {
            "name": text["name"],
            "purpose": text.get("purpose") or "",
            "manual": text.get("manual") or "",
            "chief": keys[0],
            "project_folder": None,
            "work_folders": [],
        },
        "agents": [
            {
                "key": key,
                "name": agent["name"],
                "role": agent["role"],
                "instructions": agent["instructions"],
                "permission_profile": agent["permission_profile"],
                "uses": [],
            }
            for key, agent in zip(keys, agents, strict=True)
        ],
        "flow": (
            {
                "name": flow_text.get("name") or text["name"],
                "description": flow_text.get("description") or "",
                "steps": steps,
            }
            if steps
            else None
        ),
        "routines": [],
        "assumptions": [],
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
