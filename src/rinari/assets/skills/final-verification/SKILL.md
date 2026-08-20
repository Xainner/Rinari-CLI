---
name: final-verification
description: Close a task with an evidenced completion decision against its done-when contract.
version: 1.0.0
risk: low
can_delegate: false
required_tools:
  - shell.exec
  - fs.read
optional_tools:
  - verify.evaluate
  - verify.record
  - git.diff
triggers:
  - is it done
  - verify completion
  - close out

---
# Procedure
1. Restate the done-when contract for the task (acceptance + validation list). If none exists, derive it from the request and say it is derived.
2. For each item, gather or re-run the evidence: targeted tests, lint/typecheck, the feature exercised through its public surface. Re-verify volatile facts; do not trust earlier session memory.
3. Inspect the final diff for accidental scope creep and missing doc updates.
4. Decide: DONE, IMPLEMENTED_UNVERIFIED, PARTIAL, BLOCKED, or FAILED — with the exact reason list; a missing evidence item forces the lower grade.
5. Report the decision with the evidence table (item -> command/status) and the residual risks.

# Verification
- Every evidence row corresponds to a command that was actually run in this session with a status.
- No item is marked satisfied by inference alone.

# Failure handling
- An evidence item cannot run (missing dependency, no network): mark the task with the lower grade and name the blocker; never upgrade the grade to close the loop.

# Success criteria
- An unambiguous completion grade backed by an evidence table, or an explicit blocker list.
