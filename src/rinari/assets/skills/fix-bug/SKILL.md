---
name: fix-bug
description: Reproduce, root-cause, and fix a defect with a regression test.
version: 1.0.0
risk: medium
can_delegate: false
required_tools:
  - fs.read
  - fs.search_text
  - shell.exec
  - search.regex
optional_tools:
  - git.log
  - git.diff
  - verify.record
triggers:
  - bug
  - failing
  - broken
  - regression

---
# Procedure
1. Obtain concrete evidence first: the exact error, command, and input that reproduce it. No reproduction, no fix claim.
2. Write a minimal failing reproduction (script or test) before touching the fix.
3. Trace from the failure site backward: find the minimal root cause, not just the nearest symptom.
4. Apply the narrowest fix that removes the root cause; do not refactor adjacent code in the same change unless required.
5. Turn the reproduction into a permanent regression test.
6. Verify: the regression test passes, surrounding tests pass, lint/typecheck clean.

# Verification
- The original repro now passes; the new regression test fails without the fix (revert-check when practical); the module test suite is green.

# Failure handling
- Cannot reproduce: state exactly what was tried; classify as environment-dependent and give the evidence (versions, commands).
- Root cause is in third-party code: report it with the minimal failing case instead of patching around it silently.

# Success criteria
- Fixed at the root cause, regression test in place, and evidence that prior behavior is preserved (no collateral changes in the diff).
