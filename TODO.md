# TODO — Rinari CLI

Roadmap canónico de construcción de Rinari.

> **Estado actual:** Fases 0-5 completas (2026-08-20): Fundaciones, Agent/Tool Runtime, Project intelligence, Context/Memory/Resume, y Web/HTTP/Browser/Plugins/MCP/OpenAPI/capability-search/hooks. Fase 6 completada (2026-08-20): Skill Runtime (manifest, discovery, lazy load, CLI) y multi-agent (6 built-in agents, AgentOrchestrator con limits/cancel/worktrees, `agent.*` tools, synthesis con contradictions/duplicates, subagentes scoped read-only/verifier, storage thread-safe). Pendientes documentados dentro de Fase 5: browser `auth profiles`, contribuciones de plugin no-`tools`/`hooks` (skills/adapters/commands/subagents/context), cancellation tokenizado de MCP, tools de connector nativos.
>
> **Regla:** las fases expresan **orden de dependencia de implementación**, no alcance opcional del producto.
>
> El objetivo es construir el harness completo definido en `docs/harness.md`, incluyendo sesiones CHAT/PROJECT, multi-provider, multi-model, tools, skills, browser, plugins, MCP, OpenAPI, memoria, contexto, subagentes, multi-agent, sandbox, approvals, observabilidad y evals.
>
> No se considera “terminado” un Rinari reducido que deje esos subsistemas como placeholders o experimentos futuros.

---

# 0. Fuentes de verdad

Antes de trabajar en cualquier fase, revisar:

```text
AGENTS.md
TODO.md
docs/soul.md
docs/stack.md
docs/commands.md
docs/tools.md
docs/skills.md
docs/harness.md
```

Responsabilidades:

```text
AGENTS.md
  reglas para trabajar en este repo

TODO.md
  fase actual, orden de implementación y checklist

docs/soul.md
  identidad estable de Rinari

docs/stack.md
  principios arquitectónicos

docs/commands.md
  contrato público del CLI

docs/tools.md
  catálogo y contrato de tools

docs/skills.md
  catálogo y contrato de skills

docs/harness.md
  blueprint completo del runtime y cableado entre subsistemas
```

Si una decisión cambia:

```text
identidad
→ soul.md

arquitectura/runtime
→ stack.md + harness.md

CLI/comandos
→ commands.md

tools
→ tools.md

skills
→ skills.md

orden/estado de construcción
→ TODO.md
```

---

# 1. Reglas del roadmap

## Una fase abierta a la vez

Una fase se considera cerrada cuando:

1. sus checkboxes requeridos están completos;
2. cumple su criterio de aceptación;
3. los tests correspondientes están en verde;
4. los documentos afectados están sincronizados;
5. no quedan blockers conocidos ocultos.

No abrir implementación de una fase posterior para esquivar trabajo pendiente de una fase anterior.

## Dependencia no significa producto recortado

El orden es:

```text
fundaciones
→ runtime
→ inteligencia/verification
→ contexto/durabilidad
→ web/browser/extensiones
→ multi-agent
→ UX/productización
→ hardening/release
```

Pero el target productivo incluye **todas** esas capas.

## Nada de “MVP falso”

La primera integración puede ser pequeña para validar el cableado, pero no redefine el alcance final.

No convertir:

```text
"primer vertical slice"
```

en:

```text
"producto final sin browser/MCP/plugins/multi-agent"
```

## Tests

Cuando exista código:

```text
tests deterministas
sin red pública por defecto
fake providers
fixtures
MockTransport cuando aplique
repos Git temporales
```

TDD:

```text
RED → GREEN → REFACTOR
```

cuando ayude a definir comportamiento.

No usar TDD como ceremonia para cambios triviales de docs/config.

---

# 2. Invariantes del producto

Estas reglas deben tener tests permanentes.

## Sesiones

```text
rinari chat
→ CHAT explícito, incluso dentro de un repo

rinari
→ AUTO
   proyecto detectado → PROJECT
   sin proyecto       → CHAT
```

CHAT puede convertirse en PROJECT dentro de **la misma sesión** si el usuario crea/adopta un proyecto.

Ejemplos:

```text
git init
rinari init
scaffold de proyecto
git clone
adopción explícita de carpeta como proyecto
```

La promoción preserva:

```text
session ID
conversación
provider
model
preferencias relevantes
task state relevante
artifacts
```

y añade/recalcula:

```text
project ID
project root
cwd
Git state
workspace sandbox
RINARI.md
project memory
index
skills locales
tools locales
plugins
MCP
hooks
```

## `$HOME`

```bash
cd ~
rinari
```

nunca debe convertir `$HOME` en workspace escribible implícito.

## Providers

```text
add    ≠ use
use    ≠ remove
login  ≠ add
logout ≠ remove
```

Cambiar provider nunca borra providers previos.

## Models

Cada provider recuerda su modelo propio.

Ejemplo:

```text
openai-personal → gpt-main
anthropic-work  → opus
local-ollama    → qwen
```

Cambiar de provider y volver recupera su modelo guardado.

## Seguridad

```text
prompt ≠ sandbox
```

Las restricciones críticas se aplican en runtime.

## Done

```text
editar != verificar
ejecutar test != test pasó
tool call != éxito
probablemente funciona != DONE
```

El Completion Gate debe impedir false-success.

---

# Fase 0 — Contrato del producto — COMPLETA (2026-08-16)

## Objetivo

Cerrar decisiones suficientes para empezar implementación sin que los agentes tengan que inventar arquitectura, comportamiento del CLI o alcance del producto.

## Criterio de aceptación

La fase termina cuando:

```text
[x] no queda ninguna decisión de producto obligatoria pendiente
[x] docs canónicos están coherentes entre sí
[x] AGENTS.md y TODO.md apuntan al mismo modelo operativo
[x] Fase 1 puede comenzar sin decisiones fundamentales bloqueantes
```

Cumplido el 2026-08-16 con la confirmación de la licencia MIT.

## Estado de decisiones

### Producto y alcance

- [x] **Target del producto**
      Rinari será un harness completo y productivo, no un wrapper mínimo de LLM + shell.

- [x] **Alcance integrado**
      El target incluye desde arquitectura base:

      ```text
      providers
      models
      CHAT/PROJECT sessions
      Soul
      constitution
      RINARI.md
      tools
      skills
      filesystem
      shell / PTY / processes
      Git
      code intelligence
      web
      browser
      plugins
      MCP
      OpenAPI
      context
      memory
      artifacts
      sandbox
      permissions
      approvals
      secrets
      checkpoints
      undo
      verification
      completion gate
      subagents
      multi-agent
      worktrees
      hooks
      tracing
      metrics
      evals
      budgets
      cancellation
      resume
      ```

- [x] **Primer integration checkpoint**
      Existe un vertical slice inicial para validar el núcleo, pero no redefine el producto como MVP recortado.

### Herencia de Rinari v1

- [x] **Política de herencia**
      Rinari v1 es referencia, no base de código.

- [x] **No copy/paste automático**
      Lo útil de v1 se reevalúa contra el diseño actual.

- [x] **Soul**
      Reconstruido desde cero.

- [x] **Providers**
      Se reconstruyen con registry persistente y adapters.

- [x] **Models**
      Se reconstruyen con registry separado y defaults por provider.

- [x] **Historial/sesiones**
      Se reconstruye con Session Store propio, provider-independent.

- [x] **RINARI.md**
      Se reconstruye como sistema jerárquico de instrucciones de proyecto.

- [x] **Modo agente**
      Se reconstruye sobre Agent Runtime + policy + verification.

- [x] **Tools**
      Catálogo y contrato definidos en `docs/tools.md`.

- [x] **Skills**
      Catálogo y contrato definidos en `docs/skills.md`.

- [x] **MCP**
      Se integra como capability provider que pasa por Tool Runtime.

- [x] **Web/browser**
      Se integran como subsistemas de primera clase.

- [x] **Hooks**
      Se integran como lifecycle extension points controlados.

- [x] **Subagentes**
      Se integran mediante Agent Orchestrator y permisos/budgets propios.

- [x] **Setup / doctor**
      Quedan definidos en la superficie CLI.

### Arquitectura

- [x] **System Stack**
      Definido.

- [x] **Separación Soul / Constitution / Policy / Skills**
      Definida.

- [x] **Prompt assembly**
      Definido por segmentos y trust level.

- [x] **Tool Runtime**
      Definido.

- [x] **Skill Runtime**
      Definido.

- [x] **Context Engine**
      Definido.

- [x] **Memory architecture**
      Definida.

- [x] **Artifact Store**
      Definido.

- [x] **Policy / Sandbox / Approvals**
      Separados arquitectónicamente.

- [x] **Subagents / multi-agent**
      Definidos como runtime integrado.

- [x] **Observability / evals**
      Definidos.

- [x] **Estructura objetivo de `src/rinari/`**
      Definida en `docs/harness.md`.

### Sesiones

- [x] **CHAT**
      Definido.

- [x] **PROJECT**
      Definido.

- [x] **`rinari chat`**
      Fuerza CHAT.

- [x] **`rinari`**
      AUTO según contexto.

- [x] **CHAT → PROJECT**
      Promoción in-session definida.

- [x] **Project root vs cwd**
      Separados.

- [x] **Project trust**
      Definido.

- [x] **Resume**
      Reconciliation definido.

### Providers y modelos

- [x] **Multi-provider**
      Varios providers pueden coexistir guardados.

- [x] **Login / API key**
      Soportados según adapter.

- [x] **Custom provider**
      Soportado.

- [x] **OpenAI-compatible**
      Soportado como adapter/protocolo custom.

- [x] **Multi-model**
      Varios modelos por provider.

- [x] **Provider-specific model memory**
      Cada provider recuerda default/last-used model.

- [x] **Switch sin delete**
      Semántica definida como invariante.

- [x] **Credential references**
      Separados de config normal.

### CLI

- [x] **Comando raíz**
      `rinari`.

- [x] **Superficie completa**
      Definida en `docs/commands.md`.

- [x] **Setup**
      Definido.

- [x] **Help**
      Definido.

- [x] **Doctor**
      Definido.

- [x] **Provider/providers**
      Definidos.

- [x] **Model/models**
      Definidos.

- [x] **Chat**
      Definido.

- [x] **Project**
      Definido.

- [x] **Session/resume**
      Definidos.

- [x] **Tools/skills/agents/plugins/MCP**
      Definidos.

- [x] **JSON / non-interactive**
      Contrato definido.

- [x] **Exit codes**
      Definidos.

### CLI visual

- [x] **Banner inicial**
      ASCII de Rinari + versión/runtime card.

- [x] **Model info**
      Provider, alias/model ID, capabilities.

- [x] **Reasoning effort**
      Visible cuando está configurado/soportado.

- [x] **Context usage**
      Visible cuando es conocido.

- [x] **Input/output tokens**
      Contabilizados desde datos reales del provider/runtime.

- [x] **Cached/reasoning tokens**
      Solo visibles si realmente existen.

- [x] **Cost**
      Solo calculado/mostrado con datos confiables.

- [x] **Status rail**
      Definido para ejecución activa.

- [x] **Tools/skills/agents**
      Estado visible.

- [x] **TTY / plain / JSON**
      Renderers separados conceptualmente.

- [x] **Sin métricas inventadas**
      `—` / unavailable cuando el provider no entrega la información.

### Configuración

- [x] **Directorio**
      `~/.rinari/`.

- [x] **Formato principal**
      TOML para configuración humana.

- [x] **SQLite**
      Estado persistente estructurado.

- [x] **Artifacts fuera de SQLite**
      Para blobs/logs grandes.

- [x] **Config precedence**
      Definida.

- [x] **Project config**
      Solo ejecutable/activa bajo trust cuando corresponde.

### Stack de implementación

- [x] Python 3.11+
- [x] `uv`
- [x] `src/rinari/`
- [x] `typer`
- [x] `rich`
- [x] `httpx`
- [x] `pytest`
- [x] tests sin red pública por defecto
- [x] TDD cuando aporte claridad

### Git

- [x] `main` como rama estable.
- [x] Trabajo en ramas feature/fix cuando corresponda.
- [x] Commits pequeños y descriptivos.
- [x] Conventional prefixes:

      ```text
      feat:
      fix:
      docs:
      refactor:
      test:
      chore:
      ```

- [x] No push sin petición/autorización.
- [x] No force-push casual.

### Documentación

