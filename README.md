<div align="center">

<img src="readme-img.png" alt="Rinari — tu waifu de la terminal" width="420"/>

# Rinari CLI (✿◠‿◠)

### Tu asistente personal de IA en la terminal — tsundere por defecto, competente por necesidad.

**REPL de chat · Agente de código autónomo · Tools de git · Búsqueda web · MCP · 100% local**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-0.12-8B5CF6?style=for-the-badge&logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-133%20passed-22c55e?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ec4899?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Xainner/Rinari-CLI/pulls)

</div>

---

## ✨ ¿Qué es Rinari?

**Rinari** es una CLI con LLM que se conecta a **tus propios modelos locales** (vLLM, LiteLLM, llama.cpp — cualquier endpoint OpenAI-compatible) y te da:

- 💬 **Chat interactivo** con streaming, historial y personalidad
- 🤖 **Modo agente** tipo Codex/Claude CLI: das tareas y Rinari explora, edita, ejecuta tests y commitea
- 🛠️ **10+ herramientas nativas**: git (status/diff/commit), edición quirúrgica de archivos, búsqueda de código, ejecución de tests, búsqueda web
- 🔌 **Soporte MCP** (Model Context Protocol): conecta servidores externos como tools dinámicas
- 🔒 **Privacidad**: tus prompts nunca salen de tu red

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

## ⚙️ Configuración

Los perfiles viven en `~/.rinari/config.toml` — cada uno apunta a un endpoint OpenAI-compatible:

```toml
[default]
base_url = "http://192.168.0.3:8020/v1"   # llama.cpp local
model = "qwen3.6-27b"
temperature = 0.7

[profile.casa]
base_url = "http://192.168.0.3:8020/v1"

[profile.sat]
base_url = "https://api.xainner.com/v1"    # vLLM FP8
api_key = "${SAT_KEY}"                      # expande variables de entorno
model = "qwen3.6-27b"
temperature = 0.3
```

Los perfiles heredan de `[default]`. La `api_key` admite `${ENV_VAR}`.

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
rinari -p sat                   # con otro perfil
```

```
rinari@mi-proyecto > arregla el test que falla
rinari@mi-proyecto > refactoriza el módulo auth
rinari@mi-proyecto > /exit
```

Comandos: `/new` (nuevo contexto), `/model <perfil>`, `/approve` (toggle aprobación), `/exit`, `/help`.

### Chat interactivo

```bash
rinari chat --profile casa
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

### Mantenimiento

```bash
rinari identity    # quién soy (✿◠‿◠)
rinari update      # git pull del repo
rinari sync        # uv sync (reinstala deps)
rinari models      # modelos del endpoint
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
├── cli.py        # typer entrypoints (chat, run, agent, models, identity, update, sync)
├── config.py     # perfiles TOML + ${ENV} expansion
├── client.py     # cliente OpenAI-compatible (streaming SSE, tools)
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
uv run pytest    # 133 tests, sin red (httpx MockTransport)
```

## 🌿 Ramas de feature

| Rama | Estado |
|---|---|
| `feature-tools` | ✅ mergeada — tools de git, edit_file, run_tests |
| `feature-mcp` | ✅ mergeada — web_search, servidores MCP dinámicos |
| `feature-ui` | 🌿 activa — dashboard, streaming en vivo |
| `feature-agent` | 🌿 planning explícito, retry, persistencia |
| `feature-history` | 🌿 sesiones, export |
| `feature-config` | 🌿 setup wizard, `rinari doctor` |

Ver [BRANCHING.md](BRANCHING.md) para la estrategia completa.

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).

---

<div align="center">

**Hecho con 💜 y mucho "¡No es por ti! ¡Solo...!"**

</div>
