"""«Arma tu equipo con Rinari» on the protocol (capability `agency_team_builder_v1`).

- `agency.team.draft`: the owner's description (and, later, their answers or
  the changes they ask for) becomes a proposal, or up to three questions when
  something that changes the team is unclear. Answers at once with a job id;
  the result arrives as `agency.team_draft.ready` / `.failed`. Creates nothing.
- `agency.team.apply`: creates what the owner confirmed, all at once: the
  agents (new, or the owner's own when they chose to reuse one), the team with
  its chief, manual, project and trusted work folders, its flow, its routines
  and its «To get started» list. If any part fails, what was made is undone.
- `agency.team.setup.get` / `.resolve`: that list. A catalog connector is done
  once the owner connects it; a need with no connector is done when the owner
  assigns one of their connectors or says they will do it themselves. Routines
  that waited for it start when the last piece is done.

The planning call has no tools: the model only writes a proposal, and what
it reads from the owner and the connectors is data (`agency/plan.py`).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rinari.agency import AgencyError
from rinari.agency.plan import (
    MAX_DESCRIPTION,
    MAX_FEEDBACK,
    PLAN_MAX_TOKENS,
    SYSTEM_PROMPT,
    Inventory,
    PlanError,
    inventory_text,
    parse_json,
    pending_pieces,
    questions_of,
    user_message,
    validate_plan,
)
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.shared.errors import NotFoundError

if TYPE_CHECKING:
    from rinari.engine_protocol.server import EngineServer

#: Why a routine waits: the team still misses a piece (`setup`).
PAUSED_FOR_SETUP = "setup"
MAX_ANSWERS = 6
MAX_ANSWER = 2000


def _language(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip().lower().replace("_", "-").split("-")[0]
    return "en"


class AgencyPlanner:
    def __init__(self, server: EngineServer) -> None:
        self._server = server
        self._services = server._services
        self._turns = server._turns

    # -- what the owner has -------------------------------------------------------------

    def inventory(self, profile: str, language: str = "en") -> Inventory:
        """The owner's real state, read now: nothing here is fixed to a case."""
        from rinari.mcp import catalog
        from rinari.tools.catalog import builtin_catalog
        from rinari.tools.index import _group

        inventory = Inventory()
        mcp = self._services.mcp
        rows = [row for row in mcp.list() if not (row.get("owner") or "")]
        visible = {row["name"]: row for row in rows if row.get("enabled")}
        try:
            states = {item["name"]: item for item in mcp.connectors()}
        except Exception:
            states = {}
        for name, row in visible.items():
            state = states.get(name, {})
            about = state.get("description") or state.get("title") or ""
            entry = catalog.entry_for_row(row)
            if entry is not None:
                inventory.catalog_added[entry["id"]] = name
                described = entry.get("description") or {}
                about = described.get(language) or described.get("en") or about
            try:
                tools = [getattr(tool, "name", str(tool)) for tool in mcp.known_tools(name)][:16]
            except Exception:
                tools = []
            inventory.connectors[name] = {
                "status": state.get("status") or "idle",
                "about": about,
                "tools": tools,
            }
        for entry in catalog.entries():
            if entry["id"] in inventory.catalog_added:
                continue
            described = entry.get("description") or {}
            inventory.catalog[entry["id"]] = {
                "name": entry.get("name") or entry["id"],
                "description": described.get(language) or described.get("en") or "",
            }
        groups: dict[str, list[str]] = {}
        registry = builtin_catalog()
        for name in sorted(registry.names()):
            tool = registry.get(name)
            if tool is not None:
                groups.setdefault(_group(tool), []).append(name)
        inventory.tool_groups = groups
        try:
            for skill in self._services.skills.library():
                if skill.get("enabled") and skill.get("valid"):
                    inventory.skills[skill["name"]] = skill.get("description") or ""
        except Exception:
            pass
        for agent in self._services.agency.list(profile):
            inventory.agents[agent["id"]] = {
                "name": agent["name"],
                "role": agent.get("role") or "",
                "connectors": list(agent.get("connectors") or []),
            }
        for project in self._services.projects.list(rinari_profile_id=profile):
            inventory.projects[project.id] = {
                "name": project.name or Path(project.canonical_root).name,
                "root": project.canonical_root,
            }
        return inventory

    def _profile(self, params: dict[str, Any]) -> str:
        return self._server._profile_param(params) or self._services.rinari_profiles.active_id()

    # -- the proposal ---------------------------------------------------------------------

    def _caller(self, model_ref: Any):
        """The chosen model, or the owner's default model (what a new conversation uses)."""
        from rinari.models.router import ModelRouter
        from rinari.runtime.model_caller import ModelCaller

        router = ModelRouter(self._services.providers, self._services.models)
        if isinstance(model_ref, str) and model_ref.strip():
            try:
                model = self._services.models.resolve(model_ref.strip())
            except Exception:
                raise EngineProtocolError(INVALID_PARAMS, f"Unknown model: {model_ref}") from None
            provider = self._services.ctx.provider_repo.get(model.provider_id)
            if provider is None or not provider.enabled:
                raise EngineProtocolError(INVALID_PARAMS, "That model's provider is not on.")
            return ModelCaller(router, provider, model.id)
        selection = self._services.providers.current()
        if selection is None or selection.model is None:
            raise EngineProtocolError(
                INVALID_PARAMS, "Connect a provider with a model first: the proposal uses it."
            )
        return ModelCaller(router, selection.provider, selection.model.id)

    def draft(self, params: dict[str, Any]) -> dict[str, Any]:
        description = params.get("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > MAX_DESCRIPTION
        ):
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'description' must be 1..{MAX_DESCRIPTION} characters."
            )
        feedback = params.get("feedback")
        if feedback is not None and (not isinstance(feedback, str) or len(feedback) > MAX_FEEDBACK):
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'feedback' must be text up to {MAX_FEEDBACK} characters."
            )
        answers = self._answers(params.get("answers"))
        previous = params.get("previous")
        if previous is not None and not isinstance(previous, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'previous' must be a proposal.")
        profile = self._profile(params)
        language = _language(params.get("language"))
        caller = self._caller(params.get("model"))
        inventory = self.inventory(profile, language)
        if previous is not None:
            try:
                previous, _ = validate_plan(previous, inventory)
            except PlanError:
                previous = None
        job_id = self._services.ctx.ids.new("teamdraft")
        message = user_message(
            description,
            answers=answers,
            previous=previous,
            feedback=feedback or None,
            language=language,
        )
        threading.Thread(
            target=self._run,
            args=(job_id, caller, inventory, message),
            name="agency-team-draft",
            daemon=True,
        ).start()
        return {"status": "started", "job_id": job_id}

    def _answers(self, raw: Any) -> list[dict[str, str]]:
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > MAX_ANSWERS:
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'answers' must be a list of up to {MAX_ANSWERS}."
            )
        out = []
        for item in raw:
            if not isinstance(item, dict):
                raise EngineProtocolError(INVALID_PARAMS, "Each answer is {question, answer}.")
            question, answer = item.get("question"), item.get("answer")
            if not isinstance(question, str) or not isinstance(answer, str):
                raise EngineProtocolError(INVALID_PARAMS, "Each answer is {question, answer}.")
            out.append({"question": question[:400], "answer": answer[:MAX_ANSWER]})
        return out

    def _run(self, job_id: str, caller: Any, inventory: Inventory, message: str) -> None:
        from rinari.models.types import ChatMessage, ModelRequest

        system = f"{SYSTEM_PROMPT}\n\n{inventory_text(inventory)}"
        messages = [ChatMessage.system(system), ChatMessage.user(message)]
        failure = "The model did not return a usable proposal. Try again or build the team by hand."
        try:
            for attempt in range(2):
                response = caller.invoke(
                    ModelRequest(
                        model=caller.model_id or "",
                        messages=tuple(messages),
                        max_tokens=PLAN_MAX_TOKENS,
                        # Providers that route by session (OpenCode Go) refuse a call without one.
                        session_id=job_id,
                    )
                )
                text = response.content or ""
                try:
                    raw = parse_json(text)
                    questions = questions_of(raw)
                    if questions:
                        self._emit_ready(job_id, {"kind": "questions", "questions": questions})
                        return
                    plan, notes = validate_plan(raw, inventory)
                except PlanError as exc:
                    if attempt == 0:
                        # One repair round: the model sees what is wrong with its answer.
                        messages += [
                            ChatMessage.assistant(text[:20000]),
                            ChatMessage.user(
                                f"That answer cannot be used: {exc} Answer again with the "
                                "whole JSON object only."
                            ),
                        ]
                        continue
                    failure = f"The model did not return a usable proposal ({exc})."
                    break
                self._emit_ready(
                    job_id,
                    {
                        "kind": "plan",
                        "plan": plan,
                        "notes": notes,
                        "pending": pending_pieces(plan),
                    },
                )
                return
        except Exception as exc:  # the desktop must hear back
            failure = (getattr(exc, "message", None) or str(exc))[:500]
        self._turns.emit_external(
            event("agency.team_draft.failed", {"job_id": job_id, "message": failure[:500]})
        )

    def _emit_ready(self, job_id: str, payload: dict[str, Any]) -> None:
        self._turns.emit_external(event("agency.team_draft.ready", {"job_id": job_id, **payload}))

    def check(self, params: dict[str, Any]) -> dict[str, Any]:
        """A proposal the owner edited, normalized again against what they have now
        (the review shows each connector's state from this)."""
        profile = self._profile(params)
        inventory = self.inventory(profile, _language(params.get("language")))
        try:
            plan, notes = validate_plan(params.get("plan"), inventory)
        except PlanError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        return {
            "plan": plan,
            "notes": notes,
            "pending": pending_pieces(plan),
            "connectors": {
                name: item.get("status") for name, item in sorted(inventory.connectors.items())
            },
        }

    # -- creating it ------------------------------------------------------------------------

    def apply(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile(params)
        inventory = self.inventory(profile)
        try:
            plan, _ = validate_plan(params.get("plan"), inventory)
        except PlanError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        undecided = [
            a["name"] for a in plan["agents"] if a["reuse_agent_id"] and a["reuse"] is None
        ]
        if undecided:
            raise EngineProtocolError(
                INVALID_PARAMS,
                f"Choose whether to reuse your agent or make a new one: {', '.join(undecided)}.",
            )
        default_model = params.get("model") if isinstance(params.get("model"), str) else None
        board = self._server._teams
        agency = self._services.agency
        made_agents: list[str] = []
        team: dict[str, Any] | None = None
        try:
            project_id = self._project(
                plan["team"]["project_folder"], plan["team"]["name"], profile
            )
            folders = (
                board.trust_folders(plan["team"]["work_folders"], create=True)
                if plan["team"]["work_folders"]
                else []
            )
            ids: dict[str, str] = {}
            team_connectors: list[str] = []
            for agent in plan["agents"]:
                connectors = [u["name"] for u in agent["uses"] if u["kind"] == "connector"]
                if agent["reuse"] and agent["reuse_agent_id"]:
                    existing = agency.get(agent["reuse_agent_id"])
                    if existing.get("archived_at") is not None:
                        raise AgencyError(f"{existing['name']} is archived.")
                    ids[agent["key"]] = existing["id"]
                    # Their card is theirs (every team they are in reads it): what this
                    # team needs on top goes to the team, which their post also sees.
                    team_connectors += [c for c in connectors if c not in existing["connectors"]]
                    continue
                card = {
                    "name": agent["name"],
                    "role": agent["role"],
                    "instructions": agent["instructions"],
                    "permission_profile": agent["permission_profile"],
                    "connectors": connectors,
                }
                model = agent.get("model") or default_model
                if model:
                    card["model"] = model
                created = agency.create(card, profile)
                made_agents.append(created["id"])
                ids[agent["key"]] = created["id"]
            manual = plan["team"]["manual"]
            if plan["assumptions"]:
                manual = (manual + "\n\n" if manual else "") + "\n".join(
                    f"- {item}" for item in plan["assumptions"]
                )
            team = board.teams.create(
                {
                    "name": plan["team"]["name"],
                    "purpose": plan["team"]["purpose"],
                    "manual": manual[:20000],
                    "members": [ids[a["key"]] for a in plan["agents"]],
                    "chief_agent_id": ids.get(plan["team"]["chief"]),
                    "project_id": project_id,
                    "folders": folders,
                    "connectors": list(dict.fromkeys(team_connectors)),
                },
                profile,
            )
            if project_id:
                board._rehome_posts(team)
            flow = None
            if plan["flow"] is not None:
                flow = board.work.save_flow(
                    team,
                    {
                        **plan["flow"],
                        "steps": [
                            {**step, "agents": [ids[key] for key in step["agents"]]}
                            for step in plan["flow"]["steps"]
                        ],
                    },
                )
            setup = [
                {
                    "id": f"piece-{index + 1}",
                    "agent_id": ids[piece["agent"]],
                    "kind": piece["kind"],
                    **(
                        {"catalog_id": piece["catalog_id"], "name": piece["name"]}
                        if piece["kind"] == "catalog"
                        else {"need": piece["need"]}
                    ),
                    "status": "pending",
                }
                for index, piece in enumerate(pending_pieces(plan))
            ]
            if setup:
                team = board.teams.set_setup(team["id"], setup)
            waiting = any(item["status"] == "pending" for item in setup)
            routines = []
            for routine in plan["routines"]:
                if routine["starts_flow"] and flow is not None:
                    task = board.work.create_flow_routine(
                        team,
                        routine["name"],
                        routine["schedule"],
                        flow["id"],
                        routine["input"],
                        enabled=not waiting,
                        paused_by=PAUSED_FOR_SETUP if waiting else None,
                    )
                else:
                    task = board.work.create_routine(
                        team,
                        ids[routine["agent"]],
                        routine["name"],
                        routine["schedule"],
                        routine["prompt"],
                    )
                    if waiting:
                        self._services.ctx.schedule_repo.set_enabled(
                            task["id"], False, board._clock.now(), paused_by=PAUSED_FOR_SETUP
                        )
                routines.append(task["id"])
        except (AgencyError, EngineProtocolError, NotFoundError, ValueError) as exc:
            self._undo(team, made_agents)
            if isinstance(exc, EngineProtocolError):
                raise
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        except Exception:
            self._undo(team, made_agents)
            raise
        board._changed(team["id"], "created")
        board.tick()
        return {
            "team": board.teams.view(board.teams.get(team["id"])),
            "agents": [ids[a["key"]] for a in plan["agents"]],
            "created_agents": made_agents,
            "flow_id": flow["id"] if flow else None,
            "routine_ids": routines,
            "setup": self._setup_view(board.teams.get(team["id"])),
        }

    def _project(self, folder: str | None, name: str, profile: str) -> str | None:
        """The team's project: the one already in that folder, or a new one there."""
        if not folder:
            return None
        from rinari.projects.detector import is_home_root

        root = Path(folder).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgencyError(f"Cannot create the folder {root}: {exc.strerror or exc}") from None
        if is_home_root(root, Path(self._server._user_home or Path.home())):
            raise AgencyError("Your home folder is never a project.")
        canonical = str(root.resolve())
        for project in self._services.projects.list(rinari_profile_id=profile):
            if project.canonical_root == canonical:
                return project.id
        project, _ = self._services.projects.add(root, name=name, rinari_profile_id=profile)
        return project.id

    def _undo(self, team: dict[str, Any] | None, agents: list[str]) -> None:
        """Nothing half made: the team and the agents this call created go away
        (archive first: that is what stops them; then delete for good)."""
        board = self._server._teams
        methods = self._server._agency
        if team is not None:
            try:
                board.archive({"team_id": team["id"]})
                board.delete({"team_id": team["id"]})
            except Exception:
                pass
        for agent_id in agents:
            try:
                methods.archive({"agent_id": agent_id})
                methods.delete({"agent_id": agent_id})
            except Exception:
                pass

    # -- «To get started» ------------------------------------------------------------------

    def _setup_view(self, team: dict[str, Any]) -> list[dict[str, Any]]:
        names = {}
        for item in team.get("setup") or []:
            agent_id = item.get("agent_id")
            if agent_id and agent_id not in names:
                try:
                    names[agent_id] = self._services.agency.get(agent_id).get("name") or ""
                except (AgencyError, LookupError):
                    names[agent_id] = ""
        return [
            {**item, "agent": names.get(item.get("agent_id"), "")}
            for item in team.get("setup") or []
        ]

    def refresh(self, team: dict[str, Any]) -> dict[str, Any]:
        """Mark done each catalog piece the owner has connected since, and start the
        routines that waited when nothing is left."""
        from rinari.mcp import catalog

        setup = [dict(item) for item in team.get("setup") or []]
        if not any(item.get("status") == "pending" for item in setup):
            return team
        rows = self._services.mcp.list()
        changed = False
        team_connectors = list(team.get("connectors") or [])
        for item in setup:
            if item.get("status") != "pending" or item.get("kind") != "catalog":
                continue
            owners = {"", f"team:{team['id']}", f"agent:{item.get('agent_id')}"}
            for row in rows:
                if (row.get("owner") or "") not in owners or not row.get("enabled"):
                    continue
                entry = catalog.entry_for_row(row)
                if entry is None or entry["id"] != item.get("catalog_id"):
                    continue
                if not (row.get("owner") or "") and row["name"] not in team_connectors:
                    # A connector of everyone: this team's posts see it once it is the team's.
                    team_connectors.append(row["name"])
                item["status"] = "done"
                item["resolution"] = f"connected:{row['name']}"
                changed = True
                break
        if not changed:
            return team
        return self._save_setup(team, setup, team_connectors)

    def _save_setup(
        self, team: dict[str, Any], setup: list[dict[str, Any]], connectors: list[str]
    ) -> dict[str, Any]:
        board = self._server._teams
        if connectors != list(team.get("connectors") or []):
            board.teams.update(team["id"], {"connectors": connectors})
        team = board.teams.set_setup(team["id"], setup)
        if not any(item.get("status") == "pending" for item in setup):
            now = board._clock.now()
            for task in board._team_rules(team["id"]):
                if not task.get("enabled") and task.get("paused_by") == PAUSED_FOR_SETUP:
                    self._services.ctx.schedule_repo.set_enabled(task["id"], True, now)
                    self._turns.emit_external(
                        event("schedule.changed", {"task_id": task["id"], "change": "updated"})
                    )
        return board.teams.get(team["id"])

    def setup_get(self, params: dict[str, Any]) -> dict[str, Any]:
        team = self._server._teams._team(params)
        team = self.refresh(team)
        return {"team_id": team["id"], "setup": self._setup_view(team)}

    def setup_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        """The owner settles one piece: `assign` one of their connectors to it, or
        `manual` (they will do that part themselves; it is not asked again)."""
        team = self._server._teams._team(params)
        action = params.get("action")
        if action not in ("assign", "manual"):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'action' must be assign or manual.")
        setup = [dict(item) for item in team.get("setup") or []]
        item = next((i for i in setup if i.get("id") == params.get("item_id")), None)
        if item is None:
            raise NotFoundError(f"Unknown piece: {params.get('item_id')}")
        connectors = list(team.get("connectors") or [])
        if action == "assign":
            name = params.get("connector")
            if not isinstance(name, str) or self._services.mcp.show(name) is None:
                raise EngineProtocolError(INVALID_PARAMS, f"Unknown connector: {name}")
            if name not in connectors:
                connectors.append(name)
            item["resolution"] = f"assigned:{name}"
        else:
            item["resolution"] = "manual"
        item["status"] = "done"
        team = self._save_setup(team, setup, connectors)
        return {"team_id": team["id"], "setup": self._setup_view(team)}
