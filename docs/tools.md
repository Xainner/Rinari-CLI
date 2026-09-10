# Rinari CLI — Tools Catalog

> Catálogo maestro de tools para un agente CLI generalista.  
> Las tools son capacidades atómicas o casi-atómicas que el runtime puede exponer al modelo.

---

## 1. Runtime & System

### System Information
- `system.info`
- `system.os`
- `system.arch`
- `system.hostname`
- `system.user`
- `system.cwd`
- `system.uptime`
- `system.resources`
- `system.cpu`
- `system.memory`
- `system.disk_usage`
- `system.which`
- `system.capabilities`

### Environment
- `env.get`
- `env.list`
- `env.set`
- `env.unset`
- `env.expand`

### Process Management
- `process.spawn`
- `process.exec`
- `process.list`
- `process.inspect`
- `process.wait`
- `process.signal`
- `process.kill`
- `process.stdin`
- `process.stdout`
- `process.stderr`
- `process.attach`
- `process.detach`

### Terminal / PTY
- `pty.start` — arranca un comando bajo un pseudo-terminal (TTY-aware).
- `pty.read` — lee la salida (espera hasta `timeout_s`).
- `pty.write` — escribe keystrokes (añade newline si falta).
- `pty.resize` — cambia rows/columns.
- `pty.terminate` — termina el grupo de procesos (TERM→KILL).

Implementación (fase 3): POSIX pty pair (`os.openpty`). En plataformas sin
PTY (Windows) `pty.start` devuelve `DEPENDENCY_ERROR` señalando `process.*`
(mismo patrón que el fallback de LSP). Registry por sesión en ToolContext.

### Shell
- `shell.exec`
- `shell.exec_stream`
- `shell.script`
- `shell.which`
- `shell.history`

---

## 2. Filesystem

### Read
- `fs.read`
- `fs.read_lines`
- `fs.read_binary`
- `fs.head`
- `fs.tail`
- `fs.stat`
- `fs.exists`

### Write
- `fs.write`
- `fs.write_binary`
- `fs.append`
- `fs.touch`
- `fs.truncate`

### Edit
- `fs.patch`
- `fs.replace`
- `fs.insert`
- `fs.apply_diff`

### Navigation
- `fs.list`
- `fs.tree`
- `fs.glob`
- `fs.walk`

### Search
- `fs.search_text`
- `fs.search_regex`
- `fs.search_files`
- `fs.find`
- `fs.locate`

### File Operations
- `fs.mkdir`
- `fs.copy`
- `fs.move`
- `fs.rename`
- `fs.delete`
- `fs.restore`
- `fs.chmod`
- `fs.chown`
- `fs.symlink`
- `fs.readlink`

### Compare
- `fs.diff`
- `fs.hash`
- `fs.compare`

### Watch
- `fs.watch`
- `fs.unwatch`

---

## 3. Search, Indexing & Retrieval

### Text Search
- `search.text`
- `search.regex`
- `search.fuzzy`
- `search.files`
- `search.symbol`
- `search.references`
- `search.definition`

### Semantic Search
- `search.semantic`
- `search.hybrid`
- `search.rerank`

### Indexes
- `index.create`
- `index.update`
- `index.delete`
- `index.status`
- `index.query`
- `index.rebuild`

### Embeddings
- `embedding.create`
- `embedding.batch`
- `embedding.compare`

---

## 4. Artifacts

### Artifact Lifecycle
- `artifact.create`
- `artifact.open`
- `artifact.read`
- `artifact.write`
- `artifact.update`
- `artifact.delete`
- `artifact.list`
- `artifact.metadata`

### Artifact Search
- `artifact.search`
- `artifact.find`
- `artifact.slice`

### Artifact Transfer
- `artifact.import`
- `artifact.export`
- `artifact.download`
- `artifact.upload`

### Artifact Conversion
- `artifact.convert`
- `artifact.render`
- `artifact.preview`

---

## 5. Web Search & Retrieval

### Search
- `web.search`
- `web.search_news`
- `web.search_images`
- `web.search_videos`
- `web.search_academic`
- `web.search_code`

### Fetch
- `web.fetch`
- `web.open`
- `web.reload`
- `web.download`

### Navigation
- `web.links`
- `web.click`
- `web.back`
- `web.forward`

### Content Extraction
- `web.extract_text`
- `web.extract_markdown`
- `web.extract_metadata`
- `web.extract_tables`
- `web.find`

### Provenance
- `web.cite`
- `web.sources`