- [x] `docs/soul.md`
- [x] `docs/stack.md`
- [x] `docs/commands.md`
- [x] `docs/tools.md`
- [x] `docs/skills.md`
- [x] `docs/harness.md`
- [x] `AGENTS.md` alineado con arquitectura actual.
- [x] `TODO.md` alineado con arquitectura actual.
- [x] `LICENSE` (MIT).

### Pendiente real

- [x] **Licencia**
      MIT — confirmada por Xainner el 2026-08-16. `LICENSE` agregado.

## Salida de Fase 0

Ejecutada el 2026-08-16 con la confirmación de la licencia MIT:

```text
[x] registrar decisión
[x] añadir/actualizar LICENSE
[x] marcar Fase 0 completa
[x] mover fase actual a Fase 1
[x] permitir implementación
```

---

# Fase 1 — Fundaciones, bootstrap y persistencia — COMPLETA (2026-08-16)

> Los 4 items que quedaron abiertos al cerrar Fase 1 (`Extended Identity bajo demanda`, `detectar project-creation intent`, `preservar conversación`, `recalcular permisos`) se implementaron al cerrar Fase 2 (2026-08-17): splitting de Soul (canónico siempre, Extended Identity solo en turnos de identidad), promoción in-session con detección de marker en cwd + candidate workspace acotado, persistencia de conversación en `session_messages` (restaurada en cada invocación y en promoción) y recálculo de sandbox/assembler en la misma sesión tras promover.

## Objetivo

Construir la base real sobre la que se conectará todo el harness.

Esto no es el producto final. Es la capa de dependencia requerida por el resto.

## Criterio de aceptación

Debe ser posible:

```text
instalar Rinari
ejecutar setup
guardar múltiples providers
guardar múltiples modelos
cambiar entre ellos sin perder configuración
resolver CHAT/PROJECT correctamente
crear/persistir sesiones
cargar Soul + Constitution
ejecutar doctor/status/version
```

Todo con tests deterministas.

## Packaging

- [x] Crear `pyproject.toml`.
- [x] Configurar Python `>=3.11`.
- [x] Configurar package `rinari`.
- [x] Crear `src/rinari/`.
- [x] Crear entrypoint `rinari`.
- [x] Generar `uv.lock`.
- [x] `uv sync` limpio.
- [x] Definir metadata de build.
- [x] Implementar `rinari --version`.
- [x] Implementar Build Manifest.

## Estructura base

- [x] `src/rinari/cli/`
- [x] `src/rinari/application/`
- [x] `src/rinari/runtime/`
- [x] `src/rinari/providers/`
- [x] `src/rinari/models/`
- [x] `src/rinari/sessions/`
- [x] `src/rinari/projects/`
- [x] `src/rinari/prompts/`
- [x] `src/rinari/policy/`
- [x] `src/rinari/storage/`
- [x] `src/rinari/shared/`
- [x] `assets/`
- [x] `tests/unit/`
- [x] `tests/integration/`
- [x] `tests/e2e/`
- [x] `tests/fixtures/`

## Configuración

- [x] Resolver `~/.rinari/`.
- [x] Crear config por defecto.
- [x] Parser TOML.
- [x] Config schema.
- [x] Config precedence.
- [x] `rinari config list`.
- [x] `rinari config get`.
- [x] `rinari config set`.
- [x] `rinari config unset`.
- [x] `rinari config validate`.
- [x] `rinari config path`.
- [x] Tests de config layering.

## SQLite / estado

- [x] SQLite connection layer.
- [x] WAL mode.
- [x] Schema migrations.
- [x] IDs deterministas/seguros.
- [x] Event Store base.
- [x] tablas `providers`.
- [x] tablas `models`.
- [x] tablas `projects`.
- [x] tablas `sessions`.
- [x] tablas `session_events`.
- [x] transactional repositories.
- [x] migration tests.

## Credential Store

- [x] Interfaz `CredentialStore`.
- [x] Secret references.
- [x] Backend seguro inicial.
- [x] Environment reference backend.
- [x] Redacción base.
- [x] Nunca persistir plaintext en config normal.
- [x] Tests de no-leak.

## Provider Registry

- [x] `ProviderRecord`.
- [x] IDs inmutables.
- [x] aliases mutables.
- [x] Provider Adapter interface.
- [x] auth capabilities.
- [x] login flow abstraction.
- [x] API-key flow abstraction.
- [x] custom provider adapter.
- [x] OpenAI-compatible adapter.
- [x] health/test capability.
- [x] provider registry persistence.

## Provider CLI

- [x] `rinari provider`.
- [x] `rinari provider current`.
- [x] `rinari provider use`.
- [x] `rinari providers list`.
- [x] `rinari providers add`.
- [x] `rinari providers login`.
- [x] `rinari providers logout`.
- [x] `rinari providers auth`.
- [x] `rinari providers show`.
- [x] `rinari providers test`.
- [x] `rinari providers rename`.
- [x] `rinari providers remove`.
- [x] `rinari providers discover`.

## Model Registry

- [x] `ModelRecord`.
- [x] models asociados a provider ID.
- [x] aliases.
- [x] capabilities.
- [x] availability.
- [x] provider default model.
- [x] provider last-used model.
- [x] model resolution.
- [x] model discovery abstraction.

## Model CLI

- [x] `rinari model`.
- [x] `rinari model current`.
- [x] `rinari model use`.
- [x] `rinari models list`.
- [x] `rinari models available`.
- [x] `rinari models refresh`.
- [x] `rinari models add`.
- [x] `rinari models alias`.
- [x] `rinari models show`.
- [x] `rinari models test`.
- [x] `rinari models remove`.

## Provider/model invariants

- [x] test: A → B → A preserva ambos providers.
- [x] test: modelo A1 → A2 → A1 preserva ambos.
- [x] test: logout no elimina provider.
- [x] test: logout no elimina models.
- [x] test: remove elimina solo target.
- [x] test: provider recuerda modelo.
- [x] test: alias rename preserva IDs.
- [x] test: historical session references permanecen válidas.

## Soul + Constitution

- [x] empaquetar Canonical Soul.
- [x] loader de Soul.
- [x] override `~/.rinari/soul.md`.
- [x] extraer/injectar solo Canonical Soul normalmente.
- [x] Extended Identity bajo demanda. (Fase 2: `split_soul` — el canonical se inyecta
  siempre; Extended Identity Reference solo cuando el turno pide identidad)
- [x] crear/empaquetar `constitution.md`.
- [x] loader de Constitution.
- [x] hashes/version metadata.
- [x] tests de resolution/fallback.

## Project Detector

- [x] detectar `.rinari/project.toml`.
- [x] detectar `.git`.
- [x] detectar markers secundarios.
- [x] caminar `cwd → parent`.
- [x] nested repo behavior.
- [x] project root vs cwd.
- [x] Project Identity.
- [x] canonical path.
- [x] fingerprint Git cuando aplique.

## Session kinds

- [x] `CHAT`.
- [x] `PROJECT`.
- [x] plain `rinari` AUTO.
- [x] `rinari chat` explícito.
- [x] Session Store.
- [x] Session Event base.
- [x] Session title.
- [x] session list/show/new.
- [x] session persistence.

## CHAT → PROJECT

- [x] `ProjectLifecycle`.
- [x] detectar project-creation intent. (Fase 2: marker fuerte en el cwd de la sesión
  tras el turno → `promote` in-place; el caminado hacia arriba NO promueve)
- [x] candidate workspace acotado.
- [x] re-detect después de `git init`.
- [x] re-detect después de scaffold.
- [x] re-detect después de clone.
- [x] re-detect después de `rinari init`.
- [x] promoción atómica Session CHAT → PROJECT.
- [x] preservar session ID.
- [x] preservar conversación. (Fase 2: `session_messages` persiste cada turno —
  también cancelados — y se restaura en cada invocación y en la promoción)
- [x] preservar provider/model.
- [x] recalcular permisos. (Fase 2: tras promover, el ToolContext (sandbox/cwd/kind)
  y el assembler context se reconstruyen en la misma sesión; event
  `SessionPromotedInProcess`)
- [x] evento `SessionPromotedToProject`.
- [x] rollback/reconcile si promoción falla.

## Safety invariant

- [x] test: `$HOME` no es implicit writable workspace.
- [x] test: carpeta arbitraria sin marker → CHAT.
- [x] test: `rinari chat` dentro de repo sigue siendo CHAT.
- [x] test: proyecto creado explícitamente promueve sesión.

## Setup / Doctor / Status

- [x] `rinari setup`.
- [x] rerun setup sin borrar registros existentes.
- [x] `rinari doctor`.
- [x] `rinari status`.
- [x] `rinari help`.
- [x] `rinari completion`.

---

# Fase 2 — Agent Runtime, Tool Runtime y seguridad base — COMPLETA (2026-08-16)

## Objetivo

Construir el loop autónomo real y las capacidades fundamentales de ingeniería.

## Criterio de aceptación

Rinari debe poder:

```text
entrar a PROJECT
inspeccionar repo
editar archivos
ejecutar comandos
ver Git
usar policy/sandbox/approvals
cancelarse correctamente
persistir tool results
seguir un agent loop real
```

## Model Runtime

- [x] `ModelProvider` abstraction. (ModelRouter + ModelCaller)
- [x] model invocation.
- [x] streaming.
- [x] tool calls.
- [x] structured output. (json_response → response_format; Anthropic documenta limit)
- [x] provider capability normalization.
- [x] token usage normalization.
- [x] reasoning effort config cuando exista.
- [x] model switch in-session. (`/model <alias>`, session-scoped)
- [x] provider switch in-session. (`/provider <alias>`, session-scoped)
- [x] session state provider-independent. (history = ChatMessage records)

## Prompt Assembler

- [x] `PromptSegment`.
- [x] authority.
- [x] trust.
- [x] cache policy.
- [x] stable ordering.
- [x] Constitution segment.
- [x] Runtime Policy segment.
- [x] Soul segment.
- [x] Preferences segment.
- [x] Project instruction slot. (RINARI.md/AGENTS.md/CLAUDE.md, bounded)
- [x] Skill slot.
- [x] Task/context slot.
- [x] evidence/untrusted wrapping.
- [x] tests de precedence.
- [x] Soul a través del identity loader (override user + version/sha256).
- [x] Extended Identity Reference fuera del stable segment (on-demand por turno).

## Promoción CHAT → PROJECT + conversación (cierre de items de Fase 1)

- [x] Soul: solo Canonical Soul inyectado siempre; Extended Identity y
  Maintainer Notes excluidos (`split_soul`, segment on-demand por keywords).
- [x] candidate workspace acotado para CHAT: escritura permitida solo en el
  cwd abierto (nunca $HOME); test de invariante $HOME.
- [x] detección de project-creation intent: marker fuerte (`.git` o
  `.rinari/project.toml`) en el cwd de la sesión al terminar el turno →
  promoción in-place; el caminado hacia arriba no promueve (no revierte un
  `rinari chat` explícito).
- [x] promoción in-place reconstruye en la misma sesión: sandbox/cwd/kind del
  ToolContext + assembler context (proj instructions + policy summary); evento
  `SessionPromotedInProcess`.
- [x] `session_messages` (migración 0002): persistencia de la conversación en
  cada turno (incluidos cancelados), restaurada en cada invocación.
- [x] preservar sesión/provider/model al promover (misma verificación en test).
- [x] E2E: CHAT → `rinari init` → PROJECT en la misma sesión con conversación
  restaurada (mock stateless ecualiza tamaño de conversación 2 → 4 msgs).

## Tool Registry

- [x] `ToolDefinition`.
- [x] input schema.
- [x] output schema.
- [x] risk.
- [x] side-effect class.
- [x] permissions.
- [x] idempotency metadata.
- [x] timeout metadata.
- [x] registry.
- [x] search.
- [x] describe.
- [x] load.
- [x] unload.
- [x] manifests.

## Tool Result

- [x] envelope común.
- [x] normalized errors.
- [x] provenance.
- [x] side-effect records.
- [x] artifact references.
- [x] truncation metadata.

## Tool Runtime

- [x] schema validation.
- [x] capability resolution.
- [x] policy check.
- [x] approval gate.
- [x] sandbox execution.
- [x] result normalization.
- [x] secret redaction.
- [x] event persistence.
- [x] cancellation.
- [x] budgets.
- [x] artifact spill.

## Filesystem tools

