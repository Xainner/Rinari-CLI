# TODO — Rinari CLI

Roadmap por fases. Regla: **una fase a la vez**. Una fase termina cuando sus
checkboxes están marcados y se cumple su criterio de aceptación. Nada pasa a
`main` sin tests en verde (cuando ya exista código).

Las fases desde la fase 1 en adelante son **provisionales**: se reescriben
cuando la fase 0 se termine.

---

## Fase 0 — Definición ← (actual)

Criterio de aceptación: todas las decisiones abajo tomadas y documentadas, de
modo que la fase 1 pueda construirse sin adivinar nada.

- [ ] **MVP v0.1** — definir qué funciona de punta a punta en la primera
      versión (candidatos a discutir: one-shot `rinari run "..."` → respuesta
      con streaming; REPL de chat mínimo). Decisión final: _pendiente_.
- [ ] **Herencia de v1** — decidir por item: se hereda / se reconstruye más
      tarde / se descarta.
      Items: personalidad (SOUL.md) · perfiles multi-provider · historial de
      sesiones · RINARI.md (memoria por repo) · modo agente · tools · skills ·
      MCP · web_search · hooks · subagentes · setup wizard / doctor.
      Catálogos y decisiones por tema: [docs/tools.md](docs/tools.md) y
      [docs/skills.md](docs/skills.md).
- [ ] **Arquitectura** — mapa de módulos de `src/rinari/` y estructura de
      directorios (partir de mínimo, no de la v1).
- [ ] **Superficie CLI** — nombre del comando (`rinari`) y subcomandos de la
      v0.1.
- [ ] **Configuración** — formato (TOML?), ubicación (`~/.rinari/`?), qué
      providers soporta la v0.1 (¿solo OpenAI-compatible? ¿local primero?).
- [ ] **Tests** — ¿TDD desde la fase 1? ¿pytest + MockTransport sin red,
      como v1?
- [ ] **Licencia** — ¿MIT?
- [ ] **Git** — confirmar o adaptar la estrategia de v1
      ([BRANCHING.md](https://github.com/Xainner/Rinari-CLI/blob/main/BRANCHING.md)):
      `main` estable + ramas `feature-X`, commits `feat:/fix:/docs:/chore:`.

## Fase 1 — Andamiaje _(provisional)_

Criterio de aceptación: `uv sync` limpia y `rinari run "hola"` devuelve la
respuesta del modelo en la terminal, con tests en verde.

- [ ] `pyproject.toml` (deps mínimas) + `uv.lock`
- [ ] `src/rinari/` con entrypoint CLI
- [ ] Carga de configuración
- [ ] Cliente LLM (el/los providers de la v0.1) con streaming
- [ ] `rinari run "..."` + `rinari --version`
- [ ] Primeros tests (pytest, sin red)

## Fase 2 — (definir al terminar la fase 0)

Aquí va lo que el MVP y las decisiones de herencia determinen. No inventar
alineas hasta tener esa información.

---

## Registro de decisiones

_Cuando se decide algo, anotar fecha y decisión aqui (o en la tabla del
README), y marcar el checkbox de arriba._

| Fecha | Decisión |
|---|---|
| 2026-08-16 | Stack: Python 3.11+ / uv / src layout. Fase 0 antes que código. Sin herencia de v1 por ahora. Repo nuevo, `main` como rama estable. |
| 2026-08-16 | docs/tools.md y docs/skills.md creados como borradores de definición (catálogos candidatos de v1 + decisiones pendientes). |