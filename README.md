# Rinari CLI

![Rinari — banner](docs/img/readme-img.png)

**Tu asistente personal de IA en la terminal.**

[![CI](https://github.com/Xainner/Rinari-CLI/actions/workflows/ci.yml/badge.svg)](https://github.com/Xainner/Rinari-CLI/actions)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Rinari es un harness de agentes productivo: un CLI que conversa con modelos
de lenguaje, ejecuta herramientas reales en tu máquina (archivos, shell, git,
web, navegador), recuerda contexto entre sesiones y trabaja con seguridad
por defecto — sandbox, permisos y aprobaciones antes de cualquier acción
sensible.

[Instalación](#instalación) · [Uso](#uso) · [Lo que puede hacer](#lo-que-puede-hacer) ·
[Comandos](#comandos) · [Documentación](#documentación) · [Desarrollo](#desarrollo)

---

## Instalación

Requisitos: Python 3.11+ y [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Xainner/Rinari-CLI
cd Rinari-CLI
uv sync
uv run rinari setup
uv run rinari doctor
```

`setup` te guía (proveedor, modelo, capacidades); re-ejecutarlo nunca borra
lo ya configurado. `doctor` verifica el entorno sin tocar credenciales
ni estado.

## Uso

```bash
# Conversar en el proyecto actual (detecta repo → sesión PROJECT,
# fuera de repo → sesión CHAT)
uv run rinari

# Chat explícito, aunque estés dentro de un repositorio
uv run rinari chat

# Preguntar sin modificar nada (solo lectura)
uv run rinari ask "¿dónde se valida la config?"

# Plan de implementación sin tocar archivos
uv run rinari plan "agregar export en JSON"

# Ejecutar una tarea autónoma en el workspace
uv run rinari agent "agrega tests al módulo de sesiones"

# Revisar cambios recientes sin modificarlos
uv run rinari review

# Abrir el proyecto en Rinari Code (cliente de escritorio)
uv run rinari code .
```

Conecta tu proveedor (ejemplo con endpoint compatible OpenAI):

```bash
uv run rinari providers add custom --name mi-proveedor \
  --endpoint https://tu-endpoint/v1 --api-key-env MI_API_KEY
uv run rinari models add --provider mi-proveedor \
  --model <model-id> --name mi-modelo
uv run rinari provider use mi-proveedor
uv run rinari model use mi-modelo
```

Algunos modelos viven en transporte `/responses` en vez de
`/chat/completions`:

```bash
uv run rinari models add --provider mi-proveedor \
  --model <model-id> --name mi-modelo --transport responses
```

## Lo que puede hacer

### Asistente que ejecuta, no solo responde

- Loop agente real con streaming: el modelo razona, llama herramientas,
  observa resultados estructurados y continúa hasta terminar.
- Modos de sesión **PLAN / BUILD / REVIEW**: PLAN y REVIEW trabajan en
  solo-lectura; BUILD tiene workspace. El modo nunca relaja la política
  de seguridad.
- Cola de prompts por sesión: encola instrucciones que corren
  automáticamente al terminar el turno en vivo, con los mismos límites
  y aprobaciones.
- Presupuestos por turno (llamadas al modelo, tool calls, llamadas de red,
  tiempo, subagentes) y detección de loops que frena repeticiones antes
  de quemar contexto.
- Reanudación durable: `rinari resume` reconcilia proyecto, rama, working
  tree, permisos y provider/modelo antes de continuar.

### Proveedores y modelos

- Registros persistentes multi-proveedor y multi-modelo: cambiar de
  selección **nunca elimina** configuraciones previas; cada proveedor
  recuerda su modelo.
- Credenciales fuera del código: referencias `env://`, llavero del SO
  (`keyring://`) o archivo (`file://`); las vistas siempre redactadas,
  los valores nunca aparecen en logs, trazas ni exports.
- Transporte por modelo (`chat` / `responses`) con detección automática
  para endpoints OpenCode Go.

### Sesiones y proyectos

- Sesiones **CHAT** y **PROJECT** persistentes con conversación guardada,
  pines de contexto y artefactos por sesión.
- `rinari chat` fuerza CHAT; `rinari` solo detecta proyecto (AUTO);
  promoción CHAT → PROJECT sin reiniciar; `$HOME` jamás es workspace
  implícito.
- Confianza por proyecto (`trust`), índice del repositorio con
  invalidación incremental (símbolos, referencias, mapa de tests),
  instrucciones `RINARI.md` por niveles y protección de working tree
  sucio (tus cambios sin commitear piden aprobación explícita).

### Herramientas (tools)

Catálogo amplio bajo un único Tool Runtime — toda tool pasa por schema,
política, aprobaciones, sandbox, secretos, redacción, traza y budgets:

- **Archivos y shell**: leer/escribir/editar con sandbox por raíces,
  procesos por sesión y PTY real para comandos interactivos.
- **Código**: búsqueda (archivos, regex, símbolos, referencias, híbrida),
  AST con tree-sitter, LSP (definición, referencias, diagnósticos…),
  estado git y `review` de diffs.
- **Web y automatización**: HTTP tipado con reintentos, fetching y
  extracción web, y navegador propio sobre CDP (capturas, snapshot de
  accesibilidad, clicks, formularios; uploads/downloads con provenance).
- **Memoria y contexto**: 4 stores (usuario, proyecto, episódica, patrones),
  recuperación rankeada con pins, compactación que preserva la verdad de
  la tarea y Artifact Store (`artifact://…`) para salidas grandes.
- **Tareas y verificación**: grafo de tareas con dependencias, planes de
  verificación y gate de completitud (`DONE` exige evidencia; el
  falso-éxito se rechaza).
- **Carga dinámica**: el modelo no recibe cientos de tools por request —
  ve núcleo + activadas + recientes, y descubre el resto con
  `capability.search` → `capability.activate` (`turn` con TTL o `session`).
- **Extensiones**: MCP (stdio), OpenAPI (specs JSON → tools tipadas),
  plugins con manifiesto y permisos explícitos, y hooks de ciclo de vida.

### Skills y multi-agente

- 10 skills incluidos: `repository-explore`, `implement-feature`,
  `fix-bug`, `debug`, `test`, `code-review`, `refactor`, `fix-ci`,
  `research`, `final-verification` — catálogo de una línea en el prompt,
  cuerpo cargado bajo demanda.
- 6 agentes built-in (`explore`, `reviewer`, `debugger`, `researcher`,
  `implementer`, `verifier`) con perfiles read-only/workspace, routing de
  modelo por agente, límites, cancelación propagada y worktrees aislados
  para escritores paralelos.

### Identidad (Soul)

- Sistema de identidad Soul con store versionado, activación global y
  soul empaquetado por defecto; el prompt siempre inyecta el Canonical
  Soul y la referencia extendida solo en turnos de identidad.

### Escritorio (Rinari Code)

- `rinari engine --stdio`: protocolo máquina NDJSON sobre stdio para
  clientes de escritorio (sesiones, modos, cola, tareas, verificación,
  artefactos, métricas, MCP/plugins, credenciales redactadas).
- `rinari code [path] [--session]`: abre el proyecto en el cliente de
  escritorio, con handoff de sesión.
- Bundles de perfil: aplican soul + modo + modelos por agente de una vez,
  con reporte de lo aplicado.

### Seguridad

- Sandbox de filesystem y red (allow/deny por host, fail-closed),
  perfiles `read-only`/`workspace`, aprobaciones con grants persistentes,
  redacción de secretos en cada frontera y contenido remoto tratado
  como dato desconfiado.

### Observabilidad

- Traza de eventos por sesión (`trace`, `logs`), métricas desde eventos
  (`metrics`), `status` operativo rápido y `doctor` local. Los números
  que un proveedor no expone se muestran como desconocidos — nunca se
  inventan tokens, costos ni uso.

## Comandos

Grupos principales (`rinari --help` para el detalle; casi todo soporta
`--json` para automatización):

| Área | Comandos |
|---|---|
| Conversar | `chat`, `ask`, `plan`, `agent`, `review`, `run`, `stop`, `verify` |
| Sesiones | `session`, `resume`, `checkpoint`, `undo`, `project`, `init` |
| Modelos | `providers`, `provider`, `models`, `model`, `setup` |
| Conocimiento | `index`, `tasks`, `memory`, `context`, `artifacts` |
| Capacidades | `tools`, `skills`, `agents`, `plugins`, `mcp`, `api`, `hooks`, `profiles` |
| Seguridad | `trust`, `permissions`, `approvals`, `sandbox`, `secrets`, `network` |
| Escritorio | `engine`, `code` |
| Sistema | `status`, `doctor`, `version`, `update`, `metrics`, `logs`, `trace`, `cache`, `config`, `completion`, `help` |

El contrato exacto de cada comando (semántica, persistencia y códigos de
salida) está en [docs/commands.md](docs/commands.md).

## Documentación

- [TODO.md](TODO.md) — roadmap, fases y registro de decisiones
- [AGENTS.md](AGENTS.md) — reglas de trabajo para agentes (y humanos)
- [docs/soul.md](docs/soul.md) — identidad de Rinari
- [docs/stack.md](docs/stack.md) — principios arquitectónicos
- [docs/commands.md](docs/commands.md) — contrato público del CLI
- [docs/tools.md](docs/tools.md) — catálogo y contrato de tools
- [docs/skills.md](docs/skills.md) — catálogo y contrato de skills
- [docs/harness.md](docs/harness.md) — blueprint completo del runtime
- [docs/troubleshooting.md](docs/troubleshooting.md) — resolución de problemas
- [RINARI_HARNESS_PRODUCTIZATION_REVIEW.md](RINARI_HARNESS_PRODUCTIZATION_REVIEW.md) —
  review de productización (referencia histórica)

## Desarrollo

```bash
uv sync
uv run pytest          # suite completa (network-isolated por defecto)
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
uv build               # sdist + wheel
```

Ramas por tarea y sin merge sin aprobación. Nunca se commitean secretos:
la config referencia credenciales (`env://`, llavero, secret manager),
jamás las contiene. Ver [AGENTS.md](AGENTS.md) para las reglas del repo.

## Estado

Desarrollo activo en `main` con CI (Linux 3.11/3.12 + Windows: lint,
tests, build, smoke en venv limpio y release gate en tags). Roadmap y
pendientes honestos en [TODO.md](TODO.md).

## Licencia

MIT — ver [LICENSE](LICENSE).
