# Skill authoring: regression evidence

The reported `/learn` session repeatedly received `MISSING_PROCEDURE` for a
document containing `# Procedure` followed immediately by `## Preparación`.
The old extractor reset its active section for every unknown heading. Flattening
YAML or removing Unicode did not address that cause. Diagnostic proposals were
also saved as active skills because `/learn` intentionally saves immediately.

## Reproduce and verify

Run in an isolated checkout with `uv sync --frozen`:

```console
uv run pytest tests/unit/test_skill_authoring.py tests/unit/test_skill_learning.py tests/unit/test_skill_library.py tests/unit/test_skills_runtime.py tests/unit/test_skill_reconciliation.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/unit tests/integration tests/e2e -q
uv build
```

The initial authoring regression suite on the unmodified base produced 20
failures and 6 passes: nested sections/code were lost, diagnostics lacked their
fields, and the draft-validation API did not exist. The expanded skill suite
after implementation produced **91 passes and one platform-dependent skip** on
Windows/Python 3.12. Broader suite and cross-platform CI results are recorded on
the review PR; they are separate from this targeted evidence.

| Case | Evidence |
| --- | --- |
| Nested headings H2–H6, with/without an introduction | Every step survives extraction; no false missing-procedure error. |
| Backtick/tilde fences, longer fences containing shorter ones, indented code | Literal `#` comments and apparent section headings remain instructions. |
| YAML block descriptions/lists, Unicode, LF/CRLF | Metadata and instructions retain their values. |
| Spanish and mixed-level named sections | Existing library regression continues to pass. Recognized section names remain explicit boundaries. |
| Standard skills and empty named Procedure | Standard body fallback remains; named empty sections cannot bypass validation. |
| Repeated draft checks and update checks | Library bytes, pending proposals, history, records and learned notifications stay unchanged. |
| Bad references and secrets | Draft checks and actual proposals refuse the same input without installing it. |
| Unknown required tools | Underscore transport spellings get unambiguous canonical suggestions; invalid drafts cannot be published. |
| Dynamic integrations | Nonblocking availability warnings survive through the saved result. |
| Name/description errors and changes after validation | Proposal rechecks; invalid updates preserve the previous version. |
| Model-facing error serialization | `skill_code`, issue codes, fields and suggestions are available as structured data. |
| Real Engine `/learn` path with a deterministic model | Validate first, assert nothing was saved, then propose; normal activation/event/undo behavior remains. |

An additional local read-only replay loaded all 11 original proposal documents
through the corrected parser. All retained nonempty procedures. Invalid required
tool names were still reported, rather than silently accepted. The original
private conversation and document contents are not fixtures in this repository;
the permanent tests use synthetic equivalents of their relevant structure.

## Compatibility and limits

No database migration or second skill implementation is introduced. Draft
validation uses private temporary files and shares parsing, reference checks,
secret checks and static review with proposals. It is not an approval token;
proposals revalidate, and dangerous content still waits for approval.

New proposals now reject static issues that used to be silently filtered out,
including invalid built-in tool names and descriptions over 1024 characters.
Installed skills, previous probe history and user memories are not automatically
rewritten. Static validation does not execute external procedures or establish
that every video mode, server configuration or dynamic integration works.