- [x] `fs.read`.
- [x] `fs.read_lines`.
- [x] `fs.write`.
- [x] `fs.patch`.
- [x] `fs.list`.
- [x] `fs.glob`.
- [x] `fs.search_text`.
- [x] `fs.stat`.
- [x] `fs.diff`.
- [x] safe path canonicalization.
- [x] symlink boundary tests.

## Shell / process

- [x] `shell.exec`.
- [x] streaming stdout/stderr vivo al usuario en REPL (sink por chunk; el
  resultado al modelo sigue siendo el bounded buffer final)
- [x] timeout.
- [x] cwd.
- [x] env injection.
- [x] PTY. (fase 3; tools `pty.*` con POSIX pty pair; en Windows `DEPENDENCY_ERROR` → `process.*`)
- [x] process handles. (registry por sesión + tools `process.wait/output/signal/list`)
- [x] process wait.
- [x] process signal. (INT/TERM/KILL en todas las plataformas vía killpg/taskkill;
  HUP/CONT en POSIX; kill-tree en timeout/cancel)
- [x] cancellation tree. (kill process group: win32 + posix)
- [x] output limits.
- [x] artifact spill.

## Git

- [x] status.
- [x] diff.
- [x] log.
- [x] show.
- [x] branch metadata.
- [x] dirty-worktree baseline. (migración 0003 `worktree_baselines`; snapshot
  porcelain+sha256 al abrir la sesión PROJECT, capturado una vez)
- [x] local Git policy.
- [x] remote Git classification.
- [x] preserve user changes. (runtime: `WorktreeGuard` fuerza approval antes
  de sobrescribir un file con cambios no commiteados previos a la sesión;
  test dedicado + E2E con mock: overwrite denegado, archivo intacto)
- [x] safe diff ownership metadata. (fase 3)  (`git.status` etiqueta cada path:
  `user` | `modified-in-session` | `new-in-session` + legend; `fs.write`/
  `fs.patch` devuelven `user_pre_existing_changes` cuando aplican)

## Policy Engine

- [x] capability model.
- [x] filesystem policy.
- [x] shell policy.
- [x] Git policy.
- [x] network policy foundation. (fase 4; `policy/network.py`: modo
      off/ask/allow + reglas allow/deny persistentes por host con matching
      exacto/subdominio, authoritative sobre requests del modelo; el gate del
      Tool Runtime lo aplica a `network.outbound` y audita cada decisión)
- [x] secret policy. (sensitive-file locked rule)
- [x] action-risk model.
- [x] policy explanation. (PolicyDecision.reason)
- [x] locked organization/system rules.

## Sandbox

- [x] `read-only`.
- [x] `workspace`.
- [x] `full-access`.
- [x] filesystem roots.
- [x] process limits.
- [x] network hooks. (fase 4; `NetworkGuard.assert_reachable` en
      `ToolContext.network`: los tools de red (web/http/browser de fase 5)
      deben pasar cada target por el guard — los DENYs (modo off, reglas deny,
      target inresoluble) se imponen en código, fail-closed, sin depender del
      modelo)
- [x] secret scopes. (redaction de secrets de providers)
- [x] tests de escape.

Implementation (network policy foundation + hooks): `NetworkPolicy`
(`policy/network.py`) decide por target: 1) target inresoluble → deny;
2) regla DENY → deny (siempre gana sobre el modo); 3) `network.mode=off` →
deny; 4) regla ALLOW → allow (corta el ask); 5) `network.mode=allow`; 6)
default `ask`. Matching exacto o subdominio (`github.com` cubre
`api.github.com`, no a `evilgithub.com`); `normalize_host` reduce URLs/
host:port/user@host a host canónico. Persistencia: migración 0014
(`network_rules` con UNIQUE(scope,host,decision), `network_events` audit).
`NetworkService` (repositorio + `mode()` desde config) expone
`status/test/rules/add_rule/remove_rule/events/log_event`; las reglas se
leen lazy por decisión, así `rinari network allow` aplica a una sesión en
ejecución sin restart. Integridad: `CAPABILITY_NETWORK="network.outbound"`
en `PolicyEngine` (host por decisión), classify default para namespaces
`web`/`http`/`browser`, ToolRuntime pasa `host` al policy y audita cada
decisión en `network_events`; `NetworkGuard` en `ToolContext.network`
enforcing técnico de DENYs para los tools de red de fase 5. CLI `rinari
network status|test|rules|allow|deny|remove|history`. 18 tests
(`test_network_policy.py`) + migración 0014.

## Approval Engine

- [x] allow.
- [x] prompt.
- [x] deny.
- [x] once scope.
- [x] session scope.
- [x] project scope.
- [x] persistent scope.
- [x] approval audit events.
- [x] no approval fatigue para reads normales.

## Agent Loop

- [x] RECEIVE.
- [x] ORIENT.
- [x] PLAN.
- [x] EXECUTE.
- [x] OBSERVE.
- [x] EVALUATE.
- [x] recovery. (errores de tool vuelven al modelo como tool messages)
- [x] waiting approval. (approval gate dentro del tool runtime)
- [x] blocked. (deny → tool error al modelo)
- [x] verify transition. (fase 3; segment `task-state` con completion contract + tools `verify.*`)
- [x] finalize transition. (fase 3; `run_turn` re-evalúa el gate post-turno, evento `CompletionGateEvaluated`, `TurnResult.completion`)

## Cancellation

- [x] Ctrl+C model stream.
- [x] Ctrl+C tool.
- [x] Ctrl+C subprocess.
- [x] session interruption state. (`run_turn` marca la sesión `interrupted` al
  cancelar y `active` al reanudar; visible en `rinari sessions`)
- [x] second interrupt hard stop.
- [x] cleanup.

## Trace base

- [x] session trace.
- [x] turn trace.
- [x] model call trace.
- [x] tool trace.
- [x] policy decision trace.
- [x] approval trace.
- [x] secret redaction.

---

# Fase 3 — Project intelligence, instructions y verification — COMPLETA (2026-08-16)

## Objetivo

Llevar el agente de “puede ejecutar” a “entiende repos y puede demostrar que terminó”.

## Criterio de aceptación

Una tarea de coding real debe poder completarse de punta a punta con:

```text
project instructions
repository understanding
minimal diff
tests
verification records
completion gate
dirty-worktree protection
```

## Project Trust

- [x] Trust Store. (`trust_entries`; grant por path canonical + fingerprint de identidad)
- [x] `rinari trust status`.
- [x] `rinari trust list`.
- [x] `rinari trust add`. (captura fingerprint: git HEAD+remotes, o marker, o path)
- [x] `rinari trust remove`.
- [x] untrusted project restrictions. (instrucciones del project retenidas hasta grant
      explícito; aviso en start/resume; `ProjectTrustChecked` en el trace; `rinari init`
      auto-confía el root creado)
- [x] trust revalidation. (fingerprint divergente → `revalidation-required`; re-grant
      explícito restaura; path desaparecido → `not-found`)

## RINARI.md

- [x] global engineering instruction resolver. (`~/.rinari/RINARI.md`, user-owned,
      siempre confiado; resolver en `rinari/instructions/resolver.py`)
- [x] root `RINARI.md`.
- [x] nested `RINARI.md`.
- [x] `RINARI.override.md`. (reemplaza a RINARI.md en su level, nunca apila)
- [x] root → cwd chain. (root → ... → cwd; el level más profundo gana)
- [x] scope metadata. (`global` | `root` | `dir:<rel>` + kind + sha256 + size por file)
- [x] trust metadata. (project confiado → files de la chain; no confiado → solo global)
- [x] instruction precedence tests. (`tests/unit/test_instructions_resolver.py`)
- [x] random README remains untrusted data. (nunca se lee como instruction)

## Repository state

- [x] language detection. (por extensión, conteo y ranking; `rinari/repo/state.py`)
- [x] framework hints. (django/flask/fastapi, react/vue/next/express, go-modules, gradle)
- [x] package manager detection. (uv/poetry/pip, npm/yarn/pnpm, cargo, go, maven, gradle, bundler)
- [x] build command discovery. (`<pm> run build`, cargo/go/maven/gradle/uv build, con source)
- [x] test command discovery. (scripts.test, cargo test, go test, pytest por dep/pyproject)
- [x] lint/typecheck discovery. (ruff, eslint, rubocop, mypy, tsc, scripts.typecheck)
- [x] generated file hints. (dirs generados detectados y excluidos del scan)
- [x] repository summary. (dict para el segment environment del prompt; 7 tests)

## Search

- [x] exact file search. (`search.files`, glob under the session root; `fs.glob` pre-existing)
- [x] regex/grep. (`search.regex`, regex por línea, include + bounds; `fs.search_text` literal pre-existing)
- [x] symbol search. (`search.symbols`, py/js/ts/rs/go; py/js/rs/go extraction)
- [x] references. (`search.references`, excluye definitions por defecto, flag para incluirlas)
- [x] structural search. (queries calificados `Class.method` en `search.symbols`)
- [x] hybrid search. (`search.hybrid`, ranked con reasons explicables: definition/ref/text/filename)
- Core de búsqueda pur en `rinari/repo/search.py` (bounded, skip dirs generados);
  los tools son read-only y clasifican como `fs.read` sobre su base path
  (policy normal, sin capability especial). 7 tests (test_search.py).

## Tree-sitter / AST

- [x] parser abstraction. (`rinari/ast/base.py`: `AstSymbol`/`AstImport`/`AstCall`/`AstSummary` + registry `get_parser`/`analyze_file`)
- [x] structural queries. (grammar unificado: `symbols`, `functions`, `methods`, `classes`, `imports`, `calls`, `calls:NAME`)
- [x] supported language adapters. (tree-sitter Python — symbols/imports/calls con calificación `Class.method`; regex py/js/ts/rs/go)
- [x] safe fallback. (sin grammar o parse roto → regex; sin parser → `None`/`unavailable`; `search.symbols` consume la capa AST)

Implementation (phase 3): paquete `src/rinari/ast/` (`base`, `regex_adapter`,
`tree_sitter_adapter`, `registry`). tree-sitter >= 0.26: la capsule se envuelve
con `ts.Language(...)`, todos los patterns S-expr van entre paréntesis y
`QueryCursor.captures(node)` devuelve `dict[str, list[Node]]`. El adapter
degrade graciosamente si los grammar no son importables. `find_symbols`
mejora extracción Python vía AST manteniendo fallback regex. 8 tests
(`test_ast.py`) + suite completa 359 passed / 2 skipped.

## LSP

- [x] definition. (`lsp.definition`, 1-based → LSP 0-based, resultados normalizados file/line/column)
- [x] references. (`lsp.references`, incl. definición desde el server)
- [x] symbols. (`lsp.symbols`, tree documentSymbol aplanada con `qualified_name`)
- [x] diagnostics. (`lsp.diagnostics`, últimos `publishDiagnostics` publicados por uri)
- [x] hover/type info. (`lsp.hover`)
- [x] signatures. (`lsp.signature`, signatureHelp normalizado)
- [x] rename capability when safe. (`lsp.rename` solo planea el WorkspaceEdit y lo devuelve; aplicar es un paso fs explícito)
- [x] lifecycle management. (spawn lazy por lenguaje, manager por sesión en ToolContext, `shutdown_all` en atexit, crash detectado y no re-spawn, spawn failure recordado por sesión)

Implementation (phase 3): `src/rinari/lsp/` — `client.py` (framing
Content-Length, JSON-RPC sobre stdio, handshake initialize/initialized,
sincronización de documentos full-sync, correlación de requests con thread
de lectura, `LspRequestTimeout`/`LspServerCrashed` estructurados) y
`manager.py` (gating por capabilities anunciados en `initialize`, specs por
lenguaje). Sin LSPs embebidos por default: `default_specs()` detecta
`pyright-langserver` / `typescript-language-server` en PATH; sin server, los
tools devuelven `DEPENDENCY_ERROR` con hint a `search.*` (grep fallback
universal, stack.md 69). 21 tests contra fake LSP server stdio
(`tests/fixtures/fake_lsp_server.py`, deterministicos, sin red).

## Repository Index

- [x] project-scoped index. (migración 0005: `repo_index_*` keyed por project root)
- [x] file hashes. (sha256 por file en `repo_index_files`)
- [x] symbols. (cache JSON por file; tabla `repo_index_symbols` con `qualified_name`)
- [x] imports. (`imports_json` por file, usado para el test mapping)
- [x] references. (`repo_index_references`; exclusión de definitions; bounded text scan)
- [x] test mapping. (imports + heurística `test_*.py`; `tests_touching(files)`)
- [x] incremental invalidation. (mismo hash → no se reparse; changed/removed contados)
- [x] optional semantic layer. (declarada como `none`; nunca default — stack.md 68)
- [x] `rinari index ...`. (`status|build|update|rebuild|clear|search|doctor`, commands.md 54)

