"""Engine Protocol constants (Rinari Code Phase 1, engine side)."""

from __future__ import annotations

import hashlib

PROTOCOL_NAME = "rinari-engine"
PROTOCOL_VERSION = 1


def engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("rinari")
    except Exception:
        return "0.1.0"


CAPABILITIES: dict[str, bool] = {
    "provider_catalog_v1": True,
    "provider_usage_v1": True,
    "provider_subscription_auth_v1": True,
    "provider_external_cli_v1": True,
    "claude_subscription_v1": True,
    "persistent_context_compaction_v1": True,
    "local_image_view_v1": True,
    "vision_routing_v1": True,
    "vision_routing_v2": True,
    "vision_message_routing_v3": True,
    "model_execution_policy_v1": True,
    "durable_turn_recovery_v1": True,
    "desktop_processes_v1": True,
    # Strong process identity: engine_instance_id per boot plus a
    # per-resource generation token validated by workspace.process.stop.
    "process_identity_v1": True,
    "agent_inheritance_v1": True,
    "agent_activity_v1": True,
    "browser_view_v1": True,
    # El Engine conoce la extensión del broker del browser nativo y valida sus
    # mensajes (documento 03 §5.2). Anunciarla señala soporte del protocolo,
    # **no** que exista una vista: para eso hace falta además que un host se
    # registre y que el contexto esté listo.
    "browser_host_bridge_v1": True,
    "tool_contracts_v1": True,
    "durable_operations_v1": True,
    "channel_tools_v1": True,
    "image_attachments_v1": True,
    "document_attachments_v1": True,
    "ocr_attachments_v1": True,
    "structured_tool_activity_v1": True,
    "recoverable_tool_results_v1": True,
    "ssh_targets_v1": True,
    "plan_read_scope_v1": True,
    "web_preview_v1": True,
    "desktop_workspace_v1": True,
    # Exact turn provenance authorizes external previews; a bounded Engine
    # watcher invalidates open tabs without sending file bytes in events.
    "workspace_file_watch_v1": True,
    # `workspace.file.resolve`: the authorized path, size and kind of a file
    # without reading it, so media and binaries open at any size.
    "workspace_file_resolve_v1": True,
    "artifact_resolve_v1": True,
    # Attachment preparation reports per-page coverage and accepts
    # keep_image (OCR text plus the pixels of the same image).
    "reading_coverage_v1": True,
    # Servicio documental: trabajos (render…), revisiones inmutables,
    # previews por revisión y lectura acotada; PPTX como adjunto.
    "document_jobs_v1": True,
    "document_revisions_v1": True,
    "document_preview_v1": True,
    "pptx_attachment_v1": True,
    "interactive_questions_v1": True,
    "chat": True,
    "projects": True,
    "browser": True,
    "mcp": True,
    "plugins": True,
    "subagents": True,
    "artifacts": True,
    "checkpoints": True,
    "terminal": True,
    # Session permissions, attachment/file search, model/tool lifecycle
    # telemetry, approval expiry and runtime snapshot recovery.
    "desktop_turn_runtime_v3": True,
    # Correlated, persisted, replayable narrative activity for Rinari Code.
    "activity_timeline_v1": True,
    "turn_token_usage_v1": True,
    # context.status accepts session_id; context.models lists every saved model.
    "context_status_v2": True,
    "permission_profiles_v2": True,
    "turn_changeset_v1": True,
    "personal_memory_v1": True,
    "selective_memory_v1": True,
    "memory_privacy_v1": True,
    "personal_memory_controls_v1": True,
    # memory.propose (learned facts), memory.settings.get/set, candidate
    # cards (memory.candidate.created/resolved, memory.remembered), project
    # records in memory.list/search/update/forget.
    "learned_memory_v1": True,
    # memory.export / memory.import: portable bundle of user and project
    # records plus forgotten-text suppressions (additive, digest-checked).
    "memory_portability_v1": True,
    # Rinari profiles are workspaces: an active profile (profile_bundle.active /
    # activate), projects and conversations with rinari_profile_id, list
    # filters, project/session move_profile, profile_bundle.update.
    "rinari_profiles_v1": True,
    # checklist.update keeps a live list per conversation: checklist.updated
    # on the turn, settled from the outcome, session.checklist.get/clear.
    "turn_checklist_v1": True,
    # A goal Rinari pursues turn after turn (plan 0.4.0 #23):
    # session.goal.get/set/pause/resume/clear/budget, goal.updated, and the
    # goal turns' origin {kind: user, source: goal, goal_id, iteration}.
    "session_goal_v1": True,
    # Continue by itself when a provider limit resets (plan 0.4.0 #24):
    # session.resume.get/arm/cancel/now, resume.settings.get/set,
    # session.resume.updated, errors carry details.resets_at.
    "session_resume_v1": True,
    # Fallback and auxiliary models (plan 0.4.0 #80): models.roles.get/set,
    # model.fallback on the turn when it switches.
    "model_roles_v1": True,
    # Implement or adjust a finished plan without a user bubble (plan 0.4.0
    # #55): session.turn.start origin {kind: user, source: plan_implement |
    # plan_adjust, plan_turn_id, comment?}; implementing runs in BUILD with
    # the plan's numbered steps as the checklist (reason "plan").
    "plan_turns_v1": True,
    # The conversation's repository for «Resumen» (plan 0.4.0 #82):
    # repo.status (git now, GitHub via repo.status.updated) and
    # repo.pr.create (gh, in the background: repo.pr.created / failed).
    "repo_status_v1": True,
    # session.summary (sources, results, connectors, background) and
    # session.summary.changed, coalesced per session (plan 0.4.0 #82).
    "session_summary_v1": True,
    # A message sent during the turn carries attachments, and while unread it
    # can be edited or withdrawn: session.turn.steer.update / .cancel,
    # steer.updated / steer.cancelled, STEER_ALREADY_READ (plan 0.4.0 #54).
    "steer_edit_v1": True,
    # followup.suggest notes: followup.suggested/resolved on the turn,
    # followup.list/dismiss/accept (accept starts a new conversation).
    "followup_suggestions_v1": True,
    # Plan 0.4.0 #40: sessions carry parent_session_id, lineage_kind and brief.
    "session_lineage_v1": True,
    # Child sessions: open, report, integrate (plan-sesiones-hijas 5).
    "child_sessions_v1": True,
    "advanced_settings_v1": True,
    # network.settings.get/set + network.test: proxy (system | manual | none),
    # certificates (system store via truststore | certifi | file), one factory.
    "network_settings_v1": True,
    # Projects with several working folders: project.create,
    # project.folders.validate, project.folder.add/remove, projectSummary.folders;
    # trusted extra folders count as inside the project for the policy.
    "project_folders_v1": True,
    # projectSummary.git_head: each project's branch read from .git/HEAD
    # (no git subprocess), so project lists can show it.
    "project_git_head_v1": True,
    "project_flow_v1": True,
    # image.* tools and Ajustes → Creación de imágenes (plan 0.4.0 #28).
    "image_creation_v1": True,
    # project.delete: an archived project and everything Rinari keeps about it.
    "project_delete_v1": True,
    # session.pin + sessionSummary.pinned_at (desktop Fijados).
    "session_pins_v1": True,
    # skill.* library: standard skills, reviewed installs, on/off, edit, import.
    "skill_library_v1": True,
    # speech.* local dictation (whisper.cpp): status, settings, model jobs,
    # transcription as a job with speech.transcribed / speech.failed.
    "speech_dictation_v1": True,
    # Near-duplicate gate (SIMILAR_EXISTS), skill.proposed / skill.proposal.resolved
    # chat cards, /lesson and /merge-skills, skill.duplicates.list/dismiss.
    "skill_manager_v1": True,
    # command.list + session.turn.start `command` (slash commands, skills too).
    "slash_commands_v1": True,
    # /learn + skills.propose, pending approvals, revert, auto_learn setting.
    "skill_learning_v1": True,
    # Desktop terminal: ConPTY on Windows, raw keystrokes, pty.shells, tail stream.
    "desktop_terminal_v1": True,
    "scheduled_tasks_v1": True,
    # «Repetir aquí» and time from now: a task's `target` (new_session |
    # this_conversation) with its `session_id`, run status `queued`, queue
    # entries with their `display_message` and origin {kind: schedule,
    # task_name, target}, schedule {kind: once, after: {minutes, hours,
    # days}} resolved to `at` on create, and `next_run_local` from
    # schedule.propose.
    "schedule_target_v1": True,
    # session.turn.steer: a message for the running turn, read after its
    # current step; events steer.applied / steer.returned.
    "turn_steering_v1": True,
    # Permissions v3: profiles cut by what cannot be undone, "allow_project"
    # approvals ("always here"), permission.grants.list/revoke.
    "permissions_v3": True,
    # Remote MCP servers: Streamable HTTP transport, auth (bearer/headers),
    # env/header secrets in the CredentialStore, mcp.update, mcp.probe and the
    # structured mcp.test diagnosis (code, http_status, hint, latency_ms).
    "mcp_remote_v1": True,
    # MCP servers connect in the Engine, in parallel and in the background
    # (connectors plan, C2): mcpServer.status (idle | connecting | connected |
    # reauth_required | error | disabled) and .connection (code, hint,
    # message, tools, resources, attempts, retry_in_s, changed_at), the
    # mcp.status.changed event, mcp.reconnect, tools refreshed on the
    # server's list_changed, and mcp.resources.list/read for the model.
    "mcp_background_connect_v1": True,
    # OAuth for remote MCP servers (plan de conectores C1): auth.kind "oauth",
    # mcp.auth.start/status/cancel/signout, mcp.auth.progress/finished, the
    # `oauth` support block in mcp.test/mcp.probe, hints oauth_required and
    # reauth_required, and MCP 2025-06-18 with negotiation down.
    "mcp_oauth_v1": True,
    # Permissions per connector and per tool (plan de conectores C5): the
    # server's annotations count (readOnlyHint, destructiveHint, openWorldHint),
    # Allow / Ask / Block per tool and per server, corrected annotations,
    # mcp.tools.list/set/set_default, «always» on an MCP tool's approval saves
    # Allow for it (approval.requested grant_scope "mcp_tool"), and the guard
    # of external content treats a connector's own results as the same
    # destination.
    "mcp_tool_permissions_v1": True,
    # Connector catalog and imports (connectors plan, C4): mcp.catalog.list/add
    # (catalog shipped with the Engine, `added` per entry, `next` sign_in |
    # connect), mcp.import.scan/apply (Claude Desktop, Claude Code, Cursor,
    # VS Code, Codex or a pasted JSON; only marked servers are created),
    # mcp.project_offers.list/decide (a trusted folder's .mcp.json asked once
    # per server), mcpServer.project_root and url_secret.
    "mcp_catalog_v1": True,
    # Rinari Agency A1: agents with a card (role, instructions, model,
    # permission mode, connectors, daily budget in USD or tokens) and a direct
    # chat (a CHAT session with agent_id, left out of session.list unless
    # asked for by agent_id). agency.agent.list/get/create/update/archive,
    # agency.status (what the Agency card says), agency.agent.changed and
    # usage.report scope "agent" (today's spend over its conversations).
    "agency_agents_v1": True,
    # «Créalo con Rinari»: agency.agent.draft answers {status, job_id} at once
    # and the draft (name, role, instructions, permission_profile) arrives as
    # agency.draft.ready, or agency.draft.failed with a plain message. It uses
    # the owner's default model and creates nothing.
    "agency_draft_v1": True,
    # «Probar» a proposed rule: schedule.rehearse answers {status, job_id} and
    # schedule.rehearsed says what it would do (nothing_new, condition_not_met,
    # would_run with the delivery it would send, blocked, not_rehearsable when
    # its first tool may change something, failed). Nothing is ever sent.
    "agency_rule_rehearsal_v1": True,
    # «Detener a todos»: agency.stop_all cancels the turns of every agent of the
    # profile and pauses their active rules; event agency.stopped.
    "agency_stop_all_v1": True,
    # agency.secret_pasted {session_id}: the owner sent what looks like a key in
    # an agent's chat (memory's credential patterns and the history redactor's).
    # The text is never in the event.
    "agency_secret_warning_v1": True,
    # An agent's own memory in the Agency settings (D-24):
    # agency.agent.memory.list/add; memory.update/forget edit and forget it, and
    # its place_label is the agent's name.
    "agency_agent_memory_v1": True,
    # Rinari Agency A2: teams with posts (sessions with agent_id and team_id), a
    # chief, team connectors and accounts (owner team:ID, also from
    # mcp.catalog.add), team memory (place team, agency.team.memory.*), the
    # office chat (agency.office.*: only @mentions wake, 3 hops, 5
    # mentions, 20 deliveries per chain), errands as the board's queue, one per
    # post at a time (agency.errand.*), team and global budgets
    # (agency.settings.*), a blocked errand waits until agency.errand.retry,
    # templates without accounts (agency.team.template / duplicate),
    # events agency.team.changed, agency.office.message
    # and agency.chain.stopped, and the post tools office.say, errand.create,
    # errand.review and board.list.
    "agency_teams_v1": True,
    # D-29 / D-30: a post's prompt names its team, `team.members` says who is
    # free, `office.wait` parks a mention until a teammate posts in the chain
    # (agency.chain.stopped reasons wait_expired and wait_cancelled), an
    # errand's prompt carries the office, and an agent's direct chat can leave
    # an errand to a team and is told there when it is delivered.
    "agency_team_awareness_v1": True,
    # The board's caps after review: 3 office.say per turn, 3 returns per
    # errand, mentions count for the 20 deliveries of a chain, the Engine's
    # notices (office message `system` + `notice`, worded by the app) never
    # release a wait, «Detener a todos» halts the board until agency.resume
    # (or an owner's message, errand or retry), and agency.chain.stopped
    # reasons too_many_returns, no_chief and could_not_start.
    "agency_board_v2": True,
    # Agency audit (2026-10-21): 8 hops per chain (D-31); a stopped chain
    # leaves an office message (system, author_kind "system", notice {kind:
    # stopped, reason, detail?, chain_id}) and agency.chain.stopped carries
    # message_id; agency.chain.dismiss {team_id, message_id? | chain_id?};
    # office.say answers woken / not_woken (reasons too_many_hops,
    # too_many_mentions, already_woken, stopped_by_owner,
    # not_in_team, unknown_name, ambiguous_name with candidates) and an @ that
    # starts exactly one teammate's name wakes them; a delivery wakes only the
    # agent that asked (a review turn in its post, origin agency_delivery);
    # office notice kind "left" {agent, chief}; agency.status session_ids,
    # stopped and waiting_for_owner {errands, chains}; agency.resume restores
    # what the stop blocked and paused (restored_errands, resumed_rules);
    # agency.agent.archive leaves its teams and pauses its rules (left_teams,
    # paused_rules), agency.agent.unarchive, agency.agent.list {archived};
    # agency.agent.get sessions carry agency {kind: direct | post |
    # former_post | rule_run | child | other, ...}; agent card soul_id (D-34,
    # an agent never speaks with Rinari's Soul); posts see the agent's
    # connectors and accounts plus the team's (D-33); schedule task
    # deliver_to {kind: office, team_id} | {kind: agent, agent_id, team_id}
    # and paused_by; agency.templates.list {language?} and
    # agency.team.from_template {template_id, language?, model?, name?}; the
    # direct chat's office.say {team, text}; an agent's conversations never
    # reach the Engine's home (policy rule engine_home); office.wait inside an
    # errand (it stays in progress; reasons wait_expired / wait_cancelled block
    # it); a delivery is posted whole; what a post presents with file.present
    # leaves an office notice {kind: files, items: [{uri | url, name, kind?,
    # mime?, caption?, size_bytes?}]}.
    # Since 2026-10-22 a chain is no longer cut at 20 deliveries: hops (8),
    # one queued turn per agent and chain, 5 mentions per message and the
    # budgets bound it (too_many_deliveries only appears in older notices).
    # One team's life (22-oct): agency.team.stop / agency.team.resume (just
    # that team, as stop_all does; the owner writing in its office resumes it;
    # agency.team.list says `stopped`), agency.team.archive also cuts its turns
    # and pauses the rules that work for it (paused_rules), agency.team.unarchive
    # resumes them, agency.team.list {archived: true}, and agency.team.delete /
    # agency.agent.delete remove an archived team or agent with its
    # conversations, rules and memory.
    "agency_audit_v1": True,
    # How a team works on its own (replan 22-oct, phases 2 to 4): the team's
    # `manual` (agency.team.update patch, read by every post); routines
    # (agency.routine.list / agency.routine.create {team_id, agent_id, name,
    # schedule, prompt}: an agent's rule in its post); flows
    # (agency.flow.list / save {team_id, flow: {id?, name, description, steps:
    # [{name, agents, instructions, approval: auto | chief | owner}]}} / delete /
    # start {team_id, flow_id, input} / agency.flow.run.cancel {run_id}; an
    # errand of a run carries production_run_id and flow_step; office notices
    # {kind: flow, state: started | step | done, flow, step?, run_id}); and
    # agency.team.overview {team_id}: members, routines, flows and runs, the
    # board's counts, recent activity and spend today and over 7 days.
    "agency_team_work_v1": True,
    # A scheduled task's `notify` (all | problems | none, default all): how much
    # its runs notify the owner; every schedule.run.* event and the run's turn
    # origin carry it (owner, 22-oct).
    "schedule_notify_v1": True,
    # soul.settings.get/set: character intensity (minimal | balanced | full),
    # separate from Soul selection; bundled default Soul 4.0.
    "soul_character_intensity_v1": True,
}


def home_id(home) -> str:
    """Stable identity of an Engine home: a digest of its resolved path.

    It changes only when the home moves; `engine_instance_id` changes on every
    start. Clients use it to namespace presentation state (layouts, anchors)
    so two homes never share it. Not a secret and not reversible in practice.
    """
    from pathlib import Path

    resolved = str(Path(home).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
