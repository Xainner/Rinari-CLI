"""The skills manager: near-duplicates are stopped, merges wait for the owner
and undo cleanly, the owner's own duplicates are listed, and /lesson and
/merge-skills are the owner asking.

The texts mirror a real library: one image job done by an older skill (an
image lab reached over HTTP) and a newer one (the same lab over MCP), which
word overlap misses and TF-IDF catches."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from rinari.application.services import build_services
from rinari.commands import CommandError, expand_command
from rinari.engine_protocol.server import EngineServer
from rinari.skills.manifest import SkillError
from rinari.skills.similarity import PROPOSE_THRESHOLD, SimilarityIndex, SkillText
from rinari.skills.tools import SkillToolHost, skill_tools


def skill_md(name: str, description: str, body: str, version: str = "1.0.0") -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\nrisk: low\n"
        "required_tools:\n  - fs.write\n---\n\n# Procedure\n" + body
    )


LAB_MCP = skill_md(
    "lab-image",
    "Generate and edit images with Qwen-Image in the Visual Lab over MCP: nine ratios, "
    "1mp or 2k, int8 or bf16, RGBA, ordered references and a verified download.",
    "1. Pick the ratio (1:1, 16:9, 9:16) and size (1mp, 2k).\n"
    "2. Choose int8 or bf16 and RGBA when transparency is needed.\n"
    "3. Call the Visual Lab MCP tool with the ordered references.\n"
    "4. Download the PNG or WEBP and verify it opens.\n",
)
LAB_HTTP = skill_md(
    "visual-lab-generate",
    "Generate images in the Visual Lab with ComfyUI over HTTP, int8 or bf16, 2k output, "
    "webp or jpeg, with a prompt enhancer and a verified download.",
    "1. Enhance the prompt with the Visual Lab enhancer.\n"
    "2. Send the ComfyUI workflow with int8 or bf16 at 2k.\n"
    "3. Poll until ready and download the WEBP or JPEG.\n"
    "4. Verify the image opens before showing it.\n",
)
LOGS = skill_md(
    "rotate-logs",
    "Rotate and compress the nightly log archives on the build server.",
    "1. Compress yesterday's logs with gzip.\n2. Prune archives older than 30 days.\n",
)


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return build_services(app_ctx, user_home=home)


def _install(services, md: str, name: str) -> None:
    result = services.skills.propose(name, md, owner_asked=True, session_id="ses_1")
    assert result["status"] == "active", result


# -- the score ---------------------------------------------------------------------


def test_tfidf_puts_the_superseded_pair_above_an_unrelated_skill():

    def text(name, md):
        head, _, body = md.partition("---\n\n")
        description = next(
            line.split(": ", 1)[1] for line in head.splitlines() if line.startswith("description")
        )
        return SkillText(name, description, (), body)

    index = SimilarityIndex(
        [text("lab-image", LAB_MCP), text("rotate-logs", LOGS)]
        + [SkillText(f"filler-{i}", f"filler job {i}", (), f"do filler task {i}") for i in range(8)]
    )
    found = index.similar_to(text("visual-lab-generate", LAB_HTTP))
    assert [s.name for s in found] == ["lab-image"]
    assert found[0].score >= PROPOSE_THRESHOLD
    # What they share is the lab and its formats, whatever the order of ties.
    assert set(found[0].shared) & {"int8", "bf16", "2k", "visual", "lab", "webp"}


# -- the gate ------------------------------------------------------------------------


def test_a_near_duplicate_is_refused_and_nothing_is_written(services):
    _install(services, LAB_MCP, "lab-image")
    with pytest.raises(SkillError) as exc:
        services.skills.propose("visual-lab-generate", LAB_HTTP, owner_asked=True)
    assert exc.value.code == "SIMILAR_EXISTS"
    assert [s["name"] for s in exc.value.details["similar"]] == ["lab-image"]
    assert "update_of" in exc.value.message and "distinct_from" in exc.value.message
    root = services.skills.user_skills_dir()
    assert not (root / "visual-lab-generate").exists()
    assert not (root / ".pending" / "visual-lab-generate").exists()


def test_validate_draft_shows_the_similar_skills_before_proposing(services):
    _install(services, LAB_MCP, "lab-image")
    report = services.skills.validate_draft("visual-lab-generate", LAB_HTTP)
    assert [s["name"] for s in report["similar"]] == ["lab-image"]


def test_an_unrelated_skill_passes(services):
    _install(services, LAB_MCP, "lab-image")
    result = services.skills.propose("rotate-logs", LOGS, owner_asked=True)
    assert result["status"] == "active" and result["similar_to"] == []


@pytest.mark.parametrize("reason", [None, "", "different"])
def test_a_missing_or_empty_reason_is_not_a_reason(services, reason):
    _install(services, LAB_MCP, "lab-image")
    with pytest.raises(SkillError) as exc:
        services.skills.propose(
            "visual-lab-generate",
            LAB_HTTP,
            owner_asked=True,
            distinct_from={"lab-image": reason} if reason is not None else None,
        )
    assert exc.value.code == "SIMILAR_EXISTS"


def test_a_justified_near_duplicate_waits_for_the_owner_even_when_asked(services):
    _install(services, LAB_MCP, "lab-image")
    why = "this one drives the ComfyUI workflow on the local GPU box, not the MCP lab"
    result = services.skills.propose(
        "visual-lab-generate", LAB_HTTP, owner_asked=True, distinct_from={"lab-image": why}
    )
    assert result["status"] == "pending"
    assert result["similar_to"][0]["name"] == "lab-image"
    assert result["similar_to"][0]["reason"] == why
    pending = {p["name"]: p for p in services.skills.learning.pending()}
    assert pending["visual-lab-generate"]["similar_to"][0]["reason"] == why


def test_improving_the_existing_skill_is_never_gated(services):
    _install(services, LAB_MCP, "lab-image")
    better = LAB_MCP.replace("version: 1.0.0", "version: 1.1.0").replace(
        "4. Download", "4. Keep the seed.\n5. Download"
    )
    result = services.skills.propose("lab-image", better, update_of="lab-image", owner_asked=True)
    assert result["status"] == "active" and result["similar_to"] == []


# -- merges --------------------------------------------------------------------------


def _two_old_skills(services) -> None:
    services.skills.similar_skills = lambda *_a, **_k: []  # install both as they exist today
    _install(services, LAB_MCP, "lab-image")
    _install(services, LAB_HTTP, "visual-lab-generate")
    del services.skills.similar_skills


MERGED = skill_md(
    "image-lab",
    "Generate images in the Visual Lab over MCP (preferred) or ComfyUI over HTTP.",
    "1. Prefer the MCP tool; fall back to the ComfyUI workflow.\n2. Verify the download.\n",
)


def test_a_merge_waits_then_turns_the_merged_skills_off_and_undo_turns_them_on(services):
    _two_old_skills(services)
    result = services.skills.propose(
        "image-lab", MERGED, owner_asked=True, replaces=["lab-image", "visual-lab-generate"]
    )
    assert result["status"] == "pending"
    assert result["replaces"] == ["lab-image", "visual-lab-generate"]

    approved = services.skills.learning.approve("image-lab")
    assert approved["turned_off"] == ["lab-image", "visual-lab-generate"]
    found = services.skills.discover()
    assert "image-lab" in found
    assert "lab-image" not in found and "visual-lab-generate" not in found
    # Off, never deleted.
    root = services.skills.user_skills_dir()
    assert (root / "lab-image").is_dir() and (root / "visual-lab-generate").is_dir()

    undone = services.skills.learning.revert("image-lab")
    assert undone["removed"] is True
    assert undone["turned_on"] == ["lab-image", "visual-lab-generate"]
    found = services.skills.discover()
    assert "image-lab" not in found
    assert "lab-image" in found and "visual-lab-generate" in found


def test_a_merge_can_keep_one_of_the_names(services):
    _two_old_skills(services)
    kept = MERGED.replace("name: image-lab", "name: lab-image").replace(
        "version: 1.0.0", "version: 2.0.0"
    )
    result = services.skills.propose(
        "lab-image", kept, owner_asked=True, replaces=["lab-image", "visual-lab-generate"]
    )
    assert result["status"] == "pending" and result["update"] is True
    approved = services.skills.learning.approve("lab-image")
    assert approved["turned_off"] == ["visual-lab-generate"]
    assert services.skills.discover()["lab-image"].version == "2.0.0"
    undone = services.skills.learning.revert("lab-image")
    assert undone["restored"] == "1.0.0" and undone["turned_on"] == ["visual-lab-generate"]


@pytest.mark.parametrize(
    ("replaces", "code"),
    [(["lab-image", "code-review"], "SKILL_NOT_FOUND"), (["nope", "lab-image"], "SKILL_NOT_FOUND")],
)
def test_only_the_owners_skills_can_be_merged(services, replaces, code):
    _two_old_skills(services)
    with pytest.raises(SkillError) as exc:
        services.skills.propose("image-lab", MERGED, owner_asked=True, replaces=replaces)
    assert exc.value.code == code


def test_a_merge_of_one_skill_into_itself_is_not_a_merge(services):
    _two_old_skills(services)
    same = LAB_MCP.replace("version: 1.0.0", "version: 1.1.0")
    with pytest.raises(SkillError) as exc:
        services.skills.propose("lab-image", same, owner_asked=True, replaces=["lab-image"])
    assert exc.value.code == "SKILL_INVALID"


# -- the owner's own duplicates -------------------------------------------------------


def test_duplicate_pairs_list_only_the_owners_skills_and_respect_dismissals(services):
    _two_old_skills(services)
    services.skills.similar_skills = lambda *_a, **_k: []
    _install(services, LOGS, "rotate-logs")
    del services.skills.similar_skills
    pairs = services.skills.duplicate_pairs()
    assert [p["skills"] for p in pairs] == [["lab-image", "visual-lab-generate"]]
    assert pairs[0]["shared"]
    assert all("code-review" not in p["skills"] for p in pairs)  # packaged never listed
    services.skills.dismiss_duplicate("visual-lab-generate", "lab-image")
    assert services.skills.duplicate_pairs() == []


# -- commands and authorization ----------------------------------------------------------


def test_lesson_pins_skill_author_and_never_creates_a_skill():
    expanded = expand_command("lesson", "the seed")
    assert expanded.skill == "skill-author"
    assert "## Lecciones" in expanded.message and "update_of" in expanded.message
    assert "memory.propose" in expanded.message and "Focus: the seed" in expanded.message


def test_merge_skills_needs_two_installed_names(services):
    _two_old_skills(services)
    expanded = expand_command("merge-skills", "lab-image visual-lab-generate", services.skills)
    assert '"lab-image", "visual-lab-generate"' in expanded.message
    with pytest.raises(CommandError) as one:
        expand_command("merge-skills", "lab-image", services.skills)
    assert one.value.code == "TEXT_REQUIRED"
    with pytest.raises(CommandError) as missing:
        expand_command("merge-skills", "lab-image ghost", services.skills)
    assert missing.value.code == "SKILL_NOT_FOUND"


@pytest.mark.parametrize("command", ["lesson", "merge-skills"])
def test_lesson_and_merge_are_the_owner_asking(services, command):
    tools = {t.name: t for t in skill_tools(SkillToolHost(service=services.skills))}
    result = tools["skills.propose"].handler(
        {"name": "rotate-logs", "skill_md": LOGS},
        SimpleNamespace(session_id="ses_1", turn_command=command),
    )
    assert result.data["status"] == "active"
    assert result.data["authorized_by"] == {"source": command}


def test_the_model_is_told_why_and_can_justify_through_the_tool(services):
    _install(services, LAB_MCP, "lab-image")
    tools = {t.name: t for t in skill_tools(SkillToolHost(service=services.skills))}
    turn = SimpleNamespace(session_id="ses_1", turn_command="learn")
    refused = tools["skills.propose"].handler(
        {"name": "visual-lab-generate", "skill_md": LAB_HTTP}, turn
    )
    assert refused.ok is False and refused.error.details["skill_code"] == "SIMILAR_EXISTS"
    justified = tools["skills.propose"].handler(
        {
            "name": "visual-lab-generate",
            "skill_md": LAB_HTTP,
            "distinct_from": {"lab-image": "drives the local ComfyUI box, not the MCP lab"},
        },
        turn,
    )
    assert justified.data["status"] == "pending"
    assert justified.data["pending_reason"] == "similar_to_installed_skill"


def test_a_proposal_is_announced_on_the_turn_for_the_chat_card(services):
    seen: list[tuple[str, dict]] = []
    tools = {t.name: t for t in skill_tools(SkillToolHost(service=services.skills))}
    tools["skills.propose"].handler(
        {"name": "rotate-logs", "skill_md": LOGS},
        SimpleNamespace(
            session_id="ses_1",
            turn_command="",
            activity_sink=lambda name, payload: seen.append((name, payload)),
        ),
    )
    assert [name for name, _ in seen] == ["skill.proposed"]
    payload = seen[0][1]
    assert payload["name"] == "rotate-logs" and payload["status"] == "pending"
    assert payload["description"].startswith("Rotate and compress")
    assert payload["pending_reason"] == "needs_owner_approval"


# -- protocol --------------------------------------------------------------------------


def test_duplicates_over_the_protocol(services, tmp_path):
    _two_old_skills(services)
    server = EngineServer(services, user_home=tmp_path / "home")
    try:

        def call(i, method, params):
            return json.loads(
                json.dumps(
                    server.handle_line(
                        json.dumps({"id": f"r{i}", "method": method, "params": params})
                    )
                )
            )

        listed = call(1, "skill.duplicates.list", {})
        assert listed["result"]["pairs"][0]["skills"] == ["lab-image", "visual-lab-generate"]
        dismissed = call(
            2, "skill.duplicates.dismiss", {"skills": ["lab-image", "visual-lab-generate"]}
        )
        assert dismissed["result"]["pairs"] == []
        assert call(3, "skill.duplicates.dismiss", {"skills": ["only-one"]})["ok"] is False
    finally:
        server.close()