---

## 6. Browser Automation

### Browser Lifecycle
- `browser.launch`
- `browser.close`
- `browser.status`

### Tabs
- `browser.tab_open`
- `browser.tab_close`
- `browser.tab_list`
- `browser.tab_switch`

### Navigation
- `browser.open`
- `browser.reload`
- `browser.back`
- `browser.forward`

### Page Inspection
- `browser.snapshot`
- `browser.dom`
- `browser.accessibility_tree`
- `browser.screenshot`
- `browser.console`
- `browser.network`

### Interaction
- `browser.click`
- `browser.double_click`
- `browser.hover`
- `browser.type`
- `browser.fill`
- `browser.press`
- `browser.select`
- `browser.check`
- `browser.uncheck`
- `browser.drag`
- `browser.scroll`

### Forms & Files
- `browser.upload`
- `browser.download`
- `browser.submit`

### Script Execution
- `browser.evaluate`

### Cookies & Storage
- `browser.cookies_get`
- `browser.cookies_set`
- `browser.cookies_clear`
- `browser.storage_get`
- `browser.storage_set`
- `browser.storage_clear`

---

## 7. HTTP & API

### HTTP
- `http.request`
- `http.get`
- `http.post`
- `http.put`
- `http.patch`
- `http.delete`
- `http.head`
- `http.options`

### Streaming
- `http.stream`
- `http.sse`

### WebSockets
- `ws.connect`
- `ws.send`
- `ws.receive`
- `ws.close`

### GraphQL
- `graphql.query`
- `graphql.mutate`
- `graphql.introspect`

### OpenAPI
- `openapi.load`
- `openapi.inspect`
- `openapi.generate_tools`
- `openapi.call`

### Authentication
- `auth.status`
- `auth.login`
- `auth.logout`
- `auth.refresh`
- `auth.oauth_start`
- `auth.oauth_complete`

---

## 8. Code Execution & Development

### Code Execution
- `code.run`
- `code.compile`
- `code.eval`
- `code.repl`

### Testing
- `code.test`
- `code.test_file`
- `code.test_filter`
- `code.coverage`

### Quality
- `code.lint`
- `code.format`
- `code.typecheck`
- `code.audit`

### Language Intelligence
- `code.symbols`
- `code.definition`
- `code.references`
- `code.hover`
- `code.signature`
- `code.completions`
- `code.diagnostics`
- `code.rename_symbol`

### Parsing
- `code.parse_ast`
- `code.query_ast`
- `code.transform_ast`
- `code.tree_sitter_query`

### Build
- `code.build`
- `code.clean`

---

## 9. Git

### Repository
- `git.init`
- `git.clone`
- `git.status`
- `git.config`

### History
- `git.log`
- `git.show`
- `git.blame`

### Changes
- `git.diff`
- `git.add`
- `git.reset`
- `git.restore`
- `git.clean`

### Commits
- `git.commit`
- `git.amend`
- `git.revert`
- `git.cherry_pick`

### Branches
- `git.branch_list`
- `git.branch_create`
- `git.branch_delete`
- `git.checkout`
- `git.switch`

### Integration
- `git.merge`
- `git.rebase`
- `git.abort_merge`
- `git.abort_rebase`

### Remote
- `git.remote_list`
- `git.remote_add`
- `git.fetch`
- `git.pull`
- `git.push`

### Stash
- `git.stash`
- `git.stash_list`
- `git.stash_apply`
- `git.stash_pop`

### Worktrees
- `git.worktree_list`
- `git.worktree_add`
- `git.worktree_remove`

---

## 10. Source Control Platforms

### GitHub
- `github.repo_get`
- `github.repo_search`
- `github.issue_list`
- `github.issue_get`
- `github.issue_create`
- `github.issue_update`
- `github.issue_comment`
- `github.pr_list`
- `github.pr_get`
- `github.pr_create`
- `github.pr_update`
- `github.pr_review`
- `github.pr_comment`
- `github.pr_merge`
- `github.checks`
- `github.actions_list`
- `github.actions_logs`
- `github.actions_rerun`
- `github.release_list`
- `github.release_create`
- `github.artifact_download`

### GitLab
- `gitlab.project_get`
- `gitlab.issue_list`
- `gitlab.issue_get`
- `gitlab.issue_create`
- `gitlab.mr_list`
- `gitlab.mr_get`
- `gitlab.mr_create`
- `gitlab.pipeline_list`
- `gitlab.pipeline_logs`

