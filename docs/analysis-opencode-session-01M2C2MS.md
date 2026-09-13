# Análisis de sesión: OpenCode Go / Muse Spark 1.3

Sesión: ses_01M2C2MS86NRDRCKYRA1B06FHJ.
Análisis de solo lectura de estado y código; sin cambios en runtime, sesión,
configuración, archivos remotos ni solicitudes al proveedor.

## Resultado

Dos turnos terminaron por NetworkError al consumir Responses streaming. La sesión
no está cerrada: estado `active`. Los registros demuestran terminación controlada
de turnos, no una caída del proceso del motor o de la ventana. No se inspeccionó
un dump de proceso ni se puede descartar un cierre visual independiente.

La causa próxima es timeout de transporte. Rinari configura globalmente lectura
inactiva a 30 segundos. El manejador elimina el subtipo HTTPX y no guarda fase,
bytes, último evento ni request ID, por lo que no puede afirmarse si fue espera
inicial, pausa intermedia o qué pasó dentro del proveedor. Las duraciones son
compatibles con el límite de lectura; no hay evidencia de HTTP 429, 401, 5xx o
rechazo de parámetros en los dos eventos de fallo.

## Reconstrucción (UTC; Costa Rica = UTC menos 6 horas)

| Turno | Inicio | Resultado | Llamadas del modelo completadas | Herramientas |
|---|---|---|---:|---:|
| C2SE84… | 00:31:02 | Plan terminado 00:32:04 | 5 | 4 |
| C2WFPM… | 00:32:42 | model_7 falló 00:34:02, duración 31.235 s | 6 | 6 |
| C2Z80G… | 00:34:13 | model_10 falló 00:36:40, duración 30.375 s | 9 | 9 |

22 solicitudes registradas: 20 completadas y 2 fallidas. No es una estimación de
fiabilidad general del proveedor. El primer turno contiene una llamada exitosa
de 31.234 s: el umbral no es un máximo de duración total de respuesta.

Modelo guardado: muse-spark-1.3-contributor.
Endpoint usado: https://opencode.ai/zen/go/v1/responses.
La documentación oficial Go asigna ese modelo a Responses: el endpoint concuerda.

## Hallazgos por prioridad

### 1. Persistencia omitida al fallar el proveedor (prioridad alta)

`cli/agent_runtime.py` guarda mensajes después de que `session.loop.turn` retorna,
y también en `except CancelledError`. Para NetworkError u otras excepciones, el
finally solo limpia memory_source; el flujo salta la persistencia posterior.

Evidencia SQLite de session_messages:
- Primer turno: 1 usuario, 5 assistant, 4 tool.
- Segundo turno: solo 1 usuario.
- Tercer turno: solo 1 usuario.

Los 6 + 9 ToolCompleted sí están en session_events. Es pérdida de contexto de
reanudación, no desaparición de todos los registros. `_restore_history` restaura
session_messages, no reconstruye ese intercambio desde los eventos.

Después de «continua» la primera petición recibió 13.225 tokens frente a 15.870
en la última petición exitosa previa. Repite búsqueda de herramientas, skills,
inspección SSH, GPU, dependencias y sudo. Esto concuerda con el historial omitido;
no demuestra que toda repetición sea causada exclusivamente por ese defecto.

Impacto: un fallo transitorio obliga a redescubrir trabajo y puede facilitar
repetición de acciones externas. No se debe resolver reejecutando herramientas
anteriores. Hay que persistir intercambios completos en cada frontera y conservar
una marca de interrupción y cualquier salida parcial claramente identificada.

### 2. Timeout global y diagnóstico insuficiente (prioridad alta)

`providers/adapters/http.py`: connect=15, read=30, write=30, pool=10.
La misma constante se aplica a otros adaptadores. El paquete instalado contiene
ese mismo valor. No es una condición codificada para Muse.

`responses.py` captura todos los TimeoutException como NetworkError con un mensaje
genérico. No existe distinción observable entre espera inicial, silencio del
stream y fallo de conexión. HTTPX limita la espera por fragmento, no la duración
completa de una generación.

La política debe separar conexión, primer dato, inactividad y duración total,
ser configurable por instalación/destino y mantener cancelación operativa. No se
recomienda desactivar todos los límites ni introducir una excepción por nombre
Muse. Streaming no se reintenta automáticamente; eso evita duplicar efectos,
pero hoy cualquier fallo corta el turno sin una recuperación durable adecuada.

### 3. Terminales Responses mal interpretados (prioridad alta, defecto adicional)

El acumulador solo procesa output_text.delta y response.completed. Ignora
response.failed y response.incomplete; sin completed, finalize devuelve los
fragmentos como ModelResponse con stop_reason end_turn.

Reproducción local, sin red, con fragmento «Partial answer»:
- response.failed => end_turn «Partial answer».
- response.incomplete => end_turn «Partial answer».
- EOF sin evento terminal => end_turn «Partial answer».

No se observó ese cierre como causa de los dos fallos actuales (fueron excepciones
de timeout). Sí puede explicar otras terminaciones prematuras o falsos éxitos.
Debe exigirse un terminal válido y distinguir incompleto, fallido, cancelado y
stream interrumpido, preservando texto sin ejecutarlo ni declararlo completo.

### 4. Eficiencia y precisión de herramientas (secundario)

El modelo repite anuncios de que va a preparar el bot, búsquedas y comprobaciones.
SSH sí funciona: hay resultados válidos y la creación remota de los directorios
bot, scripts, transcriber y transcripts. No hay evidencia en esta sesión de
archivos de implementación creados ni de servicio arrancado.

Comandos detectan node/pip3 ausentes; eso no equivale a SSH caído. Uno termina
con exit_code 127 y otro comando local con exit_code 255. ToolCompleted usa ok=true
para el recorrido de la herramienta mientras la presentación contiene el código
de proceso. No se debe confundir éxito del transporte/herramienta con éxito del
comando. Estas salidas no causaron los dos turn.failed: el modelo continuó después.

No se conectó a Saturno durante este análisis: el estado remoto actual no fue
verificado y podría haber cambiado después de los registros.

## Orden de corrección recomendado

1. Persistencia durable y reanudación antes de añadir reintentos.
2. Terminales y errores Responses rigurosos, con pruebas de streams truncados.
3. Política configurable de tiempos y diagnóstico de fase/último progreso.
4. Recuperación explícita del paso de modelo pendiente, nunca replay automático
   de comandos remotos, con resultados previos restaurados.
5. Evaluar de nuevo eficiencia del modelo después de corregir esos fallos del harness.

Pruebas propuestas: timeout después de varias herramientas con efectos, recarga,
reanudación sin repetición, streaming con pausas, primer dato lento, cierre antes
de terminal, response.failed/incomplete, cancelación, y proveedor real con tareas
sintéticas sin efectos externos.

## Fuentes

- Base SQLite local, eventos seq 2–329, session_messages y sesión indicada.
- Código actual y timeout del motor empaquetado.
- https://www.python-httpx.org/advanced/timeouts/
- https://opencode.ai/docs/go/
