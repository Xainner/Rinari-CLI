<div align="center">

<img src="readme-img.png" alt="Rinari — tu maid de la terminal" width="420"/>

# Rinari CLI

### Tu asistente personal de IA en la terminal — atenta, cariñosa y demoledoramente eficiente.

**REPL de chat · Agente de código autónomo · Multi-provider · Control de git completo · MCP · Búsqueda web**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-0.12-8B5CF6?style=for-the-badge&logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-329%20passed-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ec4899?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Xainner/Rinari-CLI/pulls)

</div>

---

## ✨ ¿Qué es Rinari?

**Rinari** es una CLI con LLM que se conecta a **tu modelo favorito** — local (llama.cpp, Ollama, vLLM) o en la nube (OpenAI, Anthropic, OpenRouter, DeepSeek, Gemini, OpenCode Zen y cualquier endpoint OpenAI-compatible) — y te da:

- 💬 **Chat interactivo** con streaming en vivo, sesiones reanudables (estilo Hermes) y personalidad
- 🤖 **Modo agente** tipo Codex/Claude CLI: das tareas y Rinari explora, edita, ejecuta tests, commitea, pushea y abre PRs
- 🧠 **Memoria por repositorio** (`RINARI.md`): el agente respeta las convenciones de cada proyecto
- 🔁 **Presupuesto con continuación**: al agotarse las iteraciones, pregunta *¿continuar?* y sigue con el contexto intacto
- 🛠️ **22 herramientas nativas**: control de git completo (status, diff, log, branch, stash, checkout, pull, push), edición quirúrgica, búsqueda de código, tests, búsqueda web, PRs de GitHub
- 👥 **Subagentes** (`delegate_task`): delega subtareas a un subagente de un nivel
- 🪝 **Hooks pre/post tool**: scripts que corren antes y después de cada herramienta
- 🔌 **Soporte MCP** (Model Context Protocol): conecta servidores externos como tools dinámicas
- 🔒 **Privacidad**: con modelos locales, tus prompts nunca salen de tu red

> *"Tu código no se va a arreglar solo… bueno, técnicamente ahora sí, pero con cariño."* — Rinari, tu maid de la terminal

## 🚀 Instalación

```bash
# Requisitos: Python 3.11+ y uv
uv tool install git+https://github.com/Xainner/Rinari-CLI.git

# O desde el repo
git clone https://github.com/Xainner/Rinari-CLI.git
cd Rinari-CLI
uv sync
uv tool install .
```

## ⚙️ Primeros pasos (setup wizard)

`rinari setup` te guía paso a paso **empezando por tu nombre** — así Rinari te llama por tu nombre en todas partes (el nombre se guarda en `~/.rinari/config.toml`):

```bash
rinari setup --name mi-perfil
```

```
¿Cómo te llamas?: Xainner
Guardado: te llamarás Xainner para Rinari.

Nombre del perfil [default: mi-perfil]:
¿Qué proveedor usas?
  0 → openai — OpenAI API oficial
  1 → anthropic — Anthropic Claude (API nativa)
  2 → openrouter — OpenRouter (multi-modelo)
  3 → opencode — OpenCode Zen (openai-compatible)
  4 → opencode-go — OpenCode Zen Go (openai-compatible)
  5 → deepseek — DeepSeek
  6 → gemini — Google Gemini (compat OpenAI)
  7 → local — Local (llama.cpp / Ollama / vLLM, sin key)
  8 → custom — Endpoint OpenAI-compatible propio

Elige el número del provider: 1
Endpoint [default: https://api.anthropic.com/v1]:
✓ Perfil 'mi-perfil' listo: anthropic → https://api.anthropic.com/v1 → claude-sonnet-4
  Pruébalo con: rinari run "hola" --profile mi-perfil
```

Cada proveedor conoce su endpoint por defecto y su variable de entorno de API key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, …). Los providers `openai`, `openrouter`, `opencode`, `opencode-go`, `deepseek`, `gemini`, `local` y `custom` hablan OpenAI-compatible; `anthropic` usa la API nativa de Anthropic (`/v1/messages`), y Rinari traduce todo internamente.

## ⚙️ Configuración manual

Los perfiles viven en `~/.rinari/config.toml` — cada uno apunta a un endpoint:

```toml
[user]
name = "Xainner"                          # Rinari te llama por tu nombre

[default]
base_url = "http://localhost:8080/v1"     # endpoint local OpenAI-compatible
model = "mi-modelo-local"
temperature = 0.7

[profile.nube]
base_url = "https://api.openai.com/v1"
api_key = "${OPENAI_API_KEY}"             # expande variables de entorno
model = "gpt-4o"
provider = "openai"

[profile.claude]
base_url = "https://api.anthropic.com/v1"
api_key = "${ANTHROPIC_API_KEY}"
model = "claude-sonnet-4"
provider = "anthropic"
```

