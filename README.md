# Rinari CLI

**Tu asistente personal de IA en la terminal.**

> **Estado: fases 0-7 completas + core de fase 8 (2026-08-20).**
> Producto: CLI funcional (setup, config, providers/models, sesiones
> CHAT/PROJECT, Soul/Constitution, doctor/status/version), agent + tool
> runtime con seguridad base (fase 2), trust/index/validation/checkpoints
> (fase 3), contexto/artifacts/memoria/resume durable/budgets (fase 4), el
> capability ecosystem (fase 5): Web, HTTP, Browser (CDP), plugins, MCP,
> OpenAPI, unified capability search y lifecycle hooks, skills productivos
> + multi-agent (fase 6): Skill Runtime (manifest, discovery, lazy load, CLI)
> y 6 agentes built-in con `AgentOrchestrator` (limits, cancel, worktrees),
> tools `agent.*`, synthesis con contradictions/duplicate-work y subagentes
> aislados por perfil (read-only/workspace), UI/UX + productización (fase
> 7): `RuntimeSnapshot` como fuente de verdad, renderers Rich/compact/plain/
> JSON/JSON-stream, banner + status rail + approvals UI, 24 slash commands,
> y cobertura completa de la superficie de comandos pública (~258
> subcomandos, `--json` con contracts y exit codes). Fase 8 (core): eval
> runner determinista (21 casos en 6 suites, network-isolated) con
> `rinari eval`, `metrics` como grupo (all/sessions/.../success), trace spans
> (`turn_index`/`tool_seq`), export/import como grupos con redacción de
> secretos, y tests de upgrade de esquema. Ver [TODO.md](TODO.md) por el
> roadmap y el estado de decisiones.

Segunda construcción de Rinari. La
[v1](https://github.com/Xainner/Rinari-CLI) (chat REPL, agente con 23 tools,
MCP, git, historial, 372 tests) sirvió de referencia y se conserva como tal —
referencia, no base de código. Qué se hace con cada parte de v1 quedó
resuelto item por item en la sección "Herencia de Rinari v1" de
[TODO.md](TODO.md).

## Decidido

| Decisión | Valor | Desde |
|---|---|---|
| Target del producto | Harness completo y productivo, no wrapper mínimo de LLM + shell | 2026-08-16 |
| Stack | Python 3.11+, `uv`, src layout (`src/rinari/`), Typer, Rich, HTTPX, pytest | 2026-08-16 |
| Herencia de v1 | Solo referencia; todo se reconstruye con el diseño v2 | 2026-08-16 |
| Soul | Definido desde cero: identidad, voz, reglas duras y apariencia fija | 2026-08-16 |
| Arquitectura | Principios + blueprint completo; sin mega-system-prompt | 2026-08-16 |
| Contrato CLI | Superficie completa: comandos, providers, models, config, visual | 2026-08-16 |
| Tools / Skills | Catálogos maestros con contrato y estrategia de carga lazy | 2026-08-16 |
| Sesiones | `rinari chat` fuerza CHAT; `rinari` = AUTO (proyecto → PROJECT); promoción CHAT → PROJECT sin reiniciar | 2026-08-16 |
| Providers / models | Registries persistentes; cambiar selección nunca elimina configuraciones previas | 2026-08-16 |
| Soul / Constitution | Assets canónicos empaquetados + loader con override `~/.rinari/` y metadatos de versión/hash | 2026-08-16 |
| Licencia | MIT | 2026-08-16 |

## Qué funciona hoy

```bash
uv sync
uv run rinari setup --provider openai --api-key-env OPENAI_API_KEY \
  --model gpt-4o --model-name gpt-main
uv run rinari providers add anthropic --name anthropic-work \
  --api-key-env ANTHROPIC_API_KEY
uv run rinari provider use anthropic-work
uv run rinari chat
uv run rinari status
uv run rinari doctor
uv run rinari version
```

- Config TOML con layering (defaults → usuario → perfil → proyecto) y CLI
  `rinari config ...`.
- Providers y models en SQLite (`~/.rinari/state.db`); cambiar selección nunca
  elimina configuraciones previas y cada provider recuerda su modelo.
