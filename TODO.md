# TODO — Rinari CLI

Roadmap canónico de construcción de Rinari.

> **Estado actual:** Fase 2 — Agent Runtime, Tool Runtime y seguridad base.
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

# Fase 2 — Agent Runtime, Tool Runtime y seguridad base ← ACTUAL

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
- [ ] PTY. (fase 3)
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
- [ ] network policy foundation. (fase 4, con browser/web)
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
- [ ] network hooks. (fase 4)
- [x] secret scopes. (redaction de secrets de providers)
- [x] tests de escape.

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
- [ ] verify transition. (fase 3)
- [ ] finalize transition. (fase 3)

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

# Fase 3 — Project intelligence, instructions y verification

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

- [ ] global engineering instruction resolver.
- [ ] root `RINARI.md`.
- [ ] nested `RINARI.md`.
- [ ] `RINARI.override.md`.
- [ ] root → cwd chain.
- [ ] scope metadata.
- [ ] trust metadata.
- [ ] instruction precedence tests.
- [ ] random README remains untrusted data.

## Repository state

- [ ] language detection.
- [ ] framework hints.
- [ ] package manager detection.
- [ ] build command discovery.
- [ ] test command discovery.
- [ ] lint/typecheck discovery.
- [ ] generated file hints.
- [ ] repository summary.

## Search

- [ ] exact file search.
- [ ] regex/grep.
- [ ] symbol search.
- [ ] references.
- [ ] structural search.
- [ ] hybrid search.

## Tree-sitter / AST

- [ ] parser abstraction.
- [ ] structural queries.
- [ ] supported language adapters.
- [ ] safe fallback.

## LSP

- [ ] definition.
- [ ] references.
- [ ] symbols.
- [ ] diagnostics.
- [ ] hover/type info.
- [ ] signatures.
- [ ] rename capability when safe.
- [ ] lifecycle management.

## Repository Index

- [ ] project-scoped index.
- [ ] file hashes.
- [ ] symbols.
- [ ] imports.
- [ ] references.
- [ ] test mapping.
- [ ] incremental invalidation.
- [ ] optional semantic layer.
- [ ] `rinari index ...`.

## Task Graph

- [ ] Task nodes.
- [ ] dependencies.
- [ ] status.
- [ ] blockers.
- [ ] acceptance criteria.
- [ ] evidence refs.
- [ ] `rinari tasks ...`.

## Done-when contract

- [ ] task-specific acceptance criteria.
- [ ] implementation criteria.
- [ ] validation criteria.
- [ ] scope criteria.
- [ ] unresolved criteria.

## Validation Records

- [ ] test.
- [ ] lint.
- [ ] typecheck.
- [ ] build.
- [ ] schema.
- [ ] manual check.
- [ ] custom.
- [ ] persistent evidence.

## Verification Planner

- [ ] changed-file analysis.
- [ ] targeted test selection.
- [ ] adjacent tests.
- [ ] broader suite escalation.
- [ ] user constraints.
- [ ] project instructions.
- [ ] risk input.

## Completion Gate

- [ ] DONE.
- [ ] IMPLEMENTED_UNVERIFIED.
- [ ] PARTIAL.
- [ ] BLOCKED.
- [ ] FAILED.
- [ ] reject false test success.
- [ ] reject false deploy success.
- [ ] reject “fixed” with no evidence when evidence is required.
- [ ] unresolved failure detection.

## Checkpoints / Undo

- [ ] checkpoint create.
- [ ] checkpoint list.
- [ ] checkpoint restore.
- [ ] checkpoint remove.
- [ ] agent-owned change tracking.
- [ ] mixed ownership detection.
- [ ] `rinari undo`.
- [ ] undo preview.

---

# Fase 4 — Context, artifacts, memoria y resume durable

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

- [ ] artifact URI.
- [ ] metadata.
- [ ] hashes.
- [ ] content type.
- [ ] file-backed storage.
- [ ] search.
- [ ] slicing.
- [ ] export.
- [ ] GC.
- [ ] retention.
- [ ] provenance.

## Context Engine

- [ ] context budget.
- [ ] retrieval.
- [ ] ranking.
- [ ] pins.
- [ ] dedup.
- [ ] artifact references.
- [ ] history selection.
- [ ] project retrieval.
- [ ] user-memory retrieval.
- [ ] token accounting integration.

## Compaction

- [ ] pressure thresholds.
- [ ] preserve goal.
- [ ] preserve constraints.
- [ ] preserve decisions.
- [ ] preserve task graph.
- [ ] preserve changed files.
- [ ] preserve validation.
- [ ] preserve approvals.
- [ ] preserve blockers.
- [ ] preserve artifacts.
- [ ] provider-independent compact state.
- [ ] long-horizon tests.

## User Memory

