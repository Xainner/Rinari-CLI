"""Tools of an agent in the Agency: in its post (A2) and in its direct chat (D-30).

A post (its conversation in a team) has the office, the board and its team:
`office.say`, `office.wait`, `errand.create`, `errand.review`, `board.list`
and `team.members`. An agent's direct chat has the Agency's teams:
`teams.list`, `errand.create` (to a team) and `errand.review`.

What they do is counted by the Engine from the turn that runs, never from
the model's arguments (D-2): the chain and the hop come from the work the
conversation is doing. They only exist in those conversations, so they are
always loaded there (a small model must see them, D-29); the list is fixed at
the first request, so the prompt cache holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_TEXT = 8000


@dataclass
class TeamToolHost:
    services: Any
    #: The conversation: a post (its team and its agent come from it) or an agent's direct chat.
    session_id: str


def _defs():
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )

    return ClassifiedAction, ToolDefinition, ToolErrorCode, ToolErrorInfo, ToolResult


def _fail(code, message: str):
    _, _, _, ToolErrorInfo, ToolResult = _defs()
    return ToolResult(ok=False, error=ToolErrorInfo(code, message), origin="agency")


def _ok(data: dict[str, Any]):
    *_, ToolResult = _defs()
    return ToolResult(ok=True, data=data, origin="agency")


def _work_of(board, session_id: str) -> dict[str, Any]:
    """What the conversation's running turn is for (a mention, an errand, a delivery)."""
    with board._lock:
        return dict(board._live.get(session_id) or {})


def _team_named(repo, profile: str, name: Any) -> dict[str, Any] | None:
    wanted = str(name or "").strip().lstrip("@").lower()
    if not wanted:
        return None
    return next((item for item in repo.list(profile) if item["name"].lower() == wanted), None)


def _teams_directory(host: TeamToolHost, profile: str, but: str | None = None) -> list[dict]:
    """The profile's teams as an agent may see them: never their memory nor accounts."""
    repo = host.services.ctx.team_repo
    agents = host.services.ctx.agency_repo
    out = []
    for team in repo.list(profile):
        if team["id"] == but:
            continue
        chief = agents.get(team["chief_agent_id"]) if team.get("chief_agent_id") else None
        members = [agents.get(post["agent_id"]) for post in repo.posts(team["id"])]
        out.append(
            {
                "name": team["name"],
                "purpose": team.get("purpose") or "",
                "chief": chief["name"] if chief else None,
                "members": [agent["name"] for agent in members if agent],
            }
        )
    return out


def _errand_review(host: TeamToolHost, agent_id: str, arguments):
    from rinari.agency import AgencyError

    _, _, ToolErrorCode, _, _ = _defs()
    args = arguments or {}
    errand = host.services.ctx.team_repo.errand(str(args.get("errand_id") or ""))
    if errand is None or errand.get("from_agent_id") != agent_id:
        return _fail(ToolErrorCode.NOT_FOUND, "You can only review an errand you asked for.")
    accepted = args.get("accepted")
    if not isinstance(accepted, bool):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "accepted must be true or false")
    try:
        reviewed = host.services.teams.review(errand["id"], accepted, args.get("reason"))
    except AgencyError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    board = getattr(host.services.teams, "board", None)
    if board is not None:
        board.tick()
    return _ok({"status": reviewed["status"]})


def _leave_errand(host, board, target_team, args, *, from_agent_id, from_team_id, work):
    """Leave an errand on a board, one hop further in the chain of the running work (D-2)."""
    from rinari.agency import AgencyError

    _, _, ToolErrorCode, _, _ = _defs()
    hop = int(work.get("hop", -1)) + 1
    if hop >= board.MAX_HOPS:
        board._stopped(target_team, work.get("chain_id") or "", "too_many_hops")
        return _fail(
            ToolErrorCode.PERMISSION_DENIED,
            "This chain of work is already 3 hops long; the owner was told.",
        )
    try:
        errand = host.services.teams.create_errand(
            target_team["id"],
            args,
            from_agent_id=from_agent_id,
            from_team_id=from_team_id,
            chain_id=work.get("chain_id"),
            hop=hop,
        )
    except AgencyError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    board.post_message(
        target_team,
        f"Recado «{errand['title']}»",
        author_agent_id=from_agent_id,
        chain_id=errand["chain_id"],
        hop=errand["hop"],
        errand_id=errand["id"],
        wake=False,
    )
    board.tick()
    return _ok({"errand_id": errand["id"], "status": errand["status"], "team": target_team["name"]})