- Sesiones CHAT/PROJECT persistentes con conversación persistida
  (`session_messages`); promoción CHAT → PROJECT que preserva session ID y
  conversación (vía `rinari init` o in-session cuando el trabajo crea un
  marker de proyecto en el cwd); `$HOME` nunca es workspace implícito.
- Soul y Constitution empaquetados con loader (override en `~/.rinari/`,
  versión + sha256 visibles en `rinari version` / `doctor`); el prompt siempre
  inyecta solo el Canonical Soul, y la Extended Identity Reference solo en
  turnos de identidad.
- Agent loop real (Fase 2): `rinari` / `rinari chat` / `rinari resume` ahora
  conversan con el model. Turnos con streaming, tool calls normalizadas a
  través del Tool Runtime (policy → approval → sandbox), streaming vivo de
  stdout/stderr del shell en REPL, processes por sesión
  (`process.start/wait/output/signal/list`), REPL con `/provider` y `/model`
  in-session, session-interruption state, prompt one-shot no interactivo,
  y traza de eventos persistida por sesión.
- Dirty-worktree protection (ya disponible): al abrir una sesión
  PROJECT se captura el estado no commiteado previo; sobrescribir un file
  con cambios del usuario pide approval explícita (runtime, no prompt), y
  `git.status` etiqueta la ownership de cada path (`user` /
  `modified-in-session` / `new-in-session`).
- Project trust (Fase 3): `rinari trust add|remove|status|list`; un project
  no confiado retiene sus instrucciones hasta un grant explícito, con
  fingerprint de identidad (git HEAD+remotes) y revalidation cuando la
  identidad cambia.
- Search (Fase 3): tools `search.files` (glob exacto), `search.regex`
  (regex por línea), `search.symbols` (py/js/ts/rs/go, consultas
  calificados `Class.method`), `search.references` y `search.hybrid`
  (ranked con reasons) — todos read-only bajo la policy normal.
- Repository state (Fase 3): detección por files de languages, frameworks,
  package managers y comandos de build/test/lint/typecheck (con la source de
  cada hint); alimentan el environment del prompt y se excluyen dirs
  generados del scan.
- Instrucciones de project (Fase 3): resolver de RINARI.md con chain
  `~/.rinari/RINARI.md` (global) → root → ... → cwd; `RINARI.override.md`
reemplaza a RINARI.md en su level; el level más profundo gana conflictos.
   README y demás files siguen siendo data, no instrucciones.
- AST (Fase 3): capa `rinari/ast` con query grammar unificado
  (`symbols`/`functions`/`classes`/`imports`/`calls:NAME`), adapter
  tree-sitter para Python (symbols con calificación `Class.method`, imports,
  call sites) y fallback regex para py/js/ts/rs/go. `search.symbols` ya
  consume la capa AST cuando hay grammar disponible.
- LSP (Fase 3): client JSON-RPC sobre stdio con capability gating y tools
  `lsp.definition|references|symbols|diagnostics|hover|signature|rename`
  (rename solo planea el WorkspaceEdit, nunca lo aplica). Los servers se
  detectan en PATH (`pyright-langserver`, `typescript-language-server`);
  sin server, los tools indican el fallback a `search.*`.
- Repository index (Fase 3): `rinari index status|build|update|rebuild|clear|search|doctor`;
  index project-scoped (files con hash sha256, symbols, references, test
  mapping) con invalidation incremental — los files sin cambios no se
  reparsen; la capa semántica queda declarada (`none`) como add-on opcional.
- Task graph (Fase 3): `rinari tasks list|show|tree|add|update|cancel|retry|blockers`;
  graph DAG project-scoped con detección de ciclos, y done-when contract
  (acceptance + validation obligatorios y satisfechos, sin criterios
  unresolved) que `--status done` fuerza antes de permitir completar.
- Verification (Fase 3): tools `verify.plan` (qué verificar: targeted tests
  vía test-map del index, adjacent tests, escalada a la suite si cambió
  config/shared, risk, comandos descubiertos), `verify.record` (evidencia
  persistente por kind: test|lint|typecheck|build|schema|manual|custom) y
  `verify.evaluate` (completion gate: `DONE|IMPLEMENTED_UNVERIFIED|PARTIAL|
  BLOCKED|FAILED`, con rechazo de falso-éxito y "no tests ran"). Tras cada
  turno con activity el harness re-evalúa el gate
  (`CompletionGateEvaluated`); el outcome aparece en el REPL/JSON.
  "Fixed" sin evidencia pasada no es `DONE`.