- [ ] record schema.
- [ ] provenance.
- [ ] confidence.
- [ ] explicit durable preferences.
- [ ] search.
- [ ] update.
- [ ] forget.
- [ ] sensitivity filter.

## Project Memory

- [ ] project namespace.
- [ ] stable facts.
- [ ] conflict detection.
- [ ] stale fact handling.
- [ ] promote team-relevant rules to docs when appropriate.

## Episodic / Pattern Memory

- [ ] episodic task summaries.
- [ ] pattern records.
- [ ] provenance.
- [ ] no secret storage.
- [ ] no automatic inference persistence.

## Resume

- [ ] CHAT resume.
- [ ] PROJECT resume.
- [ ] project identity reconciliation.
- [ ] branch reconciliation.
- [ ] dirty-tree reconciliation.
- [ ] trust reconciliation.
- [ ] provider auth reconciliation.
- [ ] model availability reconciliation.
- [ ] skill version reconciliation.
- [ ] policy change reconciliation.
- [ ] stale assumptions.

## Session fork

- [ ] fork state.
- [ ] preserve provenance.
- [ ] independent continuation.

## Budgets

- [ ] model calls.
- [ ] tool calls.
- [ ] cost.
- [ ] wall time.
- [ ] subagents.
- [ ] recursion depth.
- [ ] network calls.

## Loop detection

- [ ] same tool/args.
- [ ] same error.
- [ ] two-action oscillation.
- [ ] repeated rewrites.
- [ ] repeated denied approval.
- [ ] duplicated subagent work.
- [ ] force strategy change.

---

# Fase 5 — Web, browser y capability ecosystem

## Objetivo

Integrar acceso externo real bajo el mismo Tool Runtime.

## Criterio de aceptación

Web, browser, plugins, MCP y OpenAPI deben funcionar como capacidades normalizadas, sin bypass de policy, tracing o artifacts.

## Web

- [ ] search.
- [ ] fetch.
- [ ] open.
- [ ] find.
- [ ] links.
- [ ] download.
- [ ] content extraction.
- [ ] provenance.
- [ ] citations/evidence.

## HTTP

- [ ] generic request.
- [ ] streaming.
- [ ] SSE.
- [ ] auth injection.
- [ ] timeout.
- [ ] retry policy.
- [ ] rate-limit errors.
- [ ] artifact response handling.

## Browser Runtime

- [ ] Browser Manager.
- [ ] browser lifecycle.
- [ ] tabs.
- [ ] navigation.
- [ ] DOM snapshot.
- [ ] accessibility tree.
- [ ] screenshot.
- [ ] click.
- [ ] type/fill.
- [ ] select.
- [ ] check/uncheck.
- [ ] drag.
- [ ] scroll.
- [ ] forms.
- [ ] upload.
- [ ] download.
- [ ] cookies/storage.
- [ ] console.
- [ ] network observation.
- [ ] bounded evaluate.
- [ ] auth profiles.
- [ ] cancellation.
- [ ] artifact capture.

## Browser policy

- [ ] download policy.
- [ ] upload provenance.
- [ ] external side-effect classification.
- [ ] login/session isolation.
- [ ] browser subprocess ownership.

## Plugin Runtime

- [ ] manifest.
- [ ] install.
- [ ] remove.
- [ ] enable.
- [ ] disable.
- [ ] update.
- [ ] requested capabilities.
- [ ] plugin trust.
- [ ] contribution registry.
- [ ] plugin diagnostics.

## Plugin contributions

- [ ] tools.
- [ ] skills.
- [ ] provider adapters.
- [ ] commands.
- [ ] hooks.
- [ ] subagent definitions.
- [ ] context providers.

## MCP Runtime

- [ ] server registry.
- [ ] connect.
- [ ] disconnect.
- [ ] tools.
- [ ] resources.
- [ ] prompts.
- [ ] transport abstraction.
- [ ] project trust.
- [ ] secret scopes.
- [ ] MCP ToolDefinition normalization.
- [ ] normal Policy Engine path.
- [ ] tracing.
- [ ] cancellation.
- [ ] `rinari mcp ...`.

## OpenAPI Runtime

- [ ] spec loader.
- [ ] validator.
- [ ] auth detection.
- [ ] operation namespacing.
- [ ] tool schema generation.
- [ ] mutation risk defaults.
- [ ] operation overrides.
- [ ] refresh.
- [ ] `rinari api ...`.

## Unified capability search

- [ ] native tool ranking.
- [ ] plugin tools.
- [ ] MCP tools.
- [ ] OpenAPI tools.
- [ ] browser fallback.
- [ ] connector tools.
- [ ] reliability/risk ranking.

## Hooks