Implementation (phase 3): core puro en `src/rinari/repo/index.py`
(`build_index` incremental, `_scan_references`, `_test_mapping`,
`query_index`, `doctor_index`), store en
`src/rinari/storage/repositories/index.py`, service application-level en
`src/rinari/repo/index_service.py` y CLI en `src/rinari/cli/commands/index.py`.
Cross-file data (references/test-map) se recalcula cada build a partir del
cache por-file + text scan bounded; lo que evita el reparse es el hash cache
(harness.md 99). 9 tests (`test_repo_index.py`) + suite completa 390 passed /
2 skipped.

## Task Graph

- [x] Task nodes. (migración 0006 `tasks`, project-scoped, IDs `task_*`)
- [x] dependencies. (DAG con detección de ciclos en add y update)
- [x] status. (`pending | in_progress | done | blocked | cancelled`)
- [x] blockers. (texto de bloqueo + tareas waiting-on dependencies)
- [x] acceptance criteria. (checklists por grupo: `[x]`/`[ ]`, líneas sueltas = insatisfechas)
- [x] evidence refs. (append en `evidence`, refs semicolon-separated)
- [x] `rinari tasks ...`. (`list|show|tree|add|update|cancel|retry|blockers`, commands.md 31)

## Done-when contract

- [x] task-specific acceptance criteria. (obligatorio y no vacío para completar)
- [x] implementation criteria. (vacío = vacuously true)
- [x] validation criteria. (obligatorio y no vacío para completar)
- [x] scope criteria. (vacío = vacuously true)
- [x] unresolved criteria. (cualquiera pendiente impide `done`)

Implementation (phase 3): core puro en `src/rinari/tasks/core.py`
(máquina de estados, `done_when_report`, `completion_blockers`, detección
de ciclos, profundidades para `tree`), store en
`src/rinari/storage/repositories/tasks.py`, service en
`src/rinari/tasks/service.py` (reglas con errores estructurados: `BlockedError`
para deps pending, `ConflictError` para ciclos, `ValidationFailureError`
para done-when insatisfecho) y CLI en
`src/rinari/cli/commands/tasks.py`. 12 tests (`test_tasks.py`) + suite
completa 402 passed / 2 skipped.

## Validation Records

- [x] test.
- [x] lint.
- [x] typecheck.
- [x] build.
- [x] schema.
- [x] manual check.
- [x] custom.
- [x] persistent evidence.

## Verification Planner

- [x] changed-file analysis.
- [x] targeted test selection.
- [x] adjacent tests.
- [x] broader suite escalation.
- [x] user constraints.
- [x] project instructions.
- [x] risk input.

## Completion Gate

- [x] DONE.
- [x] IMPLEMENTED_UNVERIFIED.
- [x] PARTIAL.
- [x] BLOCKED.
- [x] FAILED.
- [x] reject false test success.
- [x] reject false deploy success.
- [x] reject “fixed” with no evidence when evidence is required.
- [x] unresolved failure detection.

Implementation (phase 3): migración 0007 `validation_records` (evidencia
persistente por project root, la más reciente por kind gana), core pur en
`src/rinari/verify/` (`records.py` kinds/resultados, `planner.py` selección
targeted/adjacent/broader + risk + discovered commands, `gate.py` outcomes con
rechazo de falso-éxito y "no tests ran"), service en `service.py`, store en
`storage/repositories/validation.py`, tools `verify.plan|record|evaluate`
(capabilities `state.read`/`state.write` en la Policy Engine, sin approval).
**Verify/finalize transitions**: el prompt segment `task-state` de sesiones
PROJECT lleva el completion contract, y `run_turn` re-evalúa el gate tras cada
turno con tool activity, persiste `CompletionGateEvaluated` y expone el
outcome en `TurnResult.completion` (visible en REPL/JSON). 22 tests
(`test_verify.py`) + suite completa 423 passed / 3 skipped.

## Checkpoints / Undo

- [x] checkpoint create.
- [x] checkpoint list.
- [x] checkpoint restore.
- [x] checkpoint remove.
- [x] agent-owned change tracking.
- [x] mixed ownership detection.
- [x] `rinari undo`.
- [x] undo preview.

Implementation (phase 3): migración 0008 `checkpoints` + `checkpoint_files`
(snapshot de bytes por path; 16MB cap). `src/rinari/checkpoints/core.py`
clasifica el tree dirty contra el baseline de sesión (`agent`/`user`/`mixed`
via `projects.worktree`), `plan_restore` decide restore/delete/skip y
`apply_restore` reescribe contenido snapshot. `service.py` resuelve sesión
(auto-detect latest PROJECT del proyecto, fallback error con hint),
`checkpoint_repo` en storage. CLI `rinari undo create|list|preview|restore|remove`
y bare `rinari undo` = restore del checkpoint más reciente. Restore es
**conservador**: solo paths `agent`; `user` nunca se toca; `mixed` se salta
salvo `--allow-mixed` (mixed ownership detection). 6 tests
(`test_checkpoints.py`) con git repo real aislado.

---

# Fase 4 — Context, artifacts, memoria y resume durable — COMPLETA (2026-08-17)

## Objetivo

Permitir sesiones largas sin perder verdad operacional.

## Criterio de aceptación

Rinari debe sobrevivir:

```text
muchos tool calls
context pressure
compaction
interrupt
CLI restart
repo cambiado externamente
provider/model switch
```

sin inventar estado.

## Artifact Store

- [x] artifact URI.
- [x] metadata.
- [x] hashes.
- [x] content type.
- [x] file-backed storage.
- [x] search.
- [x] slicing.
- [x] export.
- [x] GC.
- [x] retention.
- [x] provenance.

Implementation (phase 4): migración 0009 `artifacts` (metadata: hash sha256,
content type, byte count, retention `session|project|permanent`, provenance,
summary) con bytes en `<home>/artifacts/<session>/<namespace>/<name>`.
`src/rinari/artifacts/store.py` (ArtifactStore: create/create_text, get,
read_text, lines, list, search — metadata first, content scan acotado por
tamaño — export, remove, gc de sesiones desaparecidas solo con retention
session; URI `artifact://<session>/<namespace>/<name>` con parse/validate
estricto). CLI `rinari artifacts list|show|open|search|export|remove|gc`.
7 tests (`test_artifacts.py`).

## Context Engine

- [x] context budget.
- [x] retrieval.
- [x] ranking.
- [x] pins.
- [x] dedup.
- [x] artifact references.
- [x] history selection.
- [x] project retrieval. (con Project Memory)
- [x] user-memory retrieval. (con User Memory)
- [x] token accounting integration.

## Compaction

- [x] pressure thresholds.
- [x] preserve goal.
- [x] preserve constraints.
- [x] preserve decisions.
- [x] preserve task graph.
- [x] preserve changed files.
- [x] preserve validation.
- [x] preserve approvals.
- [x] preserve blockers.
- [x] preserve artifacts.
- [x] provider-independent compact state.
- [x] long-horizon tests.

Implementation (phase 4): `src/rinari/context/` — `tokens.py` (estimator
determinista chars/4, presión con umbrales harness 70/80/85, window desde
capabilities del provider con fallback `DEFAULT_CONTEXT_WINDOW`),
`compact_state.py` (`CompactState` in dependiente de provider: goal,
constraints, decisions, task graph, changed files, validations, approvals,
blockers, artifacts, project/provider-model; extracción rule-based
conservadora de la conversación + merge de evidencia persistida;
serialización a `sessions.compact_state_json`), `engine.py` (selección del
suffix seguro del historial: nunca huérfanos tool results, min_keep),
`service.py` (`ContextService.maybe_compact` recableada por hook
`on_pressure` del AgentLoop: recorta el historial in-memory _in place_,
persiste estado + evento `ContextCompacted`; `restore_compact_state` para
resume). El prompt segment `compact-state` (harness, TRUSTED, SESSION)
inyecta la verdad compactada en el system prompt. `run_turn` mapea el flag a
`TurnResult.compacted` (visible en REPL/JSON) y el persist de mensajes es
roble ante el recorte in-place (contabilidad `dropped_total`). La
conversación persistida (`session_messages`) NUNCA se recorta: resume
restaura el todo y re-aplica la selección de cola. 11 tests
(`test_context.py`) incluyendo loop end-to-end con provider fake.

Context retrieval / ranking / pins / dedup (Fase 4): `context/retrieval.py`
(`ContextRetrievalService`). Candidatos: archivos y símbolos del repository
index (PROJECT), memoria user+project, y artifacts de la sesión. Ranking
determinista (score por solapamiento de tokens + peso por fuente, pins
primero, luego score, luego prioridad de fuente) y dedup por `(source,
ref)`. Pins de sesión (`context_pins`, migración 0011): `file|symbol|
memory|artifact|term`; un pin inyecta su contenido (acotado) en el segmento
de prompt `pinned-context` (TRUSTED, SESSION) en cada turno, con el contenido
de repo envuelto como `<untrusted>` (prompt-injection rule) y un presupuesto
de caracteres total para el bloque. Tools `context.retrieve|pin|unpin|
list_pins` (capability `state.read`/`state.write`). CLI `rinari context
pins|pin|unpin|retrieve`. 8 tests (`test_context_retrieval.py`) + migración
0011 en `test_migrations.py`.

## User Memory

- [x] record schema.
- [x] provenance.
- [x] confidence.
- [x] explicit durable preferences.
- [x] search.
- [x] update.
- [x] forget.
- [x] sensitivity filter.

## Project Memory

- [x] project namespace.
- [x] stable facts.
- [x] conflict detection.
- [x] stale fact handling.
- [x] promote team-relevant rules to docs when appropriate.

## Episodic / Pattern Memory

- [x] episodic task summaries.
- [x] pattern records.
- [x] provenance.
- [x] no secret storage.
- [x] no automatic inference persistence.

Implementation (phase 4 — Memoria): migración 0010 crea 4 tablas separadas
(`user_memory`, `project_memory`, `episodic_memory`, `pattern_memory`; nunca
un bucket común, harness.md 69). `src/rinari/memory/service.py`
(`MemoryService`): user memory con `kind=preference|rule|fact`, confidence y
provenance; re-enunciar el mismo (kind, topic) refresca el registro y un
conflicto lo supera conservando el anterior como historial (stale handling +
conflict detection); project memory namespaceada por `project_root`;
episodic con resúmenes de tarea acotados a 400 chars; pattern records
(global/user) con dedup por (topic, texto). `find_sensitive_match` rechaza en
escritura cualquier texto con patrones de credential (sk-, ghp_, AKIA, JWT,
private key, bloques base64/hex largos con diversidad, etc.) y valores de
credential conocidos: nunca se guarda un secreto (no-secret storage). No hay
persistencia automática de inferencias: todo registro entra por un store/tool
explícito. `src/rinari/storage/repositories/memory.py` persiste. Tools nativos
`memory.remember/recall/update/forget/episodic` (namespace `memory`,
capability `state.write`/`state.read`, project scope exige sesión PROJECT).
Segmento de prompt `memory` (harness, TRUSTED, SESSION) inyecta el bloque de
memoria durable (user + project) marcándolo posible-estale y de menor
autoridad que las instrucciones. CLI `rinari memory
list|search|show|add|edit|forget` (deletion siempre con scope explícito).
10 tests (`test_memory.py`) + migración 0010 en `test_migrations.py`.
Retrieval de memoria expuesto vía `memory.recall` y el segmento de prompt; el
sistema general de context-retrieval (ranking/pins/dedup) quedó implementado
en el bloque de Context Engine (ver arriba).

## Resume

- [x] CHAT resume.
- [x] PROJECT resume.
- [x] project identity reconciliation.
- [x] branch reconciliation.
- [x] dirty-tree reconciliation.
- [x] trust reconciliation.
- [x] provider auth reconciliation.
- [x] model availability reconciliation.
- [x] skill version reconciliation.
- [x] policy change reconciliation.
- [x] stale assumptions.

Implementation (resume durable + reconciliation):
`SessionService.resume` (y el camino resume-de-`start`) pasa cada sesión por
`ResumeReconciler` (`src/rinari/application/reconcile.py`), que re-verifica
los hechos duraderos que la sesión asume antes de seguirla:

- `identity` — la row de proyecto existe para el root snapshot; si falta, se
  re-registra (y la sesión se re-vincula). Única corrección automática.
- `git-branch` — la rama persistida al inicio (`sessions.git_branch`,
  migración 0012; capturada en `_ensure_worktree_baseline`) vs la rama actual;
  un cambio se reporta, no se absorbe.
- `working-tree` — baseline de worktree (migración 0003) vs estado dirty
  actual; los paths que difieren se listan ("verifica antes de actuar").
- `permissions` — el permission profile active vs el grabado en la sesión
  (policy change).
- `provider` / `model` — las rows siguen existiendo (auth/disponibilidad).
- `trust` — estado de confianza del proyecto (trusted/revalidation/untrusted).
- `assumptions` — el cwd grabado sigue existiendo (stale assumptions).

Todo finding que no sea `ok` se convierte en warning visible; el JSON añade
`data.reconciliation` (subsystem/state/detail/action). No se muta el worktree
y no se descarta ninguna asunción en silencio.

Skill version reconciliation (fundación, patrón "dimensión para fase futura"
como subagent calls en budgets): las skills activas de una sesión se
persisten como pares `(name, version)` en `sessions.active_skills_json`
(migración 0015; el runtime de skills llega en fase 6). Catálogo de skills:
`src/rinari/skills/catalog.py` descubre `SKILL.md` en
`~/.rinari/skills/<name>/` (global) y `<project>/.rinari/skills/<name>/`
(project; shadowing por nombre, requiere trust), parsea el frontmatter
`name/description/version` y, si no hay versión declarada, la identidad es
`sha:<12 hex>` del contenido (todo cambio en disco mueve la versión).
`ResumeReconciler` añade el subsystem `skills`: skill no instalado →
`missing`, versión divergente → `changed` (ambos como warning, sin absorber
silencio). El fork copia `active_skills` y `session_dict` los expone.
13 tests (`test_skill_reconciliation.py`).

## Session fork

- [x] fork state.
- [x] preserve provenance.
- [x] independent continuation.

Implementation (session fork): `rinari session fork <ref> [--name alias]`
(`SessionService.fork`) crea una sesión nueva e independiente desde la
fuente: copia kind, identidad de proyecto, cwd, provider/model/profile, mode,
`compact_state`, `git_branch` grabada y la conversación completa (mensajes con
IDs nuevos; `seq` re-numerado desde 1). Provenance durable: la row lleva
`forked_from` (migración 0013) y el evento `SessionForked {from: <id>}`.
Independencia: el fork tiene su propio id, su propio espacio de mensajes y
estado `active`; la sesión fuente no se modifica (idempotente en mensajes,
título y `updated_at`). `session_dict` expone `git_branch` y `forked_from`.
7 tests (`test_session_fork.py`) + migración 0013.

## Budgets

- [x] model calls.
- [x] tool calls.
- [x] cost.
- [x] wall time.
- [x] subagents.
- [x] recursion depth.
- [x] network calls.

## Loop detection

- [x] same tool/args.
- [x] same error.
- [x] two-action oscillation.
- [x] repeated rewrites.
- [x] repeated denied approval.
- [x] duplicated subagent work.
- [x] force strategy change.

Implementation (budgets + loop detection): `BudgetMeter` +
`TurnBudgetLimits` (`runtime/budget.py`) miden por turno: model calls, tool
calls, network calls (namespaces `web`/`http`/`browser`, que aterrizan en
fase 5 pero ya cuentan al existir), wall time (vía `Clock` inyectado), cost
(estimado desde usage real × pricing declarado en los límites; sin pricing el
costo queda sin medir y nunca se inventa), subagents y recursion depth
(los límites ya existen para el runtime multi-agente posterior). El gate se
evalúa antes de cada model call (`>=`) y antes de cada tool call
(`allows_tool`); al agotarse el turno termina con `kind="budget"`, contenido
con la dimensión responsable y snapshot observable del medidor
(`TurnResult.budget`, evento `AgentTurnCompleted.budget`). `LoopDetector`
(`runtime/loopdetection.py`) es por-turno (no cruza turns) y detecta: mismo
tool+args canónicos, oscilación A-B de dos acciones, rewrites repetidas del
mismo path (`fs.write`/`fs.patch`), mismo error (signatura code+prefijo de
mensaje), approval denegada repetida y subagent duplicado. Primera detección
inyecta un nudge del harness (`[harness loop-detector] ...` como mensaje de
usuario) forzando cambio de estrategia; segunda de la misma clase detiene el
turno con `kind="loop"` (evento `LoopDetected`). `run_turn` crea los dos por
turno; JSON y REPL exponen el resultado. 18 tests (`test_budget_loop.py`,
incluyendo integración real del loop con modelo scripted).

---

# Fase 5 — Web, browser y capability ecosystem (completada 2026-08-20)

## Objetivo

Integrar acceso externo real bajo el mismo Tool Runtime.

## Criterio de aceptación

Web, browser, plugins, MCP y OpenAPI deben funcionar como capacidades normalizadas, sin bypass de policy, tracing o artifacts.

## Web

- [x] search.
- [x] fetch.
- [x] open.
- [x] find.
- [x] links.
- [x] download.
- [x] content extraction.
- [x] provenance.
- [x] citations/evidence.

Implementation (web): estado real de acceso externo bajo el mismo Tool
Runtime. Transporte acotado en `src/rinari/web/client.py`: `fetch` con
`MAX_RESPONSE_BYTES` (2 MiB), redirect-following, UA estable y mapeo de
fallos httpx -> `ToolError` codes (404 NOT_FOUND, 401/403
PERMISSION_DENIED, 429 RATE_LIMITED, 5xx/conn NETWORK_ERROR retryable,
timeout TIMEOUT); `search` keyless sobre el endpoint HTML de DuckDuckGo
(sin API key ni dependencia extra). Extracción de contenido con stdlib solo
en `src/rinari/web/html.py`: `parse_page` produce `Page` (title, meta,
language, charset, links absolutos dedup, tables acotadas, y token stream
para `text()`/`markdown()`). Sin parser de terceros; el HTML remoto se trata
como dato desconfiado (nunca se ejecuta).

Tools nativos (tools.md section 5) en `src/rinari/tools/native/web.py`:
`web.search`, `web.fetch`, `web.open`, `web.links`, `web.find` (búsqueda
literal en el texto extraído), `web.extract_text`, `web.extract_markdown`,
`web.extract_metadata`, `web.download` (al artifact dir de la sesión, 2 MiB),
`web.cite` y `web.sources` (citations/evidence: hash sha256,
fetched_at vía `now_iso(clock)`, title y snippet). Todos los tools con
`capabilities=("network.outbound",)` y `namespace="web"`, por lo que el gate
de `network.outbound` del Tool Runtime los captura y el `NetworkGuard`
(`ToolContext.network`) bloquea DENY en código antes de dialar. El transport
se inyecta por `ToolContext.web` (factory de client httpx-compat; en tests
`httpx.MockTransport`, sin sockets). 25 tests (`test_web.py`), incluyendo un
camino end-to-end por `ToolRuntime` real con policy deny/allow.

## HTTP

- [x] generic request.
- [x] streaming.
- [x] SSE.
- [x] auth injection.
- [x] timeout.
- [x] retry policy.
- [x] rate-limit errors.
- [x] artifact response handling.

Implementation (http): extensión del transporte web con métodos arbitrarios,
bodies, retry con backoff, `Retry-After` y SSE. En `src/rinari/http/client.py`
`request()` soporta GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS, `body`/`body_json`,
`query`, `headers`, `follow_redirects`, `timeout_s` (clamp 1..120) y
`max_bytes` (clamp 4 KiB..32 MiB). Retry: solo métodos idempotentes ante
429/5xx y ante errores de transporte (timeout/connect); backoff exponencial
`BASE_BACKOFF_S * 2^attempt` acotado a 30 s, o bien el valor de `Retry-After`
(delta-seconds o HTTP-date vía `parse_retry_after`). Una interacción
completada 4xx/5xx es *dato* (el tool hace la petición y reporta la respuesta);
solo fallos de transporte levantan `ToolError` codes. `sse_lines()` emite las
líneas crudas de un `text/event-stream` (validando content-type) y
`src/rinari/http/sse.py::parse_sse_events` implementa la semántica WHATWG SSE
(data multi-línea unida con newlines, event/id/retry, comentarios ignorados),
acotada a `max_events`.

Auth y redacción en `src/rinari/http/auth.py`: `apply_auth` inyecta credenciales
solo vía *secret references* (`env://VAR`, `file://key`) resueltas por el
`CredentialStore` de la sesión (`ToolContext.credentials`), nunca como plaintext
en los argumentos; soporta bearer, basic, header y query. `redact_headers`
reemplaza por `***` todo header sensible (Authorization, X-Api-Key,
proxy-authorization, cookie, token, ...) en la respuesta y `redact_url` borra el
valor de query params secretos en `final_url`. La redacción ocurre en el
transporte, así que los secretos nunca llegan al modelo ni a traces.

Tools nativos en `src/rinari/tools/native/http.py`: `http.request` (petición
genérica; `save_as` para guardar el body en el artifact dir, o auto-artifact para
bodys binarios/por encima del preview; `retry_after_s` en 429; `attempts`) y
`http.sse` (consume un endpoint SSE con `event_filter`, `max_events` y timeout).
Ambos con `capabilities=("network.outbound",)` y `namespace="http"` (el gate del
Tool Runtime + `NetworkGuard` aplican igual que en web). 42 tests
(`test_http_tools.py`) sin sockets: `httpx.MockTransport` vía el seam
`ToolContext.web`, `sleep` fake para el backoff y `CredentialStore` fake para el
auth; incluye caminend-to-end por `ToolRuntime` real con policy deny/allow y
verificación de que el secreto inyectado nunca aparece en el resultado.

## Browser engine (decisión, 2026-08-17)

**Motor elegido: CDP directo.** `src/rinari/browser/` implementa el mínimo
necesario para hablar Chrome DevTools Protocol:

- `ws.py`: client WebSocket RFC6455 sobre sockets stdlib (handshake con
  `Sec-WebSocket-Key`/`Accept` verificado, frames text con masking de cliente,
  continuation, ping/pong, close, tope de frame 8 MiB). Sin dependencias.
- `cdp.py`: sesión CDP JSON-RPC sobre el WS (request/response con matching por
  id + timeout, hilo de lectura que despacha a waiters y cola de events;
  sesiones *flattened* por target vía `Target.attachToTarget`).
- `manager.py`: `BrowserManager` (session-scoped): descubrir/conectar endpoint
  CDP (`RINARI_BROWSER_CDP` o `browser.launch`), tab lifecycle, operaciones
  acotadas (evaluate/snapshot/screenshot/input/cookies/consola/red).
  Lanzamiento gestionado: Chromium headless con `user-data-dir` aislado en
  `~/.rinari/browser/profiles/<session_id>` (login/session isolation) y
  ownership del subprocess (se mata en `close` solo si Rinari lo lanzó).

**Alternativa documentada: Playwright.** Engine off-the-shelf que también habla
CDP (y BiDi) con helpers de alto nivel. No se adopta como engine inicial por:
(1) dependencia pesada + descarga de binario del browser en el setup, en
conflicto con la estrategia de dependencias de stack.md y con tests
deterministas; (2) el alcance actual (navegación, snapshot, input, screenshots,
observe, upload/download con policy) se cubre con una decena de comandos CDP.
Si se requieren después a11y-snapshots ricos, auto-wait, tracing o BiDi, el
swap es local: `BrowserManager` aísla el driver tras su interfaz y los tools
`browser.*` no cambian.

## Browser Runtime

- [x] Browser Manager.
- [x] browser lifecycle.
- [x] tabs.
- [x] navigation.
- [x] DOM snapshot.
- [x] accessibility tree.
- [x] screenshot.
- [x] click.
- [x] type/fill.
- [x] select.
- [x] check/uncheck.
- [x] drag.
- [x] scroll.
- [x] forms.
- [x] upload.
- [x] download.
- [x] cookies/storage.
- [x] console.
- [x] network observation.
- [x] bounded evaluate.
- [ ] auth profiles.
- [x] cancellation.
- [x] artifact capture.

Implementation (browser, bloque de 2026-08-17):
`src/rinari/browser/` (ws.py client RFC6455 stdlib, cdp.py JSON-RPC + eventos
con sesiones flattened, manager.py BrowserManager) + `tools/native/browse.py`
(25 tools `browser.*`). Motor: CDP directo (decision documentada arriba +
registro de decisiones); alternativa: Playwright.

