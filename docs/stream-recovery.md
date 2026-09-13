# Recuperación de streams y contexto durable

Implementación local, 2026-09-13. Responde a los cuatro puntos del análisis
[OpenCode/Muse](analysis-opencode-session-01M2C2MS.md). No cambia el endpoint del
proveedor ni reanuda la tarea original.

## Comportamiento

1. **Persistencia durante el turno.** `DurableHistory` guarda mensajes y resultados
   con identidad estable. Se conserva el texto parcial ante desconexión o
   cancelación. La recarga completa llamadas sin resultado con un marcador de
   desenlace desconocido. La recuperación histórica usa exclusivamente eventos
   existentes, con extractos acotados, sin volver a ejecutar acciones.
2. **Esperas independientes.** Conexión, primer dato, inactividad y duración total
   son configurables globalmente y por proveedor. Los ajustes previos por modelo
   y variable de entorno se conservan. El decodificador incremental preserva
   Unicode y fragmentos; la inactividad mide bytes, no únicamente líneas SSE.
   Los latidos no eluden el límite total. Cancelación y errores cierran el stream.
3. **Terminación fiable.** Responses `failed`, `cancelled`, `incomplete` y EOF
   prematuro se distinguen de `completed`. Se conservan texto parcial, fase y
   referencias del proveedor cuando están disponibles. Una salida cortada por
   tokens nunca entrega herramientas ejecutables. Chat y Anthropic aplican la
   misma protección; los eventos Responses duplicados con secuencia se ignoran.
4. **Continuidad y presentación.** Se recuperan herramientas usadas recientemente
   sin otorgar permisos nuevos. Un proceso con salida distinta de cero genera
   actividad fallida, aunque el transporte haya funcionado. La observación al
   modelo distingue `process_status` y `task_verified: false`, incluso recortada.
   Agent conserva diagnósticos tras recarga y ofrece continuación explícita.

El historial es la autoridad. No se crean sesiones remotas encadenadas, no se
reintentan streams de forma automática ni se repiten comandos al recuperar.
Un marcador de resultado desconocido exige verificar el estado antes de volver
a intentar una acción; no garantiza que un proceso remoto se haya detenido.

## Configuración y compatibilidad

Ver [comandos](commands.md): `rinari vision execution --first-byte`, `--idle`,
`--connect`, `--total` y `--inherit-timeouts`. Los valores nuevos aceptan segundos
positivos finitos. Los límites heredados no se reinterpretan ni se eliminan.

Se amplía el esquema del motor y se generan tipos Rust/TypeScript. Agent exige
`durable_turn_recovery_v1`; un motor antiguo muestra incompatibilidad. El paquete
local incluye la fuente modificada y su hash de wheel, marcado como desarrollo.
No es una publicación ni un commit de estos cambios.

## Verificación

- Pruebas deterministas: persistencia antes del siguiente modelo, recarga sin
  repetición, texto parcial cancelado, evidencia histórica y desenlace desconocido;
  EOF, errores terminales, límite de tokens, Unicode fragmentado, primer dato,
  inactividad, latidos, tiempo total, cierre tardío y ausencia de reintentos.
- Configuración: precedencia global/proveedor/modelo/petición, CLI y valores
  inválidos; parámetros de espera ausentes del JSON enviado al proveedor.
- Motor/CLI: suite de 1.695 pruebas unitarias aprobadas y 8 omitidas; las
  comprobaciones finales de compactación, proyección histórica y adaptadores
  también pasaron por separado (43 pruebas). Ruff pasa en los módulos nuevos y
  adaptadores afectados; quedan avisos previos de longitud de línea en los dos
  archivos grandes del runtime.
- Frontend: 90 pruebas, incluyendo diagnóstico recuperado y botón Continuar sin
  envío automático. TypeScript y build Vite correctos. Vite conserva su aviso
  de tamaño de algunos chunks; no impide compilar.
- Rust: 18 pruebas unitarias y roundtrip adicional con el motor empaquetado,
  incluyendo guardar/leer tiempos de espera por el puente.
- Paquete: catálogo de 106 herramientas y capacidades; hashes OCR, reconocimiento
  español y extracción PDF escaneado correctos en un espacio temporal.
- **Proveedor real:** `provider_recovery_smoke.py --model muse-spark-1.3-contributor`
  pasó con OpenCode Go: dos solicitudes en 7,17 s, llamada `smoke.echo` y resultado
  sintético, terminación `end_turn`. Cero herramientas externas ejecutadas.
  Los fallos forzados se comprobaron con dobles y transporte local, no se atribuyen
  al proveedor real.

No se realizó inspección manual de la ventana de Agent. Las comprobaciones de UI
son pruebas de componentes. Reiniciar el proceso de desarrollo es necesario para
cargar el motor actualizado. La sesión original no se continuó ni se abrió SSH.