- PTY (Fase 3): tools `pty.start|read|write|resize|terminate` sobre un pty
  real para procesos TTY-aware (POSIX); en Windows `DEPENDENCY_ERROR` con
  hint a `process.*`.
- Checkpoints/Undo (Fase 3): `rinari undo create|list|preview|restore|remove`
  y bare `rinari undo` = restore del último checkpoint. Snapshot del tree
  dirty clasificado por ownership (agent/user/mixed contra el baseline de
  sesión): el restore **solo reescribe paths agent**, nunca toca paths del
  usuario, y `mixed` exige `--allow-mixed`.
- Artifact Store (Fase 4): `rinari artifacts list|show|open|search|export|remove|gc`.
  Outputs grandes/duraderos viven en `<home>/artifacts` con URI estable
  `artifact://<session>/<namespace>/<name>`, sha256 + content type +
  provenance + retention (`session|project|permanent`) en SQLite; search
  por metadata + contenido acotado, slicing, export y GC de sesiones sin
  referencia (solo retention `session`).
- Context Engine + Compaction (Fase 4): presupuesto de contexto con presión
  (umbrales 70/80/85) y compaction provider-independent (`CompactState`:
  goal, constraints, decisions, task graph, changed files, validations,
  approvals, blockers, artifacts) cuando la presión cruza el umbral de
  compaction. La cola del historial se recorta in-memory en cortes seguros
  (nunca huérfanos tool-result) y la verdad compactada se inyecta como
  segmento `compact-state` del system prompt; el estado persiste en
  `sessions.compact_state_json` (sobrevive a resume, re-aplicado
  determinísticamente) y la conversación persistida nunca se recorta. El
  outcome aparece en el REPL/JSON (`context: compacted`).

## Pendiente

- Capability ecosystem (Fase 5): todo converge en **un** `ToolRegistry` y
  pasa por el mismo Tool Runtime/policy/sandbox. **Plugins** (`rinari
  plugins install|list|show|enable|disable|update|remove|permissions|
  doctor`): manifiesto `plugin.json`, fuentes `user` (~/.rinari/plugins,
  confiada) y `project` (<root>/.rinari/plugins, **requiere project
  trust**), tools namespaced `<plugin>.<tool>`, fallo de carga → diagnostic
  (nunca crash); contribuciones skills/adapters/commands/subagents/context
  declarables pero reservadas en v1. **MCP** (`rinari mcp add|list|show|
  connect|disconnect|tools|resources|prompts|test|logs…`): stdio JSON-RPC 2.0
  con transporte abstracto, tools → `mcp.<server>.<tool>` (`readOnlyHint` →
  `mcp.read`, resto `mcp.call`), project scope exige trust, secretos solo
  `env://VAR`, verificado e2e con un server stdio fake. **OpenAPI**
  (`rinari api add|list|show|validate|auth|tools|refresh|test…`): specs JSON,
  tools `api.<name>.<op>`, risk por verbo (GET low / POST·PUT·PATCH medium /
  DELETE high) con overrides, auth detectado vía `env://`, invocación por el
  `httpx` de la sesión (testable offline). **Unified capability search**:
  tool `capability.search` que rankea "qué puedo hacer con X" sobre
  nativo/plugin/mcp/openapi con reliability×risk y fallback `browser.*`.
  **Hooks de ciclo de vida** (`rinari hooks list|show|enable|disable|test|
  doctor`): 13 eventos (SessionStart/End, Before/AfterModel, Pre/PostToolUse,
  ToolError, PermissionRequest, Subagent*, Before/AfterCompact, BeforeFinal),
  fuentes user/project/plugins con orden determinista, handlers `python`/
  `shell` (este exige capability `shell.exec`), project hooks gated por
  trust, y un hook fallido **nunca** rompe la sesión.
