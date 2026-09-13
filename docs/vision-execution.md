# Ejecución de visión y política común de modelos

Implementación local; sin commit/push. 2026-09-12.

## Referencia

Hermes inspeccionado: `de2d6a1b93508463c31434c1ae067e204af81238`.
[vision_tools.py](https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/tools/vision_tools.py)
separa preparación local acotada de llamadas concurrentes al LLM. Se adopta esa
separación; no se copian sus límites, políticas de reintento ni proveedores.

## Cambios

- Eliminado max_tokens=2048 exclusivo de visión y su presencia en la caché.
- Generación común: petición explícita > configuración por modelo de esta
  instalación > settings.generation del modelo > proveedor > contrato del adaptador.
  Chat/Responses omiten el máximo si no se configura. Anthropic conserva el valor
  general del adaptador (8192) porque exige el parámetro; puede sobrescribirse.
- Consultas auxiliares independientes concurrentes, con resultados colocados en el
  orden original. Nunca se combinan preguntas diferentes ni se repite el historial.
- Caché por originales, pregunta, destino, revisiones y generación efectiva.
  Las consultas idénticas simultáneas comparten análisis en una sesión, conservando
  un derivado recuperable por cada origen. Los fallos no borran resultados hermanos.
- Respuestas max_tokens se conservan completas como parciales; sin continuación
  automática. Se retiró también el recorte silencioso a 16000 caracteres del análisis.
- Presupuestos: reserva atómica al admitir la llamada auxiliar, con comprobación de
  ancestros; contador y uso protegidos. La política previa de presupuesto monetario
  sin precios del modelo auxiliar sigue devolviendo un error explícito.
- Escrituras de artefactos/eventos usan la base sincronizada existente; no hay otro
  runtime. Los eventos de un caller se serializan, los resultados pueden terminar
  fuera de orden. Preparación, espera, análisis, parcial y estados terminales visibles.

## Configuración de cada instalación

`model-execution.json` en el directorio de estado contiene:

```json
{"max_concurrency": 8, "providers": {}, "models": {}}
```

8 es la política inicial del cliente por destino, configurable, NO un número de
slots inferido ni un límite de imágenes. `providers` asigna concurrencia a IDs de
proveedores guardados; ausencia significa Heredar. `models` asigna máximos de salida
a IDs de modelos; ausencia hereda modelo/adaptador. Los valores deben ser positivos.
El servidor sigue aplicando sus propias restricciones.

Todas las rutas ModelRouter, normales/auxiliares y streaming, comparten admisión FIFO
por instalación y registro de proveedor dentro del proceso. El límite se mantiene
durante la llamada. No se deducen capacidades por marca, localhost ni nombre de modelo.
Registros de proveedor diferentes se consideran destinos independientes, incluso si
apuntan a la misma URL. Instancias de motor separadas no comparten contadores.

En Agent: Configuración > Visión e imágenes > Ejecución de modelos (todas las rutas).
CLI (estos ajustes también afectan llamadas normales):

```text
rinari vision execution --concurrency 8
rinari vision execution --provider mi-servidor --concurrency 1
rinari vision execution --provider mi-servidor --inherit
rinari vision execution --model mi-modelo --output-tokens 32000
rinari vision execution --model mi-modelo --inherit
```

No se alteró la configuración personal del usuario. No se eligieron otros proveedores.
La cola y las preparaciones consultan cancelación. Una petición HTTP síncrona ya
aceptada no puede abortarse de forma garantizada: se espera su respuesta/timeout,
se contabiliza su uso y no se publica análisis tras cancelar. No se abandona un
worker que pueda seguir escribiendo contra una sesión cerrada.

La política de reintento existente permanece: las peticiones con píxeles no se
reenvían automáticamente después de un error ambiguo/429. Se muestra el fallo;
los resultados hermanos terminados permanecen reutilizables.

## Validación

- Suite motor/CLI/agentes/adaptadores: 225 passed, 1 skipped.
- Prueba adicional de payload Chat, Responses y Anthropic: passed (omisión,
  configuración explícita, bloques visuales y valor requerido por contrato).
- Integración Rust con motor empaquetado actualizado: 1 passed.
- Frontend: 42 passed en nueve archivos; TypeScript, protocolo y build correctos.
- Dobles concurrentes: concurrencias 1 y 3, destinos independientes, cancelación
  de espera, reservas jerárquicas, orden, fallo parcial, deduplicación y caché.
- Proveedor REAL: qwen3.8-27b-uncensored seleccionado como auxiliar configurado,
  estado temporal, capacidad explícita y destino configurado a concurrencia 1.
  Adjunto y fs.read_image respondieron rojo/azul, turn.completed en ambos.
  No se impuso máximo de salida especial. El smoke configura concurrencia 1
  explícitamente para esa prueba; no cambia la política general del producto.
- No se realizó una prueba real de concurrencia comercial ni inspección manual GUI.
  La concurrencia paralela está verificada con dobles; no se atribuye a otro proveedor.

Tipos generados y capacidad `model_execution_policy_v1` actualizados. Reiniciar
Agent para usar el motor empaquetado actualizado.
