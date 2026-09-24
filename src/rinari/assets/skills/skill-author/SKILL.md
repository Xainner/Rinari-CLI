---
name: skill-author
description: Turn what worked in this conversation into a reusable skill and save it with skills.propose (what /learn runs). Use when the owner asks to learn, remember a procedure or save something as a skill.
version: 1.0.0
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
  - skills.propose
---

# Procedure
1. Read what happened with rinari.session (session_id "current"): turns, tools, outcomes. Open rinari.turn only for the turns whose steps you need in detail.
2. Decide whether it deserves a skill: a repeatable procedure (deploy, diagnose, set up, migrate) with steps that worked. A one-off answer is not a skill; say so instead of saving noise.
3. Look for an existing skill first: skills.list with a query for the task. If one covers it, read it with skills.show and improve it with update_of instead of creating another.
4. Distill, do not transcribe:
   - Goal and when to use it. This becomes the description: what it does and when, one or two sentences, with the words the owner would use.
   - Preconditions: tools, access, files, services.
   - The steps that worked, in order, with the exact commands. Drop failed attempts, keep the pitfall they revealed.
   - How to verify it worked.
   - Pitfalls and how to recover.
5. Write the SKILL.md: frontmatter with name (lowercase, hyphens), description, version (1.0.0; bump the minor version on an update), risk and required_tools (the Rinari tools the steps use); then `# Procedure`, `# Verification`, `# Failure handling`, `# Success criteria`. Keep it under about 150 lines; long material (sample output, full configs) goes to `references/<topic>.md`, referenced from the procedure.
6. Never write secrets: tokens, passwords, keys, credentials. Use placeholders (`<TOKEN>`, `$API_KEY`) and say where the owner keeps the real value. skills.propose refuses content that looks like a secret.
7. Call skills.propose once with name, skill_md and references. Report the result as it is: saved, or waiting for the owner's approval.

# Verification
- skills.propose returned ok with a status (active or pending).
- The description says what and when; the procedure has concrete steps and a way to verify them.

# Failure handling
- SENSITIVE_CONTENT: replace the secret with a placeholder and propose again.
- ALREADY_EXISTS: read that skill with skills.show and propose a new version with update_of.
- NAME_TAKEN: the name belongs to one of Rinari's own skills; choose another.
- SKILL_INVALID: fix what the message says (missing description or procedure, name mismatch).

# Success criteria
- One skill saved or proposed that another session could follow without this conversation.