- Skills productivos + multi-agent (Fase 6): **Skill Runtime** (`rinari
  skills list|search|show|activate|deactivate|install|update|remove|
  validate|test|create`): manifest frontmatter+body, discovery por source
  (`packaged` < `user` < `project` con trust), el prompt lleva solo el
  catálogo (1 línea/skill) y carga el cuerpo **bajo demanda** del skill
  activo; activación persistida en la sesión + trace en events. Incluye los 10
  skills iniciales (repository-explore, implement-feature, fix-bug, debug,
  test, code-review, refactor, fix-ci, research, final-verification). Tools
  `skills.list`/`skills.activate` para el modelo. **Multi-agent**: 6 agentes
  built-in (Explore, Reviewer, Debugger, Researcher, Implementer, Verifier)
  con allowlist/perfil/budget; `AgentOrchestrator` aplica concurrent/depth/
  total limits, cancellation propagada y worktrees (`git worktree`) para
  writers aislados. Tools `agent.spawn|wait|status|message|cancel|result|
  synthesize` (spawn = `state.write`: denegado en read-only). Cada subagente
  corre un AgentLoop **scoped** (session `{parent}::{agent}`, ToolContext
  aislado, approvals auto-deny, perfil read-only o workspace); `synthesize`
  une resultados detectando duplicate-work y contradicciones de validación y
  hace el join del task graph. Storage SQLite thread-safe para que los worker
  threads persistan eventos. 40 tests (`test_skills_runtime.py`,
  `test_agents_runtime.py`, incl. e2e CLI de spawn/wait).
- Browser (Fase 5): 25 tools `browser.*` sobre un motor **CDP directo**
  (client WebSocket RFC6455 propio en repo, cero dependencias nuevas;
  alternativa documentada: Playwright, swappable tras `BrowserManager`).
  `browser.launch` inicia un Chromium headless con perfil aislado por sesión
  y ownership del subprocess; `browser.connect` usa `RINARI_BROWSER_CDP`.
  Navegar http(s) se clasifica `network.outbound` sobre la URL (guard en
  código + approval en modo ask); lecturas (`snapshot`, `a11y`, `screenshot`,
  `console`, `network`, `cookies`, `tabs`) son `browser.read`; mutaciones
  (`click`/`fill`/`type`/`select`/`check`/`scroll`/`drag`/`evaluate`/
  `set_cookie`/…) son `browser.mutate` (consent explícito, denegado en
  read-only). Uploads pasan por el sandbox con provenance (path/bytes/sha256);
  downloads solo al artifact dir de la sesión (name/size/sha256); cookies con
  valores redactados. 40 tests contra un fake CDP server en loopback.
- HTTP (Fase 5): tools `http.request` (GET/POST/PUT/PATCH/DELETE/HEAD/
  OPTIONS, headers/query/body, auth por secret-ref `env://`/`file://` vía el
  `CredentialStore` de la sesión — bearer/basic/header/query) y `http.sse`
  (consume `text/event-stream` con semántica WHATWG acotada). Retry solo para
  métodos idempotentes (429/5xx y errores de transporte) con backoff
  exponencial o `Retry-After` (delta-seconds/HTTP-date); timeout acotado
  1..120 s; 4xx/5xx reportados como dato, no como fallo del tool. Redacción de
  secretos en la frontera del transport: headers sensibles `***` en la
  respuesta y valores de query secret borrados de `final_url`; el secreto
  inyectado nunca aparece en el resultado. Respuestas binarias o por encima
  del preview (8 KiB) se escriben como artifact en el artifact dir de la
  sesión (`save_as` opcional). 42 tests (`test_http_tools.py`) sin sockets.
- Web (Fase 5): tools `web.search|fetch|open|links|find|extract_text|
  extract_markdown|extract_metadata|download|cite|sources` bajo el mismo Tool
  Runtime: estado sin sesiones de página, transport httpx acotado (2 MiB,
  timeout, mapeo de errores por code), `NetworkGuard` bloquea DENY en código
  antes de cada dial, y extracción de HTML con stdlib (title/meta/links/texto/
  markdown/tables; el contenido remoto es dato desconfiado). `web.search` es
  keyless (endpoint HTML de DuckDuckGo, sin dependencias extra); `web.download`
  escribe al artifact dir de la sesión; `web.cite`/`web.sources` producen
  records de evidencia (sha256, fetched_at, title, snippet). 25 tests
  (`test_web.py`) sin sockets (MockTransport).