Los perfiles heredan de `[default]`. La `api_key` admite `${ENV_VAR}`. Sin `provider` explícito, se asume OpenAI-compatible.

### Servidores MCP

```toml
[mcp.servers.mi-servidor]
command = "python"
args = ["/path/al/server.py"]
```

## 🎮 Uso

### Chat interactivo — sesiones estilo Hermes

```bash
rinari chat                    # si hay sesiones previas, te deja elegir
rinari chat --new              # fuerza sesión nueva
rinari chat --resume 3         # continúa la sesión 3 directo
```

```
Sesiones recientes:
  1 · 2026-08-07 23:46:33 · 4 msgs — di hola
  [Enter] = sesión nueva

¿Continuar sesión? (número o Enter):
```

Streaming **token a token en vivo**, historial completo, comandos `/new`, `/model`, `/exit`, `/help`.

### Modo agente interactivo (como codex/claude)

```bash
rinari                          # entra directo al modo agente
rinari --cwd ~/mi-proyecto      # en un repo específico
rinari -p nube                  # con otro perfil
```

```
rinari@mi-proyecto > arregla el test que falla
rinari@mi-proyecto > refactoriza el módulo auth
rinari@mi-proyecto > /exit
```

Comandos: `/new` (nuevo contexto), `/model <perfil>`, `/approve` (toggle aprobación), `/compact` (resume la conversación), `/todos` (gestión de tareas), `/cost` (uso de tokens de la sesión), `/undo` (deshace la última edición del agente), `/exit`, `/help`. Cualquier `/comando` desconocido busca un skill en `~/.rinari/commands/<nombre>.md` y lo envía como prompt.
El agente **verifica automáticamente** los tests tras cada edición y **persiste cada sesión** en el historial.

**Presupuesto de iteraciones:** por defecto 25 por tanda (configurable con `max_iterations = 30` en el perfil o `--max-iterations`). Al agotarse, Rinari te avisa y pregunta *"¿Continuar con N iteraciones más?"* — el contexto se conserva entre tandas, así que las tareas largas no se cortan a mitad.

### Memoria por repositorio (`RINARI.md`)

Crea un `RINARI.md` en la raíz de tu proyecto (o `.rinari.md` para no versionarlo):

```markdown
# RINARI

- Usa `uv run pytest` para los tests
- Nunca edites soul.md directamente
- Convenciones del proyecto…
```

El agente lo encuentra en el cwd o en los ancestros y lo inyecta en su prompt como reglas del repo — sin que tengas que repetirle las convenciones en cada sesión.

### Agente one-shot

```bash
rinari agent "refactoriza el módulo auth" --cwd ~/mi-proyecto
rinari agent "arregla el test que falla" -y --max-iterations 15
rinari agent "crea el endpoint de login" --plan     # pide aprobación del plan antes de tocar nada
rinari agent "task" --no-verify                     # sin tests automáticos
```

### One-shot para scripts

```bash
rinari run "traduce esto al inglés"
echo "hola" | rinari run "resume esto"
```

### Gestión de modelos y diagnóstico

```bash
rinari models              # modelos del endpoint + modelo activo del perfil
rinari model               # selector interactivo (estilo hermes model)
rinari model set gpt-4o    # cambia el modelo del perfil activo
rinari doctor              # diagnostica TODOS los perfiles (env rotas, endpoints caídos, modelos)
```

```
$ rinari doctor
  ✓ casa: 3 modelo(s), activo: mi-modelo-local
  ✓ nube: ⚠ 1 modelo(s) listado: 'otro-alias' — el activo 'gpt-4o' es un alias (funciona igual)
  ✗ sat: env rota: Variable de entorno MI_KEY no está definida (usada en config.toml)
  ✗ roto: endpoint caído: Error HTTP 500: internal error

✗ Hay perfiles con problemas. Revisa arriba o usa `rinari setup` para corregir.
```

### Historial de conversaciones

```bash
rinari history                     # lista sesiones con preview
rinari history show 3              # conversación completa en markdown
rinari history rm 3                # borra (pide confirmación)
rinari history export 3            # guarda conversacion-3.md
rinari history export 3 -o notas.md
```

### Mantenimiento

```bash
rinari identity    # quién soy
rinari version     # versión instalada
rinari update      # git pull + uv sync del repo
rinari sync        # reinstala paquete y dependencias
```

## 🛠️ Herramientas del agente

