---
name: test
description: Write and maintain deterministic tests for a target: unit seams, fakes, fixtures, coverage of edge cases.
version: 1.0.0
risk: low
can_delegate: false
required_tools:
  - fs.read
  - fs.write
  - shell.exec
  - search.regex
optional_tools:
  - search.symbols
triggers:
  - write tests
  - add coverage
  - test this

---
# Procedure
1. Identify the public contract of the target (inputs, outputs, error cases, invariants) from its type signatures and docs.
2. Find the existing test conventions: test location, fixtures, fakes, naming. Copy them; do not introduce a new pattern silently.
3. Choose the seam: fake providers/transports, temp dirs, isolated git repos, scripted model responses. Keep tests network-isolated by default.
4. Cover: the happy path, the documented error classes, and the boundary cases (empty, max, unicode, unicode paths).
5. Run: targeted tests first, then the module suite, then lint.

# Verification
- All new tests pass; they fail when the target behavior is broken (mutation-check the most important one when practical).
- No test requires the public internet or a running service.

# Failure handling
- Flaky test: find the source of nondeterminism (time, order, network); a flaky test is a bug in the test until proven otherwise.

# Success criteria
- Deterministic, convention-conforming tests that pin the contract, with the run evidence.
