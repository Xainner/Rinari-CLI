---
name: refactor
description: Restructure code without changing behavior: small steps, tests first, semantic identity verified.
version: 1.0.0
risk: high
can_delegate: false
required_tools:
  - fs.read
  - fs.write
  - shell.exec
  - search.regex
  - search.references
optional_tools:
  - git.diff
  - git.status
triggers:
  - refactor
  - rename
  - clean up

---
# Procedure
1. Freeze the expected behavior: run the existing tests for the target area and record the green baseline. If coverage is absent, add characterization tests before touching code.
2. Name the refactor and its goal (a single sentence); define what is explicitly NOT in scope.
3. Move in small steps, one logical edit per step (rename, extract, move, deduplicate). Each step keeps the suite green.
4. Use search.references before moving or renaming anything public; update every call site.
5. Verify: full baseline suite passes, diff inspected for accidental behavior changes, lint/typecheck clean.

# Verification
- Behavior is identical: the same tests pass before and after (plus any new characterization tests); the diff contains no logic changes outside the reshape.

# Failure handling
- A step turns the suite red: revert that step (it was not small enough) and split it further. Never proceed over a red suite.

# Success criteria
- The stated reshape achieved with zero behavior change, proven by an unchanged (or extended) green suite.