| Herramienta | Descripción |
|---|---|
| `run_command` | Ejecuta comandos (bash en Windows, sh en Unix) con timeout |
| `read_file` | Lee archivos con límite de líneas |
| `write_file` | Crea/sobrescribe archivos |
| `edit_file` | Edición quirúrgica old→new con detección de ambigüedad |
| `search_files` | Regex en el proyecto (ignora .git/node_modules/.venv) |
| `list_dir` | Lista directorios |
| `git_status` | Rama, ahead/behind, staged/unstaged/untracked separados con conteos |
| `git_diff` | Diff por secciones (staged/unstaged/untracked), sin mutar el index, filtro por archivo |
| `git_log` | Historial reciente: hash, mensaje, autor, fecha |
| `git_branch` | Ramas locales con la actual marcada |
| `git_stash` | push (incluye untracked) / list / pop |
| `git_checkout` | Cambia de rama o la crea con `-b` |
| `git_pull` / `git_push` | Sincroniza con el remoto (requieren aprobación) |
| `git_commit` | `git add -A` + commit (requiere aprobación) |
| `run_tests` | Detecta y ejecuta pytest/npm test (usa `uv run` en proyectos uv) |
| `web_search` | Búsqueda web vía DuckDuckGo Lite (sin API key) |
| `delegate_task` | Delega una subtarea a un subagente (un nivel, sin recursión) |
| `github_create_pr` | Crea un PR desde la rama actual (requiere `GITHUB_TOKEN` y aprobación) |
| `github_list_prs` | Lista los PRs del repo (estado open/closed/all) |
| `MCP tools` | Cualquier tool expuesta por tus servidores MCP |

**Seguridad:** comandos peligrosos (`rm -rf`, `sudo`, `git push`/`pull`, `git checkout`, `stash push/pop`, `curl|sh`, `github_create_pr`, …) piden aprobación salvo `-y`. Los paths no escapan del `--cwd`.

**Hooks pre/post tool:** en `~/.rinari/hooks.toml` defines scripts que corren antes/después de cada herramienta — por tool específica o con comodín `*`:

```toml
[pre_tool]
edit_file = "python scripts/lint.py"   # solo antes de editar
"*" = "echo trabajando..."              # antes de TODAS las tools

[post_tool]
"*" = "python scripts/format.py"        # después de todas
```

Un hook que falla nunca rompe el loop (se registra como paso y el agente sigue). Los subagentes heredan los hooks del padre.

**Loop agéntico avanzado:** reintenta errores transitorios de la LLM, verifica tests tras cada edición, soporta plan + aprobación previa, inyecta *reminders* al modelo cuando el presupuesto está por agotarse (estilo Codex), persiste las sesiones, y sanea el contenido de los tool results para evitar que el modelo imite formatos raros.

## 🏗️ Arquitectura

```
src/rinari/
├── cli.py        # typer entrypoints (chat, run, agent, models, model, setup, doctor, history, …)
├── config.py     # perfiles TOML + ${ENV} expansion + tabla de providers
├── client.py     # cliente multi-provider (OpenAI-compatible + Anthropic nativo, streaming SSE)
├── history.py    # conversaciones SQLite (sesiones, export)
├── render.py     # rich: markdown, streaming en vivo, syntax highlight
├── ui.py         # dashboard de bienvenida (logo, health check, estado git)
├── repl.py       # lógica del REPL (ChatSession, comandos /)
├── identity.py   # personalidad: SOUL.md canónico + prompts de chat/agente
├── assets/soul.md# la voz de Rinari (editable, se aplica sin reinstalar)
├── mcp.py        # cliente MCP (stdio) para servidores externos
└── agent/
    ├── loop.py   # loop agéntico: model → tools → observe (retry, verify, plan, persist, budget+continue)
    ├── tools.py  # tool registry + detección de comandos peligrosos + shell seguro por plataforma
    └── prompt.py # compone el prompt del agente desde identity + memoria RINARI.md
```

## 🧪 Tests

```bash
uv run pytest    # 329 tests, sin red (httpx MockTransport)
```

## 🌿 Ramas de feature

| Rama | Estado |
|---|---|
| `feature-tools` | ✅ mergeada — tools de git, edit_file, run_tests |
| `feature-mcp` | ✅ mergeada — web_search, servidores MCP dinámicos |
| `feature-ui` | ✅ mergeada — dashboard, streaming en vivo |
| `feature-config` | ✅ mergeada — setup wizard multi-provider, `rinari doctor`, gestión de modelos |
| `feature-personality` | ✅ mergeada — Rinari maid moderna, SOUL.md, setup pregunta el nombre |
| `feature-history` | ✅ mergeada — `rinari history`, selector de sesiones en chat |
| `feature-git` | ✅ mergeada — control de git completo del agente |
| `feature-agent` | ✅ mergeada — loop avanzado: retry, verify, plan, persist |

Ver [BRANCHING.md](BRANCHING.md) para la estrategia completa.

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).

---

<div align="center">

**Hecho con 💜 y muchas tazas de té servidas por una maid muy eficiente.**

</div>
