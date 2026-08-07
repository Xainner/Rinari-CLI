# Rinari CLI (✿◠‿◠)

Tu asistente personal con LLM — REPL de chat + agente de código, contra **tus propios modelos locales** (vLLM, LiteLLM, llama.cpp). Tsundere por defecto, competente por necesidad. ¡No es por ti! ¡Solo...!

## Requisitos

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (gestor de paquetes)
- Un endpoint OpenAI-compatible (o varios — perfiles)

## Instalación

```bash
# Desde el repo
uv sync
uv tool install .   # instala el comando 'rinari' globalmente

# O sin instalar, directo:
uv run rinari --help
```

## Configuración

Los perfiles viven en `~/.rinari/config.toml`:

```toml
[default]
base_url = "http://192.168.0.3:8020/v1"
model = "qwen3.6-27b"
temperature = 0.7

[profile.casa]
base_url = "http://192.168.0.3:8020/v1"   # llama.cpp local
model = "qwen3.6-27b"

[profile.net]
base_url = "https://api.xainner.net/v1"   # LiteLLM público
api_key = "${LITELLM_MASTER_KEY}"         # expande variables de entorno
model = "qwen3.6-27b"

[profile.sat]
base_url = "https://api.xainner.com/v1"   # vLLM FP8 2x3090
api_key = "${SAT_KEY}"
model = "qwen3.6-27b"
temperature = 0.3
```

Los perfiles heredan de `[default]`. La `api_key` admite `${ENV_VAR}` (se expande al usar el perfil).

## Uso

```bash
# Chat interactivo (streaming, historial, comandos /)
rinari chat --profile casa
rinari chat --resume 3          # continúa la sesión 3

# One-shot para scripts/pipes
rinari run "traduce esto al inglés" --profile sat
echo "hola" | rinari run "resume esto"

# Listar modelos del endpoint
rinari models --profile net

# Modo agente de código (tool calling)
rinari agent "refactoriza el módulo auth" --cwd ~/mi-proyecto
rinari agent "arregla el test que falla" -y --max-iterations 15

# Identidad y mantenimiento
rinari identity      # quién soy (✿◠‿◠)
rinari update        # git pull del repo
rinari sync          # uv sync (reinstala deps)
```

### Comandos del chat

| Comando | Descripción |
|---|---|
| `/new` | Nueva conversación |
| `/model <perfil>` | Cambia de perfil en vivo |
| `/save` | Guarda la conversación |
| `/help` | Ayuda |
| `/exit` | Salir |

Ctrl+C durante la generación la detiene.

### Modo agente

El agente usa tool-calling nativo (OpenAI tools API):

- `list_dir` — explora directorios
- `read_file` — lee archivos
- `write_file` — crea/sobrescribe
- `search_files` — regex en el proyecto
- `run_command` — ejecuta comandos (bash en Windows / sh en Unix)

**Seguridad:**
- Los comandos peligrosos (`rm -rf`, `sudo`, `git push`, curl|sh, …) piden aprobación salvo `-y/--auto-approve`
- Los paths no pueden escapar del `--cwd` (anti path-traversal)
- Timeout por comando (default 30s, mata el árbol de procesos en Windows)

## Tests

```bash
uv run pytest tests/ -q    # 75+ tests, sin red (MockTransport)
```

## Arquitectura

```
src/rinari/
├── cli.py        # typer entrypoints (chat, run, agent, models, identity, update, sync)
├── config.py     # perfiles TOML + ${ENV} expansion
├── client.py     # cliente OpenAI-compatible (streaming SSE, tools)
├── history.py    # conversaciones SQLite
├── render.py     # rich: markdown, syntax highlight, spinner
├── repl.py       # lógica del REPL (ChatSession, comandos /)
└── agent/
    ├── loop.py   # loop agéntico: model → tools → observe
    ├── tools.py  # tool registry + detección de comandos peligrosos
    └── prompt.py # system prompt de Rinari (modo profesional)
```