- Endpoint: `RINARI_BROWSER_CDP` (env) o `browser.launch`/`browser.connect`;
  launch maneja un Chromium headless con `user-data-dir` aislado por sesión
  (login/session isolation) y ownership del subprocess (solo mata lo que
  lanzó).
- Clasificación de policy: navegar http(s) → `network.outbound` sobre la URL
  (guard en código, modo ask via approval); lectura del estado del browser →
  `browser.read`; mutación (click/fill/type/select/check/scroll/drag/
  set_cookie/launch/…) → `browser.mutate` (siempre consent, nunca read-only;
  denegado en perfil read-only).
- Uploads resueltos por el sandbox (provenance: path, bytes, sha256);
  downloads solo al artifact dir de la sesión (name/size/sha256 provenance).
- Cookies con valores redactados (son credentials).
- 40 tests deterministas contra un fake CDP server en loopback
  (test_browser_cdp.py): RFC6455 frame-by-frame, CDP timeout/error/eventos
  con requeue, manager y 13 tools vía ToolRuntime real (deny/ask/allow,
  sandbox, aprovisiones de sesión).

## Browser policy

- [x] download policy.
- [x] upload provenance.
- [x] external side-effect classification.
- [x] login/session isolation.
- [x] browser subprocess ownership.

## Plugin Runtime

- [x] manifest.
- [x] install.
- [x] remove.
- [x] enable.
- [x] disable.
- [x] update.
- [x] requested capabilities.
- [x] plugin trust.
- [x] contribution registry.
- [x] plugin diagnostics.

Implementation (plugins, bloque de 2026-08-20):
`src/rinari/plugins/` (manifest.py schema+validación `PluginManifest`, loader.py
importlib del entrypoint `contribute(api)` con namespace `<plugin>.<tool>`,
service.py `PluginService` install/remove/enable/disable/update/list/show/
permissions/doctor + repo tabla `plugins`). Fuentes `user` (`~/.rinari/plugins`,
confiada) y `project` (`<root>/.rinari/plugins`, **requiere project trust**).
Fallo de carga → diagnostic (code+message), nunca crash. Capability `tools` ya
contribuye; skills/adapters/commands/subagents/context-providers declarables en
el manifiesto pero **reservados** en v1 (el loader las rechaza con
`UNSUPPORTED_CONTRIBUTION` en vez de inventar semántica). `rinari plugins ...`.

## Plugin contributions

- [x] tools.
- [x] hooks.
- [ ] skills.
- [ ] provider adapters.
- [ ] commands.
- [ ] subagent definitions.
- [ ] context providers.

> skills / provider adapters / commands / subagent definitions / context
> providers: contribuciones declarables en el manifiesto pero no cargadas en
> esta pasada (v1). Son work de Fase 6 (skills/subagents) y se habilitan en el
> same loader/API cuando el runtime destino exista; el loader las rechaza de
> forma explícita hasta entonces.

## MCP Runtime

- [x] server registry.
- [x] connect.
- [x] disconnect.
- [x] tools.
- [x] resources.
- [x] prompts.
- [x] transport abstraction.
- [x] project trust.
- [x] secret scopes.
- [x] MCP ToolDefinition normalization.
- [x] normal Policy Engine path.
- [x] tracing.
- [ ] cancellation.
- [x] `rinari mcp ...`.

Implementation (MCP, bloque de 2026-08-20): `src/rinari/mcp/` — protocol.py
(JSON-RPC 2.0 newline-delimited, initialize handshake), transport.py
(`StdioTransport` subprocess + `InProcessTransport` para tests, interfaz
común = transport abstraction), client.py (`McpClient` wrappers de métodos),
adapter.py (normaliza tools/resources/prompts → `ToolDefinition` namespaced
`mcp.<server>.<tool>`; `readOnlyHint` → capability `mcp.read`, resto `mcp.call`),
service.py (`McpService` registry + lazy cache + trust gate). Capabilities
`mcp.read`/`mcp.call` añadidas al Policy Engine (read-only deniega; `call`
siempre consent). Secretos **solo** `env://VAR`. Project scope exige trust.
Tracing vía logs de proceso (`mcp.logs`). Cancellation tokenizado pendiente
(hoy: timeout del transporte). Verificado e2e con un MCP server stdio fake en
subprocess real. `rinari mcp list/add/remove/enable/disable/show/connect/
disconnect/tools/resources/prompts/test/logs`.

## OpenAPI Runtime

- [x] spec loader.
- [x] validator.
- [x] auth detection.
- [x] operation namespacing.
- [x] tool schema generation.
- [x] mutation risk defaults.
- [x] operation overrides.
- [x] refresh.
- [x] `rinari api ...`.

Implementation (OpenAPI, bloque de 2026-08-20): `src/rinari/openapi/` — spec.py
(loader+validator JSON, `SpecDocument`/`Operation`), tools.py (genera
`ToolDefinition` namespaced `api.<name>.<operationId|method_path>`; risk por
verbo GET/HEAD low-idempotent, POST/PUT/PATCH medium, DELETE high-destructive;
overrides por operación), service.py (`ApiService` add/add_url/remove/enable/
disable/list/show/validate/refresh/tool_definitions/invoke). Auth detectado
(securitySchemes http bearer / apiKey); secretos **solo** `env://VAR`,
faltante → `AUTH_REQUIRED`. Invocación vía `httpx.Client` inyectado
(`ToolContext.web`) para test con `MockTransport`; `NetworkGuard` se aplica
antes de salir. `rinari api list/add/remove/enable/disable/show/validate/
auth/tools/refresh/test`.

## Unified capability search

- [x] native tool ranking.
- [x] plugin tools.
- [x] MCP tools.
- [x] OpenAPI tools.
- [x] browser fallback.
- [ ] connector tools.
- [x] reliability/risk ranking.

Implementation (capability search, bloque de 2026-08-20): `src/rinari/
capability_search.py` — `search_capabilities` rankea sobre el ToolRegistry
único (nativo + plugin + mcp.<server> + api.<spec> conviven ahí) por
`reliability x risk`: native 1.00 > plugin 0.90 > openapi 0.85 > mcp 0.80 > web
0.75 > browser 0.70, con democión por riesgo. Sin match → fallback `browser.*`
a score 0. Tool `capability.search` (classifica `state.read`, se registra al
final del registry). Connector tools: el source `connector` ya clasifica y
tiene peso 0.95, pero aún no hay tools de connector nativos que listar (quedan
sin resultados hasta que aparezcan).

## Hooks

- [x] SessionStart.
- [x] BeforeModel.
- [x] AfterModel.
- [x] PreToolUse.
- [x] PostToolUse.
- [x] ToolError.
- [x] PermissionRequest.
- [ ] SubagentStart.
- [ ] SubagentStop.
- [x] BeforeCompact.
- [x] AfterCompact.
- [x] BeforeFinal.
- [x] SessionEnd.
- [x] trust/capability enforcement.

Implementation (hooks, bloque de 2026-08-20): `src/rinari/hooks/` — events.py
(catálogo 13 eventos), engine.py (`HookEngine` orden determinista user<project<
plugin, trust gate para fuente `project`, capability gate para handler
`shell` (`shell.exec`), timeout+captura de output, fallo de un hook **nunca**
rompe la sesión), service.py (`HookService` discovery `~/.rinari/hooks.json` +
`<root>/.rinari/hooks.json` + plugins, enable/disable en tabla `hooks`,
build_engine, test, doctor). Wiring: `runtime/agent.py` emite BeforeModel/
AfterModel/PreToolUse/PostToolUse/ToolError/BeforeFinal vía `hook_sink`;
`cli/agent_runtime.py` emite SessionStart/SessionEnd/PermissionRequest/
BeforeCompact/AfterCompact. Handler types `python` (import path) y `shell`
(comando, JSON en stdin). `rinari hooks list/show/enable/disable/test/doctor`.
SubagentStart/Stop: catalogados y forzados por el catálogo; wired desde la
Fase 6 (`agents/orchestrator.py` los emite vía `event_sink` al pool de
session_events).

---

# Fase 6 — Skills productivos y multi-agent (completada 2026-08-20)

## Objetivo

Convertir workflows expertos y paralelismo en capacidades de primera clase.

## Criterio de aceptación

Rinari debe poder delegar trabajo independiente, aislar writers, cargar skills bajo demanda y sintetizar resultados con provenance.

## Skill Runtime

- [x] skill manifest. (`SkillManifest`, frontmatter `---` + body markdown)
- [x] metadata. (name, description, source, can_delegate)
- [x] version. (semver o `sha:` prefix; fallback a `skill_version`)
- [x] description. (obligatorio para `validate`)
- [x] triggers. (lista opcional de frases activadoras)
- [x] required capabilities. (`required_tools`: request, nunca grant — harness 51)
- [x] optional capabilities. (`optional_tools`)
- [x] risk. (`low | medium | high`; validado)
- [x] procedure. (sección `# Procedure` del body)
- [x] verification. (sección `# Verification`)
- [x] failure policy. (sección `# Failure handling`)
- [x] success criteria. (sección `# Success criteria`)

## Skill discovery

- [x] packaged. (`assets/skills/<name>/SKILL.md`)
- [x] user. (`<RINARI_HOME>/skills/<name>/SKILL.md`)
- [x] project trusted. (`<root>/.rinari/skills/<name>/SKILL.md`, requiere trust del proyecto)
- [x] summaries only initially. (catálogo 1-línea por skill en el prompt)
- [x] full lazy load. (cuerpo completo solo del skill activo, inyectado por-turn)
- [x] activation trace. (eventos `SkillActivated`/`SkillDeactivated` en session_events)
- [x] conflict resolution. (project > user > packaged, harness 50)

## Skills iniciales completas

- [x] repository-explore.
- [x] implement-feature.
- [x] fix-bug.
- [x] debug.
- [x] test.
- [x] code-review.
- [x] refactor.
- [x] fix-ci.
- [x] research.
- [x] final-verification.

## Skill CLI

- [x] list.
- [x] search.
- [x] show.
- [x] activate.
- [x] deactivate.
- [x] install.
- [x] remove.
- [x] update.
- [x] validate.
- [x] test.
- [x] create.

Implementation (phase 6): core en `src/rinari/skills/` — `manifest.py`
(frontmatter parser stdlib, `load_skill_manifest`, `validate_skill`),
`service.py` (`SkillService`: discovery por source con trust, activate/
deactivate persistido en `sessions.active_skills_json` + trace,
install/update/remove/create en el user dir), `tools.py` (`skills.list`/
`skills.activate` para el modelo) y catálogo por-turn en el promptAssembler
(cuerpo de activas + 1-línea del resto, `AssemblerContext.skill_catalog`). 10 skills packaged en
`assets/skills/` con las 4 secciones. 17 tests (`test_skills_runtime.py`).

## Agent Registry

- [x] AgentDefinition. (name, description, objective, allowlist, profile, budget, provenance)
- [x] tool allowlist. (filtro del ToolRegistry por agente)
- [x] capability scope. (perfil `read-only`/`workspace` → PolicyEngine por subagente)
- [x] budget. (`AgentBudget`: model calls + tool calls via BudgetMeter)
- [x] context scope. (session id `{parent}::{agent}`, artifact_root aislado, objetivo + contexto acotado)
- [x] output contract. (`AgentResult` structurado + bloco de validación parseable)
- [x] provenance. (source builtin/project/user + trust requerido para locales)

## Built-in agents

- [x] Explore. (read-only)
- [x] Reviewer. (read-only)
- [x] Debugger. (workspace: reproduce con `shell.exec`; sin `fs.write` en allowlist)
- [x] Researcher. (read-only: web/http/search, sin writes locales por defecto)
- [x] Implementer. (workspace)
- [x] Verifier. (read-only; solo `verify.evaluate` — `verify.plan/record` son `state.write`)

## Agent Orchestrator

- [x] spawn. (`agent.spawn` = `state.write`: permitido en workspace, denegado en read-only)
- [x] status. (`agent.status`, 1 o todos)
- [x] message. (`agent.message`: fila de seguimiento, recogida entre turns)
- [x] wait. (`agent.wait` con timeout)
- [x] cancel. (`agent.cancel` → token propagado a loop y tools)
- [x] result. (`agent.result`: `AgentResult` o null si sigue corriendo)
- [x] task ownership. (campo `task_id`; join del task graph al synthesize)
- [x] bounded objective. (budget per-agent + timeout + objetivo único por spec)
- [x] concurrency controls. (`MAX_CONCURRENT` simultáneos)
- [x] max depth. (`MAX_DEPTH`: los subagentes no re-spawn por allowlist)
- [x] max total agents. (`MAX_TOTAL` por sesión)
- [x] cancellation propagation. (`_LinkedToken`: parent session ↔ spec token, ambas direcciones)

