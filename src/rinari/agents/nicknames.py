"""Nicknames for subagents: stars and constellations (plan 0.4.0 #73).

The id (`agt_001`) stays the key for the tools; the nickname is only for
showing: «Vega · explorer» in the Agents panel, the timeline and notices.
Each conversation shuffles the list the same way every time and never repeats
a nickname inside it; past the end of the list it starts over with «II».
The names read the same in Spanish and English, and none is a real person.
"""

from __future__ import annotations

import random

STAR_NAMES: tuple[str, ...] = (
    "Vega",
    "Altair",
    "Lyra",
    "Rigel",
    "Deneb",
    "Antares",
    "Capella",
    "Polaris",
    "Mira",
    "Electra",
    "Maia",
    "Alcyone",
    "Merope",
    "Taygeta",
    "Celaeno",
    "Asterope",
    "Pleione",
    "Bellatrix",
    "Mintaka",
    "Alnitak",
    "Alnilam",
    "Saiph",
    "Betelgeuse",
    "Procyon",
    "Pollux",
    "Castor",
    "Regulus",
    "Denebola",
    "Algol",
    "Mirach",
    "Alpheratz",
    "Hamal",
    "Menkar",
    "Achernar",
    "Canopus",
    "Fomalhaut",
    "Shaula",
    "Nunki",
    "Sabik",
    "Alhena",
    "Wezen",
    "Adhara",
    "Avior",
    "Mimosa",
    "Hadar",
    "Acrux",
    "Gacrux",
    "Alioth",
    "Mizar",
    "Alkaid",
    "Dubhe",
    "Merak",
    "Phecda",
    "Thuban",
    "Kochab",
    "Eltanin",
    "Sadr",
    "Albireo",
    "Enif",
    "Markab",
    "Scheat",
    "Ankaa",
    "Diphda",
    "Izar",
    "Nashira",
    "Zosma",
    "Cassiopeia",
    "Andromeda",
    "Perseus",
    "Draco",
    "Cygnus",
    "Aquila",
    "Carina",
    "Vela",
)

_SUFFIXES = ("", " II", " III", " IV", " V")


def next_nickname(session_id: str, used: set[str]) -> str:
    """The first nickname of this conversation's order that is still free."""
    order = list(STAR_NAMES)
    random.Random(session_id or "rinari").shuffle(order)
    for suffix in _SUFFIXES:
        for name in order:
            candidate = f"{name}{suffix}"
            if candidate not in used:
                return candidate
    return f"{order[0]} {len(used) + 1}"
