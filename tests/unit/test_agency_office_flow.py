"""Rinari Agency: what the studio run of 2026-10-21 found in the office.

Video was handed an errand that needed Image's character first and could not
wait for it; Image's delivery reached the office cut at 600 characters; what
the posts presented with file.present never showed in the office.
"""

# The fixtures are imported from test_agency_teams, so pytest sees them as arguments.
# ruff: noqa: F811

from __future__ import annotations

from tests.unit.test_agency_teams import (  # noqa: F401  (fixtures)
    _agent,
    _call,
    _finish,
    _no_background_board,
    _ok,
    _post,
    _team,
    _tools,
    server,
    services,
    turns,
)


def _studio(server):
    writer, image, video = (_agent(server, name) for name in ("Writer", "Image", "Video"))
    team = _team(server, "Estudio", [writer, image, video])
    return team, writer, image, video


def _errands_from_writer(server, team, writer, turns):
    """The owner asks Writer; Writer leaves one errand to Image and one to Video."""
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer un cuento"}))
    tools = _tools(server, _post(team, writer))
    to_image = tools["errand.create"].handler({"title": "El personaje", "to": "Image"}, None)
    to_video = tools["errand.create"].handler({"title": "El video", "to": "Video"}, None)
    assert to_image.ok and to_video.ok
    _finish(server, _post(team, writer), "")
    return to_image.data["errand_id"], to_video.data["errand_id"]


def _office(server, team):
    return _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]


def test_an_agent_working_an_errand_waits_for_a_teammate_and_then_finishes_it(
    server, turns
) -> None:
    team, writer, image, video = _studio(server)
    image_errand, video_errand = _errands_from_writer(server, team, writer, turns)
    pv, pi = _post(team, video), _post(team, image)
    assert {turn["session_id"] for turn in turns[-2:]} == {pi, pv}
    repo = server._services.ctx.team_repo

    waited = _tools(server, pv)["office.wait"].handler({"for": "Image"}, None)
    assert waited.ok, waited.error
    _finish(server, pv, "")
    # The errand stays Video's, in progress: nothing was delivered.
    assert repo.errand(video_errand)["status"] == "in_progress"
    assert not [
        m for m in _office(server, team) if (m.get("notice") or {}).get("kind") == "delivered"
    ]

    before = len(turns)
    _finish(server, pi, "El personaje: un zorro ártico. artifact://ses/images/zorro.png")
    assert repo.errand(image_errand)["status"] == "delivered"
    woken = [turn for turn in turns[before:] if turn["session_id"] == pv]
    # Woken once, to go on with its errand (not as a new mention).
    assert len(woken) == 1
    assert woken[0]["origin"]["kind"] == "agency_errand"
    assert woken[0]["origin"]["errand_id"] == video_errand
    assert "zorro ártico" in woken[0]["message"]

    _finish(server, pv, "Video listo: artifact://ses/videos/zorro.mp4")
    errand = repo.errand(video_errand)
    assert errand["status"] == "delivered"
    assert "zorro.mp4" in errand["result"]["summary"]


def test_a_wait_inside_an_errand_that_expires_blocks_it_with_why(server, turns) -> None:
    team, writer, _image, video = _studio(server)
    _, video_errand = _errands_from_writer(server, team, writer, turns)
    pv = _post(team, video)
    assert _tools(server, pv)["office.wait"].handler({"for": "Image"}, None).ok
    _finish(server, pv, "")
    teams = server._teams
    teams._waits[pv]["since"] -= 31 * 60
    teams._expire_waits()
    errand = server._services.ctx.team_repo.errand(video_errand)
    assert errand["status"] == "blocked" and errand["return_reason"] == "wait_expired"


def test_a_cancelled_errand_is_not_resumed_when_the_teammate_posts(server, turns) -> None:
    team, writer, image, video = _studio(server)
    _, video_errand = _errands_from_writer(server, team, writer, turns)
    pv, pi = _post(team, video), _post(team, image)
    assert _tools(server, pv)["office.wait"].handler({"for": "Image"}, None).ok
    _finish(server, pv, "")
    _ok(_call(server, "agency.errand.cancel", {"errand_id": video_errand}))
    before = len(turns)
    _finish(server, pi, "El personaje")
    assert pv not in [turn["session_id"] for turn in turns[before:]]


def test_a_delivery_reaches_the_office_whole(server, turns) -> None:
    team, writer, image, _video = _studio(server)
    image_errand, _ = _errands_from_writer(server, team, writer, turns)
    long = "Guion completo. " + "El zorro corre por la nieve. " * 60
    assert len(long) > 1500
    _finish(server, _post(team, image), long)
    delivered = next(
        m
        for m in _office(server, team)
        if m.get("errand_id") == image_errand and (m.get("notice") or {}).get("kind") == "delivered"
    )
    assert delivered["text"].endswith(long.strip())


def test_what_a_post_presents_shows_in_the_office_without_its_private_path(server, turns) -> None:
    team, writer, image, _video = _studio(server)
    _errands_from_writer(server, team, writer, turns)
    pi = _post(team, image)
    server._teams._observe(
        {
            "event": "file.presented",
            "payload": {
                "session_id": pi,
                "present_id": "call-1",
                "items": [
                    {
                        "path": "C:/Users/owner/.rinari/artifacts/ses/images/zorro.png",
                        "uri": "artifact://ses/images/zorro.png",
                        "name": "zorro.png",
                        "kind": "image",
                        "mime": "image/png",
                        "caption": "El personaje",
                    },
                    {"path": "C:/tmp/sin-uri.txt", "name": "sin-uri.txt", "kind": "text"},
                ],
            },
        }
    )
    files = [m for m in _office(server, team) if (m.get("notice") or {}).get("kind") == "files"]
    assert len(files) == 1
    card = files[0]
    assert card["system"] is True and card["agent_id"] == image["id"]
    assert card["notice"]["items"] == [
        {
            "uri": "artifact://ses/images/zorro.png",
            "name": "zorro.png",
            "kind": "image",
            "mime": "image/png",
            "caption": "El personaje",
        }
    ]
    # A notice: it wakes nobody.
    before = len(turns)
    server._teams.tick()
    assert len(turns) == before


def test_a_direct_chat_presenting_a_file_leaves_nothing_in_any_office(server, turns) -> None:
    team, *_ = _studio(server)
    server._teams._observe(
        {
            "event": "file.presented",
            "payload": {
                "session_id": "ses_not_a_post",
                "items": [{"uri": "artifact://x/y/z.png", "name": "z.png"}],
            },
        }
    )
    assert not _office(server, team)


def test_an_errand_the_owner_left_is_released_by_the_teammates_post_in_another_chain(
    server, turns
) -> None:
    team, _writer, image, video = _studio(server)
    pv, pi = _post(team, video), _post(team, image)
    errand = {"title": "El video", "to_agent_id": video["id"]}
    made = _ok(_call(server, "agency.errand.create", {"team_id": team["id"], "errand": errand}))
    errand_id = made["errand"]["id"]
    assert turns[-1]["session_id"] == pv
    assert _tools(server, pv)["office.wait"].handler({"for": "Image"}, None).ok
    _finish(server, pv, "")
    # The owner asks Image in the office: its own chain, not the errand's.
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Image el personaje"}))
    before = len(turns)
    _finish(server, pi, "Aquí está el zorro.")
    woken = [turn for turn in turns[before:] if turn["session_id"] == pv]
    assert len(woken) == 1 and woken[0]["origin"]["errand_id"] == errand_id
