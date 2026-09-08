# Rinari CLI — Auditoría del Harness y Plan de Productización

> **Repositorio auditado:** `Xainner/Rinari-CLI-2`  
> **Snapshot de referencia:** `7a789d3eabdeb041b2f57334e92d5b9e192ddd08`  
> **Fecha de revisión:** 2026-09-07  
> **Objetivo:** convertir el harness actual en un runtime de agente realmente productivo, estable y extensible, especialmente para tareas de coding con muchas rondas de tool calls, MCP, plugins, browser, subagentes y sesiones largas.  
> **Nota:** el repositorio cambió durante la revisión. Este documento está anclado al SHA anterior para que cada recomendación tenga una base concreta.

---

## 0. Conclusión ejecutiva

Rinari **no necesita otro rewrite**.

La arquitectura actual ya tiene la mayoría de los componentes difíciles de un harness serio:

- estado de sesión propio y provider-independent;
- CHAT / PROJECT y promoción de sesión;
- Tool Runtime unificado;
- policy + approvals + sandbox;
- artifacts;
- context compaction;
- memory y retrieval;
- verification/completion gate;
- MCP;
- plugins;
- OpenAPI;
- browser CDP;
- skills;
- subagentes;
- worktrees;
- hooks;
- tracing;
- evals;
- CLI Rich/JSON;
- persistencia SQLite;
- cancellation y budgets.

El problema principal ya no es **“qué feature falta”**. El problema es que varias abstracciones correctas existen como metadata o diseño, pero todavía **no gobiernan el runtime real**.

Los mayores bloqueadores de productividad son:

1. **Hay dos fuentes de verdad para los límites de tools y se contradicen.** El presupuesto fue subido a 64 tools, pero `AgentLoop` sigue imponiendo 32.
2. **Se envía el ToolRegistry completo al modelo en cada llamada.** A medida que agregas browser + MCP + OpenAPI + plugins + agents, el modelo recibe demasiados schemas.
3. **El resultado que ve el modelo pierde información crítica.** `ToolResult` tiene error code, retryability, artifacts, truncation, etc., pero `to_model_text()` casi todo lo elimina.
4. **`ToolDefinition` ya declara `timeout_ms`, `max_output_bytes`, `output_schema`, `idempotent`, `side_effects` y `always_loaded`, pero el runtime no aprovecha todos esos campos.**
5. **El cambio de provider/model in-session tiene un bug de wiring:** se crea un nuevo `ModelCaller`, pero el `AgentLoop` sigue apuntando al caller anterior.
6. **La abstracción `ChatMessage(content: str)` es demasiado pobre para providers modernos** con response items, bloques de reasoning opacos, tool calls complejas y estados de streaming.
7. **Los budgets de red y subagentes no están integrados completamente con la ejecución real.**
8. **La cobertura unitaria es amplia, pero `tests/e2e/` está vacío.** Para un harness, los bugs más caros aparecen en secuencias largas y cross-layer, no en funciones aisladas.
9. **No encontré `.github/` en el snapshot**, por lo que el producto todavía no tiene un release gate automatizado visible en el repo.

La recomendación es hacer un **Hardening Pass del runtime**, no seguir agregando features grandes hasta cerrar estos contratos.

---

# 1. Qué está bien y debe conservarse

## 1.1 Conversation state propio

La decisión de mantener `ChatMessage` y el historial bajo control de Rinari, en lugar de hacer que una thread remota de un provider sea la fuente de verdad, es correcta.

Eso permite:

- cambiar provider;
- cambiar model;
- persistir;
- resumir;
- compactar;
- inspeccionar;
- exportar;
- ejecutar evals reproducibles;
- no quedar atado a IDs de un vendor.

No cambiaría esa dirección.

Lo que sí haría es enriquecer el modelo de mensajes para no perder capacidades provider-specific importantes.

---

## 1.2 Tool Runtime único

`src/rinari/tools/runtime.py` tiene una de las mejores decisiones del repo:

```text
lookup
→ input validation
→ classification
→ policy
→ approval
→ sandbox/execution
→ normalization
→ redaction
→ event
→ artifact spill
→ bounded model observation
```

MCP, plugins, OpenAPI y tools nativas deben seguir convergiendo ahí.

No crearía runners paralelos por ecosistema.

---

## 1.3 ToolDefinition ya contiene casi toda la metadata necesaria

Hoy ya existe:

```python
output_schema
capabilities
permissions
risk
side_effects
idempotent
timeout_ms
max_output_bytes
always_loaded
namespace
manifest
```

Esto es importante: no hace falta inventar otro manifest.

El trabajo pendiente es lograr que estos campos afecten el scheduling, retries, exposición al modelo, timeouts, output validation y budgets.

---

## 1.4 Artifacts + compaction + verification

Los cambios recientes son una mejora real:

- `artifact://<session>/...`;
- spill seguro;
- guidance para leer el artifact antes de editar;
- `SessionTurnLock`;
- completion gate por turno;
- eventos con `turn_index` / `tool_seq`.

No revertiría nada de esto.

---

# 2. P0 — Fixes que haría antes de seguir expandiendo el producto

---

## P0.1 — El límite efectivo de tools todavía es 32, no 64

### Estado actual

En:

`src/rinari/runtime/budget.py`

el commit actual tiene aproximadamente:

```python
max_model_calls = 16
max_tool_calls = 64
```

Pero en:

`src/rinari/runtime/agent.py`

siguen existiendo:

```python
DEFAULT_MAX_MODEL_CALLS = 8
DEFAULT_MAX_TOOL_CALLS = 32
```

y dentro del loop:

```python
if tool_calls_made >= self._max_tool_calls or not allowed:
    result = RESOURCE_EXHAUSTED
```

Por tanto:

```text
TurnBudgetLimits.max_tool_calls = 64
AgentLoop._max_tool_calls       = 32

effective tool limit            = 32
```

Este es probablemente uno de los motivos directos de la sensación de que el harness “se queda corto”.

### Fix

Debe haber **una sola fuente de verdad**.

Recomiendo que el `BudgetMeter` sea la fuente autoritativa cuando existe.

Conceptualmente:

```python
if budget is not None:
    allowed = budget.allows_tool(tool)
else:
    allowed = tool_calls_executed < self._fallback_max_tool_calls
```

No:

```python
hard_limit AND budget_limit
```

El límite interno del AgentLoop debe ser solamente un fallback defensivo para tests o runtimes sin BudgetMeter.

### También separar