_ERRAND_PROPS = {
    "title": {"type": "string", "maxLength": 200},
    "body": {"type": "string", "maxLength": 8000},
    "done_when": {"type": "array", "items": {"type": "string"}},
}
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "errand_id": {"type": "string"},
        "accepted": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 300},
    },
    "required": ["errand_id", "accepted"],
    "additionalProperties": False,
}
_REVIEW_DESCRIPTION = (
    "Only for an errand you asked for, once it is delivered: accept it, or send it back "
    "with the reason."
)


def _kinds():
    ClassifiedAction, *_ = _defs()
    write = {
        "capabilities": ("state.write",),
        "classify": lambda _i: ClassifiedAction("state.write"),
        "always_loaded": True,
        "side_effects": "local-reversible",
    }
    read = {
        "capabilities": ("state.read",),
        "classify": lambda _i: ClassifiedAction("state.read"),
        "always_loaded": True,
        "side_effects": "none",
    }
    return write, read


def team_tools(host: TeamToolHost):
    """A post's tools: its office, its board and its team."""
    _, ToolDefinition, ToolErrorCode, _, _ = _defs()
    write, read = _kinds()
    not_post = "This conversation is not a team post."

    def context():
        repo = host.services.ctx.team_repo
        post = repo.post_for_session(host.session_id)
        board = getattr(host.services.teams, "board", None)
        if post is None or board is None:
            return None
        team = repo.get(post["team_id"])
        if team is None or team.get("archived_at") is not None:
            return None
        return repo, post, team, board

    def say(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        _repo, post, team, board = found
        text = (arguments or {}).get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, f"text must be 1..{MAX_TEXT} characters")
        work = _work_of(board, host.session_id)
        message = board.post_message(
            team,
            text.strip(),
            author_agent_id=post["agent_id"],
            chain_id=work.get("chain_id"),
            hop=int(work.get("hop") or 0) + 1,
        )
        return _ok({"posted": True, "mentions": len(message["mentions"]), "hop": message["hop"]})

    def wait(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        _repo, post, team, board = found
        name = (arguments or {}).get("for")
        if not isinstance(name, str) or not name.strip():
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "for must be a teammate's name")
        try:
            waiting = board.wait(host.session_id, team, post, name)
        except ValueError as exc:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
        return _ok(
            {
                "waiting_for": waiting["for_name"],
                "note": "End your turn now, without an answer. You are woken with what they post.",
            }
        )

    def create(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        repo, post, team, board = found
        args = dict(arguments or {})
        target_team = team
        to_team = args.pop("to_team", None)
        if to_team:
            # Between teams: only teams of the same profile, received by their chief.
            target_team = _team_named(repo, team["rinari_profile_id"], to_team)
            if target_team is None:
                return _fail(ToolErrorCode.NOT_FOUND, f"No team named {to_team}.")
            args.pop("to", None)
        to = args.pop("to", None)
        if to:
            names = board._names(target_team["id"])
            args["to_agent_id"] = names.get(str(to).lstrip("@").lower())
            if args["to_agent_id"] is None:
                return _fail(ToolErrorCode.NOT_FOUND, f"No agent named {to} in that team.")
        return _leave_errand(
            host,
            board,
            target_team,
            args,
            from_agent_id=post["agent_id"],
            from_team_id=team["id"] if target_team["id"] != team["id"] else None,
            work=_work_of(board, host.session_id),
        )

    def review(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        return _errand_review(host, found[1]["agent_id"], arguments)

    def board_list(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        repo, _post, team, board = found
        names = {agent_id: name for name, agent_id in board._names(team["id"]).items()}
        errands = [
            {
                "id": item["id"],
                "title": item["title"],
                "to": names.get(item.get("to_agent_id") or ""),
                "from": names.get(item.get("from_agent_id") or "") or "owner",
                "status": item["status"],
            }
            for item in repo.errands(team["id"])
            if item["status"] in ("pending", "in_progress", "delivered", "returned", "blocked")
        ]
        return _ok({"team": team["name"], "errands": errands})

    def members(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        _repo, post, team, board = found
        data = {
            "team": team["name"],
            "purpose": team.get("purpose") or "",
            "members": board.members(team, me=post["agent_id"]),
        }
        if (arguments or {}).get("teams"):
            data["other_teams"] = _teams_directory(host, team["rinari_profile_id"], but=team["id"])
        return _ok(data)

    return [
        ToolDefinition(
            name="office.say",
            description=(
                "Write in your team's office chat. Mention a teammate with @Name to wake "
                "them; a message without a mention wakes nobody."
            ),
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": MAX_TEXT}},
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=say,
            **write,
        ),
        ToolDefinition(
            name="office.wait",
            description=(
                "Wait for a teammate: when what you were asked depends on something another "
                "agent must do first, call this with their name and end your turn without an "
                "answer. You are woken again with what they post."
            ),
            input_schema={
                "type": "object",
                "properties": {"for": {"type": "string", "description": "The teammate's name."}},
                "required": ["for"],
                "additionalProperties": False,
            },
            handler=wait,
            **write,
        ),
        ToolDefinition(
            name="errand.create",
            description=(
                "Leave an errand on a board: to a teammate (to: their name), to your team's "
                "chief (no to), or to another team (to_team: its name; its chief takes it). "
                "Say what to do and how to know it is done (done_when)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_ERRAND_PROPS,
                    "to": {"type": "string"},
                    "to_team": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=create,
            **write,
        ),
        ToolDefinition(
            name="errand.review",
            description=_REVIEW_DESCRIPTION,
            input_schema=_REVIEW_SCHEMA,
            handler=review,
            **write,
        ),
        ToolDefinition(
            name="board.list",
            description="Your team's board: the open and delivered errands, by whom and for whom.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=board_list,
            **read,
        ),
        ToolDefinition(
            name="team.members",
            description=(
                "Who is in your team right now: each teammate's job and whether they are free, "
                "working (and on what) or waiting for someone. With teams: true, also the "
                "Agency's other teams you can leave an errand to."
            ),
            input_schema={
                "type": "object",
                "properties": {"teams": {"type": "boolean"}},
                "additionalProperties": False,
            },
            handler=members,
            **read,
        ),
    ]


def direct_tools(host: TeamToolHost):
    """An agent's direct chat (D-30): the Agency's teams, to leave them an errand."""
    _, ToolDefinition, ToolErrorCode, _, _ = _defs()
    write, read = _kinds()
    not_agent = "This conversation is not an agent's direct chat."

    def context():
        agent = host.services.agency.for_session(host.session_id)
        board = getattr(host.services.teams, "board", None)
        if agent is None or board is None:
            return None
        return agent, board

    def teams_list(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_agent)
        return _ok({"teams": _teams_directory(host, found[0]["rinari_profile_id"])})

    def create(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_agent)
        agent, board = found
        args = dict(arguments or {})
        repo = host.services.ctx.team_repo
        target = _team_named(repo, agent["rinari_profile_id"], args.pop("to_team", None))
        if target is None:
            return _fail(ToolErrorCode.NOT_FOUND, "No team with that name: see teams.list.")
        # Its chief takes it and shares it out (D-8).
        return _leave_errand(
            host,
            board,
            target,
            args,
            from_agent_id=agent["id"],
            from_team_id=None,
            work=_work_of(board, host.session_id),
        )

    def review(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_agent)
        return _errand_review(host, found[0]["id"], arguments)

    return [
        ToolDefinition(
            name="teams.list",
            description=(
                "The owner's Agency teams: what each is for, its chief and its agents. Use it "
                "to pass work to a team with errand.create."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=teams_list,
            **read,
        ),
        ToolDefinition(
            name="errand.create",
            description=(
                "Leave an errand to a team (to_team: its name); its chief takes it. Say what to "
                "do and how to know it is done (done_when). You are told here when it is delivered."
            ),
            input_schema={
                "type": "object",
                "properties": {**_ERRAND_PROPS, "to_team": {"type": "string"}},
                "required": ["title", "to_team"],
                "additionalProperties": False,
            },
            handler=create,
            **write,
        ),
        ToolDefinition(
            name="errand.review",
            description=_REVIEW_DESCRIPTION,
            input_schema=_REVIEW_SCHEMA,
            handler=review,
            **write,
        ),
    ]


__all__ = ["TeamToolHost", "direct_tools", "team_tools"]
