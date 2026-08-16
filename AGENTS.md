# AGENTS.md — reglas de trabajo

Reglas para agentes de IA (y humanos) que trabajen en este repo.

## Estado del proyecto

Este proyecto está en **Fase 0: definición** (ver [TODO.md](TODO.md)).
**No hay código todavía y no debe aparecer hasta que la Fase 0 termine.**
Antes de tocar nada, revisar qué fase estamos y qué dice su checklist.

## No negociables

- **Despacio primero.** Una fase a la vez. Sin scope creep: no implementar
  lo que no está en TODO.md. Si se te ocurre una idea, se anota en la fase
  correspondiente, no se construye.
- **Definir antes de codificar.** Si una decisión de la Fase 0 sigue en
  blanco, preguntar a Xainner. No inventar decisiones.
- **Docs vivas.** Cada decisión que se tome se refleja: checkbox en
  TODO.md + tabla de decisiones + sección "Decidido/Pendiente" del
  README.md.
- **Español** para toda la documentación.
- **Seguridad.** Nunca commitear secretos ni API keys. En configuración,
  siempre vía environment (`${ENV_VAR}`).
- **Git.** No push sin pedirlo. Commits pequeños y descriptivos
  (`feat:`, `fix:`, `docs:`, `chore:`). Trabajar en rama `feature-X`, nunca
  directo sobre `main`.

## Stack (decidido)

- Python 3.11+, gestionado con `uv`
- src layout: `src/rinari/`
- typer (CLI) · rich (render) · httpx (HTTP/SSE) · pytest (tests sin red,
  MockTransport)
- TDD cuando exista código: test primero (RED → GREEN → refactor)

## Referencia: v1

[Rinari-CLI v1](https://github.com/Xainner/Rinari-CLI) existe y está completa.

- Es **referencia**, no base: no copiar y pegar de ahí.
- Puede leerse por GitHub cuando haga falta (código, BRANCHING.md, RINARI.md).
- Lo que eventualmente se herede de v1 debe aparecer explícitamente en el
  ítem "Herencia de v1" de TODO.md (Fase 0).

## Comandos

Aún no existen — se definen en la Fase 1. Sección que se actualizará cuando
haya `pyproject.toml` (se espera: `uv run pytest`, `uv run ruff check`).

## Estilo

- Código simple y aburrido. Menos dependencias, no más.
- Sin comentarios, salvo cuando el "por qué" no sea obvio.
- Si una decisión cambia el diseño, actualizar TODO.md/README en el mismo
  cambio, no después.