Hoy `tool_calls_made` aumenta incluso cuando la llamada no se ejecuta por budget.

Conviene medir por separado:

```text
tool_calls_requested
tool_calls_executed
tool_calls_rejected
```

El budget debe gastar `executed`, no confundir intentos rechazados con ejecución real.

### Test obligatorio

```text
31 tools → ejecuta
32 tools → ejecuta
33 tools → ejecuta
63 tools → ejecuta
64 tools → ejecuta
65 tools → RESOURCE_EXHAUSTED
```

según el contrato exacto que decidas.

No mezclar `>` y `>=` entre `allows_tool()` y `exhausted()`.

Actualmente `BudgetMeter.exhausted()` usa `>` mientras los gates usan `>=`; eso crea estados donde el snapshot dice “no exhausted” aunque ya no se puede ejecutar otra llamada.

---

## P0.2 — Bug al cambiar provider/model dentro de la sesión

### Estado actual

`_apply_session()` hace:

```python
session.caller = _caller_for(...)
session.context.model_ref = ...
```

pero `AgentLoop` fue construido antes con:

```python
self._provider = model_provider
```

y no se actualiza.

Además `ModelCaller` es immutable/frozen.

Resultado potencial:

```text
UI/session record → provider/model nuevo
AgentLoop          → caller viejo
```

Es un bug de correctness, no solo UX.

### Fix mínimo

Agregar:

```python
class AgentLoop:
    def set_model_provider(self, provider):
        self._provider = provider
```

y llamar:

```python
session.loop.set_model_provider(session.caller)
```

### Fix recomendado

Mejor usar una indirection estable:

```python
class SessionModelGateway:
    current: ModelCaller

    def switch(self, caller: ModelCaller) -> None:
        self.current = caller

    def invoke(...):
        return self.current.invoke(...)
```

Entonces:

```text
AgentLoop → SessionModelGateway → ModelCaller actual
```

y nunca hay dos referencias divergentes.

### Test obligatorio

1. iniciar con fake provider A;
2. hacer un turn;
3. `/model` o `/provider`;
4. cambiar a fake provider B;
5. siguiente turn debe registrar llamada únicamente en B;
6. historial debe mantenerse.

---

## P0.3 — No enviar todos los tool schemas en cada request

### Estado actual

`AgentLoop._build_request()` hace:

```python
tools = self._tools.registry.for_model()
```

y `ToolRegistry.for_model()` sin `names` usa todo el registry.

Al mismo registry entran:

- native tools;
- skills tools;
- `agent.*`;
- browser;
- MCP;
- OpenAPI;
- plugin tools;
- capability search.

Rinari ya declara una estrategia lazy en su documentación y `ToolDefinition` ya tiene `always_loaded`, pero ese campo no controla la exposición real.

### Por qué esto degrada el harness

Aunque el provider acepte muchos functions, no significa que debas enviarlas todas.

El costo real aparece en:

```text
más tokens de schema
+ mayor latencia
+ peor selección de tool
+ nombres semánticamente parecidos
+ mayor probabilidad de argumentos incorrectos
+ context pressure más temprano
+ peor prompt caching
+ más dificultad para providers pequeños/locales
```

Con browser (~25 tools), agents, MCP y APIs externas, este problema crece rápido.

### Arquitectura propuesta: Tool Exposure / Tool View

Agregar un objeto session-scoped:

```python
@dataclass
class ToolExposure:
    core: set[str]
    activated: dict[str, Activation]
    max_schema_count: int
    max_schema_tokens: int
```

El modelo ve:

```text
core tools
+ tools activadas para el objetivo actual
+ capability.search
```

El registry sigue conteniendo todo.

```text
REGISTERED != EXPOSED
```

### Ejemplo de core PROJECT

No tiene que ser exactamente esta lista, pero conceptualmente:

```text
capability.search
fs.read_lines
fs.list
search.regex
search.symbols
search.references
fs.patch
shell.exec
git.status
git.diff
verify.plan
verify.record
verify.evaluate
```

Browser, MCP, OpenAPI, plugins especializados y la mayoría de agent tools se activan bajo demanda.

### Aprovechar `always_loaded`

Cambiar la semántica de:

```python
registry.for_model()
```

para que por defecto devuelva solamente:

```text
tool.always_loaded == True
+ session activated tools
```

No todas las registradas.

### Activación

`capability.search` ya existe.

Haría que, además de retornar resultados, pueda activar las N mejores capabilities para el siguiente model call:

```json
{
  "results": [
    {"name": "mcp.github.create_issue", "...": "..."}
  ],
  "activated": [
    "mcp.github.create_issue"
  ]
}
```

Alternativamente crear:

```text
capability.search
capability.activate
capability.deactivate
```

Prefiero activación explícita si quieres traceabilidad máxima.

### TTL

No mantener tool schemas especializados para siempre:

```text
activation_scope = turn | task | session
ttl_rounds = 4
```

### Métricas

Mostrar en trace:

```text
registered_tools
exposed_tools
tool_schema_bytes
tool_schema_estimated_tokens
activated_tools
activation_reason
```

---

## P0.4 — ToolResult debe ser una observación estructurada para el modelo

### Estado actual

`ToolResult` contiene:

```text
ok
data
error.code
error.message
error.retryable
error.details
artifacts
duration_ms
side_effects
truncated
origin
```

Pero `to_model_text()` hace esencialmente:

```python
payload = data if ok else error.message
```

El modelo pierde:

- error code;
- retryability;
- details;
- artifact references en algunos casos;
- origin;
- side effects;
- duración;
- truncation explícita.

### Resultado

El modelo recibe:

```text
"request timed out"
```

cuando debería recibir:

```json
{
  "ok": false,
  "tool": "web.fetch",
  "error": {
    "code": "TIMEOUT",
    "message": "request timed out",
    "retryable": true
  },
  "truncated": false,
  "artifacts": []
}
```

Eso mejora muchísimo:

- retry decisions;
- fallback selection;
- loop detection;
- error recovery;
- eval reproducibility.

### Nuevo contrato

```python
@dataclass(frozen=True)
class ToolObservation:
    tool_call_id: str
    tool: str
    ok: bool
    data: Any
    error: ToolErrorInfo | None
    artifacts: tuple[ArtifactRef, ...]
    truncated: bool
    origin: str
    duration_ms: float
```

`to_model_text()` serializa **siempre el envelope**.

### Bounded output

No usar `2048` caracteres como límite universal fijo.

Hazlo budget-aware:

```text
small result              → inline
medium result             → inline hasta observation budget
large result              → summary + artifact:// URI
very large structured     → summary + selected fields + artifact
```

Para coding, 2 KB puede ser demasiado poco para:

- compiler errors;
- test failures;
- diffs;
- search results;
- stack traces.

Un `ObservationBudget` separado es mejor que un corte mágico.

---

## P0.5 — Hacer efectivos los contratos de ToolDefinition

Actualmente la metadata está mejor diseñada que el executor.

### `output_schema`

Existe, pero el runtime no valida que `ToolResult.data` cumpla el schema.

Agregar:

```text
handler()
→ normalize ToolResult
→ validate output_schema
→ VALIDATION_FAILED si no cumple
```

Especialmente importante para:

- plugins;
- MCP;
- OpenAPI;
- tools generadas dinámicamente.

### `timeout_ms`

Existe, pero `ToolRuntime` llama directamente:

```python
tool.handler(arguments, ctx)
```

Sin enforcement central del timeout específico de la tool.

No basta con que algunas tools internas manejen timeout.

Agregar un `Deadline` al ToolContext:

```python
deadline_at: float | None
```

y obligar a:

- HTTP;
- MCP;
- browser;
- subprocess;
- LSP;
- plugin RPC;

a respetarlo.

Para código Python síncrono arbitrario, un thread no se puede matar de forma segura. Para plugins no confiables o potencialmente bloqueantes, considerar aislamiento por subprocess.

### `max_output_bytes`

Debe imponerse por tool, no solo por un spill threshold global.

```text
effective_max_output =
    min(tool.max_output_bytes, runtime_global_max)
```

cuando exista.

### `idempotent`

Usarlo para retries.

```text
idempotent = true
    → retry automático permitido bajo policy

idempotent = false
    → nunca repetir automáticamente después de resultado ambiguo
```

### `side_effects`

Ya aporta a policy, pero también debe gobernar scheduling:

```text
none                 → paralelo si no comparte resource lock
local-reversible     → serial por workspace/path
local-destructive    → serial + approval/checkpoint
remote-*             → serial o idempotency-key-aware
communication        → jamás auto-retry ambiguo
financial            → jamás auto-retry ambiguo
```

---

## P0.6 — Enriquecer ModelRequest / ModelResponse antes de ampliar providers

### Estado actual

El contrato normalizado es aproximadamente:

```python
ChatMessage:
    role
    content: str
    tool_calls

ModelResponse:
    content: str
    tool_calls
    usage
    stop_reason
```

Esto funcionó para levantar el runtime inicial, pero es demasiado estrecho para un harness moderno.

### Problemas

Se pierden o son difíciles de representar:

- multiple content blocks;
- reasoning/thinking blocks opacos;
- refusals;
- citations;
- provider event items;
- tool approval requests;
- richer usage;
- response item IDs;
- partial / incomplete responses;
- provider-side MCP;
- custom tool calls;
- background/resume semantics;
- cache metadata.

### Anthropic

El adapter actual conserva text + `tool_use`, pero descarta otros bloques.

Si más adelante habilitas thinking/extended reasoning, Anthropic requiere que ciertos thinking blocks asociados a tool use se devuelvan sin modificarlos en turnos posteriores.

No necesitas mostrar esos bloques al usuario ni convertirlos en “chain of thought”; sí necesitas poder conservar el bloque opaco requerido por el protocolo.

### Nuevo shape recomendado

```python
@dataclass(frozen=True)
class ModelItem:
    type: str
    id: str | None
    data: dict


@dataclass(frozen=True)
class ModelResponse:
    items: tuple[ModelItem, ...]
    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    stop_reason: StopReason
    provider_state: ProviderState | None
```

`provider_state` es transport metadata y **no** reemplaza el historial canónico de Rinari.

### Regla

```text
Rinari session history = source of truth

provider state =
    optional optimization / replay requirement
    nunca única copia del estado
```

---

## P0.7 — Adapter OpenAI dedicado a Responses API

Mantendría:

```text
OpenAICompatibleAdapter
```

para:

- Ollama;
- LM Studio;
- endpoints custom;
- providers que realmente implementan Chat Completions compatible.

Pero crearía:

```text
OpenAIResponsesAdapter
```

para OpenAI.

### Motivo

El Responses API moderno expone primitives útiles para un harness:

- function tools con strict schema;
- `tool_choice`;
- `parallel_tool_calls`;
- streaming por response items;
- `previous_response_id`;
- prompt caching controls;
- richer usage;
- built-in tools si decides integrarlos;
- MCP remoto si alguna vez quieres soportarlo como capability provider-side.

Rinari debe seguir controlando sus tools locales, policy y sandbox, pero el adapter no debería reducir OpenAI al mínimo común denominador de `/chat/completions`.

### Importante

No conviertas `previous_response_id` en fuente de verdad.

Úsalo solamente como optimización si:

- la configuración de privacidad lo permite;
- la sesión sigue persistiendo localmente;
- existe fallback a replay local.

---

## P0.8 — No convertir JSON inválido de tool args en `{}` silenciosamente

Tanto en streaming como en parsing normal, los adapters actuales pueden terminar haciendo:

```python
except JSONDecodeError:
    arguments = {}
```

Eso cambia:

```text
“el provider produjo argumentos inválidos”
```

por:

```text
“el usuario/model pidió la tool con un objeto vacío”
```

y puede generar acciones incorrectas o loops.

### Fix

Crear un estado explícito:

```python
ToolCallParseError
```

o representar:

```python
ToolCall(arguments=None, raw_arguments="...", parse_error="INVALID_JSON")
```

