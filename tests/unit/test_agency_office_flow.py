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


def test_a_post_reads_its_teammates_workspace_and_writes_only_its_own(server, turns) -> None:
    """Video could not read the guion Writer left in its folder (POLICY_DENIED), so
    both copied files to the owner's Desktop to pass them along (studio run 3)."""
    from pathlib import Path

    from rinari.cli.agent_runtime import _engine_home_guard
    from rinari.policy.engine import PermissionProfile, PolicyAction, PolicyEngine, SessionScope

    team, writer, _image, video = _studio(server)
    other = _agent(server, "Otro")
    _team(server, "Aparte", [other])
    services = server._services
    home = services.ctx.layout.root.resolve()

    def guard_of(session_id):
        record = services.sessions.show(session_id)
        return Path(record.current_cwd).resolve(), _engine_home_guard(
            services, record, Path(record.current_cwd)
        )

    writer_folder, _ = guard_of(_post(team, writer))
    guion = writer_folder / "guion.md"
    guion.parent.mkdir(parents=True, exist_ok=True)
    guion.write_text("Pico y el copo", encoding="utf-8")
    policy = PolicyEngine()

    def decide(session_id, capability, path):
        folder, guard = guard_of(session_id)
        guard.pop("shared_artifact_sessions", None)  # the tools' part, not the policy's
        scope = SessionScope(
            kind="CHAT", root=folder, cwd=folder, profile=PermissionProfile.FULL_ACCESS, **guard
        )
        return policy.decide(capability, scope, path=str(path)).action

    pv = _post(team, video)
    assert decide(pv, "fs.read", guion) is PolicyAction.ALLOW
    assert decide(pv, "fs.write", guion) is PolicyAction.DENY  # it reads, never writes there
    assert decide(other["session_id"], "fs.read", guion) is PolicyAction.DENY
    assert decide(pv, "fs.read", home / "state.db") is PolicyAction.DENY
    assert decide(pv, "fs.read", home / "workspaces") is PolicyAction.DENY


def test_a_workspace_file_a_post_presents_reaches_the_office_by_uri(server, turns) -> None:
    from pathlib import Path

    team, writer, *_ = _studio(server)
    pw = _post(team, writer)
    folder = Path(server._services.sessions.show(pw).current_cwd)
    folder.mkdir(parents=True, exist_ok=True)
    guion = folder / "pico-y-el-copo.md"
    guion.write_text("# Pico y el copo", encoding="utf-8")
    server._teams._observe(
        {
            "event": "file.presented",
            "payload": {
                "session_id": pw,
                "items": [{"path": str(guion), "name": guion.name, "kind": "markdown"}],
            },
        }
    )
    files = [m for m in _office(server, team) if (m.get("notice") or {}).get("kind") == "files"]
    assert len(files) == 1
    (item,) = files[0]["notice"]["items"]
    assert item["name"] == "pico-y-el-copo.md" and "path" not in item
    assert item["uri"].startswith(f"artifact://{pw}/")
    assert server._services.artifacts.get(item["uri"]) == guion.read_bytes()
    assert guion.exists()  # copied, never moved out of the agent's folder


def test_teammates_learn_where_a_presented_file_and_each_folder_are(server, turns) -> None:
    """Image and Video searched Rinari's home for the guion Writer presented by name
    (studio run 4): the office line carries its URI and team.members each folder."""
    team, writer, image, _video = _studio(server)
    pw = _post(team, writer)
    server._teams._observe(
        {
            "event": "file.presented",
            "payload": {
                "session_id": pw,
                "items": [{"uri": f"artifact://{pw}/media/guion.md", "name": "guion.md"}],
            },
        }
    )
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Image mira"}))
    assert f"guion.md (artifact://{pw}/media/guion.md)" in turns[-1]["message"]
    members = _tools(server, _post(team, image))["team.members"].handler({}, None)
    folders = {m["name"]: m["folder"] for m in members.data["members"]}
    assert folders["Writer"] == server._services.sessions.show(pw).current_cwd


