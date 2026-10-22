"""Rinari Agency replan (22-oct), phases 2 to 4: how a team works on its own.

A team keeps its procedure in its **manual** (every post reads it), its
periodic work in **routines** and its multi-step work in **flows**; the owner
sees it all in one **overview**.

- A routine is a rule of one of its agents that runs in that agent's post,
  with the post's history. The owner or the chief creates it; the agents of
  the team see them all (`team.routines`).
- A flow is an ordered list of steps, each with the agents who do it and who
  approves it (auto, the chief or the owner). Starting it leaves the first
  step's errands on the board; when every errand of a step is approved, the
  next step's errands are left, carrying what the earlier steps delivered.
  The Engine keeps the order, not what a model reads in a message.
- The overview is the team in one call: members and what each does, its
  routines with their next and last run, its flows and runs, its board and
  its spend today and over the last 7 days.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rinari.agency import AgencyError
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.shared.errors import NotFoundError

if TYPE_CHECKING:
    from rinari.engine_protocol.agency_teams import AgencyTeams

#: Who approves what a flow step delivers before the next step starts.
FLOW_APPROVALS = ("auto", "chief", "owner")
MAX_FLOW_NAME = 80
MAX_STEP_NAME = 80
MAX_STEP_TEXT = 8000
#: What one step hands the next, per delivery, inside the errand's body.
STEP_OUTPUT_CHARS = 4000
#: Office lines and runs the overview returns.
OVERVIEW_ACTIVITY = 40
OVERVIEW_RUNS = 20


def _text(value: Any, label: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AgencyError(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise AgencyError(f"{label} is required.")
    if len(value) > limit:
        raise AgencyError(f"{label} is at most {limit} characters.")
    return value


class TeamWork:
    """Routines, flows and the overview of the Agency's teams."""

    def __init__(self, board: AgencyTeams) -> None:
        self._board = board
        self._services = board._services
        self._repo = board._repo
        self._clock = board._clock
        self._turns = board._turns
        self._services.teams.on_reviewed = self.on_reviewed

    # -- helpers ---------------------------------------------------------------------

    def _team(self, team_id: Any) -> dict[str, Any]:
        team = self._repo.get(team_id) if isinstance(team_id, str) else None
        if team is None:
            raise NotFoundError(f"Unknown team: {team_id}")
        return team

    def _agent_id(self, team: dict[str, Any], who: Any) -> str:
        """A member of the team by id or by name, as a mention writes it."""
        if not isinstance(who, str) or not who.strip():
            raise AgencyError("An agent of the team is needed.")
        members = {post["agent_id"] for post in self._repo.posts(team["id"])}
        if who in members:
            return who
        found = self._board._names(team["id"]).get(who.strip().lstrip("@").lower())
        if found is None:
            raise AgencyError(f"No agent named {who} in the team «{team['name']}».")
        return found

    def _guard(self, work):
        try:
            return work()
        except AgencyError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        except LookupError as exc:
            raise NotFoundError(str(exc)) from None

    def _notice(self, team: dict[str, Any], text: str, notice: dict[str, Any], chain: str) -> None:
        self._board.post_message(
            team,
            text,
            author_agent_id=None,
            chain_id=chain,
            hop=0,
            wake=False,
            system=True,
            notice=notice,
            author_kind="system",
        )

    # -- routines (phase 2) ----------------------------------------------------------------

    def routines(self, team_id: str) -> list[dict[str, Any]]:
        """The rules that work for this team, with their next and last run."""
        from rinari.schedule.rules import ScheduleError, describe, parse_schedule

        names = {agent_id: name for name, agent_id in self._board._names(team_id).items()}
        out = []
        for task in self._board._team_rules(team_id):
            agent = self._services.ctx.agency_repo.get(task.get("agent_id") or "")
            last = self._services.ctx.schedule_repo.last_run(task["id"])
            try:
                when = describe(parse_schedule(task["schedule"]))
            except (ScheduleError, KeyError, TypeError):
                when = ""
            out.append(
                {
                    "id": task["id"],
                    "name": task["name"],
                    "agent_id": task.get("agent_id"),
                    "agent": (agent or {}).get("name") or names.get(task.get("agent_id") or ""),
                    "schedule": task["schedule"],
                    "when": when,
                    "prompt": task.get("prompt") or "",
                    "enabled": bool(task.get("enabled")),
                    "paused_by": task.get("paused_by"),
                    "next_run_at": task.get("next_run_at") if task.get("enabled") else None,
                    "last": (
                        {
                            "status": last.get("status"),
                            "finished_at": last.get("finished_at"),
                            "summary": (last.get("summary") or "")[:600],
                            "session_id": last.get("session_id"),
                        }
                        if last
                        else None
                    ),
                }
            )
        return out

    def create_routine(
        self,
        team: dict[str, Any],
        who: Any,
        name: Any,
        schedule: Any,
        prompt: Any,
        *,
        by_session: str | None = None,
    ) -> dict[str, Any]:
        """A routine for a member: its rule, running in its post of this team. One per
        agent and name: the same name again updates it (the chief re-reads the manual).
        `by_session`: a post created it, announced as schedule.propose announces it."""
        if team.get("archived_at") is not None:
            raise AgencyError("An archived team takes no routines.")
        agent_id = self._agent_id(team, who)
        post = self._repo.post(team["id"], agent_id)
        if post is None:
            raise AgencyError("That agent has no post in this team.")
        raw = {
            "name": _text(name, "The routine's name", 80, required=True),
            "kind": "agent",
            "schedule": schedule,
            "prompt": _text(prompt, "What the routine does", MAX_STEP_TEXT, required=True),
            "agent_id": agent_id,
            "target": "this_conversation",
            "session_id": post["session_id"],
        }
        same = [
            task
            for task in self._board._team_rules(team["id"])
            if task.get("agent_id") == agent_id
            and task.get("session_id") == post["session_id"]
            and (task.get("name") or "").lower() == raw["name"].lower()
        ]
        schedules = self._services.schedules
        if same:
            task = schedules.update(
                same[0]["id"], {"schedule": raw["schedule"], "prompt": raw["prompt"]}
            )
            change = "updated"
        elif by_session:
            task = schedules.propose(raw, session_id=by_session, create=True)["task"]
            change = "created"
        else:
            task = schedules.create(raw)
            change = "created"
        self._turns.emit_external(
            event("schedule.changed", {"task_id": task["id"], "change": change})
        )
        self._board._changed(team["id"], "routine")
        return task

    def routines_list(self, params: dict[str, Any]) -> dict[str, Any]:
        team = self._team(params.get("team_id"))
        return {"routines": self.routines(team["id"])}

    def routine_create(self, params: dict[str, Any]) -> dict[str, Any]:
        from rinari.schedule.rules import ScheduleError
        from rinari.schedule.service import ScheduledTaskError

        team = self._team(params.get("team_id"))
        try:
            task = self.create_routine(
                team,
                params.get("agent_id"),
                params.get("name"),
                params.get("schedule"),
                params.get("prompt"),
            )
        except (AgencyError, ScheduleError, ScheduledTaskError) as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        return {"routine": next(r for r in self.routines(team["id"]) if r["id"] == task["id"])}

    # -- flows (phase 3) ----------------------------------------------------------------

    def _normalize_flow(self, team: dict[str, Any], raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AgencyError("A flow needs its name and steps.")
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise AgencyError("A flow needs at least one step.")
        steps = []
        for index, step in enumerate(steps_raw, start=1):
            if not isinstance(step, dict):
                raise AgencyError(f"Step {index} must be an object.")
            agents = step.get("agents")
            if isinstance(agents, str):
                agents = [agents]
            if not isinstance(agents, list) or not agents:
                raise AgencyError(f"Step {index} needs who does it (agents).")
            approval = step.get("approval") or "auto"
            if approval not in FLOW_APPROVALS:
                raise AgencyError(f"Step {index}: approval is one of {', '.join(FLOW_APPROVALS)}.")
            steps.append(
                {
                    "name": _text(
                        step.get("name"), f"Step {index}'s name", MAX_STEP_NAME, required=True
                    ),
                    "agents": list(dict.fromkeys(self._agent_id(team, who) for who in agents)),
                    "instructions": _text(
                        step.get("instructions"), f"Step {index}'s instructions", MAX_STEP_TEXT
                    ),
                    "approval": approval,
                }
            )
        return {
            "name": _text(raw.get("name"), "The flow's name", MAX_FLOW_NAME, required=True),
            "description": _text(raw.get("description"), "The description", MAX_STEP_TEXT),
            "steps": steps,
        }

    def save_flow(self, team: dict[str, Any], raw: Any) -> dict[str, Any]:
        if team.get("archived_at") is not None:
            raise AgencyError("An archived team takes no flows.")
        flow = self._normalize_flow(team, raw)
        flow_id = raw.get("id") if isinstance(raw, dict) else None
        now = self._clock.now()
        current = self._repo.flow(flow_id) if isinstance(flow_id, str) else None
        if current is not None and current["team_id"] != team["id"]:
            raise AgencyError("That flow belongs to another team.")
        if current is None:
            same = [
                f for f in self._repo.flows(team["id"]) if f["name"].lower() == flow["name"].lower()
            ]
            current = same[0] if same else None
        flow_id = current["id"] if current else self._services.ctx.ids.new("flow")
        saved = self._repo.save_flow(
            flow_id,
            {
                **flow,
                "team_id": team["id"],
                "created_at": current["created_at"] if current else now,
                "updated_at": now,
            },
        )
        self._board._changed(team["id"], "flow")
        return saved

    def _flow_named(self, team: dict[str, Any], which: Any) -> dict[str, Any]:
        if isinstance(which, str):
            flow = self._repo.flow(which)
            if flow is not None and flow["team_id"] == team["id"]:
                return flow
            for flow in self._repo.flows(team["id"]):
                if flow["name"].lower() == which.strip().lower():
                    return flow
        raise AgencyError(f"No flow named {which} in the team «{team['name']}».")

    def start(
        self, team: dict[str, Any], which: Any, input_text: Any, *, started_by: str | None = None
    ) -> dict[str, Any]:
        """Start a run of a flow: its first step's errands go on the board."""
        if team.get("archived_at") is not None:
            raise AgencyError("An archived team runs no flows.")
        flow = self._flow_named(team, which)
        now = self._clock.now()
        run_id = self._services.ctx.ids.new("flowrun")
        run = self._repo.save_flow_run(
            run_id,
            {
                "flow_id": flow["id"],
                "team_id": team["id"],
                "status": "running",
                "step": 0,
                "input": _text(input_text, "The input", MAX_STEP_TEXT),
                "outputs": [],
                "started_by": started_by,
                "chain_id": self._services.ctx.ids.new("chain"),
                "created_at": now,
                "updated_at": now,
            },
        )
        self._notice(
            team,
            flow["name"],
            {"kind": "flow", "state": "started", "flow": flow["name"], "run_id": run_id},
            run["chain_id"],
        )
        self._launch(team, flow, run)
        return self._repo.flow_run(run_id) or run

    def _outputs(self, flow: dict[str, Any], run: dict[str, Any]) -> str:
        """What the run's steps delivered so far, one block per delivery."""
        return "\n\n".join(
            f"[{flow['steps'][item['step']]['name']} · "
            f"{self._board._author(item.get('agent_id'))}]\n{item.get('summary') or ''}"
            for item in run.get("outputs") or []
        )

    def _launch(self, team: dict[str, Any], flow: dict[str, Any], run: dict[str, Any]) -> None:
        """Leave the errands of the run's current step on the board."""
        step = flow["steps"][run["step"]]
        earlier = self._outputs(flow, run)
        body = "\n\n".join(
            part
            for part in (
                f"Step {run['step'] + 1} of {len(flow['steps'])} of the flow «{flow['name']}».",
                step.get("instructions") or "",
                f"What the run was started with:\n{run['input']}" if run.get("input") else "",
                f"What the earlier steps delivered:\n{earlier}" if earlier else "",
            )
            if part
        )
        chief = self._board._active_chief(team)
        for agent_id in step["agents"]:
            if self._repo.post(team["id"], agent_id) is None:
                continue  # it left the team: the step goes on with the others
            self._services.teams.create_errand(
                team["id"],
                {
                    "title": f"{flow['name']} · {step['name']}",
                    "body": body[:8000],
                    "to_agent_id": agent_id,
                    "production_run_id": run["id"],
                    "flow_step": run["step"],
                },
                from_agent_id=chief if step["approval"] == "chief" else None,
                chain_id=run["chain_id"],
                hop=0,
            )
        self._board.tick()

    def step_approval(self, errand: dict[str, Any]) -> str | None:
        """Who approves this errand's step, when it belongs to a running flow."""
        run_id = errand.get("production_run_id")
        run = self._repo.flow_run(run_id) if run_id else None
        if run is None or run["status"] != "running":
            return None
        flow = self._repo.flow(run["flow_id"])
        step = errand.get("flow_step")
        if flow is None or step is None or step >= len(flow["steps"]):
            return None
        return flow["steps"][step]["approval"]

    def on_delivered(self, errand: dict[str, Any]) -> bool:
        """A flow step approved on its own: accepted as delivered. True when it was."""
        if self.step_approval(errand) != "auto":
            return False
        self._services.teams.review(errand["id"], True)
        return True

    def on_reviewed(self, errand: dict[str, Any]) -> None:
        """An accepted errand of a flow step: when the whole step is, the next one starts."""
        run_id = errand.get("production_run_id")
        run = self._repo.flow_run(run_id) if run_id else None
        if run is None or run["status"] != "running" or errand.get("flow_step") != run["step"]:
            return
        flow = self._repo.flow(run["flow_id"])
        team = self._repo.get(run["team_id"])
        if flow is None or team is None:
            return
        step_errands = [
            item
            for item in self._repo.errands(team["id"])
            if item.get("production_run_id") == run["id"] and item.get("flow_step") == run["step"]
        ]
        if any(item["status"] not in ("reviewed", "cancelled") for item in step_errands):
            return
        outputs = list(run.get("outputs") or []) + [
            {
                "step": run["step"],
                "agent_id": item.get("to_agent_id"),
                "errand_id": item["id"],
                "summary": ((item.get("result") or {}).get("summary") or "")[:STEP_OUTPUT_CHARS],
            }
            for item in step_errands
            if item["status"] == "reviewed"
        ]
        now = self._clock.now()
        nxt = run["step"] + 1
        done = nxt >= len(flow["steps"])
        run = self._repo.save_flow_run(
            run["id"],
            {
                **run,
                "outputs": outputs,
                "step": run["step"] if done else nxt,
                "status": "done" if done else "running",
                "updated_at": now,
                "finished_at": now if done else None,
            },
        )
        if done:
            self._notice(
                team,
                flow["name"],
                {"kind": "flow", "state": "done", "flow": flow["name"], "run_id": run["id"]},
                run["chain_id"],
            )
            self._report_done(team, flow, run)
        else:
            self._notice(
                team,
                flow["steps"][nxt]["name"],
                {
                    "kind": "flow",
                    "state": "step",
                    "flow": flow["name"],
                    "step": flow["steps"][nxt]["name"],
                    "run_id": run["id"],
                },
                run["chain_id"],
            )
            self._launch(team, flow, run)
        self._board._changed(team["id"], "flow")

    def _report_done(self, team: dict[str, Any], flow: dict[str, Any], run: dict[str, Any]) -> None:
        """A finished run goes back to whoever started it: the chief (or an agent) reviews
        and answers the owner; one the owner started reaches the chief to report."""
        who = run.get("started_by") or self._board._active_chief(team)
        post = self._repo.post(team["id"], who) if who else None
        if post is None:
            return
        outputs = self._outputs(flow, run)
        self._board.post_message(
            team,
            f"@{self._board._author(who)} The flow «{flow['name']}» is done. What each step "
            f"delivered:\n\n{outputs}"[:8000],
            author_agent_id=None,
            chain_id=run["chain_id"],
            hop=0,
            mentions=[who],
            author_kind="system",
        )

    def cancel_run(self, run_id: Any) -> dict[str, Any]:
        run = self._repo.flow_run(run_id) if isinstance(run_id, str) else None
        if run is None:
            raise LookupError(f"Unknown flow run: {run_id}")
        if run["status"] != "running":
            return run
        for item in self._repo.errands(run["team_id"]):
            if item.get("production_run_id") == run["id"] and item["status"] in (
                "pending",
                "in_progress",
                "returned",
                "blocked",
                "delivered",
            ):
                self._board.errand_cancel({"errand_id": item["id"]})
        now = self._clock.now()
        run = self._repo.save_flow_run(
            run["id"], {**run, "status": "cancelled", "updated_at": now, "finished_at": now}
        )
        self._board._changed(run["team_id"], "flow")
        return run

    def _flow_view(self, flow: dict[str, Any]) -> dict[str, Any]:
        return {
            **flow,
            "steps": [
                {**step, "agent_names": [self._board._author(a) for a in step["agents"]]}
                for step in flow["steps"]
            ],
        }

    def _run_view(self, run: dict[str, Any]) -> dict[str, Any]:
        flow = self._repo.flow(run["flow_id"])
        return {
            **run,
            "flow": flow["name"] if flow else None,
            "steps": len(flow["steps"]) if flow else 0,
            "step_name": (
                flow["steps"][run["step"]]["name"]
                if flow and run["step"] < len(flow["steps"])
                else None
            ),
        }

    # Protocol methods.

    def flow_list(self, params: dict[str, Any]) -> dict[str, Any]:
        team = self._team(params.get("team_id"))
        return {
            "flows": [self._flow_view(flow) for flow in self._repo.flows(team["id"])],
            "runs": [
                self._run_view(run) for run in self._repo.flow_runs(team["id"], limit=OVERVIEW_RUNS)
            ],
        }

    def flow_save(self, params: dict[str, Any]) -> dict[str, Any]:
        team = self._team(params.get("team_id"))
        return {
            "flow": self._flow_view(self._guard(lambda: self.save_flow(team, params.get("flow"))))
        }

    def flow_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        flow = (
            self._repo.flow(params.get("flow_id"))
            if isinstance(params.get("flow_id"), str)
            else None
        )
        if flow is None:
            raise NotFoundError(f"Unknown flow: {params.get('flow_id')}")
        for run in self._repo.flow_runs(flow["team_id"], statuses=("running",)):
            if run["flow_id"] == flow["id"]:
                self.cancel_run(run["id"])
        self._repo.delete_flow(flow["id"])
        self._board._changed(flow["team_id"], "flow")
        return {"deleted": flow["id"]}

    def flow_start(self, params: dict[str, Any]) -> dict[str, Any]:
        team = self._team(params.get("team_id"))
        self._board._owner_acts(team)
        run = self._guard(lambda: self.start(team, params.get("flow_id"), params.get("input")))
        return {"run": self._run_view(run)}

    def flow_run_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"run": self._run_view(self._guard(lambda: self.cancel_run(params.get("run_id"))))}

    # -- the overview (phase 4) ---------------------------------------------------------------

    def overview(self, params: dict[str, Any]) -> dict[str, Any]:
        """The team in one call, for its panel."""
        from rinari.engine_protocol.agency import spend_of

        team = self._team(params.get("team_id"))
        stopped = team["id"] in self._board._stopped_teams()
        errands = self._repo.errands(team["id"])
        counts: dict[str, int] = {}
        for item in errands:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        agents = []
        for post in self._repo.posts(team["id"]):
            agent = self._services.ctx.agency_repo.get(post["agent_id"])
            if agent is None:
                continue
            agents.append(
                {
                    "agent_id": agent["id"],
                    "name": agent["name"],
                    "today": spend_of(self._services, "agent", agent["id"], {}, days=1),
                    "week": spend_of(self._services, "agent", agent["id"], {}, days=7),
                }
            )
        activity = [
            {
                "id": item["id"],
                "at": item["created_at"],
                "kind": (item.get("notice") or {}).get("kind") or "message",
                "line": self._board._line(item, 300),
                "errand_id": item.get("errand_id"),
            }
            for item in self._repo.messages(team["id"], limit=OVERVIEW_ACTIVITY)
        ]
        return {
            "team": {**self._services.teams.view(team), "stopped": stopped},
            "members": self._board.members(team),
            "routines": self.routines(team["id"]),
            "flows": [self._flow_view(flow) for flow in self._repo.flows(team["id"])],
            "runs": [
                self._run_view(run) for run in self._repo.flow_runs(team["id"], limit=OVERVIEW_RUNS)
            ],
            "errands": counts,
            "activity": list(reversed(activity)),
            "spend": {
                "today": spend_of(self._services, "team", team["id"], team.get("budget") or {}),
                "week": spend_of(self._services, "team", team["id"], {}, days=7),
                "agents": agents,
            },
        }
