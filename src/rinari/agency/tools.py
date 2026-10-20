"""Tools of an agent in its post (Rinari Agency A2): the office and the board.

Only a post's conversation has them. What they do is counted by the Engine
from the turn that runs, never from the model's arguments (D-2): the chain
and the hop come from the work the post is doing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TeamToolHost:
    services: Any
    #: The post's conversation: its team and its agent come from it.
    session_id: str


def team_tools(host: TeamToolHost):
    from rinari.agency import AgencyError
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )

    def fail(code, message: str) -> ToolResult:
        return ToolResult(ok=False, error=ToolErrorInfo(code, message), origin="agency")

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

    def work_of(board) -> dict[str, Any]:
        with board._lock:
            return dict(board._live.get(host.session_id) or {})

    def say(arguments, ctx):
        found = context()
        if found is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "This conversation is not a team post.")
        _repo, post, team, board = found
        text = (arguments or {}).get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            return fail(ToolErrorCode.INVALID_ARGUMENT, "text must be 1..8000 characters")
        work = work_of(board)
        message = board.post_message(
            team,
            text.strip(),
            author_agent_id=post["agent_id"],
            chain_id=work.get("chain_id"),
            hop=int(work.get("hop") or 0) + 1,
        )
        return ToolResult(
            ok=True,
            data={"posted": True, "mentions": len(message["mentions"]), "hop": message["hop"]},
            origin="agency",
        )

    def create(arguments, ctx):
        found = context()
        if found is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "This conversation is not a team post.")
        repo, post, team, board = found
        args = dict(arguments or {})
        target_team = team
        to_team = args.pop("to_team", None)
        if to_team:
            # Between teams: only teams of the same profile, received by their chief.
            target_team = next(
                (
                    item
                    for item in repo.list(team["rinari_profile_id"])
                    if item["name"].lower() == str(to_team).lower()
                ),
                None,
            )
            if target_team is None:
                return fail(ToolErrorCode.NOT_FOUND, f"No team named {to_team}.")
            args.pop("to", None)
        to = args.pop("to", None)
        if to:
            names = board._names(target_team["id"])
            args["to_agent_id"] = names.get(str(to).lstrip("@").lower())
            if args["to_agent_id"] is None:
                return fail(ToolErrorCode.NOT_FOUND, f"No agent named {to} in that team.")
        work = work_of(board)
        hop = int(work.get("hop") or 0) + 1
        if hop >= board.MAX_HOPS:
            # The Engine's cap, not the prompt's (D-2): the chain stops here.
            board._stopped(team, work.get("chain_id") or "", "too_many_hops")
            return fail(
                ToolErrorCode.PERMISSION_DENIED,
                "This chain of errands is already 3 hops long; the owner was told.",
            )
        try:
            errand = host.services.teams.create_errand(
                target_team["id"],
                args,
                from_agent_id=post["agent_id"],
                from_team_id=team["id"] if target_team["id"] != team["id"] else None,
                chain_id=work.get("chain_id"),
                hop=hop,
            )
        except AgencyError as exc:
            return fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
        board.post_message(
            target_team,
            f"Recado «{errand['title']}»",
            author_agent_id=post["agent_id"],
            chain_id=errand["chain_id"],
            hop=errand["hop"],
            errand_id=errand["id"],
            wake=False,
        )
        board.tick()
        return ToolResult(
            ok=True, data={"errand_id": errand["id"], "status": errand["status"]}, origin="agency"
        )

    def review(arguments, ctx):
        found = context()
        if found is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "This conversation is not a team post.")
        _repo, post, _team, board = found
        args = arguments or {}
        errand = host.services.ctx.team_repo.errand(str(args.get("errand_id") or ""))
        if errand is None or errand.get("from_agent_id") != post["agent_id"]:
            return fail(ToolErrorCode.NOT_FOUND, "Only the agent that asked reviews an errand.")
        accepted = args.get("accepted")
        if not isinstance(accepted, bool):
            return fail(ToolErrorCode.INVALID_ARGUMENT, "accepted must be true or false")
        try:
            reviewed = host.services.teams.review(errand["id"], accepted, args.get("reason"))
        except AgencyError as exc:
            return fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
        board.tick()
        return ToolResult(ok=True, data={"status": reviewed["status"]}, origin="agency")

    def board_list(arguments, ctx):
        found = context()
        if found is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "This conversation is not a team post.")
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
        return ToolResult(ok=True, data={"team": team["name"], "errands": errands}, origin="agency")

    common = {"always_loaded": False, "side_effects": "local-reversible"}
    return [
        ToolDefinition(
            name="office.say",
            description=(
                "Write in your team's office chat. Mention a teammate with @Name to wake "
                "them; a message without a mention wakes nobody."
            ),
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 8000}},
                "required": ["text"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=say,
            **common,
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
                    "title": {"type": "string", "maxLength": 200},
                    "body": {"type": "string", "maxLength": 8000},
                    "done_when": {"type": "array", "items": {"type": "string"}},
                    "to": {"type": "string"},
                    "to_team": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=create,
            **common,
        ),
        ToolDefinition(
            name="errand.review",
            description=(
                "Review an errand you asked for once it is delivered: accept it, or send it "
                "back with the reason."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "errand_id": {"type": "string"},
                    "accepted": {"type": "boolean"},
                    "reason": {"type": "string", "maxLength": 300},
                },
                "required": ["errand_id", "accepted"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=review,
            **common,
        ),
        ToolDefinition(
            name="board.list",
            description="Your team's board: the open and delivered errands, by whom and for whom.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            capabilities=("state.read",),
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=board_list,
            always_loaded=False,
            side_effects="none",
        ),
    ]


__all__ = ["TeamToolHost", "team_tools"]