### Bitbucket
- `bitbucket.repo_get`
- `bitbucket.issue_list`
- `bitbucket.pr_list`
- `bitbucket.pr_get`
- `bitbucket.pr_create`

---

## 11. Package Managers & Dependencies

### Generic Package API
- `package.search`
- `package.info`
- `package.install`
- `package.remove`
- `package.update`
- `package.outdated`
- `package.lock`
- `package.audit`

### JavaScript
- `npm.install`
- `npm.remove`
- `npm.run`
- `npm.audit`
- `pnpm.install`
- `pnpm.run`
- `yarn.install`
- `yarn.run`

### Python
- `pip.install`
- `pip.uninstall`
- `uv.add`
- `uv.remove`
- `uv.sync`
- `poetry.add`
- `poetry.remove`
- `poetry.install`

### Rust
- `cargo.add`
- `cargo.remove`
- `cargo.build`
- `cargo.test`

### Go
- `go.get`
- `go.mod_tidy`
- `go.build`
- `go.test`

### JVM
- `maven.build`
- `maven.test`
- `gradle.build`
- `gradle.test`

---

## 12. Databases

### Generic
- `db.connect`
- `db.disconnect`
- `db.query`
- `db.execute`
- `db.schema`
- `db.tables`
- `db.describe`
- `db.explain`

### Transactions
- `db.transaction_begin`
- `db.transaction_commit`
- `db.transaction_rollback`

### SQL Databases
- `sqlite.query`
- `postgres.query`
- `mysql.query`
- `mssql.query`

### NoSQL
- `mongodb.find`
- `mongodb.aggregate`
- `mongodb.insert`
- `mongodb.update`
- `mongodb.delete`

### Redis
- `redis.get`
- `redis.set`
- `redis.delete`
- `redis.scan`
- `redis.publish`
- `redis.subscribe`

### Vector Stores
- `vector.upsert`
- `vector.search`
- `vector.delete`
- `vector.collections`

---

## 13. Containers

### Docker
- `docker.info`
- `docker.images`
- `docker.pull`
- `docker.build`
- `docker.run`
- `docker.ps`
- `docker.inspect`
- `docker.logs`
- `docker.exec`
- `docker.stop`
- `docker.rm`
- `docker.networks`
- `docker.volumes`

### Generic Containers
- `container.create`
- `container.run`
- `container.exec`
- `container.logs`
- `container.inspect`
- `container.stop`
- `container.delete`

---

## 14. Kubernetes

- `k8s.contexts`
- `k8s.use_context`
- `k8s.namespaces`
- `k8s.get`
- `k8s.describe`
- `k8s.logs`
- `k8s.exec`
- `k8s.apply`
- `k8s.delete`
- `k8s.scale`
- `k8s.rollout_status`
- `k8s.rollout_restart`
- `k8s.events`
- `k8s.port_forward`

---

## 15. Cloud

### Generic
- `cloud.resources_list`
- `cloud.resource_get`
- `cloud.logs_query`
- `cloud.deploy`
- `cloud.destroy`
- `cloud.costs`
- `cloud.identity`

### AWS
- `aws.identity`
- `aws.s3_list`
- `aws.s3_get`
- `aws.s3_put`
- `aws.ec2_list`
- `aws.lambda_list`
- `aws.lambda_invoke`
- `aws.cloudwatch_logs`
- `aws.iam_inspect`

### GCP
- `gcp.identity`
- `gcp.storage_list`
- `gcp.storage_get`
- `gcp.storage_put`
- `gcp.compute_list`
- `gcp.functions_list`
- `gcp.logging_query`

### Azure
- `azure.identity`
- `azure.storage_list`
- `azure.storage_get`
- `azure.storage_put`
- `azure.compute_list`
- `azure.functions_list`
- `azure.logs_query`

---

## 16. Infrastructure as Code

### Terraform / OpenTofu
- `iac.init`
- `iac.validate`
- `iac.plan`
- `iac.apply`
- `iac.destroy`
- `iac.output`
- `iac.state_list`
- `iac.state_show`

---

## 17. Deployment Platforms

### Vercel
- `vercel.projects`
- `vercel.deploy`
- `vercel.deployments`
- `vercel.logs`

### Cloudflare
- `cloudflare.zones`
- `cloudflare.dns_list`
- `cloudflare.dns_update`
- `cloudflare.workers_deploy`

### Supabase
- `supabase.projects`
- `supabase.query`
- `supabase.functions`
- `supabase.logs`

