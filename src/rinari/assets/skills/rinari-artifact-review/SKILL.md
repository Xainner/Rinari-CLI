---
name: rinari-artifact-review
description: Independently review generated documents and decks for content, design, preservation and the real scope of the evidence.
version: 1.0.0
risk: low
can_delegate: false
required_tools:
  - documents.capabilities
  - documents.inspect
  - documents.read
  - documents.render
  - documents.validate
  - documents.review
  - skills.read
optional_tools:
  - documents.diff
  - documents.job.get
triggers:
  - revisar documento generado
  - revisar presentación
  - verificar presentación
  - auditar documento
  - control de calidad documental
  - review the deck
---
# Procedure
1. Take the goal, the exact revision (`rev_…`), the acceptance criteria and the sources you were given. Read `references/checklists.md` and `references/reporting.md` (`skills.read`).
2. `documents.validate` with the required texts as `expected_text`; it reports structure, layout, content and preservation with evidence.
3. `documents.render` that revision and look at every page with `show`. Never judge from renders of another revision.
4. Separate content, data, preservation and design problems and locate each one by page/slide and shape.
5. Record the review with `documents.review` (pages seen and findings with severity, message and fix). Do not edit or finalize: return the report to whoever asked.
6. State what you did not review and which renderer was used.

# Verification
- Every conclusion has a scope and evidence; a file that opens is not a reviewed file.
- A visual review does not validate figures against sources; say which figures you could check and against what.

# Failure handling
- No renderer: report structure and content only, and that the visual review was not possible.
- Without sources, check internal consistency only and say so.
- If the revision changed while you reviewed, start again on the new one.

# Success criteria
- The report gives actionable, located findings and the real coverage, with no approval that the evidence does not support.
