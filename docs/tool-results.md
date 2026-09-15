# Resultados recuperables y ejecución automática

Implementación local revisada el 2026-09-14. Sin commit, push ni despliegue.

## Comportamiento

- `to_model_text` serializa sin recortar JSON. El Engine decide la entrega antes de construir la siguiente petición.
- Se conservan resultados saneados, errores y referencias inmutables. Los artefactos usan contenido identificado por SHA-256 y escritura atómica.
- Presupuesto UTF-8 configurable por resultado y ronda; reparto equitativo y redistribución del espacio de respuestas pequeñas. Los lotes mantienen todos sus miembros o devuelven un diagnóstico explícito si sus metadatos no caben.
- Los extractos respetan líneas/entradas. `artifact.read` ajusta el cursor al texto realmente entregado, sin crear cadenas de artefactos. Los límites de lectura del origen siguen existiendo y son distintos del presupuesto de entrega.
- `ToolResult.truncated` significa «el modelo no observó el resultado completo». Se activa siempre que la entrega es parcial (`data.delivery_partial`), aunque la fuente estuviera entera; `data.source_partial` conserva la información sobre la fuente. La actividad y Agent muestran así una observación incompleta con evidencia recuperable, no un resultado completo.
- En Windows, las rutas de artefactos que superan el `MAX_PATH` heredado se abren con la forma extendida `\\?\`; los nombres derivados con varios SHA-256 no dependen de `LongPathsEnabled`.
- Las observaciones finales se persisten como mensajes; ante un fallo de la asignación final se conserva la observación anterior respaldada. Tras un crash se recuperan observaciones registradas por turno; identificadores ambiguos no se asocian por conjetura.
- Las evidencias visuales anteriores conservan su ruta existente. Recuperar una actividad visual sin su mensaje no afirma haber restaurado píxeles.
- Coordinador de autorización, presupuesto y consolidación separado del ejecutor de lecturas. Las demás herramientas son barreras. Resultados en orden lógico y actividad de finalización conforme termina cada trabajo.
- Un ejecutor compartido por proceso, también para `fs.read(paths)`. Las lecturas anidadas se ejecutan en el mismo trabajador sin crear otro pool. Las solicitudes concurrentes respetan el menor límite activo.
- Cancelar impide nuevos despachos y conserva resultados terminados; el coordinador espera a que termine un handler activo antes de declarar concluido su grupo.
- El progreso real reinicia el presupuesto de recuperación. Las instrucciones piden hallazgos y decisiones, sin narrar cada lote.
- Agent elimina el botón «Continuar». Un error conserva su explicación e historial; el usuario puede escribir un nuevo mensaje. No se reejecutan acciones automáticamente tras un error.
- La actividad distingue cantidad de lecturas y archivos informados por el motor. La información técnica queda separada de la presentación.

## Configuración

```toml
[runtime.tools]
max_concurrency = 4
observation_bytes = 65536
round_observation_bytes = 262144
```

Estas opciones pertenecen al Engine y se heredan en subagentes. No son límites de salida del modelo ni de concurrencia de visión. Un cambio de configuración se aplica al construir el contexto de una sesión.

## Componentes

- `tools/observations.py`: proyección, recuperación y asignación de presupuestos.
- `tools/runtime.py`: autorización previa, saneamiento, artefactos y entrega final.
- `runtime/tool_coordinator.py`, `tools/executor.py`, `tools/scheduler.py`: grupos, barreras y ejecución compartida.
- `runtime/durable_history.py`: recuperación sin ejecutar acciones ni recortar nuevamente el JSON.
- `runtime/agent.py`, `runtime/governor.py`: consolidación, persistencia y progreso.
- Agent: actividad, contadores, eliminación del botón y contrato generado.
- Gateway: transporte de actividad estructurada sin presentar la observación privada del proveedor.

Capacidad requerida: `recoverable_tool_results_v1`. Tipos Rust y TypeScript generados del esquema del Engine. El paquete Agent es de desarrollo sobre `fb2978b8d3a85f0107343dee0dd97ac1767f0aa3`, con cambios locales; su procedencia exacta está en `ENGINE_SOURCE.json`.

## Validación

- Motor/CLI: 1.827 pruebas aprobadas, 8 omitidas en la batería de unitarios e integración; revisión posterior focalizada: 111 aprobadas, 1 omitida.
- Frontend: 113 aprobadas. TypeScript y build Vite aprobados; advertencia existente de tamaño de chunks.
- Rust: 40 aprobadas, 1 ignorada. Comprobación de protocolo aprobada.
- Gateway: 10 aprobadas, incluidas comprobaciones con Engine real en instalación temporal. Sin producción.
- Paquete Windows: 106 herramientas únicas, esquemas de entrada/salida, capacidades, hashes OCR, reconocimiento español y extracción de PDF escaneado comprobados.
- Los 372 archivos de la fuente empaquetada se compararon byte a byte con el checkout revisado.
- Proveedor real: Muse Spark 1.3 recibió una observación sintética de 62.443 bytes con seis archivos y devolvió sus seis marcadores, en dos peticiones y 6,23 segundos. No se reanudó ninguna sesión original ni se ejecutaron herramientas externas.
- Un intento anterior del caso real no devolvió todos los marcadores. La repetición posterior pasó; esto comprueba transporte y capacidad de lectura, no una conducta determinista del modelo ni una mejora temporal universal.

## Límites de lo comprobado

Las pruebas de UI son automatizadas; no se presenta una inspección manual de la ventana Tauri como realizada. La comparación del número de rondas con la sesión original no se ejecutó. Las herramientas no habilitadas expresamente siguen siendo secuenciales. Los tests del catálogo verifican el contrato compartido; no certifican una operación real contra cada integración externa. Las referencias antiguas sin evidencia recuperable permanecen marcadas como incompletas.

## Catálogo auditado

Todos los productores nativos pasan por el contrato común. Elegibilidad explícita actual:

`artifact.metadata`, `artifact.read`, `fs.glob`, `fs.list`, `fs.read`, `fs.read_lines`, `fs.search_text`, `fs.stat`, `search.files`.

| Herramienta | Concurrencia | Efectos declarados |
|---|---|---|
| `agent.cancel` | `serial` | `local_reversible` |
| `agent.message` | `serial` | `local_reversible` |
| `agent.result` | `serial` | `none` |
| `agent.spawn` | `serial` | `local_reversible` |
| `agent.status` | `serial` | `none` |
| `agent.synthesize` | `serial` | `none` |
| `agent.wait` | `serial` | `none` |
| `artifact.metadata` | `local-read` | `none` |
| `artifact.read` | `local-read` | `none` |
| `browser.a11y` | `serial` | `none` |
| `browser.check` | `serial` | `remote-reversible` |
| `browser.click` | `serial` | `remote-reversible` |
| `browser.close` | `serial` | `local-reversible` |
| `browser.connect` | `serial` | `local-reversible` |
| `browser.console` | `serial` | `none` |
| `browser.cookies` | `serial` | `none` |
| `browser.download` | `serial` | `local-reversible` |
| `browser.drag` | `serial` | `remote-reversible` |
| `browser.evaluate` | `serial` | `remote-reversible` |
| `browser.fill` | `serial` | `remote-reversible` |
| `browser.launch` | `serial` | `local-reversible` |
| `browser.navigate` | `serial` | `remote-reversible` |
| `browser.network` | `serial` | `none` |
| `browser.open` | `serial` | `remote-reversible` |
| `browser.screenshot` | `serial` | `local-reversible` |
| `browser.scroll` | `serial` | `remote-reversible` |
| `browser.select` | `serial` | `remote-reversible` |
| `browser.set_cookie` | `serial` | `remote-reversible` |
| `browser.snapshot` | `serial` | `none` |
| `browser.status` | `serial` | `none` |
| `browser.tabs` | `serial` | `none` |
| `browser.tabs_close` | `serial` | `local-reversible` |
| `browser.type` | `serial` | `remote-reversible` |
| `browser.upload` | `serial` | `communication` |
| `capability.activate` | `serial` | `none` |
| `capability.deactivate` | `serial` | `none` |
| `capability.search` | `serial` | `none` |
| `context.list_pins` | `serial` | `none` |
| `context.pin` | `serial` | `local-reversible` |
| `context.retrieve` | `serial` | `none` |
| `context.unpin` | `serial` | `local-reversible` |
| `fs.diff` | `serial` | `none` |
| `fs.glob` | `local-read` | `none` |
| `fs.list` | `local-read` | `none` |
| `fs.patch` | `serial` | `local-reversible` |
| `fs.read` | `local-read` | `none` |
| `fs.read_image` | `serial` | `none` |
| `fs.read_lines` | `local-read` | `none` |
| `fs.search_text` | `local-read` | `none` |
| `fs.stat` | `local-read` | `none` |
| `fs.write` | `serial` | `local-reversible` |
| `git.branch` | `serial` | `none` |
| `git.diff` | `serial` | `none` |
| `git.log` | `serial` | `none` |
| `git.show` | `serial` | `none` |
| `git.status` | `serial` | `none` |
| `http.request` | `serial` | `local-reversible` |
| `http.sse` | `serial` | `none` |
| `lsp.definition` | `serial` | `none` |
| `lsp.diagnostics` | `serial` | `none` |
| `lsp.hover` | `serial` | `none` |
| `lsp.references` | `serial` | `none` |
| `lsp.rename` | `serial` | `none` |
| `lsp.signature` | `serial` | `none` |
| `lsp.symbols` | `serial` | `none` |
| `memory.episodic` | `serial` | `local-reversible` |
| `memory.forget` | `serial` | `local-destructive` |
| `memory.recall` | `serial` | `none` |
| `memory.remember` | `serial` | `local-reversible` |
| `memory.update` | `serial` | `local-reversible` |
| `process.list` | `serial` | `none` |
| `process.output` | `serial` | `none` |
| `process.signal` | `serial` | `local-reversible` |
| `process.start` | `serial` | `local-reversible` |
| `process.wait` | `serial` | `none` |
| `pty.read` | `serial` | `none` |
| `pty.resize` | `serial` | `none` |
| `pty.start` | `serial` | `local-reversible` |
| `pty.terminate` | `serial` | `local-destructive` |
| `pty.write` | `serial` | `local-reversible` |
| `search.files` | `local-read` | `none` |
| `search.hybrid` | `serial` | `none` |
| `search.references` | `serial` | `none` |
| `search.regex` | `serial` | `none` |
| `search.symbols` | `serial` | `none` |
| `shell.exec` | `serial` | `local-reversible` |
| `skills.activate` | `serial` | `local_reversible` |
| `skills.deactivate` | `serial` | `local_reversible` |
| `skills.list` | `serial` | `none` |
| `skills.show` | `serial` | `none` |
| `ssh.inspect` | `serial` | `none` |
| `user.ask` | `serial` | `none` |
| `verify.evaluate` | `serial` | `none` |
| `verify.plan` | `serial` | `none` |
| `verify.record` | `serial` | `local-reversible` |
| `web.cite` | `serial` | `none` |
| `web.download` | `serial` | `local-reversible` |
| `web.extract_markdown` | `serial` | `none` |
| `web.extract_metadata` | `serial` | `none` |
| `web.extract_text` | `serial` | `none` |
| `web.fetch` | `serial` | `none` |
| `web.find` | `serial` | `none` |
| `web.links` | `serial` | `none` |
| `web.open` | `serial` | `none` |
| `web.search` | `serial` | `none` |
| `web.sources` | `serial` | `none` |