### Firebase
- `firebase.projects`
- `firebase.deploy`
- `firebase.functions`
- `firebase.firestore_query`

---

## 18. Secrets & Credentials

### Secret Store
- `secret.exists`
- `secret.list`
- `secret.inject`
- `secret.rotate`
- `secret.delete`

### Credential Scopes
- `credential.request`
- `credential.status`
- `credential.revoke`

> Evitar `secret.get_plaintext` cuando sea posible.  
> El runtime debe inyectar secretos directamente a procesos y connectors.

---

## 19. Memory

### General Memory
- `memory.store`
- `memory.get`
- `memory.search`
- `memory.update`
- `memory.delete`
- `memory.list`

### Scoped Memory
- `memory.user_get`
- `memory.user_store`
- `memory.project_get`
- `memory.project_store`
- `memory.session_get`
- `memory.session_store`
- `memory.episodic_search`
- `memory.knowledge_search`

### Memory Administration
- `memory.forget`
- `memory.expire`
- `memory.provenance`

---

## 20. Context Management

- `context.inspect`
- `context.search_history`
- `context.retrieve`
- `context.compact`
- `context.summarize`
- `context.pin`
- `context.unpin`
- `context.list_pins`
- `context.artifacts`
- `context.token_usage`
- `context.trim`

---

## 21. Agents & Subagents

### Lifecycle
- `agent.spawn`
- `agent.status`
- `agent.list`
- `agent.wait`
- `agent.cancel`
- `agent.result`

### Communication
- `agent.message`
- `agent.broadcast`

### Delegation
- `agent.delegate`
- `agent.delegate_batch`

### Resource Control
- `agent.set_budget`
- `agent.set_tools`
- `agent.set_context`

---

## 22. Task Graph & Orchestration

### Tasks
- `task.create`
- `task.get`
- `task.list`
- `task.update`
- `task.start`
- `task.complete`
- `task.fail`
- `task.cancel`

### Dependencies
- `task.add_dependency`
- `task.remove_dependency`
- `task.blockers`

### Workflows
- `workflow.create`
- `workflow.run`
- `workflow.pause`
- `workflow.resume`
- `workflow.cancel`
- `workflow.status`

### Parallel Execution
- `workflow.parallel`
- `workflow.join`

### Verification (fase 3)
- `verify.plan` — decide qué verificar para un set de files cambiados
  (targeted tests vía test-map del repo index o convenciones, adjacent tests,
  escalada a la suite completa si cambió config/shared, risk level,
  discovered lint/typecheck/build commands).
- `verify.record` — persiste una unidad de evidencia de validación
  (`kind` ∈ test|lint|typecheck|build|schema|manual|custom,
  `result` ∈ passed|failed|error|skipped + command/summary/detail).
- `verify.evaluate` — evalúa el completion gate con la evidencia más
  reciente: `DONE | IMPLEMENTED_UNVERIFIED | PARTIAL | BLOCKED | FAILED`.
  Rechaza falso-éxito (marca de failure en la salida de un `passed`) y
  "no tests ran".

Notas: los tools clasifican como `state.read`/`state.write` (persistencia de
evidencia local, sin efecto externo) y nunca piden approval. El harness
re-evalúa el gate tras cada turno con activity (evento `CompletionGateEvaluated`);
un claim de "fixed" sin evidencia pasada queda como `IMPLEMENTED_UNVERIFIED`/
`PARTIAL`. Sin CLI dedicada: la evidencia queda en el trace de la sesión.

---

## 23. Time, Scheduling & Watches

### Time
- `time.now`
- `time.parse`
- `time.format`
- `time.convert_timezone`
- `time.duration`

### Scheduling
- `schedule.create`
- `schedule.get`
- `schedule.list`
- `schedule.update`
- `schedule.pause`
- `schedule.resume`
- `schedule.cancel`

### Condition Watches
- `watch.create`
- `watch.get`
- `watch.list`
- `watch.update`
- `watch.cancel`

---

## 24. Communication

### Email
- `email.search`
- `email.get`
- `email.thread_get`
- `email.draft`
- `email.send`
- `email.reply`
- `email.forward`
- `email.archive`
- `email.delete`
- `email.labels`

### Slack
- `slack.channels`
- `slack.search`
- `slack.messages`
- `slack.thread`
- `slack.send`
- `slack.reply`
- `slack.react`

### Discord
- `discord.channels`
- `discord.messages`
- `discord.send`
- `discord.reply`