y convertirlo en una observación estructurada para el modelo:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "Model emitted invalid JSON for tool arguments",
    "retryable": true
  }
}
```

No ejecutar la tool.

---

## P0.9 — Budget de red basado en capability, no en prefijo del nombre

Actualmente el BudgetMeter considera red por namespaces como:

```text
web
http
browser
```

Pero Rinari también puede hacer red mediante:

```text
mcp.*
api.*
plugin.*
```

El ToolRuntime ya sabe clasificar:

```text
network.outbound
```

Ese dato debe alimentar el budget.

### Fix

En lugar de:

```python
budget.note_tool_call(call.name)
```

usar:

```python
budget.note_tool_call(tool=definition, action=classified_action)
```

y contar network cuando:

```text
action.capability == network.outbound
```

No cuando el nombre empieza por X.

---

## P0.10 — Integrar el budget de subagentes con el parent turn

`TurnBudgetLimits` tiene:

```text
max_subagent_calls
max_recursion_depth
```

pero `agent.spawn` delega directamente al orchestrator.

El parent `BudgetMeter` no parece ser la autoridad de esa llamada.

El orchestrator sí tiene sus propios límites, lo cual es bueno, pero quedan dos modelos de budget separados.

### Recomendación: Hierarchical Budget Ledger

```text
Session Turn Budget
├── main agent
├── subagent A
├── subagent B
└── subagent C
```

Cada child recibe una cuota.

El parent acumula:

```text
model calls
tool calls
network calls
cost
wall time
subagent count
max depth
```

Así evitas:

```text
main: 64 tools
+ agent A: 64
+ agent B: 64
+ agent C: 64
```

sin que el status principal refleje el costo real.

---

# 3. Cambiar la filosofía de budgets: soft limit + hard safety limit

Subir 32 → 64 ayuda, pero no resuelve el diseño.

Un coding agent real no sabe de antemano si una tarea correcta requiere:

```text
6
18
37
84
```

tool executions.

La solución tampoco es “unlimited”.

## 3.1 Budget profile

Agregar perfiles:

```toml
[runtime.budget.quick]
soft_model_calls = 6
soft_tool_calls = 20
hard_model_calls = 12
hard_tool_calls = 40
wall_time_s = 180

[runtime.budget.normal]
soft_model_calls = 14
soft_tool_calls = 48
hard_model_calls = 28
hard_tool_calls = 112
wall_time_s = 900

[runtime.budget.deep]
soft_model_calls = 24
soft_tool_calls = 96
hard_model_calls = 48
hard_tool_calls = 192
wall_time_s = 1800
```

Los valores exactos deben salir de evals, no tomarse como dogma.

## 3.2 `auto` como default

El profile `auto` puede elegir según:

```text
CHAT simple
read-only repository question
debug
implementation
review
research
multi-agent
```

## 3.3 Al llegar al soft limit

No cortar.

Insertar una señal de runtime:

```text
Budget pressure:
- soft tool budget reached
- task is not complete
- continue only if meaningful progress is still being made
- prefer verification/finalization over broad exploration
```

## 3.4 Progress detector

Continuar si hubo progreso reciente:

```text
new file evidence
new search evidence
new validation evidence
changed task state
new artifact
new resolved blocker
new diagnostic
successful fallback
```

Detener si:

```text
same tool + same args
same error
same search query
no state delta
no evidence delta
loop detector triggered
```

## 3.5 Al llegar al hard limit

No devolver solamente:

```text
Stopped: budget exhausted
```

Persistir:

```text
continuation checkpoint
remaining objective
completed actions
unresolved blockers
latest verification
suggested next action
```

y devolver:

```text
kind = needs_resume
resume_token/session_id = ...
```

En REPL:

```text
Budget hard limit reached.
Work is checkpointed and resumable.
Use /continue to extend this turn.
```

Esto hace que un límite deje de sentirse como “el agente murió”.

---

# 4. Tool exposure dinámico: el cambio con mayor impacto después de los P0

## 4.1 Tres niveles

### Nivel A — Always loaded

Muy pocas tools fundamentales.

### Nivel B — Task activated

Tools relevantes al objetivo actual.

Ejemplo:

```text
“fix failing FastAPI tests”
```

puede activar:

```text
search.*
fs.*
shell.*
verify.*
lsp.*
```

No necesita las 25 browser tools.

### Nivel C — On-demand ecosystem

```text
MCP
OpenAPI
plugins
browser
specialized agents
```

se descubren mediante capability search.

---

## 4.2 Schema token budget

No limitar solamente por número de tools.

Dos tools pueden tener schemas de tamaños radicalmente distintos.

Agregar:

```python
max_tool_schema_tokens
```

y construir el set:

```text
required core
+ high score task tools
+ recently used tools
+ activated skills
```

hasta agotar el budget.

---

## 4.3 Mejorar capability.search

El ranking actual es simple y determinista, lo cual es una buena base.

Para un registry grande usaría scoring ponderado:

```text
exact tool-name match       +10
namespace match              +6
manifest tags                +5
verb/object match            +4
description tokens           +2
recently successful          +2
task-mode relevant           +2
risk demotion
health demotion
unavailable                 reject
```

No necesitas embeddings para la primera versión.

---

## 4.4 Health-aware discovery

Una tool no debería rankear alto si su backend está roto.

Incluir:

```text
healthy
degraded
auth_required
offline
disabled
```

para MCP/plugins/OpenAPI/browser.

`capability.search` debe considerar health.

---

# 5. Parallel tool calls sin romper seguridad

Los providers modernos pueden proponer varias tool calls en un mismo model turn.

Hoy Rinari las ejecuta secuencialmente.

Eso es correcto como baseline, pero lento para exploración.

## 5.1 Scheduler

Clasificar calls:

```text
READ_ONLY + idempotent + recursos independientes
    → concurrent

writes
    → serial

same path
    → serial

shell mutation
    → serial

remote side effects
    → serial unless explicit idempotency contract
