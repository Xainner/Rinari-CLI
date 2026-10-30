# Rinari-CLI — reglas del proyecto

Rinari-CLI es el Engine de Rinari: sesiones, proveedores y modelos,
herramientas, permisos, memoria, contexto, skills y agentes. Lo usan el
terminal (`rinari`) y la app de escritorio Rinari Agent (`Xainner/Rinari-Agent`),
que lo arranca y le habla por el protocolo v1. Aquí están solo las reglas
vigentes.

Las reglas de agosto de 2026 están en
[`docs/historia/agents-v1.md`](docs/historia/agents-v1.md) y el roadmap v1, en
[`docs/historia/roadmap-v1.md`](docs/historia/roadmap-v1.md). Son históricos y no
autorizan nada, pero los comentarios del código citan sus secciones por número:
«AGENTS.md 11» o «AGENTS.md section 13» se refieren a `agents-v1.md`, y las
secciones de «TODO.md», a `roadmap-v1.md`.

## Orden de fusión con Rinari Agent

- Un cambio que toca los dos repositorios se fusiona **primero aquí**.
  Después, en Rinari Agent, se fija ese commit de `main` en
  `engine-manifest.json` y se regeneran sus tipos con `npm run
  protocol:generate`. La app nunca apunta a una rama ni a un commit sin
  fusionar.
- Los PR se fusionan con el CI en verde y solo con la autorización del dueño.

## Protocolo

- Un método, evento o campo nuevo del protocolo va en
  `src/rinari/engine_protocol/schema/v1.json`, con su capacidad en
  `CAPABILITIES` de `src/rinari/engine_protocol/protocol.py`. La app exige las
  capacidades que necesita en su `engine-manifest.json`
  (`required_capabilities`), así que una capacidad no se renombra ni se
  reutiliza con otro significado: si cambia el contrato, la capacidad nueva
  lleva otra versión, como `vision_routing_v2`.
- El Engine es dueño del comportamiento; los clientes lo presentan. Lo que se
  muestra (tokens, coste, contexto, estado) sale de lo que el Engine sabe y
  nunca se inventa.

## Migraciones

- Son archivos `src/rinari/storage/migrations/NNNN_nombre.sql`, numerados en
  el **orden de fusión**: si otra rama fusiona antes una migración con el
  mismo número, la tuya se renumera antes de fusionar. Una versión se
  identifica por número y nombre, y el Engine se detiene si una base ya
  aplicó otro archivo con ese número (`migrations/runner.py`, `SUPERSEDED`).
- **Sin `;` en los comentarios:** el runner parte el archivo por `;`, así que
  un punto y coma dentro de un comentario rompe la migración.
- Cada migración corre en una transacción, conserva las referencias de
  proveedores, modelos y sesiones, y nunca copia secretos en claro.

## Herramientas

- **Núcleo pequeño, lo demás bajo demanda** ([`docs/tools.md`](docs/tools.md)
  §63). Cada petición al modelo lleva solo el núcleo (`always_loaded=True`);
  el resto lo encuentra con `capability.search` o lo activa por nombre. Cada
  herramienta del núcleo se paga en tokens en cada llamada y empeora la
  elección y la caché.
- **El núcleo no crece por un caso.** Una herramienta entra al núcleo solo
  por decisión del dueño. La prueba
  `test_the_always_loaded_core_does_not_grow_by_itself`
  (`tests/unit/test_tool_contract.py`) fija la lista: cambiarla es cambiar
  esta regla.
- **Si un modelo no sabe que algo existe, se le dice en una línea**, no se le
  carga el esquema: un hecho de la conversación (`_runtime_facts`), la
  sección de su equipo o una skill. Ejemplo: `schedule.propose` sigue bajo
  demanda y los hechos de cada conversación dicen que se programa con ella
  (22-oct).
- Una herramienta nueva declara su contrato de salida en
  `src/rinari/tools/output_contracts.py` (el empaquetado de la Nightly lo
  exige) y suma su nombre a las cuentas del catálogo en las pruebas.

## Nada específico del dueño

Las mejoras son generales. Nada en el código, las pruebas, los prompts ni los
valores por defecto depende del dueño: ni su nombre, ni sus rutas, ni sus
proveedores, modelos o idioma. Si un caso real del dueño destapa un fallo, se
corrige para cualquier usuario y la prueba usa datos neutros.

## Principios que siguen valiendo

- Los secretos nunca se guardan en la base de estado, en los logs ni en las
  vistas; se redactan al guardar el historial.
- `$HOME` nunca es un proyecto con escritura implícita.
- Las extensiones de un proyecto que no es de confianza (skills, hooks,
  plugins, MCP) no se ejecutan, y sus instrucciones (`RINARI.md`,
  `AGENTS.md`) no entran en el prompt.
- Las pruebas son deterministas y no necesitan internet: proveedores y relojes
  falsos, repositorios temporales y respuestas guionizadas.
- Las pruebas y el código van en inglés, con nombres que describen el
  comportamiento (`test_<lo que pasa>`). La documentación sigue el idioma que
  ya tiene cada documento.
- Un comando nuevo de `rinari` se documenta en [`docs/commands.md`](docs/commands.md).

## Antes de subir

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

- La suite **completa** de pytest, no solo los archivos tocados: el CI corre
  `tests/unit`, `tests/integration` y `tests/e2e` en Linux (Python 3.11 y
  3.12) y `tests/unit` y `tests/e2e` en Windows.
- No corras suites pesadas (la de pytest completa, o `ui:e2e --all` de Rinari
  Agent) a la vez que otra sesión en la misma máquina: compiten por CPU y
  aparecen fallos por tiempo que no son reales.
- Mientras el CI de un PR está en curso, los cambios nuevos se dejan en
  commits locales y se suben cuando termina, para no reiniciarlo.

## Dónde está el resto

- [`TODO.md`](TODO.md): los pendientes que siguen vigentes.
- [`docs/harness.md`](docs/harness.md), [`docs/tools.md`](docs/tools.md),
  [`docs/skills.md`](docs/skills.md) y [`docs/stack.md`](docs/stack.md): el
  diseño del harness y su catálogo.
- [`docs/desktop/`](docs/desktop/): el contrato con la app de escritorio.
- [`docs/historia/`](docs/historia/README.md): reglas, roadmap y análisis
  antiguos.