- Skill version reconciliation (Fase 4): las skills activas de una sesión se
  persisten como pares `(name, version)` (`sessions.active_skills_json`,
  migración 0015) y `rinari resume` las reconcilia contra el catálogo
  (`SKILL.md` global en `~/.rinari/skills/` y project-local, version
  declarada o fallback `sha:` del contenido): skill desinstalada o versión
  divergente → warning, sin absorber silencio. El runtime de skills llega en
  fase 6. 13 tests (`test_skill_reconciliation.py`).
- Network policy + hooks (Fase 4): `rinari network status|test|rules|
  allow|deny|remove|history`. El modo (`network.mode` = off/ask/allow) +
  reglas allow/deny persistentes por host (exacto o subdominio) deciden cada
  target — authoritative sobre requests del modelo; rules deny siempre
  ganan, `allow` corta el ask, y el target inresoluble se niega. El gate de
  `network.outbound` corre en el Tool Runtime (con approval cuando `ask`) y
  audita cada decisión en `network_events` (migración 0014); los tools de red
  de fase 5 deben pasar cada connection target por `NetworkGuard`
  (fail-closed: DENYs impuestos en código). Las reglas se leen lazy, así
  `rinari network allow` aplica a una sesión en ejecución sin restart.
  18 tests (`test_network_policy.py`).
- Budgets + loop detection (Fase 4): cada turno corre con un `BudgetMeter`
  (model calls, tool calls, network calls, wall time vía clock inyectado,
  cost estimado desde usage real × pricing declarado — sin pricing el costo
  queda sin medir y nunca se inventa —, subagents y recursion depth) y un
  `LoopDetector` por-turno (mismo tool+args, oscilación A-B, rewrites del
  mismo path, mismo error, approval denegada repetida, subagent duplicado).
  Presupuesto agotado → turno termina con `kind="budget"` + snapshot
  observable (`TurnResult.budget`); primera detección de loop inyecta un
  nudge del harness forzando cambio de estrategia, la segunda detiene el
  turno con `kind="loop"` (evento `LoopDetected`). 18 tests
  (`test_budget_loop.py`).
- Session fork (Fase 4): `rinari session fork <ref> [--name alias]` crea una
  sesión independiente desde la fuente: copia kind, identidad de proyecto,
  cwd, provider/model/profile, mode, `compact_state`, branch grabada y la
  conversación completa (mensajes con IDs nuevos, `seq` re-numerado).
  Provenance durable vía `sessions.forked_from` (migración 0013) + evento
  `SessionForked`; la sesión fuente no se modifica. 7 tests
  (`test_session_fork.py`).
- Resume durable + reconciliation (Fase 4): `rinari resume` (y el
  resume-implícito de `rinari`) pasa la sesión por un reconciler que
  re-verifica los hechos duraderos antes de seguirla: identidad de proyecto
  (re-registra la row si falta; única corrección automática), branch
  (persistida en `sessions.git_branch` al inicio vs actual), working tree
  (baseline de Fase 3 vs estado dirty actual), permission profile (policy
  change), provider/model (siguen existiendo), trust y cwd grabado. Todo
  finding `!= ok` se muestra como warning; el JSON emite
  `data.reconciliation` con subsystem/state/detail/action. 10 tests
  (`test_resume_reconciliation.py`) + migración 0012.
- Context retrieval + pins (Fase 4): candidatos del index del repo
  (archivos/símbolos), memoria y artifacts de la sesión, con ranking
  determinista y dedup por `(source, ref)`; pins de sesión que inyectan su
  contenido (acotado, repo como `<untrusted>`) en el segmento de prompt
  `pinned-context` cada turno. Tools `context.retrieve/pin/unpin/list_pins`
  y CLI `rinari context pins|pin|unpin|retrieve`. 8 tests
  (`test_context_retrieval.py`).
- User/Project/Episodic/Pattern Memory (Fase 4): 4 stores separadas en
  SQLite (migración 0010) con provenance, confidence, conflict/supersede y
  stale handling; filtro de sensibilidad que rechaza secretos en escritura;
  nunca hay persistencia automática de inferencias (todo por store/tool
  explícito). Tools `memory.remember/recall/update/forget/episodic`,
  segmento de prompt `memory` (durable, posible-estale, menor autoridad) y
  CLI `rinari memory list|search|show|add|edit|forget`. 10 tests
  (`test_memory.py`).