### Microsoft Teams
- `teams.channels`
- `teams.messages`
- `teams.send`
- `teams.reply`

### Telegram
- `telegram.chats`
- `telegram.messages`
- `telegram.send`

### SMS
- `sms.send`
- `sms.history`

---

## 25. Productivity & SaaS

### Calendar
- `calendar.list`
- `calendar.events`
- `calendar.event_get`
- `calendar.availability`
- `calendar.create`
- `calendar.update`
- `calendar.delete`
- `calendar.respond`

### Contacts
- `contacts.search`
- `contacts.get`
- `contacts.create`
- `contacts.update`

### Drive / Storage
- `drive.search`
- `drive.list`
- `drive.get`
- `drive.upload`
- `drive.download`
- `drive.move`
- `drive.delete`

### Docs
- `docs.get`
- `docs.create`
- `docs.update`
- `docs.append`
- `docs.export`

### Sheets
- `sheets.get`
- `sheets.read`
- `sheets.write`
- `sheets.append`
- `sheets.create`
- `sheets.export`

### Notion
- `notion.search`
- `notion.page_get`
- `notion.page_create`
- `notion.page_update`
- `notion.database_query`

### Linear
- `linear.search`
- `linear.issue_get`
- `linear.issue_create`
- `linear.issue_update`
- `linear.comment`

### Jira
- `jira.search`
- `jira.issue_get`
- `jira.issue_create`
- `jira.issue_update`
- `jira.comment`

### Asana
- `asana.search`
- `asana.task_get`
- `asana.task_create`
- `asana.task_update`

---

## 26. Documents

### Generic Documents
- `document.parse`
- `document.create`
- `document.edit`
- `document.convert`
- `document.extract`
- `document.render`

### PDF
- `pdf.read`
- `pdf.create`
- `pdf.edit`
- `pdf.merge`
- `pdf.split`
- `pdf.render`
- `pdf.extract_text`
- `pdf.extract_tables`

### DOCX
- `docx.read`
- `docx.create`
- `docx.edit`
- `docx.convert`

### Spreadsheets
- `spreadsheet.read`
- `spreadsheet.write`
- `spreadsheet.formula`
- `spreadsheet.chart`
- `spreadsheet.create`
- `spreadsheet.export`

### Presentations
- `slides.read`
- `slides.create`
- `slides.edit`
- `slides.render`
- `slides.export`

### CSV / Tabular
- `csv.read`
- `csv.write`
- `csv.transform`

### Archives
- `archive.create`
- `archive.list`
- `archive.extract`

---

## 27. Images

### Inspection
- `image.inspect`
- `image.metadata`
- `image.ocr`
- `image.detect_objects`

### Manipulation
- `image.resize`
- `image.crop`
- `image.rotate`
- `image.convert`
- `image.compress`

### Generation & Editing
- `image.generate`
- `image.edit`
- `image.inpaint`
- `image.outpaint`
- `image.upscale`

---

## 28. Audio

- `audio.inspect`
- `audio.transcribe`
- `audio.translate`
- `audio.generate`
- `audio.synthesize`
- `audio.convert`
- `audio.trim`
- `audio.merge`

---

## 29. Video

- `video.inspect`
- `video.metadata`
- `video.extract_frames`
- `video.transcribe`
- `video.generate`
- `video.edit`
- `video.trim`
- `video.merge`
- `video.convert`

---

## 30. Computer Use / GUI

### Screen
- `computer.screenshot`
- `computer.screen_info`

### Mouse
- `computer.click`
- `computer.double_click`
- `computer.move_mouse`
- `computer.drag`
- `computer.scroll`

### Keyboard
- `computer.type`
- `computer.key`
- `computer.hotkey`

### Windows
- `computer.windows`
- `computer.focus_window`
- `computer.move_window`
- `computer.resize_window`
- `computer.close_window`

### Clipboard
- `clipboard.read`
- `clipboard.write`
- `clipboard.clear`

### Accessibility
- `computer.accessibility_tree`
- `computer.accessibility_action`

---

## 31. Math & Deterministic Reasoning

### Calculator
- `math.calculate`
- `math.evaluate`

### Symbolic Math
- `math.symbolic`
- `math.solve`
- `math.simplify`
- `math.integrate`
- `math.differentiate`

### Statistics
- `stats.describe`
- `stats.correlation`
- `stats.regression`
- `stats.hypothesis_test`

### Optimization
- `optimizer.solve`
- `optimizer.linear`
- `optimizer.integer`