def test_an_errand_names_the_teammates_and_their_jobs_to_pass_their_part(server, turns) -> None:
    """The MetaTrader chief did a whole errand alone, noting the research was missing,
    with a researcher in the team: its prompt said only «do it» (21-oct)."""
    operator = _agent(server, "Operator", role="Operar MetaTrader")
    researcher = _agent(server, "Investigador", role="Buscar noticias del oro")
    team = _team(server, "Mesa", [operator, researcher], chief_agent_id=operator["id"])
    errand = {"title": "Operar si es viable", "to_agent_id": operator["id"]}
    _ok(_call(server, "agency.errand.create", {"team_id": team["id"], "errand": errand}))
    prompt = turns[-1]["message"]
    assert turns[-1]["session_id"] == _post(team, operator)
    assert "- Investigador: Buscar noticias del oro" in prompt
    assert "- Operator" not in prompt
    assert "office.wait" in prompt


def test_a_post_knows_its_periodic_work_is_a_rule(server, turns) -> None:
    """Told to check something every 30 s, a team member said it had no scheduler of
    its own (22-oct): the post's prompt names schedule.propose."""
    from rinari.cli.agent_runtime import _agency_place

    team, writer, *_ = _studio(server)
    services = server._services
    record = services.sessions.show(_post(team, writer))
    text = "\n".join(_agency_place(services, record, services.ctx.agency_repo.get(writer["id"])))
    assert "schedule.propose" in text and "in_this_conversation" in text


# -- one team's life (owner, 22-oct) ----------------------------------------------------------


def _rule_in_post(server, agent, session_id, name="Vigilancia"):
    task = server._services.schedules.create(
        {
            "name": name,
            "kind": "agent",
            "schedule": {"kind": "interval", "seconds": 30},
            "prompt": "Revisa",
            "agent_id": agent["id"],
            "target": "this_conversation",
            "session_id": session_id,
        }
    )
    return task["id"]


def test_archiving_a_team_stops_its_turns_waits_and_rules_and_unarchiving_resumes_them(
    server, turns, monkeypatch
) -> None:
    """An archived team kept its agents working: turns, waits and a rule in a post."""
    team, writer, image, _video = _studio(server)
    pw, pi = _post(team, writer), _post(team, image)
    rule = _rule_in_post(server, writer, pw)
    _ok(
        _call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer @Image hagan"})
    )
    assert _tools(server, pi)["office.wait"].handler({"for": "Writer"}, None).ok
    cancelled = []
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: cancelled.append(sid))
    archived = _ok(_call(server, "agency.team.archive", {"team_id": team["id"]}))
    assert archived["paused_rules"] == 1
    assert pw in cancelled and pi in cancelled
    assert pi not in server._teams._waits
    repo = server._services.ctx.schedule_repo
    assert repo.get(rule)["enabled"] is False and repo.get(rule)["paused_by"] == "team_archived"
    # Nothing of it starts any more, not even a message that mentions one of its posts.
    before = len(turns)
    server._teams.post_message(
        team, "@Image ya", author_agent_id=writer["id"], chain_id=None, hop=1
    )
    server._teams.tick()
    assert len(turns) == before
    listed = _ok(_call(server, "agency.team.list", {"archived": True}))["teams"]
    assert [t["id"] for t in listed] == [team["id"]]
    back = _ok(_call(server, "agency.team.unarchive", {"team_id": team["id"]}))
    assert back["resumed_rules"] == 1 and repo.get(rule)["enabled"] is True


def test_a_team_is_stopped_and_resumed_alone(server, turns, monkeypatch) -> None:
    team, writer, image, _video = _studio(server)
    other, *_ = _studio(server)
    _rule_in_post(server, writer, _post(team, writer))
    errand = {"title": "Algo", "to_agent_id": image["id"]}
    made = _ok(_call(server, "agency.errand.create", {"team_id": team["id"], "errand": errand}))
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    stopped = _ok(_call(server, "agency.team.stop", {"team_id": team["id"]}))
    assert stopped["paused_rules"] == 1 and stopped["blocked_errands"] == 1
    teams = {t["id"]: t for t in _ok(_call(server, "agency.team.list", {}))["teams"]}
    assert teams[team["id"]]["stopped"] is True and teams[other["id"]]["stopped"] is False
    # The other team keeps working.
    before = len(turns)
    _ok(_call(server, "agency.office.post", {"team_id": other["id"], "text": "@Writer hola"}))
    assert len(turns) == before + 1
    before = len(turns)
    server._teams.post_message(
        team, "@Image ya", author_agent_id=writer["id"], chain_id=None, hop=1
    )
    server._teams.tick()
    assert len(turns) == before
    resumed = _ok(_call(server, "agency.team.resume", {"team_id": team["id"]}))
    assert resumed["resumed_rules"] == 1 and resumed["restored_errands"] == 1
    assert server._services.ctx.team_repo.errand(made["errand"]["id"])["status"] in (
        "pending",
        "in_progress",
    )