- [ ] SessionStart.
- [ ] BeforeModel.
- [ ] AfterModel.
- [ ] PreToolUse.
- [ ] PostToolUse.
- [ ] ToolError.
- [ ] PermissionRequest.
- [ ] SubagentStart.
- [ ] SubagentStop.
- [ ] BeforeCompact.
- [ ] AfterCompact.
- [ ] BeforeFinal.
- [ ] SessionEnd.
- [ ] trust/capability enforcement.

---

# Fase 6 — Skills productivos y multi-agent

## Objetivo

Convertir workflows expertos y paralelismo en capacidades de primera clase.

## Criterio de aceptación

Rinari debe poder delegar trabajo independiente, aislar writers, cargar skills bajo demanda y sintetizar resultados con provenance.

## Skill Runtime

- [ ] skill manifest.
- [ ] metadata.
- [ ] version.
- [ ] description.
- [ ] triggers.
- [ ] required capabilities.
- [ ] optional capabilities.
- [ ] risk.
- [ ] procedure.
- [ ] verification.
- [ ] failure policy.
- [ ] success criteria.

## Skill discovery

- [ ] packaged.
- [ ] user.
- [ ] project trusted.
- [ ] summaries only initially.
- [ ] full lazy load.
- [ ] activation trace.
- [ ] conflict resolution.

## Skills iniciales completas

- [ ] repository-explore.
- [ ] implement-feature.
- [ ] fix-bug.
- [ ] debug.
- [ ] test.
- [ ] code-review.
- [ ] refactor.
- [ ] fix-ci.
- [ ] research.
- [ ] final-verification.

## Skill CLI

- [ ] list.
- [ ] search.
- [ ] show.
- [ ] activate.
- [ ] deactivate.
- [ ] install.
- [ ] remove.
- [ ] update.
- [ ] validate.
- [ ] test.
- [ ] create.

## Agent Registry

- [ ] AgentDefinition.
- [ ] tool allowlist.
- [ ] capability scope.
- [ ] budget.
- [ ] context scope.
- [ ] output contract.
- [ ] provenance.

## Built-in agents

- [ ] Explore.
- [ ] Reviewer.
- [ ] Debugger.
- [ ] Researcher.
- [ ] Implementer.
- [ ] Verifier.

## Agent Orchestrator

- [ ] spawn.
- [ ] status.
- [ ] message.
- [ ] wait.
- [ ] cancel.
- [ ] result.
- [ ] task ownership.
- [ ] bounded objective.
- [ ] concurrency controls.
- [ ] max depth.
- [ ] max total agents.
- [ ] cancellation propagation.

## Agent isolation

- [ ] read-only Explore.
- [ ] read-only Reviewer default.
- [ ] Researcher without local writes default.
- [ ] verifier restrictions.
- [ ] per-agent permissions.
- [ ] per-agent budgets.
- [ ] no permission inheritance bugs.

## Worktrees

- [ ] worktree manager.
- [ ] isolated writer workspace.
- [ ] branch naming.
- [ ] patch/commit result.
- [ ] merge/integration.
- [ ] conflict reporting.
- [ ] cleanup.
- [ ] prevent blind same-tree parallel writes.

## Multi-agent synthesis

- [ ] structured AgentResult.
- [ ] evidence refs.
- [ ] contradiction detection.
- [ ] independent verification.
- [ ] task graph join.
- [ ] duplicate-work avoidance.

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

---

# Decisiones pendientes

## Bloqueante de Fase 0

- [x] **Licencia del proyecto**

      Resuelta: MIT (confirmada por Xainner, 2026-08-16).
      Ya no quedan bloqueantes de Fase 0.

## No bloqueantes hasta su fase

Estas decisiones pueden resolverse durante implementación porque no cambian el contrato de producto ya definido:

- [ ] backend exacto inicial del Credential Store por plataforma;
- [ ] librería/engine exacto para browser automation;
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
  Fase 2 — Agent Runtime, Tool Runtime y seguridad base

COMPLETADO
  fase 0 completa (2026-08-16)
  identidad
  arquitectura
  CLI contract
  provider/model semantics
  session semantics
  CHAT → PROJECT
  tool/skill catalogs
  full harness target
  visual CLI contract
  testing strategy
  Git strategy
  licencia (MIT + LICENSE)
  fase 1 completa (2026-08-16)
  packaging (pyproject, uv, entrypoint `rinari`)
  estructura base src/rinari
  configuración + SQLite + credential store
  provider/model registry + CLI
  session CHAT/PROJECT + persistencia
  Soul + Constitution (assets, loader, overrides)
  Build Manifest
  doctor/status/version

SIGUIENTE
  Model Runtime (ModelProvider, streaming, tool calls)
  → Prompt Assembler (segmentos, trust, caching)
  → Tool Registry / Tool Runtime
  → Filesystem + Shell/process + Git tools
  → Policy / Sandbox / Approval
  → Agent loop + cancellation + trace
```
