# Rinari CLI

**Tu asistente personal de IA en la terminal.**

> **Estado: fase 0 — definición.** Este repo no tiene código todavía, a propósito.
> Esta vez se hace despacio y deliberado: primero se define, después se construye.
> Ver [TODO.md](TODO.md) por el roadmap.

Segunda construcción de Rinari. La
[v1](https://github.com/Xainner/Rinari-CLI) (chat REPL, agente con 23 tools,
MCP, git, historial, 372 tests) sirvió de referencia y se conserva como tal —
pero este proyecto parte de cero con un alcance que se decide aquí, en la
fase 0.

## Decidido

| Decisión | Valor | Desde |
|---|---|---|
| Stack | Python 3.11+ con `uv`, src layout (`src/rinari/`) | 2026-08-16 |
| Herramientas CLI (provisionales) | typer + rich + httpx (SSE) + pytest | 2026-08-16 |
| Enfoque | Fases pequeñas, definición antes que código, TDD cuando exista código | 2026-08-16 |
| Herencia de v1 | Nada, por ahora — se decide item por item en la fase 0 | 2026-08-16 |

## Pendiente de definir

- Alcance del MVP v0.1 (qué funciona de punta a punta en la primera versión)
- Qué se hereda de v1: personalidad (SOUL.md), perfiles de config, historial,
  memoria por repo (RINARI.md), agente, MCP
- Arquitectura: módulos y estructura de directorios
- Superficie CLI: nombre del comando y subcomandos iniciales
- Formato y ubicación de la configuración
- Estrategia de tests
- Licencia (¿MIT, como v1?)

Todas estas preguntas viven en la
[fase 0 de TODO.md](TODO.md).

## Docs

- [TODO.md](TODO.md) — roadmap por fases, con la definición actual
- [AGENTS.md](AGENTS.md) — reglas de trabajo para agentes de IA (y humanos)
- [docs/tools.md](docs/tools.md) — tools del agente: catálogo candidato y
  decisiones pendientes (borrador)
- [docs/skills.md](docs/skills.md) — skills: formato, catálogo candidato y
  decisiones pendientes (borrador)