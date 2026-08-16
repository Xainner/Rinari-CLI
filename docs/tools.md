# Tools del agente — definición

> **Estado: borrador de definición (Fase 0).** Lista de candidatos tomada de
> v1, pendiente de validar con Xainner. Nada de aquí es decisión todavía.

## Qué es una tool

Función que el modelo puede invocar dentro del loop agéntico: nombre,
descripción y JSON Schema de parámetros (formato function-calling estándar,
OpenAI-compatible). El resultado (texto) vuelve al modelo como mensaje tool.

## Catálogo candidato (de v1)

### Archivos

| Tool | Qué hace | Notas en v1 |
|---|---|---|
| `read_file` | Lee un archivo (límite de líneas) | Bloqueo de secrets |
| `write_file` | Crea/sobrescribe un archivo | Bloqueo de secrets |
| `edit_file` | Edición old→new quirúrgica, detecta ambigüedad | |
| `apply_patch` | Diff unificado multi-hunk (estilo Codex) | ¿Redundante con `edit_file`? |
| `search_files` | Búsqueda regex en el proyecto | Ignora `.git`, `node_modules`, `.venv` |
| `list_dir` | Lista un directorio | |

### Ejecución

| Tool | Qué hace | Notas en v1 |
|---|---|---|
| `run_command` | Comandos shell con timeout | Detección de peligrosos (`rm -rf`, `sudo`, `curl\|sh`) |
| `run_tests` | Detecta y ejecuta pytest / npm test | Usa `uv run` en proyectos uv |

### Git

`git_status` · `git_diff` · `git_log` · `git_branch` · `git_stash` ·
`git_checkout` · `git_pull` · `git_push` · `git_commit`

En v1: `push`/`pull`/`checkout`/`stash`/`commit` piden aprobación.

### Red / GitHub

| Tool | Qué hace | Notas en v1 |
|---|---|---|
| `web_search` | Búsqueda web vía DuckDuckGo Lite | Sin API key |
| `github_create_pr` | Crea PR desde la rama actual | `GITHUB_TOKEN` + aprobación |
| `github_list_prs` | Lista PRs del repo | open/closed/all |

### Delegación / extensibilidad

| Tool | Qué hace | Notas en v1 |
|---|---|---|
| `delegate_task` | Subagente de un nivel (sin recursión) | Hereda aprobación y hooks |
| MCP tools | Servidores MCP externos como tools dinámicas | stdio |

## Decisiones pendientes (Fase 0)

- [ ] ¿Las tools entran en v0.1? (si el MVP no incluye modo agente, todo
      esto se pospone a la fase correspondiente)
- [ ] Conjunto mínimo viable: ¿cuáles 4–6 primeras tools? (candidato:
      `read_file`, `write_file`, `edit_file`, `run_command`, `search_files`,
      `run_tests`)
- [ ] `apply_patch`: sí / no / después (¿es necesaria si ya hay `edit_file`?)
- [ ] Git: las 9 de golpe o mínimo al inicio (`status`, `diff`, `commit`)?
- [ ] Modelo de seguridad: aprobación de peligrosos, bloqueo de secrets
      (`.env`, keys, `.ssh/`), sandbox por niveles
      (read-only / workspace-write / full access)
- [ ] Presupuesto de iteraciones con pregunta "¿continuar?"
- [ ] Tool calls en paralelo
- [ ] Hooks pre/post tool
- [ ] `delegate_task` / subagentes
- [ ] MCP