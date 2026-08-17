# Rinari CLI

**Tu asistente personal de IA en la terminal.**

> **Estado: fase 2 — agent runtime, tool runtime y seguridad base.**
> Fases 0 (contrato del producto) y 1 (fundaciones, bootstrap y persistencia)
> completas: el CLI real funciona — setup, config, providers/models,
> sesiones CHAT/PROJECT, Soul/Constitution, doctor/status/version. Ver
> [TODO.md](TODO.md) por el roadmap y el estado de decisiones.

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
| Soul / Constitution | Assets canónicos empaquetados + loader con override `~/.rinari/` y metadatos de versión/hash | 2026-08-16 |
| Licencia | MIT | 2026-08-16 |

## Qué funciona hoy

```bash
uv sync
uv run rinari setup --provider openai --api-key-env OPENAI_API_KEY \
  --model gpt-4o --model-name gpt-main
uv run rinari providers add anthropic --name anthropic-work \
  --api-key-env ANTHROPIC_API_KEY
uv run rinari provider use anthropic-work
uv run rinari chat
uv run rinari status
uv run rinari doctor
uv run rinari version
```

- Config TOML con layering (defaults → usuario → perfil → proyecto) y CLI
  `rinari config ...`.
- Providers y models en SQLite (`~/.rinari/state.db`); cambiar selección nunca
  elimina configuraciones previas y cada provider recuerda su modelo.
- Sesiones CHAT/PROJECT persistentes con promoción atómica CHAT → PROJECT que
  preserva el session ID; `$HOME` nunca es workspace implícito.
- Soul y Constitution empaquetados con loader (override en `~/.rinari/`,
  versión + sha256 visibles en `rinari version` / `doctor`).
- Agent loop real (Fase 2): `rinari` / `rinari chat` / `rinari resume` ahora
  conversan con el model. Turnos con streaming, tool calls normalizadas a
  través del Tool Runtime (policy → approval → sandbox), REPL con
  `/provider` y `/model` in-session, prompt one-shot no interactivo,
  y traza de eventos persistida por sesión.

## Pendiente

- Fase 2 (agent runtime) mayormente completa; queda para cierre:
  streaming live de stdout/stderr del shell, PTY/process handles, network
  policy sandbox, session-interruption state, y verify/finalize transitions
  (estos dos últimos son de Fase 3). Checklist completo en
  [TODO.md](TODO.md).
- 4 items de Fase 1 siguen abiertos porque dependen del agent loop de Fase 2:
  `Extended Identity bajo demanda`, `detectar project-creation intent`,
  `preservar conversación`, `recalcular permisos`.
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