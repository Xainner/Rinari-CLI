---
name: code-review
description: Review code changes for bugs, security issues, contract violations, and maintainability (read-only by default).
version: 1.0.0
risk: low
can_delegate: true
required_tools:
  - git.diff
  - fs.read
  - search.regex
optional_tools:
  - git.log
  - git.show
triggers:
  - review
  - diff
  - pull request

---
# Procedure
1. Bound the review: diff range (worktree vs base) and the intent of the change.
2. Read every hunk in context: call the changed function, trace how the new code is reached, check error paths.
3. Check the invariants the codebase depends on: permissions, secret handling, migrations, provider/model persistence, session semantics.
4. Look for: logic errors, unhandled error classes, race/async hazards, injection surfaces, scope creep (changes not needed by the diff).
5. Report findings ranked by severity with file:line references and a concrete reproduction or counterexample for each.

# Verification
- Every reported finding cites the exact code location and the failure it enables.
- No style-only nitpicks are reported as issues.

# Failure handling
- The diff is too large or spans too many concerns: review what is reviewable and say clearly what was skipped.

# Success criteria
- A severity-ranked list of real issues (or an explicit clean bill with the risks checked), each with location and evidence.