### SAT / SMT
- `solver.sat`
- `solver.smt`
- `solver.constraints`

---

## 32. Structured Data Utilities

### JSON
- `json.parse`
- `json.stringify`
- `json.query`
- `json.patch`
- `json.diff`

### YAML
- `yaml.parse`
- `yaml.stringify`
- `yaml.validate`

### XML
- `xml.parse`
- `xml.query`
- `xml.validate`

### JQ / JSONPath
- `jq.run`
- `jsonpath.query`

### Regex
- `regex.test`
- `regex.match`
- `regex.replace`
- `regex.explain`

---

## 33. Validation

- `validate.json_schema`
- `validate.openapi`
- `validate.xml_schema`
- `validate.yaml`
- `validate.syntax`
- `validate.contract`
- `validate.assert`
- `validate.url`
- `validate.email`
- `validate.datetime`

---

## 34. Security

### Policy
- `policy.check`
- `policy.explain`
- `policy.list`
- `policy.evaluate_action`

### Permissions
- `permission.check`
- `permission.request`
- `permission.grant`
- `permission.revoke`
- `permission.list`

### Sandbox
- `sandbox.create`
- `sandbox.exec`
- `sandbox.inspect`
- `sandbox.reset`
- `sandbox.destroy`

### Security Scanning
- `security.scan_dependencies`
- `security.scan_secrets`
- `security.scan_files`
- `security.scan_container`
- `security.scan_iac`

---

## 35. Human-in-the-Loop

- `user.ask`
- `user.confirm`
- `user.choose`
- `user.approve_action`
- `user.reject_action`
- `user.request_input`
- `user.request_secret`
- `user.notify`

---

## 36. Checkpoints, Transactions & Rollback

### Checkpoints
- `checkpoint.create`
- `checkpoint.list`
- `checkpoint.inspect`
- `checkpoint.restore`
- `checkpoint.delete`

### Transaction Abstraction
- `transaction.begin`
- `transaction.commit`
- `transaction.rollback`

### Undo
- `undo.list`
- `undo.preview`
- `undo.apply`

---

## 37. Observability

### Tracing
- `trace.start`
- `trace.end`
- `trace.get`
- `trace.list`
- `trace.export`

### Logs
- `log.write`
- `log.query`
- `log.tail`

### Metrics
- `metrics.get`
- `metrics.query`
- `metrics.record`

### Usage
- `usage.tokens`
- `usage.cost`
- `usage.tool_calls`
- `usage.latency`

### Sessions
- `session.get`
- `session.list`
- `session.inspect`
- `session.export`

---

## 38. Evaluation

### Eval Suites
- `eval.create`
- `eval.run`
- `eval.compare`
- `eval.report`

### Assertions
- `eval.assert_output`
- `eval.assert_tool_use`
- `eval.assert_trajectory`

### Model Judging
- `eval.judge`
- `eval.rank`

### Regression
- `eval.regression`
- `eval.benchmark`

---

## 39. Budgets & Limits

- `budget.get`
- `budget.set`
- `budget.check`
- `budget.consume`
- `budget.remaining`

Budget dimensions:
- model calls
- tool calls
- tokens
- cost
- wall-clock execution
- subagents
- recursion depth
- network requests
- storage
- CPU
- memory

---

## 40. Cancellation

- `cancel.task`
- `cancel.workflow`
- `cancel.agent`
- `cancel.process`
- `cancel.request`
- `cancel.all`

Cancellation should propagate through child operations.

---

## 41. Provenance

- `provenance.get`
- `provenance.trace`
- `provenance.sources`
- `provenance.verify`
- `provenance.attach`

Every important result should be traceable to:
- file + line range
- URL
- API request
- SQL query
- tool call
- model turn
- subagent
- timestamp
- artifact

---

## 42. Tool Discovery & Dynamic Loading

### Discovery
- `tools.list`
- `tools.search`
- `tools.describe`
- `tools.capabilities`

### Loading
- `tools.load`
- `tools.unload`
- `tools.reload`

### Registration
- `tools.register`
- `tools.unregister`

### Generation
- `tools.from_openapi`
- `tools.from_mcp`
- `tools.from_plugin`
- `tools.compose`

---

## 43. MCP

- `mcp.servers`
- `mcp.connect`
- `mcp.disconnect`
- `mcp.tools`
- `mcp.resources`
- `mcp.prompts`
- `mcp.call`
- `mcp.read_resource`
- `mcp.subscribe`

---

## 44. Plugins & Connectors