## Agent isolation

- [x] read-only Explore.
- [x] read-only Reviewer default. (harness 112: Explore/Reviewer/Verifier no-writes)
- [x] Researcher without local writes default.
- [x] verifier restrictions. (validación `state.read` solo)
- [x] per-agent permissions. (PolicyEngine con scope propio; approvals auto-deny `prompt=None`)
- [x] per-agent budgets. (budget meter + limits de proceso: 60s, 128KB output)
- [x] no permission inheritance bugs. (test: subagente read-only denegado en `fs.write` aunque el parent pueda)

## Worktrees

- [x] worktree manager. (`agents/worktree_manager.py`)
- [x] isolated writer workspace. (`git worktree add` por writer con `use_worktree`)
- [x] branch naming. (`subagent/<slug>` con sufijo incremental)
- [x] patch/commit result. (`commit_result` + `patch` HEAD~1..HEAD)
- [x] merge/integration. (`try_merge` no-ff en el worktree principal)
- [x] conflict reporting. (`--diff-filter=U` + `abort_merge`)
- [x] cleanup. (`remove` + best-effort `branch -D`)
- [x] prevent blind same-tree parallel writes. (writers solo escriben en su worktree; el main tree no se toca hasta merge)

## Multi-agent synthesis

- [x] structured AgentResult. (status, ok, summary, validation, files, branch/commit, conflicts, usage)
- [x] evidence refs. (`extract_evidence`: `artifact://...` refs)
- [x] contradiction detection. (`_contradictions`: checks passed/failed entre agentes)
- [x] independent verification. (verifier como agente separado con perfil read-only)
- [x] task graph join. (join solo en `synthesize` — evita carreras entre spawns simultáneos; contradicciones bloquean el auto-join)
- [x] duplicate-work avoidance. (`_duplicate_work`: files_changed en común)

Implementation (phase 6): `src/rinari/agents/` — `definition.py` (6 agentes
builtin, budgets, perfiles), `registry.py` (builtin + project/user con trust),
`orchestrator.py` (AgentOrchestrator: limits, cancel, worktrees, synthesize)
con `SubagentStart`/`SubagentStop` en session_events, `worktree_manager.py`,
`runtime.py` (`make_subagent_runner`: AgentLoop scoped por subagente,
`_LinkedToken`, ToolContext aislado) y `tools.py` (7 tools `agent.*`).
Wiring en `cli/agent_runtime.py` (`build_agent_session` → orchestrator +
`agent.*`/`skills.*` tools; cancel de subagentes en `session.end()`).
Fix de storage: `Database` thread-safe (RLock + `check_same_thread=False`,
transacciones nestables per-thread, `seq` asignado atómicamente) porque los
worker threads persisten eventos. 23 tests (`test_agents_runtime.py`,
incluido e2e CLI de spawn/wait).

---

# Fase 7 — CLI visual, ergonomía y productización completa

## Objetivo

Hacer que el harness completo sea agradable, transparente y usable diariamente.

## Criterio de aceptación

La CLI debe comunicar claramente:

```text
quién es Rinari
qué modelo/provider usa
qué está haciendo
qué contexto consume
qué permisos tiene
qué tools/skills/agents están activos
qué validación existe
qué costo/tokens existen cuando son conocidos
```

sin convertir la terminal en ruido.

## Startup banner

- [ ] ASCII de Rinari.
- [ ] versión desde Build Manifest.
- [ ] session kind.
- [ ] mode.
- [ ] provider.
- [ ] model alias.
- [ ] provider model ID.
- [ ] reasoning effort.
- [ ] context used/window.
- [ ] profile.
- [ ] project.
- [ ] branch.
- [ ] dirty state.
- [ ] loaded tools.
- [ ] active skills.
- [ ] agents.
- [ ] network state.
- [ ] responsive width.

## Runtime Snapshot

- [ ] una fuente de verdad para UI.
- [ ] provider/model data.
- [ ] usage accounting.
- [ ] context metrics.
- [ ] project state.
- [ ] policy state.
- [ ] tool state.
- [ ] skill state.
- [ ] agent state.
- [ ] validation state.

## Usage Accounting

- [ ] input tokens.
- [ ] output tokens.
- [ ] cached tokens cuando provider los dé.
- [ ] reasoning tokens cuando provider los dé.
- [ ] model calls.
- [ ] tool calls.
- [ ] elapsed.
- [ ] cost con pricing confiable.
- [ ] no inventar unsupported metrics.

## Status rail

- [ ] orient.
- [ ] plan.
- [ ] execute.
- [ ] verify.
- [ ] approval.
- [ ] blocked.
- [ ] complete.
- [ ] active tool.
- [ ] context usage.
- [ ] effort.
- [ ] cost.
- [ ] agents.
- [ ] elapsed.

## Renderers

- [ ] Rich TTY.
- [ ] compact.
- [ ] plain.
- [ ] JSON.
- [ ] JSON stream.
- [ ] `NO_COLOR`.
- [ ] `TERM=dumb`.
- [ ] `--no-banner`.
- [ ] `--no-progress`.
- [ ] width adaptation.

## Interactive slash commands

- [ ] `/help`.
- [ ] `/status`.
- [ ] `/provider`.
- [ ] `/model`.
- [ ] `/mode`.
- [ ] `/usage`.
- [ ] `/tokens`.
- [ ] `/plan`.
- [ ] `/tasks`.
- [ ] `/diff`.
- [ ] `/test`.
- [ ] `/review`.
- [ ] `/skills`.
- [ ] `/tools`.
- [ ] `/agents`.
- [ ] `/permissions`.
- [ ] `/checkpoint`.
- [ ] `/undo`.
- [ ] `/compact`.
- [ ] `/context`.
- [ ] `/trace`.
- [ ] `/new`.
- [ ] `/resume`.
- [ ] `/exit`.

## Approvals UI

- [ ] exact action.
- [ ] target.
- [ ] reason.
- [ ] risk.
- [ ] expected side effects.
- [ ] allow once.
- [ ] allow session.
- [ ] deny.
- [ ] critical action distinction.

## Command coverage

- [ ] todos los comandos públicos de `docs/commands.md` registrados.
- [ ] `--help` consistente.
- [ ] JSON contracts.
- [ ] exit codes.
- [ ] no model call para commands deterministas.
- [ ] shell completion.

---

# Fase 8 — Observability, evals y hardening productivo

## Objetivo

Probar que Rinari es confiable en escenarios largos, peligrosos y reales.

## Criterio de aceptación

No se libera el harness completo hasta pasar:

```text
unit
integration
E2E
safety
trajectory evals
long-horizon
provider/model switching
browser
MCP/plugin
multi-agent
resume
migration
```

## Observability

- [ ] trace hierarchy.
- [ ] sessions.
- [ ] turns.
- [ ] prompt segment metadata.
- [ ] model calls.
- [ ] tool calls.
- [ ] policy.
- [ ] approvals.
- [ ] skills.
- [ ] agents.
- [ ] browser.
- [ ] MCP.
- [ ] plugins.
- [ ] compaction.
- [ ] completion gate.

## Metrics

- [ ] task success.
- [ ] first-pass success.
- [ ] human intervention.
- [ ] unnecessary questions.
- [ ] validation pass.
- [ ] regressions.
- [ ] unrelated-change rate.
- [ ] permission prompts.
- [ ] loop rate.
- [ ] resume success.
- [ ] tool errors.
- [ ] subagent usefulness.
- [ ] latency.
- [ ] cost per successful task.

## Eval runner

- [ ] suite registry.
- [ ] fixtures.
- [ ] deterministic assertions.
- [ ] model judge support.
- [ ] trajectory assertions.
- [ ] compare runs.
- [ ] reports.
- [ ] history.

## Soul evals

- [ ] identity.
- [ ] AI disclosure.
- [ ] persona age.
- [ ] tone.
- [ ] disagreement.
- [ ] frustration.
- [ ] uncertainty.
- [ ] scope.
- [ ] no false success.
- [ ] no repetitive catchphrases.

## Coding evals

- [ ] single-file bug.
- [ ] cross-module bug.
- [ ] feature.
- [ ] refactor.
- [ ] migration.
- [ ] dependency upgrade.
- [ ] failing CI.
- [ ] flaky test.
- [ ] race condition.
- [ ] performance regression.
- [ ] config bug.

## Trajectory evals

- [ ] inspect before edit.
- [ ] correct file discovery.
- [ ] minimal scope.
- [ ] relevant validation.
- [ ] recovery.
- [ ] no unnecessary questions.
- [ ] no loops.
- [ ] accurate completion.

## Security evals

- [ ] malicious README.
- [ ] malicious source comment.
- [ ] malicious compiler output.
- [ ] malicious web page.
- [ ] malicious GitHub issue.
- [ ] malicious MCP resource.
- [ ] malicious subagent report.
- [ ] secret in env.
- [ ] secret in stderr.
- [ ] write outside workspace.
- [ ] force push.
- [ ] external send.
- [ ] untrusted plugin.
- [ ] untrusted project hook.

## Browser evals

- [ ] page navigation.
- [ ] forms.
- [ ] SPA interaction.
- [ ] download.
- [ ] upload policy.
- [ ] auth profile.
- [ ] cancellation.
- [ ] prompt injection.
- [ ] artifact capture.

## MCP/plugin evals

- [ ] schema normalization.
- [ ] policy.
- [ ] approval.
- [ ] redaction.
- [ ] cancellation.
- [ ] bad server/tool.
- [ ] project trust.
- [ ] capability conflict.

## Multi-agent evals

- [ ] bounded context.
- [ ] read-only agent cannot write.
- [ ] parallel writers isolated.
- [ ] cancellation.
- [ ] contradictory results.
- [ ] duplicate work.
- [ ] verifier independence.
- [ ] max depth.
- [ ] max concurrency.

## Long-horizon

- [ ] 50+ tool calls.
- [ ] 100+ tool calls.
- [ ] compaction.
- [ ] interruption.
- [ ] resume.
- [ ] provider switch.
- [ ] subagents.
- [ ] failure recovery.
- [ ] final verification.

## Migration tests

- [ ] config schema upgrade.
- [ ] SQLite schema upgrade.
- [ ] provider preservation.
- [ ] model preservation.
- [ ] session preservation.
- [ ] credential references.
- [ ] skill/plugin version migration.
- [ ] export/import schema.

---

# Fase 9 — Release del harness completo

## Objetivo

Preparar y publicar una release que represente realmente el producto definido.

## Criterio de aceptación

Todos los gates críticos anteriores pasan en plataformas soportadas.

## Release checklist

### Funcional

- [ ] setup.
- [ ] CHAT.
- [ ] PROJECT.
- [ ] CHAT → PROJECT.
- [ ] providers.
- [ ] models.
- [ ] tools.
- [ ] skills.
- [ ] browser.
- [ ] plugins.
- [ ] MCP.
- [ ] OpenAPI.
- [ ] memory.
- [ ] context/compaction.
- [ ] multi-agent.
- [ ] resume.
- [ ] verification.
- [ ] completion gate.

### Seguridad

- [ ] sandbox.
- [ ] approvals.
- [ ] secrets.
- [ ] redaction.
- [ ] trust.
- [ ] destructive-action gates.
- [ ] prompt-injection suite.

### Calidad

- [ ] unit tests.
- [ ] integration tests.
- [ ] E2E.
- [ ] eval suites.
- [ ] no blockers críticos.
- [ ] supported migration paths.
- [ ] docs sincronizados.

### Plataformas

- [ ] Linux.
- [ ] macOS.
- [ ] Windows.

### Packaging

- [ ] clean install.
- [ ] clean upgrade.
- [ ] `uv` workflow.
- [ ] shell completions.
- [ ] version/build manifest.
- [ ] release artifact.

### Documentación

- [ ] README final.
- [ ] install.
- [ ] setup.
- [ ] providers/models.
- [ ] project usage.
- [ ] chat usage.
- [ ] skills/tools.
- [ ] MCP/plugins.
- [ ] browser.
- [ ] multi-agent.
- [ ] security.
- [ ] troubleshooting.
- [ ] migration.
- [ ] contributing.
- [ ] architecture links.

