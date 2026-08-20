---
name: implement-feature
description: Implement a requested feature end to end: plan minimal slices, code with existing conventions, add tests, verify.
version: 1.0.0
risk: medium
can_delegate: false
required_tools:
  - fs.read
  - fs.write
  - fs.search_text
  - search.regex
  - shell.exec
optional_tools:
  - git.diff
  - verify.record
  - search.symbols
triggers:
  - implement
  - add feature
  - new command

---
# Procedure
1. Restate the requirement and its done-when in one sentence; if acceptance criteria are missing, state your interpretation before coding.
2. Explore the existing pattern to copy: find the closest existing feature/command/tool and read how it is wired (registration, config, tests).
3. Plan the minimal slice: files to touch, interfaces created, edge cases. Prefer editing existing modules over new abstractions.
4. Implement with the project's conventions (naming, error handling, logging). No TODOs left behind without a reason.
5. Add or extend tests for the new path (deterministic, offline fakes).
6. Verify: run the targeted tests, then lint/typecheck, and record the evidence.

# Verification
- New/changed tests run and pass; affected module tests pass; lint/typecheck clean.
- The feature is reachable through the public surface (CLI entry point, API, config) per the docs it updates.

# Failure handling
- A failing test you cannot fix: stop, report the exact failure and what was tried (never delete or skip a test to go green).
- The requirement conflicts with a documented constraint: report the conflict with references and propose the closest satisfying alternative.

# Success criteria
- Behavior implemented per the requirement + tests proving it + validation evidence + docs updated where behavior changed.
