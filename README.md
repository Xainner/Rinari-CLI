<div align="center">

<img src="readme-img.png" alt="Rinari — tu waifu de la terminal" width="420"/>

# Rinari CLI (✿◠‿◠)

### Tu asistente personal de IA en la terminal — tsundere por defecto, competente por necesidad.

**REPL de chat · Agente de código autónomo · Multi-provider · Tools de git · Búsqueda web · MCP**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-0.12-8B5CF6?style=for-the-badge&logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-199%20passed-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ec4899?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Xainner/Rinari-CLI/pulls)

</div>

---

## ✨ ¿Qué es Rinari?

**Rinari** es una CLI con LLM que se conecta a **tu modelo favorito** — local (llama.cpp, Ollama, vLLM) o en la nube (OpenAI, Anthropic, OpenRouter, DeepSeek, Gemini, OpenCode Zen y cualquier endpoint OpenAI-compatible) — y te da:

- 💬 **Chat interactivo** con streaming, historial y personalidad
- 🤖 **Modo agente** tipo Codex/Claude CLI: das tareas y Rinari explora, edita, ejecuta tests y commitea
- 🛠️ **10+ herramientas nativas**: git (status/diff/commit), edición quirúrgica de archivos, búsqueda de código, ejecución de tests, búsqueda web
- 🔌 **Soporte MCP** (Model Context Protocol): conecta servidores externos como tools dinámicas
- 🔒 **Privacidad**: con modelos locales, tus prompts nunca salen de tu red

> *"¡No es por ti! ¡Solo... construí esto porque quería."* — Rinari, probablemente

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

El comando `rinari setup` te guía paso a paso: eliges el proveedor, te pide (o autocompleta) el endpoint, lee la API key desde la variable de entorno del proveedor si existe, conecta y lista los **modelos reales** de tu endpoint, y crea el perfil:

```bash
rinari setup --name mi-perfil
```

```
¿Qué proveedor usas?
  0 → openai — OpenAI API oficial
  1 → anthropic — Anthropic Claude (API nativa)
  2 → openrouter — OpenRouter (multi-modelo)
  3 → opencode — OpenCode Zen (openai-compatible)
  4 → deepseek — DeepSeek
  5 → gemini — Google Gemini (compat OpenAI)
  6 → local — Local (llama.cpp / Ollama / vLLM, sin key)
  7 → custom — Endpoint OpenAI-compatible propio

Elige el número del provider: 1
Endpoint [default: https://api.anthropic.com/v1]:
...
✓ Perfil 'mi-perfil' listo: anthropic → https://api.anthropic.com/v1 → claude-sonnet-4
  Pruébalo con: rinari run "hola" --profile mi-perfil
```

Cada proveedor conoce su endpoint por defecto y su variable de entorno de API key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, …). Los providers `openai`, `openrouter`, `opencode`, `deepseek`, `gemini`, `local` y `custom` hablan OpenAI-compatible; `anthropic` usa la API nativa de Anthropic (`/v1/messages`), y Rinari traduce todo internamente.

## ⚙️ Configuración manual

Los perfiles viven en `~/.rinari/config.toml` — cada uno apunta a un endpoint:

```toml
[default]
base_url = "http://localhost:8080/v1"   # endpoint local OpenAI-compatible
model = "mi-modelo-local"
temperature = 0.7

[profile.nube]
base_url = "https://api.openai.com/v1"
api_key = "${OPENAI_API_KEY}"            # expande variables de entorno
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

Comandos: `/new` (nuevo contexto), `/model <perfil>`, `/approve` (toggle aprobación), `/exit`, `/help`.

### Chat interactivo

```bash
rinari chat --profile nube
rinari chat --resume 3          # continúa la sesión 3
```

### One-shot para scripts

```bash
rinari run "traduce esto al inglés"
echo "hola" | rinari run "resume esto"
```

### Agente one-shot

```bash
rinari agent "refactoriza el módulo auth" --cwd ~/mi-proyecto
rinari agent "arregla el test que falla" -y --max-iterations 15
```

### Gestión de modelos y diagnóstico

```bash
rinari models              # modelos del endpoint + modelo activo del perfil
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

### Mantenimiento

```bash
rinari identity    # quién soy (✿◠‿◠)
rinari update      # git pull del repo
rinari sync        # uv sync (reinstala deps)
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
| `git_status` | Estado del repo: rama, limpio, cambios |
| `git_diff` | Diff de cambios (incluye untracked) |
| `git_commit` | `git add -A` + commit (requiere aprobación) |
| `run_tests` | Detecta y ejecuta pytest/npm test (usa `uv run` en proyectos uv) |
| `web_search` | Búsqueda web vía DuckDuckGo Lite (sin API key) |
| `MCP tools` | Cualquier tool expuesta por tus servidores MCP |

**Seguridad:** comandos peligrosos (`rm -rf`, `sudo`, `git push`, `curl|sh`, …) piden aprobación salvo `-y`. Los paths no escapan del `--cwd`.

## 🏗️ Arquitectura

```
src/rinari/
├── cli.py        # typer entrypoints (chat, run, agent, models, model set, setup, doctor, …)
├── config.py     # perfiles TOML + ${ENV} expansion + tabla de providers
├── client.py     # cliente multi-provider (OpenAI-compatible + Anthropic nativo, streaming SSE)
├── history.py    # conversaciones SQLite
├── render.py     # rich: markdown, syntax highlight
├── ui.py         # dashboard de bienvenida (logo, health check, estado git)
├── repl.py       # lógica del REPL (ChatSession, comandos /)
├── mcp.py        # cliente MCP (stdio) para servidores externos
└── agent/
    ├── loop.py   # loop agéntico: model → tools → observe (con bridge MCP)
    ├── tools.py  # tool registry + detección de comandos peligrosos
    └── prompt.py # system prompt de Rinari (modo profesional)
```

## 🧪 Tests

```bash
uv run pytest    # 199 tests, sin red (httpx MockTransport)
```

## 🌿 Ramas de feature

| Rama | Estado |
|---|---|
| `feature-tools` | ✅ mergeada — tools de git, edit_file, run_tests |
| `feature-mcp` | ✅ mergeada — web_search, servidores MCP dinámicos |
| `feature-ui` | ✅ mergeada — dashboard, streaming en vivo |
| `feature-agent` | 🌿 activa — planning explícito, retry, persistencia |
| `feature-history` | 🌿 activa — sesiones, export |
| `feature-config` | 🌿 activa — setup wizard multi-provider, `rinari doctor`, gestión de modelos |

Ver [BRANCHING.md](BRANCHING.md) para la estrategia completa.

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).

---

<div align="center">

**Hecho con 💜 y mucho "¡No es por ti! ¡Solo...!"**

</div>
