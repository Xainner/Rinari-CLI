"""Tools of an agent in the Agency: in its post (A2) and in its direct chat (D-30).

A post (its conversation in a team) has the office, the board and its team:
`office.say`, `office.wait`, `errand.create`, `errand.review`, `board.list`
and `team.members`. An agent's direct chat has the Agency's teams:
`teams.list`, `office.say` (in the office of one of its teams), `errand.create`
(to a team) and `errand.review`. `office.say` says which mentions woke their
agent and which did not, and why (D-31), so the agent never claims it told
someone it did not.

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

#: What each reason a mention did not wake its agent means, for the model.
_NOT_WOKEN = {
    "too_many_hops": "this chain of work reached its hop limit",
    "too_many_mentions": "a message wakes at most 5 agents",
    "already_woken": "this turn already woke them in this chain",
    "stopped_by_owner": "the owner stopped the Agency",
    "not_in_team": "they are not in this team",
    "unknown_name": "no teammate has that name",
    "ambiguous_name": "several teammates match that name",
}


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


def _external(ctx: Any) -> bool:
    """Whether the calling turn read something from outside the machine."""
    return bool(getattr(getattr(ctx, "turn_state", None), "external_content", False))


def _work_of(board, session_id: str) -> dict[str, Any]:
    """What the conversation's running turn is for (a mention, an errand, a delivery)."""
    with board._lock:
        return dict(board._live.get(session_id) or {})


def _said(team: dict, message: dict, report: dict, names: list[str]) -> Any:
    """`office.say`'s result: what was posted, who was woken and who was not (D-31)."""
    not_woken = report.get("not_woken") or []
    data: dict[str, Any] = {
        "posted": True,
        "team": team["name"],
        "hop": message["hop"],
        "mentions": len(report.get("woken") or []),
        "woken": report.get("woken") or [],
        "not_woken": not_woken,
    }
    if not_woken:
        lines = []
        for item in not_woken:
            why = _NOT_WOKEN.get(item["reason"], item["reason"])
            if item.get("candidates"):
                why += f" ({', '.join(item['candidates'])})"
            lines.append(f"@{item['name']}: {why}")
        data["note"] = (
            "Posted, but these mentions were NOT delivered, so do not say they were told: "
            + "; ".join(lines)
            + ". Mention a teammate by one of these exact names: "
            + ", ".join(names)
            + "."
        )
    return _ok(data)


def _team_named(repo, profile: str, name: Any) -> dict[str, Any] | None:
    wanted = str(name or "").strip().lstrip("@").lower()
    if not wanted:
        return None
    return next((item for item in repo.list(profile) if item["name"].lower() == wanted), None)


def _teams_directory(
    host: TeamToolHost, profile: str, but: str | None = None, mine: set[str] | None = None
) -> list[dict]:
    """The profile's teams as an agent may see them: never their memory nor accounts.

    With `mine` (the ids of the agent's teams), each says whether it is one of them.
    """
    repo = host.services.ctx.team_repo
    agents = host.services.ctx.agency_repo
    out = []
    for team in repo.list(profile):
        if team["id"] == but:
            continue
        chief = agents.get(team["chief_agent_id"]) if team.get("chief_agent_id") else None
        members = [agents.get(post["agent_id"]) for post in repo.posts(team["id"])]
        item = {
            "name": team["name"],
            "purpose": team.get("purpose") or "",
            "chief": chief["name"] if chief else None,
            "members": [agent["name"] for agent in members if agent],
        }
        if mine is not None:
            item["yours"] = team["id"] in mine
        out.append(item)
    return out


def _errand_review(host: TeamToolHost, agent_id: str, arguments, external: bool = False):
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
        reviewed = host.services.teams.review(
            errand["id"], accepted, args.get("reason"), external=external
        )
    except AgencyError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    board = getattr(host.services.teams, "board", None)
    if board is not None:
        if reviewed["status"] == "returned" and board.hold_if_halted(reviewed["id"]):
            return _ok({"status": "blocked", "reason": "stopped_by_owner"})
        board.tick()
    return _ok({"status": reviewed["status"]})