```

Ejemplo paralelo seguro:

```text
read pyproject.toml
read src/foo.py
git.status
search.symbols("Bar")
```

Ejemplo no paralelo:

```text
fs.patch(foo.py)
fs.patch(foo.py)
pytest
```

---

## 5.2 Determinismo

Aunque se ejecuten concurrentemente, devolver al modelo los observations en orden estable:

```text
original tool call order
```

y persistir:

```text
started_at
completed_at
execution_order
response_order
```

---

# 6. Provider layer: separar “portable core” de “provider features”

## 6.1 Capability matrix por model, no solo por adapter

Hoy capabilities como:

```text
streaming
tool_calls
structured_output
reasoning_effort
max_context_tokens
```

se declaran a nivel de adapter.

Pero dos modelos del mismo provider pueden diferir.

Agregar:

```python
ProviderModelCapabilities
```

resuelto por:

```text
provider + provider_model_id
```

con fallback al adapter.

---

## 6.2 Streaming usage

El OpenAI-compatible streaming actual termina con `Usage()` vacío.

Eso debilita:

- metrics;
- cost;
- context pressure;
- budget;
- UX tokens.

Si el endpoint soporta streaming usage, capturarlo.

Si no, marcarlo como:

```text
usage_source = unavailable
```

No inventar números.

---

## 6.3 Error taxonomy provider → Rinari

Normalizar:

```text
AUTH
RATE_LIMIT
CONTEXT_OVERFLOW
INVALID_TOOL_SCHEMA
INVALID_TOOL_ARGUMENTS
MODEL_NOT_FOUND
MODEL_UNAVAILABLE
SAFETY_BLOCK
SERVER_ERROR
STREAM_INTERRUPTED
TIMEOUT
```

con:

```text
retryable
retry_after
request_id
provider
model
```

El model runtime debe poder decidir:

```text
retry same
fallback model
compact context
reduce tools
ask auth
stop
```

---

## 6.4 Retry policy

### Model calls

Retry con backoff para:

```text
429
5xx
connection reset
temporary timeout
```

solo si no se produjo un response final ambiguo.

### Tool calls

Auto-retry solamente cuando:

```text
ToolDefinition.idempotent == true
AND error.retryable == true
```

Para side effects remotos no idempotentes:

```text
no blind retry
```

---

# 7. MCP: actualizar el contrato para structured output moderno

El cliente actual conserva:

```text
name
description
input_schema
annotations
```

pero no `outputSchema`.

Además `McpService.invoke()` solo conserva `structuredContent` cuando es:

```text
dict | list
```

Las revisiones modernas de MCP amplían structured output y JSON Schema.

## Cambios

`McpToolInfo`:

```python
output_schema: dict | None
```

Preservar también:

```text
title
icons
annotations
meta
```

cuando sean útiles.

Al ejecutar:

```text
if output_schema exists:
    validate structuredContent
```

No restringir structured content artificialmente a `dict/list` si la versión negociada permite otros JSON values.

## Protocol version

Persistir:

```text
negotiated_protocol_version
server_capabilities
server_info
```

y adaptar codecs por versión.

## Conformance tests

Agregar fixtures para:

```text
2025-era tool
2026-07-28 tool
object structured output
array structured output
string structured output
invalid structured output
tool error
timeout
server disconnect
```

---

# 8. Cancellation real

El `CancellationToken` actual es cooperativo:

```python
_cancelled: bool
throw_if_cancelled()
```

Eso sirve entre boundaries, pero una llamada HTTP bloqueada puede tardar hasta el timeout en devolver control.

## Mejoras

Agregar callbacks:

```python
token.on_cancel(callback)
```

El ModelGateway puede registrar:

```text
close stream
cancel async task
close request
```

Subprocess:

```text
SIGINT
→ grace period
→ SIGTERM
→ grace period
→ SIGKILL
```

según platform/policy.

Browser/MCP:

```text
abort pending request
close pending RPC waiter
```

Objetivo de UX:

```text
Ctrl+C significa “detener trabajo actual”
no “esperar hasta que la red expire”
```

---

# 9. Extension failures: no más silent degradation invisible

Hay varios lugares que usan:

```python
contextlib.suppress(Exception)
```

para que:

- plugin;
- MCP;
- OpenAPI;
- hook;

no tumben la sesión.

La intención es correcta.

El problema es que “no crash” se convierte en “desapareció sin explicación”.

## Mantener resiliencia, agregar diagnostics

Crear:

```python
ExtensionDiagnostic:
    source
    component
    severity
    message
    exception_type
    timestamp
```

y un session health snapshot.

Mostrar:

```text
MCP github       healthy
MCP jira         auth_required
plugin foo       load_failed
browser          unavailable
hook lint        failed last run
```

Comando:

```bash
rinari doctor --runtime
rinari doctor --extensions
```

REPL:

```text
/status
```

debe mostrar degradaciones relevantes sin inundar.

---

# 10. Context Engine: contar también tool schemas y system segments

El pressure calculation no debe mirar solo:

```text
history / usage input tokens
```

El input real contiene:

```text
system prompt
Soul
policy
instructions
skills
task state
memory
compact state
history
tool schemas
```

Con tool ecosystem grande, los schemas pueden ser una fracción importante.

## Context accounting

Antes de invocar:

```python
ContextSnapshot:
    system_tokens
    history_tokens
    tool_schema_tokens
    pinned_tokens
    compact_state_tokens
    estimated_total
    provider_window
    pressure
```

Esto también permite detectar:

```text
“el problema no es la conversación; son 80 schemas”
```

y reducir tools antes de compactar historial útil.

---

# 11. E2E: el hueco de calidad más importante

El repo tiene muchas pruebas unitarias, pero en el snapshot auditado:

```text
tests/e2e/
    __init__.py
```

No hay una suite E2E real allí.

Para un agent harness esto es peligroso porque los fallos más importantes aparecen al encadenar:

```text
model
→ tool
→ persistence
→ model
→ approval
→ tool error
→ retry
→ compaction
→ resume
→ verification
```

## 11.1 Fake provider determinista

Crear un provider scripted:

```python
ScriptedProvider([ToolCall(...), ToolCall(...), ToolCall(...), FinalAnswer(...)])
```

Debe poder simular:

```text
streaming
malformed args
duplicate call id
timeout
429
5xx
stream disconnect
max tokens
empty response
parallel calls
```

---

## 11.2 Casos E2E mínimos

### Long coding turn

```text
40+ tools reales/fake
múltiples inspect/edit/test cycles
final answer
no premature budget stop
```

### 64 boundary

Verificar el límite exacto configurado.

### Dynamic tools

Registrar 150 tools.

Assert:

```text
registered = 150+
exposed per model request <= configured schema budget
```

y que capability search active la correcta.

### Provider switch

A → B in-session y siguiente request va a B.

### Resume

Interrumpir después de tool result, reiniciar runtime, resume sin orphan tool call.

### Context compaction

Forzar compaction en medio de una tarea con tool history.

Assert:

```text
no orphan tool result
task truth survives
verification evidence survives
```

### Large output

Tool produce 1 MB.

Assert:

```text
artifact created
model gets bounded observation
artifact.read works
no context explosion
```

### Tool timeout

Tool cuelga.

Assert:

```text
TIMEOUT
retryable metadata correct
session stays healthy
```

### MCP structuredContent

Object, array y scalar según protocol version.

### Approval

```text
ask → deny
ask → once
ask → session
```

sin doble ejecución.

### Dirty worktree

No sobrescribir user changes silenciosamente.

### Multi-agent aggregate budget

Varios agents no pueden evadir el parent hard budget.

### Cancellation

Cancelar:

```text
model stream
shell process
MCP request
browser action
subagent
```

### Crash/reconcile

Matar el proceso entre:

```text
ToolRequested
ToolCompleted
assistant continuation
```

y validar reconciliación.

---

# 12. CI / release gate

No encontré `.github/` en el snapshot.

Para un CLI con:

- POSIX PTY;
- Windows fallback;
- subprocess;
- SQLite;
- terminal rendering;
- networking;
- package build;

la CI cross-platform debe ser parte del producto.

## Workflow recomendado

### Pull request

```text
ruff
pytest unit
pytest integration
pytest e2e hermetic
build wheel
install wheel
rinari version
rinari doctor --non-interactive
```

### Matrix

Como mínimo:

```text
Linux / Python 3.11
Linux / latest supported Python
Windows / latest supported Python
macOS / latest supported Python
```

No necesitas correr la matriz completa sobre todos los tests si se vuelve pesada:

```text
Linux → suite completa
Windows/macOS → platform + smoke + e2e críticos
```

### Release

```text
build sdist/wheel
fresh-venv install
pipx/uv tool style smoke
version consistency
migration upgrade test
package assets present
CLI help smoke
```

---

# 13. Observability que hace debugging del harness mucho más fácil

Cada model round debería poder reconstruirse como span:

```text
Turn
└── ModelRound 1
    ├── request
    ├── ToolBatch
    │   ├── ToolCall
    │   └── ToolCall
    └── observation
