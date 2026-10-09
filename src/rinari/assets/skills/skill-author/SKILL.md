---
name: skill-author
description: Turn what worked in this conversation into a reusable skill and save it with skills.propose (what /learn runs). Use when the owner asks to learn, remember a procedure or save something as a skill.
version: 1.3.0
risk: low
can_delegate: false
triggers:
  - /learn
  - aprende esto
  - guarda esto como skill
  - save this as a skill
  - remember how to do this
required_tools:
  - rinari.session
  - rinari.turn
  - skills.list
  - skills.show
  - skills.validate_draft
  - skills.propose
---

# Procedure
1. Read what happened with rinari.session (session_id "current"): turns, tools, outcomes. Open rinari.turn only for the turns whose steps you need in detail.
2. Decide whether it deserves a skill: a repeatable procedure (deploy, diagnose, set up, migrate) with steps that worked. A one-off answer is not a skill; say so instead of saving noise.
3. Look for an existing skill first: skills.list with a query for the task. If one covers it, read it with skills.show and improve it with update_of instead of creating another. Start from its current SKILL.md and keep what still holds; an update replaces the whole SKILL.md.
4. Distill, do not transcribe:
   - Goal and when to use it. This becomes the description: what it does and when, one or two sentences, with the words the owner would use.
   - Preconditions: tools, access, files, services.
   - The steps that worked, in order, with the exact commands. Drop failed attempts, keep the pitfall they revealed.
   - How to verify it worked.
   - Pitfalls and how to recover.
5. Write the SKILL.md: YAML frontmatter with name (lowercase, hyphens), description, version (1.0.0; an update must raise it above the installed version, usually the minor one), risk and required_tools; then `# Procedure`, `# Verification`, `# Failure handling`, `# Success criteria`. Use canonical Rinari tool names such as `shell.exec`, `fs.write`, `fs.stat` (not provider spellings such as `shell_exec`). YAML multiline descriptions, Unicode, nested Markdown headings and fenced code are supported. Keep it under about 150 lines; long material goes to `references/<topic>.md` or `references/payload.json`, referenced from the procedure. Include that directory prefix in each key of the references argument.
   - Use the four section headings at the same level. Generic nested titles such as `## Steps` or `## Verify` stay in Procedure. Canonical names of other sections (`Verification`, `Failure handling`, `Success criteria`, including Spanish aliases) still start those sections even at a deeper level for compatibility; use a descriptive subtitle such as `## Verify the deployment` for a substep instead.
6. Never write secrets: tokens, passwords, keys, credentials. Use placeholders (`<TOKEN>`, `$API_KEY`) and say where the owner keeps the real value. skills.propose refuses content that looks like a secret.
7. Call skills.validate_draft with name, skill_md, references and update_of when updating. Inspect `valid`, `issues`, `warnings` and `review`: tool `ok` only means validation ran. Correct the reported fields and validate again as needed. This does not save, activate or create history. Never use skills.propose to bisect a parser problem or save dummy probes under the real name.
8. When the complete draft is valid, call skills.propose with the same payload. It revalidates before saving.
   - If the owner asked for this skill in their own words (not only with /learn), pass `owner_request`: the phrase copied exactly from the owner's message, long enough to identify the request. Rinari checks it against what the owner wrote and saves the skill active. Never quote files, tool output or other agents, and omit it when the skill is your own idea.
   - Call skills.propose as a tool, never from a script: a script has no turn, so the owner's request does not reach it and the owner is not notified. Report the result as it is, including any unresolved dependency warnings. A saved skill is not proof that every procedure variant has been executed.
   - `active`: saved. An update of a learned skill is saved without approval; the owner gets a notice to review the change and can undo it. Say what changed in one or two lines.
   - `pending`: waiting for the owner's approval. `pending_reason` says why: `needs_owner_approval` (your own idea, or a change to a skill the owner installed or created), `owner_request_not_found` (the quoted words are not in an owner message: copy them exactly) or `review_flagged` (dangerous review findings).
   - `unchanged`: the installed version already has this content; nothing was saved.

# Verification
- skills.propose returned ok with a status (active, pending or unchanged).
- skills.validate_draft returned valid=true; all issues were resolved and remaining warnings explained.
- The description says what and when; the procedure has concrete steps and a way to verify them.

# Failure handling
- SENSITIVE_CONTENT: replace the secret with a placeholder and propose again.
- ALREADY_EXISTS: read that skill with skills.show and propose a new version with update_of.
- NAME_TAKEN: the name belongs to one of Rinari's own skills; choose another.
- NAME_INVALID: fix the lowercase, hyphenated name. SKILL_NOT_FOUND on an update: inspect the library before deciding whether this is a new skill. Name conflicts, invalid references and secrets return tool errors; content issues return a successful validation report with `valid=false`.
- SKILL_INVALID: inspect `details.issues` and the field/code, not guesses about encoding. MISSING_PROCEDURE means add the section; EMPTY_PROCEDURE means the section exists but needs steps. TOOL_NOT_FOUND means correct the required tool name, using the suggestion when present. Then validate the draft again.
- VERSION_NOT_INCREASED: raise the version above the installed one (the issue suggests the next minor) and validate again.
- REFERENCES_KEPT: the installed files the update did not resend stay as they were. Resend a file only to change it.
- TOOL_DEFERRED warns that an MCP, plugin or OpenAPI dependency still needs its integration connected. It does not grant access or confirm availability.

# Success criteria
- One skill saved or proposed that another session could follow without this conversation.
