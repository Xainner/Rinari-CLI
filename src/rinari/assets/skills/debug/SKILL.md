---
name: debug
description: Diagnose unknown runtime behavior: instrument, bisect, hypothesize, confirm.
version: 1.0.0
risk: medium
can_delegate: true
required_tools:
  - shell.exec
  - fs.read
  - fs.search_text
optional_tools:
  - process.start
  - process.output
  - git.log
triggers:
  - why is
  - strange behavior
  - crash
  - unexpected

---
# Procedure
1. Characterize the symptom precisely: inputs, expected vs actual, frequency, environment.
2. Form 2-3 competing hypotheses ranked by likelihood; check the cheapest one first.
3. Instrument or bisect to isolate the failing boundary (module, call, commit). Prefer deterministic probes over log spam.
4. Confirm the hypothesis with a minimal experiment; only then explain the mechanism.
5. Fix or hand off with the confirmed mechanism and the minimal repro.

# Verification
- A single experiment demonstrates the mechanism (flip one variable and the behavior changes as predicted).

# Failure handling
- Hypotheses exhausted: report the isolated boundary, the evidence for/against each hypothesis, and what observation would discriminate the next one.

# Success criteria
- A confirmed root-cause mechanism (not a guess) plus either a fix or a precise, reproducible handoff.