def _leave_errand(
    host, board, target_team, args, *, from_agent_id, from_team_id, work, external=False
):
    """Leave an errand on a board, one hop further in the chain of the running work (D-2)."""
    from rinari.agency import AgencyError

    _, _, ToolErrorCode, _, _ = _defs()
    hop = int(work.get("hop", -1)) + 1
    if hop >= board.MAX_HOPS:
        board._stopped(target_team, work.get("chain_id") or "", "too_many_hops")
        return _fail(
            ToolErrorCode.PERMISSION_DENIED,
            f"This chain of work is already {board.MAX_HOPS} hops long: the errand was not "
            "left, and the owner was told.",
        )
    try:
        errand = host.services.teams.create_errand(
            target_team["id"],
            args,
            from_agent_id=from_agent_id,
            from_team_id=from_team_id,
            chain_id=work.get("chain_id"),
            hop=hop,
            external=external,
        )
    except AgencyError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    board.post_notice(target_team, errand, author_agent_id=from_agent_id)
    if board.hold_if_halted(errand["id"]):
        # The owner stopped the Agency: it stays on the board, blocked.
        return _ok(
            {
                "errand_id": errand["id"],
                "status": "blocked",
                "reason": "stopped_by_owner",
                "team": target_team["name"],
            }
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
    # The office and the board are the Agency's own bookkeeping, with no effect
    # outside it: allowed in a read-only post. Each agent woken by them works
    # with its own permissions.
    write = {
        "capabilities": ("state.mutate",),
        "classify": lambda _i: ClassifiedAction("state.mutate"),
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
        if not board.count_say(host.session_id):
            # The Engine's cap, not the prompt's (D-2): a turn says at most 3 things.
            return _fail(
                ToolErrorCode.RESOURCE_EXHAUSTED,
                "You already wrote 3 times in the office this turn: put the rest in your answer.",
            )
        work = _work_of(board, host.session_id)
        report: dict[str, Any] = {}
        message = board.post_message(
            team,
            text.strip(),
            author_agent_id=post["agent_id"],
            chain_id=work.get("chain_id"),
            hop=int(work.get("hop") or 0) + 1,
            external=_external(ctx),
            report=report,
        )
        return _said(team, message, report, list(board._display_names(team["id"])))

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
            external=_external(ctx),
        )

    def review(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_post)
        return _errand_review(host, found[1]["agent_id"], arguments, _external(ctx))

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
                "Write in your team's office chat. Mention a teammate with @ and their exact "
                "name (see team.members) to wake them; a message without a mention wakes "
                "nobody. The result says who was woken and who was not."
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
                "Wait for a teammate: when what you were asked (a mention or an errand) "
                "depends on something another agent must do first, call this with their name "
                "and end your turn without an answer. You are woken again with what they post; "
                "an errand stays yours meanwhile."
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
                "Who is in your team right now: each teammate's job, whether they are free, "
                "working (and on what) or waiting for someone, and their folder (you can read "
                "the files they leave there). With teams: true, also the Agency's other teams "
                "you can leave an errand to."
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
        agent = found[0]
        mine = {team["id"] for team in host.services.teams.teams_of(agent["id"])}
        return _ok({"teams": _teams_directory(host, agent["rinari_profile_id"], mine=mine)})

    def say(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_agent)
        agent, board = found
        args = arguments or {}
        team = host.services.teams.team_of(agent["id"], args.get("team"))
        if team is None:
            mine = [team["name"] for team in host.services.teams.teams_of(agent["id"])]
            return _fail(
                ToolErrorCode.NOT_FOUND,
                "You can only write in the office of a team you work in: "
                + (", ".join(mine) if mine else "you are in no team")
                + ".",
            )
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, f"text must be 1..{MAX_TEXT} characters")
        if not board.count_say(host.session_id):
            return _fail(
                ToolErrorCode.RESOURCE_EXHAUSTED,
                "You already wrote 3 times in an office this turn: put the rest in your answer.",
            )
        work = _work_of(board, host.session_id)
        report: dict[str, Any] = {}
        message = board.post_message(
            team,
            text.strip(),
            author_agent_id=agent["id"],
            chain_id=work.get("chain_id"),
            hop=int(work.get("hop") or 0) + 1,
            external=_external(ctx),
            woken=board.turn_woken(host.session_id),
            report=report,
        )
        return _said(team, message, report, list(board._display_names(team["id"])))

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
            external=_external(ctx),
        )

    def review(arguments, ctx):
        found = context()
        if found is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, not_agent)
        return _errand_review(host, found[0]["id"], arguments, _external(ctx))

    return [
        ToolDefinition(
            name="teams.list",
            description=(
                "The owner's Agency teams: what each is for, its chief, its agents and whether "
                "you work in it (yours). Pass work to a team with errand.create, or write in "
                "the office of one of yours with office.say."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=teams_list,
            **read,
        ),
        ToolDefinition(
            name="office.say",
            description=(
                "Write in the office chat of a team you work in (team: its name), as yourself. "
                "Mention a teammate with @ and their exact name to wake them; a message without "
                "a mention wakes nobody. The result says who was woken and who was not."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team": {"type": "string", "description": "The team's name."},
                    "text": {"type": "string", "maxLength": MAX_TEXT},
                },
                "required": ["team", "text"],
                "additionalProperties": False,
            },
            handler=say,
            **write,
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
