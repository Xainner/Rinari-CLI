# Rinari CLI by task

Global: `--json` emits the machine envelope; `rinari help <command>` shows options. In the desktop the `rinari` command may not be on PATH: prefer the `rinari.*` tools for state questions.

| Task | Command |
|---|---|
| Start or continue a chat | `rinari chat`, `rinari resume <session>` |
| One-shot tasks | `rinari ask`, `rinari plan`, `rinari review`, `rinari agent`, `rinari run` |
| Stop the running turn | `rinari stop` |
| Sessions | `rinari session list \| show <id> \| current \| fork \| rename \| archive \| delete \| export \| import` |
| Events of a session | `rinari logs show \| tail \| search \| export <session>`, `rinari trace` |
| Providers and models | `rinari providers …`, `rinari provider`, `rinari models list \| available \| refresh \| add \| alias \| show \| test \| remove`, `rinari model` |
| Context | `rinari context settings [--model M --window N \| --threshold P \| --summarizer M]` (`--window 0` restores automatic), `rinari context compact`, `pins`, `retrieve` |
| Skills | `rinari skills list \| search \| show \| activate \| deactivate \| install \| validate \| path` |
| Secrets | `rinari secrets …` (values never shown), `rinari secrets cleanup --apply` for a full OS vault |
| Health | `rinari status`, `rinari doctor` (never mutates), `rinari version` |
| Project | `rinari init`, `rinari project`, `rinari trust`, `rinari index`, `rinari tasks`, `rinari flow` |
| Changes | `rinari checkpoint`, `rinari undo` |
| Extensions | `rinari mcp`, `rinari plugins`, `rinari api`, `rinari hooks`, `rinari agents`, `rinari profiles` |
| Policy | `rinari permissions`, `rinari approvals`, `rinari sandbox`, `rinari network` |
| Desktop | `rinari desktop .` opens the current folder in Rinari Agent (`rinari code` is an alias) |
| Evaluations | `rinari eval` |
