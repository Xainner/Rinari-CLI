---
name: repository-explore
description: Map an unfamiliar repository: layout, entry points, build/test commands, key abstractions.
version: 1.0.0
risk: low
can_delegate: true
required_tools:
  - fs.list
  - fs.read
  - fs.glob
  - search.files
  - search.regex
optional_tools:
  - search.symbols
  - git.log
triggers:
  - new repository
  - get oriented
  - where is

---
# Procedure
1. List the top two levels of the tree; identify language(s), package config (pyproject.toml, package.json, go.mod, Cargo.toml, ...), README, and docs.
2. Read the README and any AGENTS.md / CONTRIBUTING to learn the declared commands (build, test, lint, run).
3. Locate entry points (main modules, CLI entry, server bootstrap) and the 2-3 core abstractions around them.
4. Check git log (last 10) for recent direction and convention signals.

# Verification
- You can name the entry point path and the exact commands to build/test/lint from project config (not from guesses).
- Every claim in the summary points to a file you actually read.

# Failure handling
- Monorepo or multiple languages: split the scope per language and say which part was not explored.
- No README: say so, and list the config files used as evidence.

# Success criteria
- A concise map: layout, entry points, build/test/lint commands with their source of truth, and the core abstractions to study next.