def test_the_owner_writing_in_a_stopped_teams_office_resumes_it(server, turns, monkeypatch) -> None:
    team, *_ = _studio(server)
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    _ok(_call(server, "agency.team.stop", {"team_id": team["id"]}))
    before = len(turns)
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer sigue"}))
    assert len(turns) == before + 1


def test_a_team_or_agent_is_deleted_only_archived_and_takes_what_is_only_its(
    server, turns, monkeypatch
) -> None:
    team, writer, image, _video = _studio(server)
    pw = _post(team, writer)
    rule = _rule_in_post(server, writer, pw)
    refused = _call(server, "agency.team.delete", {"team_id": team["id"]})
    assert refused["ok"] is False
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    _ok(_call(server, "agency.team.archive", {"team_id": team["id"]}))
    gone = _ok(_call(server, "agency.team.delete", {"team_id": team["id"]}))
    assert gone["deleted_rules"] == 1 and gone["deleted_sessions"] >= 3
    assert server._services.ctx.team_repo.get(team["id"]) is None
    assert server._services.ctx.schedule_repo.get(rule) is None
    # An agent: archived first, then its conversations and rules go with it.
    refused = _call(server, "agency.agent.delete", {"agent_id": image["id"]})
    assert refused["ok"] is False
    _ok(_call(server, "agency.agent.archive", {"agent_id": image["id"]}))
    direct = image["session_id"]
    deleted = _ok(_call(server, "agency.agent.delete", {"agent_id": image["id"]}))
    assert deleted["deleted"] == image["id"]
    assert server._services.ctx.agency_repo.get(image["id"]) is None
    assert server._services.ctx.session_repo.get(direct) is None


def test_saving_the_agency_settings_keeps_a_stopped_team_stopped(
    server, turns, monkeypatch
) -> None:
    team, *_ = _studio(server)
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    _ok(_call(server, "agency.team.stop", {"team_id": team["id"]}))
    _ok(_call(server, "agency.settings.set", {"errand_mode": "turn"}))
    teams = {t["id"]: t for t in _ok(_call(server, "agency.team.list", {}))["teams"]}
    assert teams[team["id"]]["stopped"] is True


# -- phase 1 of the Agency replan (22-oct) -----------------------------------------------------


def test_an_owner_message_naming_nobody_goes_to_the_chief(server, turns) -> None:
    team, writer, image, _video = _studio(server)
    assert team["chief_agent_id"] == writer["id"]
    before = len(turns)
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "Equipo, empiecen"}))
    woken = turns[before:]
    assert [t["session_id"] for t in woken] == [_post(team, writer)]
    assert "as its chief, it comes to you" in woken[0]["message"]
    # Naming someone still goes straight to them, not to the chief.
    before = len(turns)
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Image hola"}))
    assert [t["session_id"] for t in turns[before:]] == [_post(team, image)]


def test_queued_turns_survive_a_restart_and_a_stop(
    server, turns, services, tmp_path, monkeypatch
) -> None:
    """Queued mentions lived only in memory: a restart or a stop lost them (22-oct)."""
    from rinari.engine_protocol.server import EngineServer

    team, writer, image, _video = _studio(server)
    pi = _post(team, image)
    _ok(
        _call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer @Image hagan"})
    )
    # Image is busy with another turn: Writer's mention of it waits in the queue.
    server._teams.post_message(
        team, "@Image ¿y la imagen?", author_agent_id=writer["id"], chain_id=None, hop=1
    )
    assert server._teams._mentions.get(pi)
    queued = list(server._teams._mentions[pi])
    # A stop keeps the queue; resuming carries on with it.
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    _ok(_call(server, "agency.team.stop", {"team_id": team["id"]}))
    assert server._teams._mentions.get(pi) == queued
    # A new Engine on the same home finds it again.
    again = EngineServer(services, user_home=tmp_path / "home2")
    try:
        again._teams._recover()
        assert [m["message"]["text"] for m in again._teams._mentions.get(pi, [])] == [
            m["message"]["text"] for m in queued
        ]
    finally:
        again.close()
