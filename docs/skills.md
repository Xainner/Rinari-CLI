# Skills — definición

> **Estado: borrador de definición (Fase 0).** Catálogo candidato (de v1)
> pendiente de validar con Xainner. Nada de aquí es decisión todavía.

## Qué es una skill

Archivo Markdown con instrucciones que el modelo carga cuando la tarea
encaja con ella. No se ejecuta: guía.

- **Tool** = acción que el modelo invoca (código, tiene parámetros)
- **Skill** = conocimiento/procedimiento inyectado al contexto (markdown)

## Formato (compatible Claude Code / Codex / OpenCode)

```markdown
---
description: <una línea: qué hace y cómo>
allowed-tools: tool_a, tool_b
---

# Título

Instrucciones paso a paso…
```

- El nombre de la skill es el del archivo (o el del directorio si usa
  `SKILL.md` dentro de una carpeta).
- El system prompt le dice al modelo qué skills hay disponibles y para qué;
  cuando una tarea encaja, el modelo la "carga" (lee el contenido completo)
  antes de trabajar, y sigue sus instrucciones.

## Catálogo candidato (de v1)

| Skill | Descripción |
|---|---|
| `commit` | Mensajes de commit convencionales (lee el diff y el estilo del repo antes de escribir) |
| `debug` | Depuración sistemática en 4 fases: entender → hipótesis → verificar → fix |
| `docs` | Escribir documentación del proyecto |
| `explain` | Explicar código de forma clara |
| `refactor` | Refactorizar manteniendo el comportamiento (tests de por medio) |
| `review` | Revisión de código / PR |
| `security` | Revisión de seguridad de cambios |
| `test` | Escribir y ejecutar tests |

## Distribución

- **Catálogo oficial**: embebido en el paquete (`assets/skills/`).
- **Usuario**: `~/.rinari/commands/` — ahí los skills se convierten en
  `/comandos` del REPL (cualquier `/comando` desconocido busca un skill).
- En v1 también se podían instalar skills desde repos de GitHub
  (formato Claude Code).

## Decisiones pendientes (Fase 0)

- [ ] ¿Skills ya en v0.1, o junto con el modo agente?
- [ ] Mismo formato (frontmatter + `allowed-tools`)?
- [ ] Mismo catálogo inicial (8 skills) o recorte?
- [ ] Instalar desde GitHub, o solo catálogo embebido + commands locales?
- [ ] ¿Subcomando `rinari skills` (list / install / rm / show)?