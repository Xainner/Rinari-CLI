# 06 — Automatic turn governor

## Estado

El gobernador automático es parte del runtime del Engine. Rinari continúa
mientras produce evidencia nueva y usa límites de emergencia únicamente como
circuito de seguridad; no presenta un presupuesto ordinario al usuario.

Componentes autoritativos:

- `ProgressMonitor` clasifica ciclos saludables, lentos, estancados o en loop.
- `TurnGovernor` decide continuar, compactar, consolidar, cambiar estrategia,
  finalizar o detener.
- `BudgetMeter` contabiliza consumo local y agregado.
- `EmergencyCircuitBreaker` aplica los límites extraordinarios sin confundirse
  con la política normal del turno.

## Política

La recuperación permite como máximo tres intervenciones:

1. consolidar la evidencia disponible;
2. cambiar de estrategia;
3. producir el resultado útil posible o detenerse de forma estructurada.

La presión de contexto pasa primero por `TurnGovernor`. Una decisión `compact`
emite `governor.compact`, ejecuta la compactación y reevalúa el contexto. El
mismo historial no se compacta dos veces.

Los límites de emergencia predeterminados son 120 minutos, 500 llamadas de
modelo, 5000 herramientas y 100 llamadas de subagentes. El límite monetario
está desactivado de forma predeterminada. La cancelación del usuario y las
decisiones de permisos siempre tienen precedencia.

## Protocolo de escritorio

El Engine publica actividad observable, nunca chain-of-thought privado:

```text
governor.progress
governor.nudge
governor.compact
governor.consolidate
governor.stop
usage.updated
turn.stopped
```

`turn.stopped` es terminal y distinto de `turn.completed`. Snapshot conserva
el estado del gobernador, recuperaciones, compactaciones, presión de contexto
y consumo. Rinari Code reconstruye ese estado con el mismo reducer usado para
eventos vivos. `Continuar` inicia otro turno dentro de la misma sesión.

## Evidencia de aceptación

- Más de 100 ciclos útiles continúan sin consumir recuperaciones.
- Loops y estancamiento ejercitan la secuencia completa y terminan con razón
  estructurada.
- Las pruebas de contexto cubren compactación, deduplicación y snapshot.
- Las pruebas de presupuesto cubren contabilidad padre/hijo y el circuito de
  emergencia.
- Las pruebas de protocolo cubren `turn.stopped`, cancelación y reconstrucción.
- Las pruebas de Rinari Code cubren eventos vivos y recuperación desde snapshot.

La suite completa permanece aislada de red; un turno real con provider se
valida como gate de integración de Rinari Code, sin registrar credenciales.