└── ModelRound 2
```

## Guardar por model invocation

```text
provider
model
round_index
input token estimate
actual usage
tool schemas exposed
tool schema bytes/tokens
history message count
context pressure
time-to-first-token
total model latency
stop reason
retry count
request ID
```

## Por tool

```text
tool_call_id
name
origin
capability
side_effect_class
attempt
duration
ok
error_code
retryable
output bytes
artifact count
truncated
approval outcome
```

## CLI

Agregar:

```bash
rinari trace last
rinari trace turn <n>
rinari trace tools
```

En REPL:

```text
/trace
/tools
/budget
```

---

# 14. UX para que el harness “se sienta productivo”

La productividad no es solo inteligencia.

El usuario necesita entender qué está pasando sin leer logs.

## Status rail

Mostrar opcionalmente:

```text
model        gpt-...
effort       high
mode         PROJECT / agent
context      43%
round        7
tools        18 executed
budget       normal · 46 remaining
subagents    2 running
git          3 files changed
verify       tests pending
```

No hace falta renderizar todo siempre. Un modo compact y `/status` bastan.

## Tool events

Agrupar tools rápidas:

```text
✓ inspected 7 files
✓ found 12 references
✓ patched 2 files
▶ running targeted tests
```

en vez de imprimir ruido por cada read pequeño.

## Budget pressure

No presentar como error técnico.

```text
Rinari is nearing the normal task budget.
It is finishing verification before deciding whether to continue.
```

## Degraded capabilities

Una sola advertencia clara:

```text
MCP jira unavailable (auth required). Other tools remain available.
```

No ocultarla y tampoco repetirla cada round.

---

# 15. Arquitectura objetivo

```text
┌────────────────────────────────────────────────────┐
│ CLI / REPL / one-shot / JSON                       │
└──────────────────────────┬─────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────┐
│ Session Host                                       │
│ - session lock                                     │
│ - provider/model selection                         │
│ - RuntimeSnapshot                                  │
│ - persistence                                      │
└──────────────────────────┬─────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────┐
│ Agent Runtime                                      │
│                                                    │
│  TurnController                                    │
│  ├─ BudgetLedger                                   │
│  ├─ ContextManager                                 │
│  ├─ ToolExposure                                   │
│  ├─ LoopDetector                                   │
│  ├─ ProgressDetector                               │
│  └─ CompletionGate                                 │
│                                                    │
│  ModelGateway                                      │
│  └─ Provider Adapter                               │
│                                                    │
│  ToolScheduler                                     │
│  └─ ToolRuntime                                    │
│     ├─ schema                                      │
│     ├─ policy                                      │
│     ├─ approval                                    │
│     ├─ deadline                                    │
│     ├─ execute                                     │
│     ├─ output validation                           │
│     ├─ redaction                                   │
│     └─ artifact                                    │
└──────────────────────────┬─────────────────────────┘
                           │
         ┌─────────────────┼──────────────────┐
         │                 │                  │
   native tools         extensions         agents
                      MCP/OpenAPI/plugin
```

---

# 16. Agent loop recomendado

Pseudocódigo:

```python
def run_turn(ctx, user_input):
    ledger = BudgetLedger.from_profile(ctx.profile)
    exposure = tool_exposure.for_turn(ctx, user_input)

    append_user_message(user_input)

    while True:
        check_cancel()

        context = context_manager.prepare(
            history=ctx.history,
            tools=exposure.current(),
            budget=ledger,
        )

        if context.pressure.high:
            exposure.shrink_if_tool_heavy()
            context_manager.compact_if_needed()

        response = model_gateway.invoke(
            context,
            tools=exposure.current(),
        )

        ledger.record_model(response.usage)
        persist_model_response(response)

        if response.final:
            return completion_gate.finalize(response)

        calls = normalize_tool_calls(response)

        invalid = validate_calls(calls)
        if invalid:
            append_structured_observations(invalid)
            continue

        plan = tool_scheduler.plan(calls)

        observations = tool_scheduler.execute(
            plan,
            ledger=ledger,
            cancellation=ctx.cancel,
        )

        append_structured_observations(observations)

        exposure.observe(calls, observations)
        progress.observe(calls, observations)

        if ledger.soft_exhausted():
            if not progress.meaningful():
                return checkpoint_and_stop("budget/no-progress")

        if ledger.hard_exhausted():
            return checkpoint_and_stop("budget/hard")
