# TODO — Rinari-CLI

Pendientes que siguen vigentes. Salen de revisar, una por una, las 108
casillas sin marcar del roadmap v1 contra el código (2026-10-12, `main`
cf4828b): 55 ya estaban hechas, 29 ya no aplican y estas son las que quedan.
El roadmap entero está en
[docs/historia/roadmap-v1.md](docs/historia/roadmap-v1.md); los números entre
paréntesis son sus líneas.

La deuda de la app de escritorio está en `docs/debt.md` de Rinari Agent, y
los hallazgos de la 0.3.2, en su plan (rama `0.3.2`).

## Seguridad: suite de prompt-injection

Hoy solo tiene prueba una página hostil leída con `web.fetch` y las
instrucciones de un proyecto que no es de confianza.

- [ ] Casos por fuente: comentario malicioso en el código, salida de un
      compilador o de un proceso, issue de GitHub, recurso MCP (ya se marca
      como `mcp:<tool>`, pero sin prueba), página abierta con `browser.*`,
      informe de un subagente y un secreto en las variables de entorno que el
      modelo intenta imprimir (2472–2478, 2495, 2623).
- [ ] Decidir si la salida de procesos y los informes de subagentes marcan el
      turno como «con contenido externo». Hoy solo lo hacen la red no local y
      MCP.

## Subagentes y hooks

- [ ] Que los hooks del usuario para `SubagentStart` y `SubagentStop` se
      ejecuten. `agents/orchestrator.py` emite los dos eventos y llegan a la
      actividad, pero `_build_orchestrator` (`cli/agent_runtime.py`) no recibe
      el `hook_engine`, así que un hook declarado para ellos nunca corre
      (1978–1979).
- [ ] Que el agente `verifier` sea de verdad de solo lectura: su objetivo dice
      «Read-only», pero su perfil es `inherit` (`agents/definition.py`).
      Con su prueba (2517).

## MCP

- [ ] Cancelación real: al cancelar el turno, mandar
      `notifications/cancelled` y dejar de esperar, en vez de aguantar hasta
      el timeout (`mcp/client.py`). Con su prueba (1896, 2504).

## Navegador

- [ ] Perfiles con sesión iniciada para reutilizar logins, con su UX y su
      limpieza. Hoy el navegador CDP y el del host Electron usan perfiles o
      particiones que no persisten (1807, 2493).

## Credenciales

- [ ] Migrar las entradas antiguas del vault sin scope de home, o permitir
      limpiarlas con confirmación. Hoy `application/credentials_gc.py` las
      deja como `unknown` para siempre (755).
- [ ] Prueba de que una actualización conserva las referencias de
      credenciales; `test_migrations_upgrade.py` no las toca (2540).

## Sesiones largas

- [ ] Llevar el eval `long_horizon.many_tool_calls` de 50 a más de 100
      llamadas (2524).
- [ ] Un caso largo con subagentes en curso durante una compactación y al
      reanudar (2529).

## Métricas en `rinari metrics`

Todas se pueden sacar de eventos que ya se guardan.

- [ ] Éxito al primer intento: verificado sin rehacer, a partir de
      `CompletionGateEvaluated` por turno (2406).
- [ ] Tasa de validaciones aprobadas (2409).
- [ ] Éxito al reanudar una sesión (2414).
- [ ] Tasa de subagentes que terminan con evidencia o validación (2416).
- [ ] Coste por tarea verificada, con el precio manual que ya existe en los
      ajustes del modelo (`input_price_per_mtok` / `output_price_per_mtok`);
      hoy `metrics cost` devuelve `unknown` (2418).
