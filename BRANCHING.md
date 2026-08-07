# Estrategia de Ramas — Rinari CLI

Git flow ligero, repo **local** por ahora (sin remotes). `main` es la rama
estable: todo lo que llega a `main` debe pasar tests (`uv run pytest`) y ser
verificable.

## Ramas de feature

| Rama | Propósito | Ideas concretas |
|---|---|---|
| `feature-tools` | Ampliar el tool registry del agente | tools de git (status/diff/commit), edición por patch (no reescribir archivo completo), ejecución de tests automática, `man`/doc lookup, tool de búsqueda web |
| `feature-ui` | Interfaz y rendering | streaming en vivo del agente (mostrar tokens mientras piensa), prompt_toolkit multiline en REPL, syntax highlight en respuestas, spinner/pasos más bonitos, soporte de temas/colores |
| `feature-agent` | Loop agéntico avanzado | planificación explícita (el modelo escribe un plan antes de actuar), verificación automática de cambios, retry inteligente tras errores de tool, persistencia de contexto entre sesiones, presets de tareas |
| `feature-history` | Historial y sesiones | listar/borrar sesiones del chat (`rinari history`), resume de sesiones del agente, exportar conversaciones (md/json), búsqueda en historial |
| `feature-config` | Configuración | wizard `rinari setup` (detecta endpoints, genera config.toml), más perfiles preconfigurados, gestión de keys con fallback a archivo .env, validación de endpoints (`rinari doctor`) |
| `feature-mcp` | Integración MCP | soporte Model Context Protocol para conectar herramientas externas (bases de datos, navegador, servicios), clientes MCP configurable por perfil |

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