### Licencia

- [ ] `LICENSE` presente y consistente con decisión de Fase 0.

---

# Roadmap visual

```text
FASE 0
Contrato del producto
    │
    ▼
FASE 1
Fundaciones / storage / provider / model / session
    │
    ▼
FASE 2
Agent Runtime / Tool Runtime / sandbox
    │
    ▼
FASE 3
Project intelligence / RINARI.md / verification
    │
    ▼
FASE 4
Context / artifacts / memory / resume
    │
    ▼
FASE 5
Web / Browser / Plugins / MCP / OpenAPI
    │
    ▼
FASE 6
Skills / Subagents / Multi-Agent / Worktrees
    │
    ▼
FASE 7
CLI visual / UX / comandos completos
    │
    ▼
FASE 8
Observability / Evals / Hardening
    │
    ▼
FASE 9
Release completa
```

Todas las fases después de Fase 0 son **parte del target productivo**.

---

# Integration checkpoints

Estos checkpoints sirven para detectar errores arquitectónicos temprano.

No son releases recortadas.

## Checkpoint A — Bootstrap

Debe funcionar:

```text
setup
providers
models
switch persistente
CHAT/PROJECT
Soul
Constitution
sessions
```

## Checkpoint B — Coding core

Debe funcionar:

```text
project
fs
shell
Git
sandbox
approval
agent loop
validation
completion gate
```

## Checkpoint C — Durable agent

Debe funcionar:

```text
tasks
artifacts
context
compaction
memory
resume
checkpoints
```

## Checkpoint D — External ecosystem

Debe funcionar:

```text
web
browser
plugins
MCP
OpenAPI
hooks
```

## Checkpoint E — Multi-agent

Debe funcionar:

```text
skills
subagents
worktrees
parallel task graph
verifier
```

## Checkpoint F — Full product

Debe funcionar el escenario completo descrito abajo.

---

# Escenario E2E obligatorio del harness

Antes de considerar Rinari completo:

```text
1. ejecutar `rinari chat` fuera de un proyecto
2. conversar con Rinari
3. pedir crear una aplicación en la carpeta actual
4. Rinari crea/scaffoldea archivos
5. Rinari ejecuta git init
6. misma sesión promueve CHAT → PROJECT
7. carga project state + RINARI.md
8. indexa/comprende repo
9. activa skill apropiada
10. usa tools nativas para implementar
11. usa web para documentación externa actual
12. usa browser cuando una UI/webflow lo requiere
13. usa una tool proveniente de MCP
14. usa una capability proveniente de plugin
15. delega exploración/review a subagentes
16. usa worktree si hay parallel writer
17. Verifier revisa acceptance criteria
18. trace registra tool/browser/agent/plugin/MCP
19. contexto alcanza threshold y compacta
20. sesión se interrumpe
21. `rinari resume` reconcilia repo real
22. provider cambia sin perder configuración previa
23. provider original se restaura con su modelo
24. validation final corre
25. Completion Gate acepta evidencia
26. Rinari reporta exactamente lo sucedido
```

El escenario debe usar los mismos servicios comunes:

```text
Session Engine
Capability Resolver
Tool Registry
Skill Runtime
Policy Engine
Context Engine
Memory
Artifact Store
Task Graph
Agent Orchestrator
Verification Engine
Completion Gate
Event Store
```

Si cada integración necesita un bypass especial, la arquitectura no está suficientemente unificada.

---

# Fixtures requeridos

Mantener repos/proyectos fixture para:

```text
empty-directory
tiny-python
python-cli
typescript-app
rust-cli
monorepo
nested-repo
dirty-worktree
nested-rinari-instructions
malicious-readme
untrusted-project-config
failing-tests
failing-ci
large-repo
```

---

# Fake infrastructure requerida para tests

## Fake provider

Debe soportar scripted responses:

```text
assistant text
tool call
parallel tool calls
usage metadata
error
timeout
stream
```

## Fake browser target

Local fixture server para:

```text
forms
SPA
download
upload
auth state
prompt injection
```

## Fake MCP server

Debe poder simular:

```text
valid tools
bad schema
timeout
malicious resource
side-effect tool
```

## Fake plugin

Debe simular:

```text
tool contribution
skill contribution
permission request
hook
failure
```

---

# Quality gates por PR

Cuando exista implementación, todo PR debe pasar lo aplicable:

```text
[ ] scope del TODO respetado
[ ] tests relevantes
[ ] no red pública en tests normales
[ ] lint
[ ] typecheck si está configurado
[ ] diff inspeccionado
[ ] docs sincronizados
[ ] no secrets
[ ] migrations probadas si aplica
[ ] provider/model invariants intactos
[ ] session invariants intactos
[ ] sandbox/policy intactos
[ ] no false-success
```

---

# Registro de decisiones

Registrar aquí decisiones de producto/roadmap que cambien el contrato.

| Fecha | Decisión |
|---|---|
| 2026-08-16 | Stack inicial confirmado: Python 3.11+, `uv`, `src/rinari/`, Typer, Rich, HTTPX y pytest. |
| 2026-08-16 | Rinari v1 queda como referencia, no como base de código ni fuente para copy/paste automático. |
| 2026-08-16 | `docs/tools.md` y `docs/skills.md` pasan a ser catálogos maestros del harness. |
| 2026-08-16 | Soul de Rinari definido desde cero y separado del comportamiento operativo del harness. |
| 2026-08-16 | Se separan Soul, Harness Constitution, Runtime Policy, project instructions, skills y environment context. |
| 2026-08-16 | Arquitectura completa definida en `docs/stack.md` y `docs/harness.md`; no se usará un mega-system-prompt como sustituto del runtime. |
| 2026-08-16 | Superficie CLI completa definida en `docs/commands.md`. |
| 2026-08-16 | Providers y models se guardan en registries persistentes; cambiar selección nunca elimina configuraciones previas. Cada provider recuerda su modelo propio. |
| 2026-08-16 | `rinari chat` fuerza CHAT. Plain `rinari` resuelve AUTO: proyecto detectado → PROJECT; sin proyecto → CHAT. |
| 2026-08-16 | Una sesión CHAT puede promoverse a PROJECT sin reiniciar cuando el usuario crea/adopta explícitamente un proyecto; conserva session ID, conversación, provider y model. |
| 2026-08-16 | `$HOME` nunca será workspace escribible implícito por ejecutar `rinari` sin proyecto. |
| 2026-08-16 | El target productivo incluye browser, plugins, MCP, OpenAPI, tools, skills y multi-agent; el orden por fases es dependencia de implementación, no alcance opcional. |
| 2026-08-16 | La CLI visual mostrará ASCII/versión/model/runtime info y usage real (tokens/context/reasoning/cost cuando exista), sin inventar métricas no expuestas. |
| 2026-08-16 | AGENTS.md deja de hardcodear Fase 0; TODO.md es la fuente de verdad de la fase actual. |
| 2026-08-16 | Tests normales serán deterministas y sin red pública; se usarán fake providers, fixtures y transports controlados. |
| 2026-08-16 | `main` se mantiene como rama estable; no push sin petición/autorización y se prefieren commits pequeños con conventional prefixes. |
| 2026-08-16 | Licencia MIT confirmada por Xainner; `LICENSE` agregado. Fase 0 completa; se abre Fase 1 (fundaciones, bootstrap y persistencia). |
| 2026-08-16 | Fase 1 completa: packaging, estructura base, config, estado SQLite, credential store, provider/model/session registries + CLI, Soul/Constitution (assets + loader + overrides), Build Manifest, doctor/status/version. 4 items quedan abiertos por depender del agent loop de Fase 2. Se abre Fase 2 (Agent Runtime, Tool Runtime y seguridad base). |
| 2026-08-17 | Browser engine: **CDP (Chrome DevTools Protocol) directo**, con un client WebSocket RFC6455 mínimo en repo y **cero dependencias nuevas**; Rinari se conecta a cualquier browser Chromium-family expuesto con `--remote-debugging-port` (conectar a endpoint existente o lanzar un Chromium gestionado headless con profile aislado por sesión). **Alternativa documentada: Playwright** (`playwright.chromium` / `connect_over_cdp`), descartada como engine inicial por ser dependencia pesada que requiere descarga binaria del browser en el setup (conflicta con la estrategia de dependencias y con tests aislados de red); el `BrowserManager` aísla el driver tras una interfaz, de modo que Playwright puede adoptarse después sin reescribir tools/policy si se necesitan sus helpers de alto nivel (a11y snapshots, auto-wait, BiDi). |

---

# Decisiones pendientes

## Bloqueante de Fase 0

- [x] **Licencia del proyecto**

      Resuelta: MIT (confirmada por Xainner, 2026-08-16).
      Ya no quedan bloqueantes de Fase 0.

## No bloqueantes hasta su fase

Estas decisiones pueden resolverse durante implementación porque no cambian el contrato de producto ya definido:

- [ ] backend exacto inicial del Credential Store por plataforma;
- [x] ~~librería/engine exacto para browser automation~~ — resuelta 2026-08-17:
      **CDP directo** (ver registro de decisiones y sección "Browser engine" en Fase 5).
      Alternativa documentada: Playwright.
- [ ] librería SQLite/ORM ligera vs SQL explícito;
- [ ] set exacto de LSPs soportados inicialmente;
- [ ] implementación exacta del semantic index;
- [ ] formato exacto del plugin manifest;
- [ ] transporte MCP inicial;
- [ ] pricing source/caching para cost accounting;
- [ ] detalle de terminal ASCII final de Rinari.

Cada una debe decidirse antes de implementar el subsistema correspondiente, con tests y actualización documental si altera contratos.

---

# Estado actual resumido

```text
FASE ACTUAL
  Fase 5 — Web, browser y capability ecosystem
  (Web, HTTP y Browser terminadas; plugins, MCP, OpenAPI, unified search y
  hooks pendientes)

COMPLETADO
  fase 0 completa (2026-08-16)
  identidad, arquitectura, CLI contract, catalogs, harness target,
  testing/Git strategy, licencia
  fase 1 completa (2026-08-16)
  packaging (uv, entrypoint `rinari`), configuración + SQLite + credential
  store, provider/model registry + CLI, session CHAT/PROJECT + persistencia,
  Soul + Constitution, doctor/status/version
  fase 2 completa (2026-08-16)
  Model Runtime (streaming, tool calls, retries), Prompt Assembler,
  Tool Registry/Runtime (fs/shell/process/git/web/repo), Policy/Sandbox/
  Approval, Agent loop + cancellation + trace, CHAT→PROJECT promoción
  in-session, dirty-worktree baseline (preservación de cambios del usuario),
  LSP/tree-sitter
  fase 3 completa (2026-08-16)
  Project Trust + fingerprint, RINARI.md resolver (root→cwd),
  repository index (migración 0005, incremental), task graph + done-when
  (0006), validation records + verification planner + completion gate
  (0007), checkpoints/undo `rinari undo` (0008, ownership agent/user/mixed),
PTY tools
   fase 4 completa (2026-08-17)
   Artifact Store + Context Engine + compaction, memoria, context
   retrieval + pins, resume durable + reconciliation (9 subsystems,
   incluido skills), session fork, budgets + loop detection por turno,
network policy foundation + hooks, skill version reconciliation
    (migraciones 0009-0015)

EN FASE 5 (hasta el momento)
    Web (src/rinari/web + tools web.*): fetch/extract/links/find/cite/sources
    search keyless (DDG HTML) + download a artifact, transport acotado con
    mapeo de errores, guard network.outbound en código; 25 tests
    HTTP (src/rinari/http + tools http.request/http.sse): request genérico
    (7 métodos, body/query/auth secret-ref), retry idempotente + backoff +
    Retry-After, SSE WHATWG acotado, redacción de secretos en respuesta/URL,
    artifact de respuesta; 42 tests (test_http_tools.py)
    Browser (src/rinari/browser + tools browser.*, 25 tools): motor CDP
    directo con client WebSocket RFC6455 propio (cero dependencias nuevas;
    alternativa documentada: Playwright, swappable tras BrowserManager);
    launch/connect lifecycle con subprocess ownership + per-session profiles,
    navigation network-gated por policy, lectura/mutación browser.classificadas
    (browser.read/browser.mutate), uploads por sandbox con provenance,
    downloads solo a artifact dir, cookies redactadas; 40 tests contra un
    fake CDP server en loopback (test_browser_cdp.py)
```