```

---

# 17. Cambios concretos por archivo

## `src/rinari/runtime/agent.py`

### Cambiar

- eliminar doble enforcement del tool limit;
- introducir `ToolExposure`;
- no usar `registry.for_model()` sin filtro;
- soportar provider gateway swappable;
- separar attempted/executed/rejected calls;
- structured observations;
- soft/hard budget handling;
- progress signal;
- scheduler para batches.

### Mantener

- loop detector;
- event sink;
- pressure hook;
- provider-independent history.

---

## `src/rinari/runtime/budget.py`

### Cambiar

- límites desde config/profile;
- semántica consistente `>=`;
- soft + hard;
- accounting por capability;
- hierarchical child budgets;
- estimated cost opcional;
- snapshot con `remaining`.

Agregar:

```text
requested_tools
executed_tools
rejected_tools
retry_count
child_model_calls
child_tool_calls
```

---

## `src/rinari/tools/registry.py`

### Cambiar

- `for_model()` no debe significar “todo” por default;
- usar `always_loaded`;
- aceptar `ToolExposure`;
- calcular schema bytes/tokens;
- health status;
- activation/deactivation;
- aliases/tags.

---

## `src/rinari/capability_search.py`

### Cambiar

- ranking ponderado;
- health-aware;
- mode-aware;
- activación;
- registrar reason de selección;
- devolver schema cost estimado.

---

## `src/rinari/tools/definition.py`

### Cambiar

- `ToolResult.to_model_text()` → structured envelope;
- añadir modelo `ToolObservation`;
- quizás `retry_policy`;
- resource/conflict key opcional para scheduler.

Ejemplo:

```python
resource_keys: Callable[[dict], tuple[str, ...]] | None
```

para saber si dos writes pueden correr simultáneamente.

---

## `src/rinari/tools/runtime.py`

### Cambiar

- output schema validation;
- per-tool deadline;
- per-tool output cap;
- structured accounting;
- deeper redaction;
- retry hook para idempotent tools;
- return execution metadata al BudgetLedger.

### Redaction

El helper recursivo actual contempla principalmente:

```text
str
dict
list
```

Extender a:

```text
tuple
set
dataclass
nested error.details
custom result structures
```

y asegurar redaction antes de persistir events.

---

## `src/rinari/cli/agent_runtime.py`

### Cambiar

- arreglar provider/model switch;
- construir budget desde config/mode;
- mantener un `SessionModelGateway`;
- mantener `ToolExposure` session-scoped;
- extension diagnostics;
- `/budget`, `/tools`, `/trace`;
- continuation checkpoint.

---

## `src/rinari/models/types.py`

### Cambiar

Ampliar de string-only a response items/content blocks.

Agregar:

```text
ProviderState
ResponseItem
ToolCallParseState
RequestControls
```

Sin meter tipos OpenAI/Anthropic directos en el core.

---

## `src/rinari/providers/adapters/openai_compatible.py`

### Mantener para

```text
Ollama
LM Studio
custom compatible endpoints
```

### Mejorar

- invalid tool args no → `{}`;
- streamed usage cuando exista;
- provider-specific capability probing;
- robust finish reason mapping.

---

## Nuevo: `src/rinari/providers/adapters/openai_responses.py`

Implementar OpenAI nativo moderno.

No sustituir el estado local.

---

## `src/rinari/providers/adapters/anthropic.py`

### Mejorar

- preservar content blocks requeridos por protocolo;
- preparar opaque thinking/reasoning transport state;
- no reducir todo a text + tool_use;
- invalid streamed JSON args explícito;
- richer cache/usage metadata cuando lo soporte el model.

---

## `src/rinari/mcp/client.py`

### Cambiar

`McpToolInfo` debe conservar:

```text
outputSchema
annotations
meta
protocol-sensitive fields
```

No perder información del server.

---

## `src/rinari/mcp/service.py`

### Cambiar

- preservar cualquier `structuredContent` válido según versión;
- output validation;
- protocol version;
- tool health;
- reconnect policy.

---

## `tests/e2e/`

Crear la suite de harness real.

Este cambio tiene prioridad alta.

---

## `.github/workflows/`

Crear CI + release checks.

---

# 18. Orden de implementación recomendado

No haría veinte refactors simultáneos.

## Etapa A — Correctness del loop

**Complejidad: S/M**

- unificar tool limit;
- `>=` consistente;
- provider switch;
- structured error observation;
- malformed tool args;
- tests boundary.

Resultado:

```text
el harness deja de cortarse/engañarse por wiring básico
```

---

## Etapa B — Tool exposure

**Complejidad: M**

- usar `always_loaded`;
- ToolExposure;
- schema budget;
- capability activation;
- metrics.

Resultado:

```text
muchas capabilities registradas sin destruir cada model request
```

---

## Etapa C — Tool contract enforcement

**Complejidad: M/L**

- output schema;
- deadlines;
- output caps;
- idempotency;
- scheduler;
- network accounting.

Resultado:

```text
tools confiables y recuperables
```

---

## Etapa D — Provider runtime v2

**Complejidad: L**

- ResponseItem model;
- OpenAI Responses adapter;
- richer Anthropic block preservation;
- provider error taxonomy;
- cancellation.

Resultado:

```text
portabilidad sin reducir providers modernos al mínimo común denominador
```

---

## Etapa E — E2E + CI + release gate

**Complejidad: M**

Debe ocurrir en paralelo con A-D, no al final.

Resultado:

```text
cada refactor del harness se prueba como producto
```

---

# 19. Qué NO haría ahora

## No agregaría más tool families grandes

Hasta resolver exposición dinámica.

Cada nueva family empeora el problema de schema saturation.

## No subiría simplemente el límite a 256

Sin:

- loop detection;
- progress;
- cost;
- context;
- retries correctos.

Más límite puede significar loops más caros.

## No usaría un único `capability.invoke(name, args)` para todo

Aunque simplifica el schema visible, destruye parte del valor de typed function calling.

Úsalo como fallback si quieres, no como arquitectura principal.

## No haría provider-native conversation state la fuente de verdad

Rompería:

- provider switching;
- offline eval;
- portability;
- exports;
- reproducibility.

## No convertiría toda la arquitectura a async de golpe

Primero define:

```text
deadline
cancellation
scheduler
transport boundaries
```

Luego migra únicamente las capas donde el async aporte valor real.

---

# 20. Definition of Done para llamar al harness “productivo”

Consideraría el runtime suficientemente sólido cuando pase, de forma repetible, lo siguiente.

## Long task

Una tarea scripted de coding requiere más de 32 tool executions y completa correctamente bajo un profile adecuado.

## Tool saturation

Registrar 100–200 tools no implica enviar 100–200 schemas al model.

## Provider switch

Cambiar provider/model in-session afecta la siguiente invocation real.

## Tool errors

El modelo recibe:

```text
code
message
retryable
artifact
truncation
```

no solo una string.

## Contract enforcement

`timeout_ms`, `max_output_bytes` y `output_schema` tienen efecto verificable.

## MCP

Structured output moderno se conserva y valida.

## Resume

Crash/interruption en tool sequence no corrompe el historial.

## Cancellation

Ctrl+C corta model/tool/subagent work de manera predecible.

## Budget

Soft limit guía; hard limit checkpointa; no hay doble límite oculto.

## Multi-agent

Child usage aparece en accounting global.

## Context

El runtime puede explicar cuántos tokens aproximados consumen:

```text
system
history
tools
pins
compact state
```

## Verification

El modelo no puede convertir un claim de “fixed” en DONE sin evidence.

## E2E

Existe una suite hermética que reproduce las secuencias anteriores.

## CI

Linux + Windows + macOS tienen al menos smoke/runtime coverage.

---

# 21. Acceptance checklist implementable

```text
[ ] AgentLoop usa una única fuente de verdad para tool limits
[ ] BudgetMeter boundary semantics son consistentes
[ ] provider/model switch actualiza el gateway usado por AgentLoop
[ ] ToolRegistry.for_model no expone todo por defecto
[ ] always_loaded tiene semántica runtime real
[ ] existe ToolExposure session-scoped
[ ] existe schema count/token budget
[ ] capability.search puede activar tools
[ ] ToolResult se convierte a structured observation
[ ] errors conservan code + retryable
[ ] malformed tool JSON nunca se ejecuta como {}
[ ] output_schema se valida
[ ] timeout_ms se respeta
[ ] max_output_bytes se respeta
[ ] idempotent gobierna retries
[ ] side_effects gobierna scheduling
[ ] network budget usa capability real
[ ] subagent usage se agrega al parent
[ ] OpenAI nativo usa adapter Responses
[ ] OpenAI-compatible permanece separado
[ ] Anthropic puede preservar content blocks opacos
[ ] MCP conserva outputSchema
[ ] MCP structuredContent no se limita artificialmente a dict/list
[ ] cancellation llega a transport/process/browser/MCP
[ ] extension failures aparecen en diagnostics
[ ] context accounting incluye tool schemas
[ ] tests/e2e tiene long-turn harness tests
[ ] existe CI cross-platform
[ ] wheel/sdist se instalan en entorno limpio
[ ] rinari doctor valida runtime, adapters y extensions
```

---

# 22. Config propuesta

Ejemplo, no contrato definitivo:

```toml
[runtime]
budget_profile = "auto"
parallel_read_tools = 4
tool_schema_max_count = 24
tool_schema_max_tokens = 9000
tool_activation_ttl_rounds = 4
structured_tool_observations = true

