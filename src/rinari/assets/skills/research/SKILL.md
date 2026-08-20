---
name: research
description: Answer a technical question with cited evidence: web research, local docs, and a synthesized answer.
version: 1.0.0
risk: low
can_delegate: true
required_tools:
  - web.search
  - web.fetch
optional_tools:
  - web.cite
  - fs.search_text
triggers:
  - how do
  - best way
  - what is the state of

---
# Procedure
1. Decompose the question into 2-4 answerable sub-questions.
2. Search and fetch focused sources; prefer primary sources (spec, official docs, repo) over aggregators.
3. Cross-check at least two independent sources per factual claim; record citations (title, URL, snippet, fetched_at).
4. Synthesize: direct answer first, then the supporting evidence, then conflicts or gaps found between sources.
5. Mark inference vs observed: every statement is either cited or explicitly labeled as your inference.

# Verification
- Every load-bearing claim carries a citation; the citations were actually fetched (not invented).

# Failure handling
- Sources conflict: present both with citations and the criterion to decide; do not silently pick one.
- The question is out of scope for web research (e.g., requires a running system): say so and list what observation would answer it.

# Success criteria
- A direct, citation-backed answer where claims are separated from inferences.
