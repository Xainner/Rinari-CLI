---
name: fix-ci
description: Diagnose and repair failing CI checks (test failures, lint, type errors, broken pipelines).
version: 1.0.0
risk: medium
can_delegate: false
required_tools:
  - shell.exec
  - fs.read
  - git.status
  - git.diff
optional_tools:
  - git.log
triggers:
  - ci
  - pipeline
  - build failing

---
# Procedure
1. Read the failure list in order; separate flaky/infrastructure failures from real code failures (evidence: does it fail locally with the same command?).
2. For each real failure, reproduce locally with the exact CI command; if no CI runner config exists in the repo, reconstruct it from project config and say so.
3. Fix root causes (fix-bug flow). Prefer fixing the code over editing expected values; if the expectation was intentionally outdated, update it with a reason.
4. Re-run the failed suites, then the full gate (tests + lint + typecheck) to confirm the pipeline would pass.

# Verification
- The exact CI commands now pass locally; the failing checks are gone, not suppressed.

# Failure handling
- A failure depends on CI-only state (secrets, runners, network): report exactly what is missing and stop; never fake a green run.

# Success criteria
- Every enumerated failing check fixed or explicitly classified as out of local control, with the final gate run as evidence.