[runtime.budget.normal]
soft_model_calls = 14
soft_tool_calls = 48
hard_model_calls = 28
hard_tool_calls = 112
wall_time_s = 900

[runtime.context]
compact_pressure = 0.80
emergency_pressure = 0.90
prefer_tool_schema_reduction = true

[runtime.retry]
model_max_attempts = 3
idempotent_tool_max_attempts = 2

[runtime.providers.openai]
transport = "responses"
store_remote_state = false

[runtime.tools]
validate_outputs = true
respect_tool_deadlines = true
```

---

# 23. Métricas que usaría para decidir si los cambios funcionan

No medir únicamente “tests pass”.

## Agent effectiveness

```text
task success rate
verification DONE rate
false-DONE rate
average model rounds per successful task
average tools per successful task
loop abort rate
budget abort rate
resume success rate
```

## Tool quality

```text
tool selection accuracy
invalid argument rate
tool error rate
retry success rate
duplicate-call rate
unknown-tool rate
```

## Context

```text
schema tokens / total input
history tokens / total input
compactions per task
post-compaction success rate
```

## Providers

```text
TTFT
total latency
stream interruption rate
429 rate
retry rate
usage reporting availability
```

## Multi-agent

```text
spawn success
duplicate work
conflicts
child budget share
useful-result rate
```

---

# 24. Evals específicas para tu problema de tool calls

Crear una suite:

```text
harness-long-tool-chain
```

Casos:

```text
01_10_reads_then_answer
02_20_reads_5_edits_test
03_40_mixed_tools
04_64_boundary
05_soft_budget_continue
06_hard_budget_checkpoint
07_same_tool_loop
08_retryable_timeout
09_non_idempotent_ambiguous_failure
10_dynamic_tool_activation
11_150_registered_tools
12_provider_switch_mid_task
13_compact_during_long_task
14_resume_after_tool_31
15_parallel_read_batch
16_serial_write_conflict
17_mcp_tool_chain
18_subagent_budget_aggregation
```

Grader:

```text
final kind
completion status
expected files
validation
model calls
tool calls
loop signal
budget signal
context pressure
```

Esta suite debería convertirse en una de las gates principales del producto.

---

# 25. Fuentes externas relevantes al diseño

## OpenAI

La API moderna de Responses soporta, entre otras primitives, function tools con schema estricto, `parallel_tool_calls`, `tool_choice`, response streaming events, `previous_response_id`, prompt caching y richer response items.

Documentación:

- https://platform.openai.com/docs/api-reference/responses
- https://platform.openai.com/docs/guides/function-calling

Recomendación para Rinari:

```text
usar esas capacidades en el adapter OpenAI
sin cederle al provider la propiedad del session state
```

## Anthropic

La API de Messages usa content blocks y, cuando se combinan ciertas modalidades de thinking con tool use, el cliente debe preservar bloques requeridos por el protocolo al continuar la conversación.

Documentación:

- https://docs.anthropic.com/en/api/messages
- https://docs.anthropic.com/en/docs/build-with-claude/tool-use

## MCP

Las revisiones recientes del protocolo/SDK amplían el uso de `outputSchema` y `structuredContent`.

Referencias:

- https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- https://py.sdk.modelcontextprotocol.io/v2/advanced/low-level-server/

---

# 26. Prioridad final

Si solo fueras a hacer siete cambios, haría estos en este orden:

1. **Eliminar el límite oculto de 32 tools y unificar budgets.**
2. **Arreglar provider/model switching para que AgentLoop use realmente el caller nuevo.**
3. **Implementar ToolExposure y dejar de enviar el registry completo.**
4. **Hacer ToolResult estructurado y conservar error metadata.**
5. **Enforzar timeout/output schema/max output/idempotency.**
6. **Crear E2E long-turn + CI cross-platform.**
7. **Evolucionar ModelResponse + adapters modernos (OpenAI Responses / richer Anthropic / MCP output schema).**

Después de eso, Rinari deja de ser principalmente una colección muy completa de subsistemas y pasa a tener lo que más importa en un coding agent:

```text
un loop largo,
predecible,
recuperable,
observable,
con tools correctas,
sin saturar contexto,
y sin detenerse artificialmente a mitad del trabajo.
```

Ese es el salto de “harness implementado” a **harness productivo**.