### Plugins
- `plugin.search`
- `plugin.install`
- `plugin.uninstall`
- `plugin.enable`
- `plugin.disable`
- `plugin.update`
- `plugin.info`
- `plugin.list`

### Connectors
- `connector.list`
- `connector.connect`
- `connector.disconnect`
- `connector.status`
- `connector.invoke`

---

## 45. Tool Composition

- `composition.create`
- `composition.run`
- `composition.inspect`
- `composition.save`
- `composition.delete`

Example composed tool:

```text
get_project_releases
  -> web.fetch
  -> web.extract_text
  -> json.parse
  -> search.filter
```

---

## 46. Notifications

- `notification.send`
- `notification.list`
- `notification.dismiss`

Targets may include:
- CLI
- desktop
- email
- Slack
- webhook
- push notification

---

## 47. Networking

- `network.dns_lookup`
- `network.ping`
- `network.traceroute`
- `network.port_check`
- `network.connections`
- `network.download`
- `network.upload`

---

## 48. Cryptography & Hashing

- `crypto.hash`
- `crypto.verify_hash`
- `crypto.random`
- `crypto.uuid`
- `crypto.encrypt`
- `crypto.decrypt`
- `crypto.sign`
- `crypto.verify_signature`

Sensitive cryptographic operations should require explicit permissions.

---

## 49. Compression & Encoding

- `encoding.base64_encode`
- `encoding.base64_decode`
- `encoding.url_encode`
- `encoding.url_decode`
- `encoding.hex_encode`
- `encoding.hex_decode`
- `compression.gzip`
- `compression.gunzip`
- `compression.zip`
- `compression.unzip`

---

## 50. Location, Maps & Timezones

- `location.geocode`
- `location.reverse_geocode`
- `location.distance`
- `location.route`
- `location.nearby`
- `timezone.lookup`

---

## 51. Finance & Market Data

- `finance.quote`
- `finance.history`
- `finance.exchange_rate`
- `finance.company_info`
- `finance.market_status`
- `finance.news`

Mutating financial operations should live behind separate high-risk connectors.

---

## 52. Weather

- `weather.current`
- `weather.forecast`
- `weather.historical`
- `weather.alerts`

---

## 53. Knowledge & Reference

- `knowledge.lookup`
- `knowledge.search`
- `knowledge.cite`
- `knowledge.entities`

---

## 54. Models

### Model Routing
- `model.list`
- `model.info`
- `model.route`
- `model.invoke`

### Embedding / Reranking
- `model.embed`
- `model.rerank`

### Model Budgeting
- `model.estimate_cost`
- `model.estimate_tokens`

---

## 55. Prompt & Template Resources

- `prompt.list`
- `prompt.get`
- `prompt.render`
- `prompt.validate`

- `template.list`
- `template.get`
- `template.render`

---

## 56. Cache

- `cache.get`
- `cache.set`
- `cache.delete`
- `cache.clear`
- `cache.stats`

---

## 57. Queues & Events

### Queue
- `queue.publish`
- `queue.consume`
- `queue.peek`
- `queue.ack`
- `queue.retry`

### Event Bus
- `event.emit`
- `event.subscribe`
- `event.unsubscribe`
- `event.history`

---

## 58. Webhooks

- `webhook.create`
- `webhook.list`
- `webhook.update`
- `webhook.delete`
- `webhook.test`

---

## 59. CLI Interaction

- `cli.print`
- `cli.progress`
- `cli.table`
- `cli.prompt`
- `cli.select`
- `cli.confirm`
- `cli.open_editor`
- `cli.open_url`
- `cli.copy`

---

## 60. Tool Contract Metadata

Every tool should declare metadata similar to:

```ts
type ToolDefinition = {
  name: string
  description: string
  inputSchema: JSONSchema
  outputSchema: JSONSchema

  permissions: string[]
  risk: "low" | "medium" | "high" | "critical"
  sideEffects: boolean
  idempotent: boolean
  requiresApproval: boolean

  networkScope?: string[]
  filesystemScope?: string[]
  secretScopes?: string[]

  timeoutMs?: number
  retryPolicy?: RetryPolicy
  maxOutputBytes?: number
}
```

Enforcement (runtime contract, not decoration):

```text
outputSchema, when declared, is enforced: a violating result returns
VALIDATION_FAILED with no retry — a contract bug, not a transient
failure. Retries (at most 2 attempts) apply only to idempotent tools
whose sideEffects are not communication / financial / credential, and
only for NETWORK_ERROR / RATE_LIMITED errors flagged retryable;
TIMEOUT is never retried. timeoutMs narrows a cooperative deadline_at
for the call (sync handlers cannot be preempted). A per-tool
maxOutputBytes caps the observation before the global bound applies.
```