- Los 4 items de Fase 1 que dependían del agent loop (Extended Identity bajo
  demanda, project-creation intent, preservar conversación, recalcular
  permisos) quedaron resueltos al cerrar Fase 2.
- Decisiones no bloqueantes (engine de browser, backend del credential store,
  SQLite/ORM, LSPs iniciales, etc.): sección "Decisiones pendientes" de
  [TODO.md](TODO.md). Se resuelve cada una antes de implementar su
  subsistema.
- UI/UX + productización (Fase 7): `RuntimeSnapshot` (`cli/snapshot.py`) como
  única fuente de verdad de la terminal — provider/model, context
  used/window, profile, project/branch/dirty, tools, skills, agents, network,
  usage acumulativo (tokens/model calls/tool calls/elapsed; cost solo con
  pricing confiable, `unknown` cuando no existe). Renderers
  Rich/compact/plain/JSON/JSON-stream detectados por entorno
  (`TERM=dumb`, `NO_COLOR`, `--json`, piped) con `--no-banner` y
  `--no-progress`. Banner de arranque, status rail post-turn, approval UI
  por panel rich con riesgo y grants persistentes
  (`policy/approval_store.py`, `rinari approvals`). 24 slash commands
  (`/status /usage /tokens /diff /test /review /checkpoint /undo /compact /
  context /trace /new /resume` …). Cobertura completa de la superficie
  pública de comandos de [docs/commands.md](docs/commands.md) (~258
subcomandos con `--help` y `--json` con un mismo contract + exit codes):
   se añadieron `profiles project checkpoint permissions approvals sandbox
   secrets tools trace logs metrics cache agents` y los top-level `ask plan
   agent review run stop verify export import update`, más sesiones con
   rename/stop/archive/delete y export/import de sesión v1. Los comandos
   deterministas no gastan model; `ask`/`plan`/`review` corren read-only.
- Evals + observabilidad + hardening (core, Fase 8): paquete `rinari/evals`
   con un **eval runner determinista y network-isolated** (model scripted con
   lanes para el encolado parent/subagent; el harness real de policy/sandbox/
   approvals/loop/compaction/completion gate corre sin tocar). CLI `rinari
   eval list|run|show|compare|report|history|create|validate`. 21 casos en 6
   suites (security, soul, trajectory, coding, long-horizon, multi-agent);
   `eval compare` expone regressions/fixes entre runs. `rinari metrics` ahora
   es un grupo (`all|sessions|models|tools|skills|agents|cost|latency|
   success`) donde `success` reporta task success (gate VERIFIED), human
   intervention, loop rate y tool errors a partir de eventos persistidos, sin
   inventar métricas (cost/model latency = `unknown`). Trace spans:
   `turn_index` en turn/tool events y `tool_seq` en `ToolCompleted`,
   monotónicos a través de resume. `export`/`import` ahora son grupos
   (`export session|config|skills|profile`, `import session|config`) con
   redacción de hojas secret-named; y tests de upgrade de esquema SQLite
   (0004->latest in place preservando providers/models/sesiones/eventos).

## Docs

- [TODO.md](TODO.md) — fases 0-9, fuentes de verdad, invariantes del producto,
  registro de decisiones
- [AGENTS.md](AGENTS.md) — reglas de trabajo para agentes de IA (y humanos)
- [docs/soul.md](docs/soul.md) — el soul de Rinari: canonical soul, extended
  identity/apariencia y notas de mantenimiento
- [docs/stack.md](docs/stack.md) — principios arquitectónicos y runtime
  productivo (95 secciones)
- [docs/commands.md](docs/commands.md) — contrato público del CLI (comandos,
  providers, models, config, modales, CI)
- [docs/tools.md](docs/tools.md) — catálogo maestro de tools (64 dominios,
  contrato de tool, resultado estándar, carga lazy)
- [docs/skills.md](docs/skills.md) — catálogo maestro de skills (104
  capacidades, capas de carga, contrato de skill, skills vs tools)
- [docs/harness.md](docs/harness.md) — blueprint completo: boot, sesiones,
  state, policy/sandbox, runtime, visual, testing y dependencia de
  implementación