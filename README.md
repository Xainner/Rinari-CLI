# Rinari CLI

**Tu asistente personal de IA en la terminal.**

> **Estado: fase 0 — contrato del producto, por cerrar.**
> Queda un solo bloqueante: la **licencia**. Todo lo demás del producto
> está definido. Ver [TODO.md](TODO.md) por el roadmap y el estado de
> decisiones.

Segunda construcción de Rinari. La
[v1](https://github.com/Xainner/Rinari-CLI) (chat REPL, agente con 23 tools,
MCP, git, historial, 372 tests) sirvió de referencia y se conserva como tal —
referencia, no base de código. Qué se hace con cada parte de v1 quedó
resuelto item por item en la sección "Herencia de Rinari v1" de
[TODO.md](TODO.md).

## Decidido

| Decisión | Valor | Desde |
|---|---|---|
| Target del producto | Harness completo y productivo, no wrapper mínimo de LLM + shell | 2026-08-16 |
| Stack | Python 3.11+, `uv`, src layout (`src/rinari/`), Typer, Rich, HTTPX, pytest | 2026-08-16 |
| Herencia de v1 | Solo referencia; todo se reconstruye con el diseño v2 | 2026-08-16 |
| Soul | Definido desde cero: identidad, voz, reglas duras y apariencia fija | 2026-08-16 |
| Arquitectura | Principios + blueprint completo; sin mega-system-prompt | 2026-08-16 |
| Contrato CLI | Superficie completa: comandos, providers, models, config, visual | 2026-08-16 |
| Tools / Skills | Catálogos maestros con contrato y estrategia de carga lazy | 2026-08-16 |
| Sesiones | `rinari chat` fuerza CHAT; `rinari` = AUTO (proyecto → PROJECT); promoción CHAT → PROJECT sin reiniciar | 2026-08-16 |
| Providers / models | Registries persistentes; cambiar selección nunca elimina configuraciones previas | 2026-08-16 |

## Pendiente

- **Licencia** (candidata: MIT) — el único bloqueante de la fase 0. Su cierre
  abre la fase 1 (ver "Salida de Fase 0" en
  [TODO.md](TODO.md)).
- Decisiones no bloqueantes (engine de browser, backend del credential store,
  SQLite/ORM, LSPs iniciales, etc.): sección "Decisiones pendientes" de
  [TODO.md](TODO.md). Se resuelve cada una antes de implementar su
  subsistema.

## Docs

- [TODO.md](TODO.md) — fases 0-9, fuentes de verdad, invariantes del producto,
  registro de decisiones
- [AGENTS.md](AGENTS.md) — reglas de trabajo para agentes de IA (y humanos)
- [docs/soul.md](docs/soul.md) — el soul de Rinari: canonical soul, extended
  identity/apariencia y notas de mantenimiento
- [docs/stack.md](docs/stack.md) — principios arquitectónicos y runtime
  productivo (95 secciones)
- [docs/commands.md](docs/commands.md) — contrato público del CLI (comandos,
  providers, models, config, modales, CI)
- [docs/tools.md](docs/tools.md) — catálogo maestro de tools (64 dominios,
  contrato de tool, resultado estándar, carga lazy)
- [docs/skills.md](docs/skills.md) — catálogo maestro de skills (104
  capacidades, capas de carga, contrato de skill, skills vs tools)
- [docs/harness.md](docs/harness.md) — blueprint completo: boot, sesiones,
  state, policy/sandbox, runtime, visual, testing y dependencia de
  implementación