---

## 61. Standard Tool Result

Recommended common result envelope:

```ts
type ToolResult<T> = {
  ok: boolean
  data?: T

  error?: {
    code: string
    message: string
    retryable: boolean
    details?: unknown
  }

  artifacts?: ArtifactRef[]

  metadata: {
    toolCallId: string
    durationMs: number
    timestamp: string
    sideEffects?: string[]
    provenance?: ProvenanceRef[]
  }
}
```

What the model actually receives (`to_model_text`) is this envelope
serialized as bounded JSON: `{ok, tool?, data | error{code, message,
retryable, details?}, artifacts[] (URIs), truncated?, duration_ms?}`,
cut at ~2KB inline with an `[output truncated]` mark. Large outputs
spill to artifacts upstream; continue reading them with
`artifact.read`.

---

## 62. Standard Error Classes

- `INVALID_ARGUMENT`
- `NOT_FOUND`
- `ALREADY_EXISTS`
- `PERMISSION_DENIED`
- `AUTH_REQUIRED`
- `AUTH_EXPIRED`
- `RATE_LIMITED`
- `TIMEOUT`
- `NETWORK_ERROR`
- `DEPENDENCY_ERROR`
- `CONFLICT`
- `RESOURCE_EXHAUSTED`
- `SANDBOX_VIOLATION`
- `POLICY_DENIED`
- `APPROVAL_REQUIRED`
- `APPROVAL_DENIED`
- `CANCELLED`
- `PARTIAL_FAILURE`
- `VALIDATION_FAILED`
- `TOOL_NOT_FOUND`
- `UNKNOWN`

---

## 63. Recommended Tool Loading Strategy

Implemented as budgeted per-request exposure: each model request
carries core tools + explicitly activated tools + recently used tools
(last 16, for continuity) — never the whole registry. The schema
budget is 96 tool schemas / ~32.000 estimated schema tokens. Core and
activated tools are never dropped to fit (metrics flag `over_budget`
instead); only the recent-tools tail is cut. `capability.search` is
always exposed so the model can always recover the on-demand
ecosystem. The intended flow is search, then activate exactly what the
task needs:

- `capability.search` — rank capabilities across native, plugin, MCP,
  OpenAPI and the browser fallback (typed connector first, browser
  DOM last; exact name matches win outright).
- `capability.activate` — expose tools by exact name, scope `turn`
  (decays after ~4 model rounds, `ttl_rounds` 1–32) or `session`.
- `capability.deactivate` — drop previously activated tools.

### Per-turn execution plan

Each turn's tool calls are planned into ordered groups: consecutive
read-only + idempotent + non-network calls may share a group, every
other call runs alone, and groups always run in order with
observations reported back in the original call order. Execution is
still serial (policy/approval/event ordering); the plan is computed
and traced as `execution_plan` on the model-invoked event, ready for
a concurrent executor.

### Always Loaded (core)
- `fs.*`
- `shell.*`
- `search.*`
- `artifact.*`
- `capability.search`
- `capability.activate`
- `capability.deactivate`
- `user.*`
- `context.*`
- `budget.*`
- `cancel.*`

### On Demand (lazy until activated)
- browser (`browser.*`)
- web
- git
- GitHub / GitLab
- databases
- containers
- cloud
- documents
- multimedia
- communication
- productivity
- MCP servers (`mcp.*`)
- OpenAPI services (`api.*`)
- plugins (`plugin.*`)

### Dynamic
- project-specific plugins
- user plugins
- temporary composed tools

---

## 64. Preferred Capability Order

When several interfaces can solve the same task:

```text
typed API
  ↓
native connector
  ↓
MCP / OpenAPI
  ↓
filesystem / structured local API
  ↓
browser DOM
  ↓
accessibility tree
  ↓
computer vision + coordinates
  ↓
raw shell escape hatch
```

Prefer the most structured, deterministic and least risky interface available.
# Structured desktop questions

`user.ask` requests clarification through a host-provided interaction channel.
It uses the common ToolRuntime and is available in PLAN without authorizing
filesystem mutations. Unsupported hosts omit it from model exposure.
The [interactive workspace contract](desktop/06-interactive-workspace.md)
defines question schemas, explicit replies, skips, cancellation and recovery.
