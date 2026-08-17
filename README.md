# Rinari CLI

**Tu asistente personal de IA en la terminal.**

> **Estado: fase 2 — agent runtime, tool runtime y seguridad base.**
> Fases 0 (contrato del producto) y 1 (fundaciones, bootstrap y persistencia)
> completas: el CLI real funciona — setup, config, providers/models,
> sesiones CHAT/PROJECT, Soul/Constitution, doctor/status/version. Ver
> [TODO.md](TODO.md) por el roadmap y el estado de decisiones.

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

## Pendiente

- Queda en Fase 3: checkpoints/undo (`rinari undo`). En Fase 4: network
  policy + network hook del sandbox, y reconciliation de resume.
  Checklist completo en [TODO.md](TODO.md).
- Los 4 items de Fase 1 que dependían del agent loop (Extended Identity bajo
  demanda, project-creation intent, preservar conversación, recalcular
  permisos) quedaron resueltos al cerrar Fase 2.
- Decisiones no bloqueantes (engine de browser, backend del credential store,
  SQLite/ORM, LSPs iniciales, etc.): sección "Decisiones pendientes" de
  [TODO.md](TODO.md). Se resuelve cada una antes de implementar su
  subsistema.

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