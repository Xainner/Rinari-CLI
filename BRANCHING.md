# Estrategia de Ramas — Rinari CLI

Git flow ligero, repo **local** por ahora (sin remotes). `main` es la rama
estable: todo lo que llega a `main` debe pasar tests (`uv run pytest`) y ser
verificable.

## Ramas de feature

| Rama | Propósito | Estado |
|---|---|---|
| `feature-tools` | Ampliar el tool registry del agente | ✅ **MERGEADA a main** (git status/diff/commit, edit_file, run_tests con detección uv) |
| `feature-ui` | Interfaz y rendering | ✅ **MERGEADA a main** (logo ASCII de Rinari, dashboard con latencia/tools, streaming en vivo del agente) |
| `feature-agent` | Loop agéntico avanzado | ✅ **MERGEADA a main** (retry ante errores transitorios, verificación automática de tests, plan + aprobación, persistencia de sesiones) |
| `feature-cli-polish` | Pulido de CLI | ✅ **MERGEADA a main** (`/compact` resumir contexto, `/todos` lista de tareas, `--output json` para run/agent) |
| `feature-history` | Historial y sesiones | ✅ **MERGEADA a main** (`rinari history` listar/ver/borrar/exportar sesiones) |
| `feature-git` | Control de git | ✅ **MERGEADA a main** (status rico, diff sin hacks, log, branch, stash, checkout, pull/push) |
| `feature-config` | Configuración | ✅ **MERGEADA a main** (wizard `rinari setup` multi-provider, `rinari doctor`, gestión de modelos, README genérico) |
| `feature-personality` | Personalidad | ✅ **MERGEADA a main** (Rinari reconstruida: maid moderna con humor seco, SOUL.md canónico, setup pregunta el nombre, emojis sí / kaomoji no) |
| `feature-mcp` | Integración MCP + búsqueda web | ✅ **MERGEADA a main** (web_search DuckDuckGo, servidores MCP como tools dinámicas) |

## Flujo de trabajo

```
main (estable)
 └── feature-X (trabajo)
       ├── commits pequeños con TDD
       └── al terminar: merge a main (o PR si algún día hay remote)
```

1. Trabajar SIEMPRE en una rama de feature, nunca en `main`.
2. TDD: test primero (RED), implementación (GREEN), refactor.
3. Commits frecuentes y descriptivos (`feat:`, `fix:`, `docs:`, `chore:`).
4. Antes de mergear a main: `uv run pytest` completo en verde + smoke test
   manual contra un endpoint real.

## Comandos útiles

```bash
git checkout feature-tools        # cambiar de rama
git checkout -b feature-nueva main  # crear rama nueva desde main
git branch                        # listar ramas
git merge feature-tools           # integrar a la rama actual (desde main)
```

## Crear una rama nueva

¿Otra área de trabajo? Crear desde `main`:

```bash
git checkout -b feature-mi-idea main
```

Y agregar la fila correspondiente a la tabla de arriba en este archivo.
