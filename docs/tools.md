# Rinari CLI — Tools Catalog

## Autoría de skills

`skills.validate_draft` recibe `{name, skill_md, references?, update_of?}` y
devuelve `{name, version, valid, issues, warnings, review, previous_version,
unchanged}`. Tiene capacidad
`state.read`: revisa archivos temporales privados sin cambiar la biblioteca,
propuestas pendientes, registros, historial ni eventos. `ok` indica que la
validación corrió; `valid` indica si el borrador pasó.

`skills.propose` recibe el mismo contenido y vuelve a validarlo antes de guardar.
Los errores conservan `details.skill_code` y `details.issues` cuando existen.
Las referencias deben incluir el prefijo `references/`, `scripts/` o `assets/`.
Las herramientas requeridas usan nombres canónicos (`shell.exec`, `fs.write`),
no los nombres de transporte del proveedor. Una dependencia dinámica pendiente
de conexión devuelve `TOOL_DEFERRED`; no equivale a permiso o disponibilidad.

Una actualización (`update_of`) debe subir la versión (`VERSION_NOT_INCREASED`)
y conserva las referencias que no se reenvían (`REFERENCES_KEPT`). Si la skill es
aprendida, `skills.propose` la guarda activa sin aprobación y el dueño recibe un
aviso para revisarla y deshacerla; una skill nueva fuera de `/learn`, una skill
instalada o creada por el dueño y cualquier contenido peligroso esperan
aprobación. Reenviar el mismo contenido devuelve `status: unchanged`.

## Esperas y relecturas (2026-10-08)

Una auditoría de sesiones reales encontró dos desperdicios generales: esperas a
ciegas por la shell para que arrancara un servidor (`ping -n 6 127.0.0.1`,
`Start-Sleep`, `timeout /t`, `sleep`: 73 llamadas, ~24 minutos) y el mismo
archivo sin cambios leído entero una y otra vez (36 relecturas en un turno, 73 en
una sesión), cada una reenviando todo su texto al modelo.

### `wait.for`

Herramienta núcleo (siempre expuesta, namespace `wait`) que espera **una**
condición y vuelve en cuanto se cumple:

- `port` (+ `host`, por defecto `localhost`): el puerto TCP acepta conexiones.
- `url`: la URL responde con un estado entre `status_min` y `status_max` (por
  defecto 200–499: un 404 ya demuestra que el servidor está arriba; 502/503 son un
  proxy o un servidor arrancando). GET sin seguir redirecciones.
- `output` (+ `handle`, `regex`): el texto (o la regex) aparece en la salida de un
  proceso de `process.start` o `shell.exec` con `background=true`.
- `file`: el archivo existe.

`handle` junto a `port`, `url` o `file` corta la espera si ese proceso termina.
`timeout_s` por defecto 60, máximo 600; `interval_s` 0,1–10 (0,5 por defecto);
ningún sondeo bloquea más de 3 s, y la cancelación del turno la despierta al
instante. Devuelve `condition`, `target`, `ready`, `state` (`ready`,
`timed_out`, `exited`), `elapsed_s`, `checks`, `last` (estado HTTP, error de
conexión, línea encontrada, tamaño del archivo), `process` (`exit_code` y la cola
de la salida si terminó) y `timeout` cuando se agota. Una condición mal formada
se rechaza antes de la política (`INVALID_ARGUMENT`).

Política: nunca más débil que la herramienta que haría la comprobación. Un puerto
o una URL son `network.outbound` en modo lectura, como un GET de `http.request`
(local permitido; internet según `network.mode`, y marca el turno como que leyó
contenido externo); un archivo es `fs.read`; vigilar un handle es
`process.local`. Las descripciones de `shell.exec` y `process.start` y la
referencia `efficiency.md` del manual remiten a `wait.for` en vez de dormir.

### Relecturas sin cambios

`fs.read` y `fs.read_lines` responden `unchanged: true` con una nota (sin
`text`, con `sha256`/`size_bytes` o el rango y `total_lines`/`next_line`) cuando
el modelo todavía ve exactamente ese texto en un resultado anterior de la
conversación. `fresh: true` fuerza el texto completo. La comprobación es sobre la
conversación, no sobre contadores (`tools/read_cache.py`):

- La caché es por `AgentContext` (un subagente tiene la suya) y llega a los
  handlers como `ToolContext.reads`; sin `AgentLoop` (tests, hosts sin bucle) se
  lee siempre entero.
- El resultado anterior debe seguir en el historial, llegar intacto a la próxima
  petición según `settle.py` (`settled_boundary` / `kept_intact`: dentro de las
  rondas que se conservan o por debajo de `MIN_CHARS`) y contener **el mismo
  texto** que la lectura nueva, que se hace siempre: un archivo escrito por
  Rinari o cambiado en disco no coincide y se lee entero. Un resultado recortado,
  derramado a artefacto o redactado tampoco coincide.
- Un rango de `fs.read_lines` cuenta si está entero dentro de un rango anterior o
  de un `fs.read` completo del mismo archivo; las lecturas en lote (`paths`) se
  recuerdan por archivo.
- La compactación (`compact_revision`) o un historial más corto vacían la caché.
- Textos de menos de 512 caracteres se envían siempre: la nota costaría lo mismo.

## Documentos de oficina (2026-10-07)

Servicio documental del Engine (`src/rinari/documents`) para PPTX, XLSX, DOCX y
PDF. Las herramientas son perezosas: las activan las skills `rinari-presentations`,
`rinari-spreadsheets`, `rinari-documents`, `rinari-pdf` y `rinari-artifact-review`.

- Todo archivo es una **revisión inmutable** en el Artifact Store (`documents`);
  crear o editar produce otra con su padre. El original nunca se toca y solo
  `documents.finalize` con `save_to` escribe en el proyecto, sin sobrescribir.
- `documents.capabilities` dice qué se puede hacer aquí y por qué no: render con
  Office local (COM, instancia propia, macros fuera) o LibreOffice; recálculo
  solo con Excel; redacción con `raster-pdfium`.
- `documents.inspect` / `documents.read`: inventario y contenido acotado con
  identificadores estables (diapositiva y `shape_id`, bloque de Word, rango de
  hoja con fórmula y caché por separado, página de PDF y campos de formulario).
- `documents.templates` y `documents.create`: DeckSpec (pptx), WorkbookSpec
  (xlsx), ReportSpec (docx y pdf desde el mismo spec) o plantilla Word con
  contexto (sandbox Jinja, variables estrictas). Esquemas cerrados.
- `documents.edit`: operaciones tipadas `pptx.*`, `xlsx.*`, `docx.*`, `pdf.*` con
  precondiciones (`expected_*`, `REVISION_CONFLICT`) y diff de preservación por
  partes OOXML (`preserve_strict` por defecto).
- `documents.render`, `documents.review`, `documents.validate`, `documents.diff`,
  `documents.finalize`: el informe va por dimensión (`structure`, `layout`,
  `content`, `preservation`, `formulas`, `fields`, `forms`, `text_layer`,
  `visual`, `redaction`) con evidencia; lo visual solo pasa si `documents.review`
  cubrió cada página renderizada de esa revisión exacta.
- `spreadsheets.query`: datasets DuckDB (CSV, TSV, Parquet, JSON, XLSX) y una
  sola `SELECT` con el acceso externo desactivado; resultados grandes como
  dataset derivado o CSV protegido contra inyección de fórmulas.
- `spreadsheets.recalculate`: recálculo certificado con Excel; sin Excel, las
  fórmulas quedan con su resultado pendiente (nunca un 0 inventado).
- `pdf.redact`: rasteriza solo las páginas afectadas, reescribe el archivo y
  verifica con otra librería que nada se recupera; si algo se recupera, falla.
- Trabajos (`documents.job.get` / `.cancel`): proceso hijo cancelable; un
  reinicio deja los activos como `interrupted`; cancelar termina solo el proceso
  de Office que arrancó el trabajo.

## Parámetros desconocidos (2026-10-06)

Una herramienta nativa rechaza, antes de ejecutar nada, cualquier parámetro que su
schema no declare (`INVALID_ARGUMENT`, `details.unknown_arguments` y `accepted`). El
mensaje nombra el parámetro parecido y su significado: `timeout_ms` junto a
`timeout_s` en `shell.exec` sugiere `timeout_s` (segundos). Antes se aceptaba y se
ignoraba, y el comando moría a los 60 s por defecto. Las variantes con un único
significado se normalizan antes de esta comprobación (ver «Tolerancia de
argumentos»). Las herramientas de MCP, plugins y OpenAPI conservan su
propio schema. `shell.exec` y `process.wait` devuelven `timeout`
(`requested_s`, `effective_s`, `source`: `argument`, `default` o `deadline`); el
límite de la herramienta (600 s) acota la espera, y para trabajos más largos se
usa `background=true` con `process.wait`.

## Tolerancia de argumentos (2026-10-08)

Una auditoría de sesiones reales (varios proveedores, modelos locales de llama.cpp
incluidos) encontró llamadas perdidas por variantes previsibles. Una herramienta
nativa puede declarar `ToolDefinition.normalize`: el runtime la aplica **antes**
de validar y de clasificar, así que la política y las aprobaciones deciden sobre la
llamada canónica. Solo se reescriben equivalencias exactas; si hay dos variantes, la
canónica ya está o el valor no es numérico, la llamada sigue su curso y la
validación la rechaza. Nunca se inventa una ruta, un destino ni un valor. Cada
reescritura deja una nota en `ToolResult.notes` (sale como `notes` en la
observación del modelo, p. ej. `used timeout_s=300 (received timeout_ms=300000)`)
y un evento `ToolArgumentsNormalized` con la herramienta, las notas y el
`tool_call_id`, para medirlas.

- `shell.exec` / `process.wait`: `timeout_ms` → `timeout_s` (÷1000) y `timeout` →
  `timeout_s` (misma unidad), solo sin `timeout_s`. `process.start` no tiene
  timeout y sigue rechazándolo.
- `shell.exec` / `process.start`: con `command` y `argv` a la vez se ejecuta `argv`
  (la forma literal, la que clasifica la política) y se descarta `command`.
- `fs.patch`: `{path, edits[, expected_hash]}` → `{files: [{path, edits[, expected_hash]}]}`.
  Con `replace_all`, `old_string` o `files` al lado no se toca.
- `fs.read_lines`: `start_line`/`end_line` → `start`/`end`.
- `fs.read` no tiene rango: `start`, `end`, `start_line`, `end_line`, `offset` o
  `limit` devuelven `INVALID_ARGUMENT` con la llamada exacta a `fs.read_lines`.

Otras correcciones del mismo lote:

- `fs.read_lines` devuelve `text` con filas `N| línea` (más `start_line`,
  `end_line`, `total_lines`, `next_line`) en vez de un objeto por línea; el
  prefijo `N| ` no forma parte del archivo.
- `fs.search_text` acepta `regex` (por defecto `false`) y `max_results`; en los dos
  modos ignora mayúsculas y devuelve rutas absolutas. Si la búsqueda literal no
  encuentra nada y el patrón parece una regex, `note` lo dice. `search.regex` sigue
  siendo la búsqueda regex sensible a mayúsculas.
- `fs.write` crea las carpetas que faltan cuando quedan estrictamente dentro de una
  raíz de escritura concedida (las lista en `created_dirs`); fuera de ellas —por
  ejemplo, una escritura aprobada fuera del workspace— responde `NOT_FOUND` y pide
  revisar la ruta o pasar `create_parents=true`. Escribir sobre una carpeta es
  `INVALID_ARGUMENT`.
- `fs.glob` / `search.files` aceptan `include_dirs` (por defecto `false`); las
  carpetas terminan en el separador del sistema. Si no hay archivos pero sí
  carpetas que coinciden, `note` lo indica.
- `memory.recall` con `scope=project` en una sesión sin proyecto busca en la memoria
  del usuario y lo dice en `notes` (leer no cambia nada). `memory.remember`,
  `memory.update` y `memory.forget` con `scope=project` sin proyecto responden
  `INVALID_ARGUMENT` con la salida (`scope=user` o abrir la carpeta como proyecto):
  archivar un dato del proyecto como memoria personal lo llevaría a todos los chats.
- `memory.propose {text, topic, kind?: environment|workflow|preference|fact, scope?: user|project}`
  guarda un dato estable aprendido trabajando (hosts, puertos, rutas, cómo se arranca o
  prueba un proyecto, comandos que funcionan en esta máquina) sin citar al dueño. Según
  el ajuste `learned_facts` (`ask` por defecto, `auto`) queda como propuesta en el chat
  o se guarda con procedencia `learned:session/<id>`; lo sensible y lo aprendido tras
  leer contenido externo siempre es propuesta. `scope=project` sin proyecto pasa a
  `user` con una nota. Rechaza secretos (también las formas que oculta el redactor) y
  responde `already_known`, `already_proposed`, `declined` o `forgotten` en vez de
  duplicar. Un subagente no propone: lo reporta en su resultado. Detalle en
  `docs/harness.md` §73.1.
- Skills: un `# Procedure` vacío seguido de encabezados de su mismo nivel
  (`## Procedure` + `## Paso 1`) los adopta como subsecciones; `##`/`###` bajo
  `# Procedure` ya eran contenido. Si aun así falta el procedimiento, el error
  incluye un `SKILL.md` mínimo válido.

## Contexto de imágenes (Hermes, 2026-09-12)

`fs.read_image` carga la referencia solicitada mediante la política visual del
motor. No tiene un cupo de cuatro imágenes por sesión ni interpreta frases del
usuario. El historial conserva originales; la proyección retira lotes de herramientas
superados y la compactación retira adjuntos antiguos. El lote visual más reciente
permanece completo. Las restricciones se validan por bytes y por límites declarados
del proveedor/modelo, sin omisiones silenciosas. El modo auxiliar procesa un mensaje
por operación y conserva un derivado recuperable; reproducir historia no lo reanaliza.

> Catálogo maestro de tools para un agente CLI generalista.  
> Las tools son capacidades atómicas o casi-atómicas que el runtime puede exponer al modelo.

---

## 1. Runtime & System

### System Information
- `system.info`
- `system.os`
- `system.arch`
- `system.hostname`
- `system.user`
- `system.cwd`
- `system.uptime`
- `system.resources`
- `system.cpu`
- `system.memory`
- `system.disk_usage`
- `system.which`
- `system.capabilities`

### Environment
- `env.get`
- `env.list`
- `env.set`
- `env.unset`
- `env.expand`

### Process Management
- `process.spawn`
- `process.exec`
- `process.list`
- `process.inspect`
- `process.wait`
- `wait.for` — espera un puerto, una URL, una línea en la salida de un handle o un archivo (ver «Esperas y relecturas»).
- `process.signal`
- `process.kill`
- `process.stdin`
- `process.stdout`
- `process.stderr`
- `process.attach`
- `process.detach`

### Terminal / PTY
- `pty.start` — arranca un comando bajo un pseudo-terminal (TTY-aware).
- `pty.read` — lee la salida (espera hasta `timeout_s`).
- `pty.write` — escribe keystrokes (añade newline si falta).
- `pty.resize` — cambia rows/columns.
- `pty.terminate` — termina el grupo de procesos (TERM→KILL).

Implementación (fase 3): POSIX pty pair (`os.openpty`). En plataformas sin
PTY (Windows) `pty.start` devuelve `DEPENDENCY_ERROR` señalando `process.*`
(mismo patrón que el fallback de LSP). Registry por sesión en ToolContext.

### Shell
- `shell.exec`
- `shell.exec_stream`
- `shell.script`
- `shell.which`
- `shell.history`

---

## 2. Filesystem

### Read
- `fs.read`
- `fs.read_image` — view local PNG/JPEG/WebP pixels through the session's vision adapter;
  preserves an immutable artifact for history and the desktop image viewer. List folders
  first, then inspect up to four images per batch. Filesystem permissions apply.
- `fs.read_pdf_pages` — show pages of a PDF (local path or attached `artifact://`) as
  images, up to four per call, for charts, tables, layout, scans or pages beyond the
  20 prepared with the attachment. Renders are cached as derived artifacts.
- `fs.read_lines`
- `fs.read_binary`
- `fs.head`
- `fs.tail`
- `fs.stat`
- `fs.exists`

### Write
- `fs.write`
- `fs.write_binary`
- `fs.append`
- `fs.touch`
- `fs.truncate`

### Edit
- `fs.patch`
- `fs.replace`
- `fs.insert`
- `fs.apply_diff`

### Navigation
- `fs.list`
- `fs.tree`
- `fs.glob`
- `fs.walk`

### Search
- `fs.search_text`
- `fs.search_regex`
- `fs.search_files`
- `fs.find`
- `fs.locate`

### File Operations
- `fs.mkdir`
- `fs.copy`
- `fs.move`
- `fs.rename`
- `fs.delete`
- `fs.restore`
- `fs.chmod`
- `fs.chown`
- `fs.symlink`
- `fs.readlink`

### Compare
- `fs.diff`
- `fs.hash`
- `fs.compare`

### Watch
- `fs.watch`
- `fs.unwatch`

---

## 3. Search, Indexing & Retrieval

### Text Search
- `search.text`
- `search.regex`
- `search.fuzzy`
- `search.files`
- `search.symbol`
- `search.references`
- `search.definition`

### Semantic Search
- `search.semantic`
- `search.hybrid`
- `search.rerank`

### Indexes
- `index.create`
- `index.update`
- `index.delete`
- `index.status`
- `index.query`
- `index.rebuild`

### Embeddings
- `embedding.create`
- `embedding.batch`
- `embedding.compare`

---

## 4. Artifacts

### Artifact Lifecycle
- `artifact.create`
- `artifact.open`
- `artifact.read`
- `artifact.write`
- `artifact.update`
- `artifact.delete`
- `artifact.list`
- `artifact.metadata`
- `artifact.export`: copia los bytes originales de un artefacto de la sesión (por ejemplo, un adjunto) a un archivo real, para programas o subidas que necesitan una ruta. Pasa por la política de escritura y nunca sobrescribe.

### Artifact Search
- `artifact.search`
- `artifact.find`
- `artifact.slice`

### Artifact Transfer
- `artifact.import`
- `artifact.export`
- `artifact.download`
- `artifact.upload`

### Artifact Conversion
- `artifact.convert`
- `artifact.render`
- `artifact.preview`

---

## 5. Web Search & Retrieval

### Search
- `web.search`
- `web.search_news`
- `web.search_images`
- `web.search_videos`
- `web.search_academic`
- `web.search_code`

### Fetch
- `web.fetch`
- `web.open`
- `web.reload`
- `web.download`

### Navigation
- `web.links`
- `web.click`
- `web.back`
- `web.forward`

### Content Extraction
- `web.extract_text`
- `web.extract_markdown`
- `web.extract_metadata`
- `web.extract_tables`
- `web.find`

### Provenance
- `web.cite`
- `web.sources`

---

## 6. Browser Automation

### Browser Lifecycle
- `browser.launch`
- `browser.close`
- `browser.status`

### Tabs
- `browser.tab_open`
- `browser.tab_close`
- `browser.tab_list`
- `browser.tab_switch`

### Navigation
- `browser.open`
- `browser.reload`
- `browser.back`
- `browser.forward`

### Page Inspection
- `browser.snapshot`
- `browser.dom`
- `browser.accessibility_tree`
- `browser.screenshot`
- `browser.console`
- `browser.network`

### Interaction
- `browser.click`
- `browser.double_click`
- `browser.hover`
- `browser.type`
- `browser.fill`
- `browser.press`
- `browser.select`
- `browser.check`
- `browser.uncheck`
- `browser.drag`
- `browser.scroll`

### Forms & Files
- `browser.upload`
- `browser.download`
- `browser.submit`

### Script Execution
- `browser.evaluate`

### Cookies & Storage
- `browser.cookies_get`
- `browser.cookies_set`
- `browser.cookies_clear`
- `browser.storage_get`
- `browser.storage_set`
- `browser.storage_clear`

---

## 7. HTTP & API

### HTTP
- `http.request`
- `http.get`
- `http.post`
- `http.put`
- `http.patch`
- `http.delete`
- `http.head`
- `http.options`

### Streaming
- `http.stream`
- `http.sse`

### WebSockets
- `ws.connect`
- `ws.send`
- `ws.receive`
- `ws.close`

### GraphQL
- `graphql.query`
- `graphql.mutate`
- `graphql.introspect`

### OpenAPI
- `openapi.load`
- `openapi.inspect`
- `openapi.generate_tools`
- `openapi.call`

### Authentication
- `auth.status`
- `auth.login`
- `auth.logout`
- `auth.refresh`
- `auth.oauth_start`
- `auth.oauth_complete`

---

## 8. Code Execution & Development

### Code Execution
- `code.run`
- `code.compile`
- `code.eval`
- `code.repl`

### Testing
- `code.test`
- `code.test_file`
- `code.test_filter`
- `code.coverage`

### Quality
- `code.lint`
- `code.format`
- `code.typecheck`
- `code.audit`

### Language Intelligence
- `code.symbols`
- `code.definition`
- `code.references`
- `code.hover`
- `code.signature`
- `code.completions`
- `code.diagnostics`
- `code.rename_symbol`

### Parsing
- `code.parse_ast`
- `code.query_ast`
- `code.transform_ast`
- `code.tree_sitter_query`

### Build
- `code.build`
- `code.clean`

---

## 9. Git

### Repository
- `git.init`
- `git.clone`
- `git.status`
- `git.config`

### History
- `git.log`
- `git.show`
- `git.blame`

### Changes
- `git.diff`
- `git.add`
- `git.reset`
- `git.restore`
- `git.clean`

### Commits
- `git.commit`
- `git.amend`
- `git.revert`
- `git.cherry_pick`

### Branches
- `git.branch_list`
- `git.branch_create`
- `git.branch_delete`
- `git.checkout`
- `git.switch`

### Integration
- `git.merge`
- `git.rebase`
- `git.abort_merge`
- `git.abort_rebase`

### Remote
- `git.remote_list`
- `git.remote_add`
- `git.fetch`
- `git.pull`
- `git.push`

### Stash
- `git.stash`
- `git.stash_list`
- `git.stash_apply`
- `git.stash_pop`

### Worktrees
- `git.worktree_list`
- `git.worktree_add`
- `git.worktree_remove`

---

## 10. Source Control Platforms

### GitHub
- `github.repo_get`
- `github.repo_search`
- `github.issue_list`
- `github.issue_get`
- `github.issue_create`
- `github.issue_update`
- `github.issue_comment`
- `github.pr_list`
- `github.pr_get`
- `github.pr_create`
- `github.pr_update`
- `github.pr_review`
- `github.pr_comment`
- `github.pr_merge`
- `github.checks`
- `github.actions_list`
- `github.actions_logs`
- `github.actions_rerun`
- `github.release_list`
- `github.release_create`
- `github.artifact_download`

### GitLab
- `gitlab.project_get`
- `gitlab.issue_list`
- `gitlab.issue_get`
- `gitlab.issue_create`
- `gitlab.mr_list`
- `gitlab.mr_get`
- `gitlab.mr_create`
- `gitlab.pipeline_list`
- `gitlab.pipeline_logs`

### Bitbucket
- `bitbucket.repo_get`
- `bitbucket.issue_list`
- `bitbucket.pr_list`
- `bitbucket.pr_get`
- `bitbucket.pr_create`

---

## 11. Package Managers & Dependencies

### Generic Package API
- `package.search`
- `package.info`
- `package.install`
- `package.remove`
- `package.update`
- `package.outdated`
- `package.lock`
- `package.audit`

### JavaScript
- `npm.install`
- `npm.remove`
- `npm.run`
- `npm.audit`
- `pnpm.install`
- `pnpm.run`
- `yarn.install`
- `yarn.run`

### Python
- `pip.install`
- `pip.uninstall`
- `uv.add`
- `uv.remove`
- `uv.sync`
- `poetry.add`
- `poetry.remove`
- `poetry.install`

### Rust
- `cargo.add`
- `cargo.remove`
- `cargo.build`
- `cargo.test`

### Go
- `go.get`
- `go.mod_tidy`
- `go.build`
- `go.test`

### JVM
- `maven.build`
- `maven.test`
- `gradle.build`
- `gradle.test`

---

## 12. Databases

### Generic
- `db.connect`
- `db.disconnect`
- `db.query`
- `db.execute`
- `db.schema`
- `db.tables`
- `db.describe`
- `db.explain`

### Transactions
- `db.transaction_begin`
- `db.transaction_commit`
- `db.transaction_rollback`

### SQL Databases
- `sqlite.query`
- `postgres.query`
- `mysql.query`
- `mssql.query`

### NoSQL
- `mongodb.find`
- `mongodb.aggregate`
- `mongodb.insert`
- `mongodb.update`
- `mongodb.delete`

### Redis
- `redis.get`
- `redis.set`
- `redis.delete`
- `redis.scan`
- `redis.publish`
- `redis.subscribe`

### Vector Stores
- `vector.upsert`
- `vector.search`
- `vector.delete`
- `vector.collections`

---

## 13. Containers

### Docker
- `docker.info`
- `docker.images`
- `docker.pull`
- `docker.build`
- `docker.run`
- `docker.ps`
- `docker.inspect`
- `docker.logs`
- `docker.exec`
- `docker.stop`
- `docker.rm`
- `docker.networks`
- `docker.volumes`

### Generic Containers
- `container.create`
- `container.run`
- `container.exec`
- `container.logs`
- `container.inspect`
- `container.stop`
- `container.delete`

---

## 14. Kubernetes

- `k8s.contexts`
- `k8s.use_context`
- `k8s.namespaces`
- `k8s.get`
- `k8s.describe`
- `k8s.logs`
- `k8s.exec`
- `k8s.apply`
- `k8s.delete`
- `k8s.scale`
- `k8s.rollout_status`
- `k8s.rollout_restart`
- `k8s.events`
- `k8s.port_forward`

---

## 15. Cloud

### Generic
- `cloud.resources_list`
- `cloud.resource_get`
- `cloud.logs_query`
- `cloud.deploy`
- `cloud.destroy`
- `cloud.costs`
- `cloud.identity`

### AWS
- `aws.identity`
- `aws.s3_list`
- `aws.s3_get`
- `aws.s3_put`
- `aws.ec2_list`
- `aws.lambda_list`
- `aws.lambda_invoke`
- `aws.cloudwatch_logs`
- `aws.iam_inspect`

### GCP
- `gcp.identity`
- `gcp.storage_list`
- `gcp.storage_get`
- `gcp.storage_put`
- `gcp.compute_list`
- `gcp.functions_list`
- `gcp.logging_query`

### Azure
- `azure.identity`
- `azure.storage_list`
- `azure.storage_get`
- `azure.storage_put`
- `azure.compute_list`
- `azure.functions_list`
- `azure.logs_query`

---

## 16. Infrastructure as Code

### Terraform / OpenTofu
- `iac.init`
- `iac.validate`
- `iac.plan`
- `iac.apply`
- `iac.destroy`
- `iac.output`
- `iac.state_list`
- `iac.state_show`

---

## 17. Deployment Platforms

### Vercel
- `vercel.projects`
- `vercel.deploy`
- `vercel.deployments`
- `vercel.logs`

### Cloudflare
- `cloudflare.zones`
- `cloudflare.dns_list`
- `cloudflare.dns_update`
- `cloudflare.workers_deploy`

### Supabase
- `supabase.projects`
- `supabase.query`
- `supabase.functions`
- `supabase.logs`

### Firebase
- `firebase.projects`
- `firebase.deploy`
- `firebase.functions`
- `firebase.firestore_query`

---

## 18. Secrets & Credentials

### Secret Store
- `secret.exists`
- `secret.list`
- `secret.inject`
- `secret.rotate`
- `secret.delete`

### Credential Scopes
- `credential.request`
- `credential.status`
- `credential.revoke`

> Evitar `secret.get_plaintext` cuando sea posible.  
> El runtime debe inyectar secretos directamente a procesos y connectors.

---

## 19. Memory

### General Memory
- `memory.store`
- `memory.get`
- `memory.search`
- `memory.update`
- `memory.delete`
- `memory.list`

### Scoped Memory
- `memory.user_get`
- `memory.user_store`
- `memory.project_get`
- `memory.project_store`
- `memory.session_get`
- `memory.session_store`
- `memory.episodic_search`
- `memory.knowledge_search`

### Memory Administration
- `memory.forget`
- `memory.expire`
- `memory.provenance`

---

## 20. Context Management

- `context.inspect`
- `context.search_history`
- `context.retrieve`
- `context.compact`
- `context.summarize`
- `context.pin`
- `context.unpin`
- `context.list_pins`
- `context.artifacts`
- `context.token_usage`
- `context.trim`

---

## 21. Agents & Subagents

### Lifecycle
- `agent.spawn`
- `agent.status`
- `agent.list`
- `agent.wait`
- `agent.cancel`
- `agent.result`

### Communication
- `agent.message`
- `agent.broadcast`

### Delegation
- `agent.delegate`
- `agent.delegate_batch`

### Resource Control
- `agent.set_budget`
- `agent.set_tools`
- `agent.set_context`

---

## 22. Task Graph & Orchestration

### Tasks
- `task.create`
- `task.get`
- `task.list`
- `task.update`
- `task.start`
- `task.complete`
- `task.fail`
- `task.cancel`

### Dependencies
- `task.add_dependency`
- `task.remove_dependency`
- `task.blockers`

### Workflows
- `workflow.create`
- `workflow.run`
- `workflow.pause`
- `workflow.resume`
- `workflow.cancel`
- `workflow.status`

### Parallel Execution
- `workflow.parallel`
- `workflow.join`

### Verification (fase 3)
- `verify.plan` — decide qué verificar para un set de files cambiados
  (targeted tests vía test-map del repo index o convenciones, adjacent tests,
  escalada a la suite completa si cambió config/shared, risk level,
  discovered lint/typecheck/build commands).
- `verify.record` — persiste una unidad de evidencia de validación
  (`kind` ∈ test|lint|typecheck|build|schema|manual|custom,
  `result` ∈ passed|failed|error|skipped + command/summary/detail).
- `verify.evaluate` — evalúa el completion gate con la evidencia más
  reciente: `DONE | IMPLEMENTED_UNVERIFIED | PARTIAL | BLOCKED | FAILED`.
  Rechaza falso-éxito (marca de failure en la salida de un `passed`) y
  "no tests ran".

Notas: los tools clasifican como `state.read`/`state.write` (persistencia de
evidencia local, sin efecto externo) y nunca piden approval. El harness
re-evalúa el gate tras cada turno con activity (evento `CompletionGateEvaluated`);
un claim de "fixed" sin evidencia pasada queda como `IMPLEMENTED_UNVERIFIED`/
`PARTIAL`. Sin CLI dedicada: la evidencia queda en el trace de la sesión.

---

## 23. Time, Scheduling & Watches

### Time
- `time.now`
- `time.parse`
- `time.format`
- `time.convert_timezone`
- `time.duration`

### Scheduling
- `schedule.create`
- `schedule.get`
- `schedule.list`
- `schedule.update`
- `schedule.pause`
- `schedule.resume`
- `schedule.cancel`

### Condition Watches
- `watch.create`
- `watch.get`
- `watch.list`
- `watch.update`
- `watch.cancel`

---

## 24. Communication

### Email
- `email.search`
- `email.get`
- `email.thread_get`
- `email.draft`
- `email.send`
- `email.reply`
- `email.forward`
- `email.archive`
- `email.delete`
- `email.labels`

### Slack
- `slack.channels`
- `slack.search`
- `slack.messages`
- `slack.thread`
- `slack.send`
- `slack.reply`
- `slack.react`

### Discord
- `discord.channels`
- `discord.messages`
- `discord.send`
- `discord.reply`

### Microsoft Teams
- `teams.channels`
- `teams.messages`
- `teams.send`
- `teams.reply`

### Telegram
- `telegram.chats`
- `telegram.messages`
- `telegram.send`

### SMS
- `sms.send`
- `sms.history`

---

## 25. Productivity & SaaS

### Calendar
- `calendar.list`
- `calendar.events`
- `calendar.event_get`
- `calendar.availability`
- `calendar.create`
- `calendar.update`
- `calendar.delete`
- `calendar.respond`

### Contacts
- `contacts.search`
- `contacts.get`
- `contacts.create`
- `contacts.update`

### Drive / Storage
- `drive.search`
- `drive.list`
- `drive.get`
- `drive.upload`
- `drive.download`
- `drive.move`
- `drive.delete`

### Docs
- `docs.get`
- `docs.create`
- `docs.update`
- `docs.append`
- `docs.export`

### Sheets
- `sheets.get`
- `sheets.read`
- `sheets.write`
- `sheets.append`
- `sheets.create`
- `sheets.export`

### Notion
- `notion.search`
- `notion.page_get`
- `notion.page_create`
- `notion.page_update`
- `notion.database_query`

### Linear
- `linear.search`
- `linear.issue_get`
- `linear.issue_create`
- `linear.issue_update`
- `linear.comment`

### Jira
- `jira.search`
- `jira.issue_get`
- `jira.issue_create`
- `jira.issue_update`
- `jira.comment`

### Asana
- `asana.search`
- `asana.task_get`
- `asana.task_create`
- `asana.task_update`

---

## 26. Documents

### Generic Documents
- `document.parse`
- `document.create`
- `document.edit`
- `document.convert`
- `document.extract`
- `document.render`

### PDF
- `pdf.read`
- `pdf.create`
- `pdf.edit`
- `pdf.merge`
- `pdf.split`
- `pdf.render`
- `pdf.extract_text`
- `pdf.extract_tables`

### DOCX
- `docx.read`
- `docx.create`
- `docx.edit`
- `docx.convert`

### Spreadsheets
- `spreadsheet.read`
- `spreadsheet.write`
- `spreadsheet.formula`
- `spreadsheet.chart`
- `spreadsheet.create`
- `spreadsheet.export`

### Presentations
- `slides.read`
- `slides.create`
- `slides.edit`
- `slides.render`
- `slides.export`

### CSV / Tabular
- `csv.read`
- `csv.write`
- `csv.transform`

### Archives
- `archive.create`
- `archive.list`
- `archive.extract`

---

## 27. Images

### Inspection
- `image.inspect`
- `image.metadata`
- `image.ocr`
- `image.detect_objects`

### Manipulation
- `image.resize`
- `image.crop`
- `image.rotate`
- `image.convert`
- `image.compress`

### Generation & Editing
- `image.generate`
- `image.edit`
- `image.inpaint`
- `image.outpaint`
- `image.upscale`

---

## 28. Audio

- `audio.inspect`
- `audio.transcribe`
- `audio.translate`
- `audio.generate`
- `audio.synthesize`
- `audio.convert`
- `audio.trim`
- `audio.merge`

---

## 29. Video

- `video.inspect`
- `video.metadata`
- `video.extract_frames`
- `video.transcribe`
- `video.generate`
- `video.edit`
- `video.trim`
- `video.merge`
- `video.convert`

---

## 30. Computer Use / GUI

### Screen
- `computer.screenshot`
- `computer.screen_info`

### Mouse
- `computer.click`
- `computer.double_click`
- `computer.move_mouse`
- `computer.drag`
- `computer.scroll`

### Keyboard
- `computer.type`
- `computer.key`
- `computer.hotkey`

### Windows
- `computer.windows`
- `computer.focus_window`
- `computer.move_window`
- `computer.resize_window`
- `computer.close_window`

### Clipboard
- `clipboard.read`
- `clipboard.write`
- `clipboard.clear`

### Accessibility
- `computer.accessibility_tree`
- `computer.accessibility_action`

---

## 31. Math & Deterministic Reasoning

### Calculator
- `math.calculate`
- `math.evaluate`

### Symbolic Math
- `math.symbolic`
- `math.solve`
- `math.simplify`
- `math.integrate`
- `math.differentiate`

### Statistics
- `stats.describe`
- `stats.correlation`
- `stats.regression`
- `stats.hypothesis_test`

### Optimization
- `optimizer.solve`
- `optimizer.linear`
- `optimizer.integer`

### SAT / SMT
- `solver.sat`
- `solver.smt`
- `solver.constraints`

---

## 32. Structured Data Utilities

### JSON
- `json.parse`
- `json.stringify`
- `json.query`
- `json.patch`
- `json.diff`

### YAML
- `yaml.parse`
- `yaml.stringify`
- `yaml.validate`

### XML
- `xml.parse`
- `xml.query`
- `xml.validate`

### JQ / JSONPath
- `jq.run`
- `jsonpath.query`

### Regex
- `regex.test`
- `regex.match`
- `regex.replace`
- `regex.explain`

---

## 33. Validation

- `validate.json_schema`
- `validate.openapi`
- `validate.xml_schema`
- `validate.yaml`
- `validate.syntax`
- `validate.contract`
- `validate.assert`
- `validate.url`
- `validate.email`
- `validate.datetime`

---

## 34. Security

### Policy
- `policy.check`
- `policy.explain`
- `policy.list`
- `policy.evaluate_action`

### Permissions
- `permission.check`
- `permission.request`
- `permission.grant`
- `permission.revoke`
- `permission.list`

### Sandbox
- `sandbox.create`
- `sandbox.exec`
- `sandbox.inspect`
- `sandbox.reset`
- `sandbox.destroy`

### Security Scanning
- `security.scan_dependencies`
- `security.scan_secrets`
- `security.scan_files`
- `security.scan_container`
- `security.scan_iac`

---

## 35. Human-in-the-Loop

- `user.ask`
- `user.confirm`
- `user.choose`
- `user.approve_action`
- `user.reject_action`
- `user.request_input`
- `user.request_secret`
- `user.notify`

---

## 36. Checkpoints, Transactions & Rollback

### Checkpoints
- `checkpoint.create`
- `checkpoint.list`
- `checkpoint.inspect`
- `checkpoint.restore`
- `checkpoint.delete`

### Transaction Abstraction
- `transaction.begin`
- `transaction.commit`
- `transaction.rollback`

### Undo
- `undo.list`
- `undo.preview`
- `undo.apply`

---

## 37. Observability

### Tracing
- `trace.start`
- `trace.end`
- `trace.get`
- `trace.list`
- `trace.export`

### Logs
- `log.write`
- `log.query`
- `log.tail`

### Metrics
- `metrics.get`
- `metrics.query`
- `metrics.record`

### Usage
- `usage.tokens`
- `usage.cost`
- `usage.tool_calls`
- `usage.latency`

### Sessions
- `session.get`
- `session.list`
- `session.inspect`
- `session.export`

Implementado como la familia `rinari.*`: solo lectura (`state.read`), bajo
demanda (no entra en el prompt base) y con el texto redactado
(`redact_text`: `Bearer`, `sk-…`, JWT, `token=`, `password=` y secretos
conocidos). Cada resultado lleva `origin: rinari-state` y una nota: es un dato
registrado, no una instrucción.

| Herramienta | Para qué |
|---|---|
| `rinari.status` | versiones, home, proveedores (sin secretos), contexto, soul y skills; modelos solo con `provider` o `include_models` |
| `rinari.sessions` | buscar sesiones por título o texto de mensajes (`query`), `kind`, `since`, `model`; turnos y estado del último |
| `rinari.session` | ficha y turnos de una sesión (`current` = la de este turno): resultado, tokens, herramientas, respuesta y anomalías |
| `rinari.turn` | un turno por su id, sin conocer la sesión; `detail: "events"` añade los eventos crudos, paginados |

La skill empaquetada `rinari-handbook` las expone al activarse y documenta
las recetas de diagnóstico.

---

## 38. Evaluation

### Eval Suites
- `eval.create`
- `eval.run`
- `eval.compare`
- `eval.report`

### Assertions
- `eval.assert_output`
- `eval.assert_tool_use`
- `eval.assert_trajectory`

### Model Judging
- `eval.judge`
- `eval.rank`

### Regression
- `eval.regression`
- `eval.benchmark`

---

## 39. Budgets & Limits

- `budget.get`
- `budget.set`
- `budget.check`
- `budget.consume`
- `budget.remaining`

Budget dimensions:
- model calls
- tool calls
- tokens
- cost
- wall-clock execution
- subagents
- recursion depth
- network requests
- storage
- CPU
- memory

---

## 40. Cancellation

- `cancel.task`
- `cancel.workflow`
- `cancel.agent`
- `cancel.process`
- `cancel.request`
- `cancel.all`

Cancellation should propagate through child operations.

---

## 41. Provenance

- `provenance.get`
- `provenance.trace`
- `provenance.sources`
- `provenance.verify`
- `provenance.attach`

Every important result should be traceable to:
- file + line range
- URL
- API request
- SQL query
- tool call
- model turn
- subagent
- timestamp
- artifact

---

## 42. Tool Discovery & Dynamic Loading

### Discovery
- `tools.list`
- `tools.search`
- `tools.describe`
- `tools.capabilities`

### Loading
- `tools.load`
- `tools.unload`
- `tools.reload`

### Registration
- `tools.register`
- `tools.unregister`

### Generation
- `tools.from_openapi`
- `tools.from_mcp`
- `tools.from_plugin`
- `tools.compose`

---

## 43. MCP

- `mcp.servers`
- `mcp.connect`
- `mcp.disconnect`
- `mcp.tools`
- `mcp.resources`
- `mcp.prompts`
- `mcp.call`
- `mcp.read_resource`
- `mcp.subscribe`

---

## 44. Plugins & Connectors

### Plugins
- `plugin.search`
- `plugin.install`
- `plugin.uninstall`
- `plugin.enable`
- `plugin.disable`
- `plugin.update`
- `plugin.info`
- `plugin.list`

### Connectors
- `connector.list`
- `connector.connect`
- `connector.disconnect`
- `connector.status`
- `connector.invoke`

---

## 45. Tool Composition

- `composition.create`
- `composition.run`
- `composition.inspect`
- `composition.save`
- `composition.delete`

Example composed tool:

```text
get_project_releases
  -> web.fetch
  -> web.extract_text
  -> json.parse
  -> search.filter
```

---

## 46. Notifications

- `notification.send`
- `notification.list`
- `notification.dismiss`

Targets may include:
- CLI
- desktop
- email
- Slack
- webhook
- push notification

---

## 47. Networking

- `network.dns_lookup`
- `network.ping`
- `network.traceroute`
- `network.port_check`
- `network.connections`
- `network.download`
- `network.upload`

---

## 48. Cryptography & Hashing

- `crypto.hash`
- `crypto.verify_hash`
- `crypto.random`
- `crypto.uuid`
- `crypto.encrypt`
- `crypto.decrypt`
- `crypto.sign`
- `crypto.verify_signature`

Sensitive cryptographic operations should require explicit permissions.

---

## 49. Compression & Encoding

- `encoding.base64_encode`
- `encoding.base64_decode`
- `encoding.url_encode`
- `encoding.url_decode`
- `encoding.hex_encode`
- `encoding.hex_decode`
- `compression.gzip`
- `compression.gunzip`
- `compression.zip`
- `compression.unzip`

---

## 50. Location, Maps & Timezones

- `location.geocode`
- `location.reverse_geocode`
- `location.distance`
- `location.route`
- `location.nearby`
- `timezone.lookup`

---

## 51. Finance & Market Data

- `finance.quote`
- `finance.history`
- `finance.exchange_rate`
- `finance.company_info`
- `finance.market_status`
- `finance.news`

Mutating financial operations should live behind separate high-risk connectors.

---

## 52. Weather

- `weather.current`
- `weather.forecast`
- `weather.historical`
- `weather.alerts`

---

## 53. Knowledge & Reference

- `knowledge.lookup`
- `knowledge.search`
- `knowledge.cite`
- `knowledge.entities`

---

## 54. Models

### Model Routing
- `model.list`
- `model.info`
- `model.route`
- `model.invoke`

### Embedding / Reranking
- `model.embed`
- `model.rerank`

### Model Budgeting
- `model.estimate_cost`
- `model.estimate_tokens`

---

## 55. Prompt & Template Resources

- `prompt.list`
- `prompt.get`
- `prompt.render`
- `prompt.validate`

- `template.list`
- `template.get`
- `template.render`

---

## 56. Cache

- `cache.get`
- `cache.set`
- `cache.delete`
- `cache.clear`
- `cache.stats`

---

## 57. Queues & Events

### Queue
- `queue.publish`
- `queue.consume`
- `queue.peek`
- `queue.ack`
- `queue.retry`

### Event Bus
- `event.emit`
- `event.subscribe`
- `event.unsubscribe`
- `event.history`

---

## 58. Webhooks

- `webhook.create`
- `webhook.list`
- `webhook.update`
- `webhook.delete`
- `webhook.test`

---

## 59. CLI Interaction

- `cli.print`
- `cli.progress`
- `cli.table`
- `cli.prompt`
- `cli.select`
- `cli.confirm`
- `cli.open_editor`
- `cli.open_url`
- `cli.copy`

---

## 60. Tool Contract Metadata

Every tool should declare metadata similar to:

```ts
type ToolDefinition = {
  name: string
  description: string
  inputSchema: JSONSchema
  outputSchema: JSONSchema

  permissions: string[]
  risk: "low" | "medium" | "high" | "critical"
  sideEffects: boolean
  idempotent: boolean
  requiresApproval: boolean

  networkScope?: string[]
  filesystemScope?: string[]
  secretScopes?: string[]

  timeoutMs?: number
  retryPolicy?: RetryPolicy
  maxOutputBytes?: number
}
```

Enforcement (runtime contract, not decoration):

```text
outputSchema, when declared, is enforced: a violating result returns
VALIDATION_FAILED with no retry — a contract bug, not a transient
failure. Retries (at most 2 attempts) apply only to idempotent tools
whose sideEffects are not communication / financial / credential, and
only for NETWORK_ERROR / RATE_LIMITED errors flagged retryable;
TIMEOUT is never retried. timeoutMs narrows a cooperative deadline_at
for the call (sync handlers cannot be preempted). A per-tool
maxOutputBytes caps the observation before the global bound applies.
```

---

## 61. Standard Tool Result

Recommended common result envelope:

```ts
type ToolResult<T> = {
  ok: boolean
  data?: T

  error?: {
    code: string
    message: string
    retryable: boolean
    details?: unknown
  }

  artifacts?: ArtifactRef[]

  metadata: {
    toolCallId: string
    durationMs: number
    timestamp: string
    sideEffects?: string[]
    provenance?: ProvenanceRef[]
  }
}
```

What the model actually receives (`to_model_text`) is this envelope
serialized as bounded JSON: `{ok, tool?, data | error{code, message,
retryable, details?}, artifacts[] (URIs), truncated?, duration_ms?}`,
cut at ~2KB inline with an `[output truncated]` mark. Large outputs
spill to artifacts upstream; continue reading them with
`artifact.read`.

---

## 62. Standard Error Classes

- `INVALID_ARGUMENT`
- `NOT_FOUND`
- `ALREADY_EXISTS`
- `PERMISSION_DENIED`
- `AUTH_REQUIRED`
- `AUTH_EXPIRED`
- `RATE_LIMITED`
- `TIMEOUT`
- `NETWORK_ERROR`
- `DEPENDENCY_ERROR`
- `CONFLICT`
- `RESOURCE_EXHAUSTED`
- `SANDBOX_VIOLATION`
- `POLICY_DENIED`
- `APPROVAL_REQUIRED`
- `APPROVAL_DENIED`
- `CANCELLED`
- `PARTIAL_FAILURE`
- `VALIDATION_FAILED`
- `TOOL_NOT_FOUND`
- `UNKNOWN`

---

## 63. Recommended Tool Loading Strategy

Implemented as budgeted per-request exposure: each model request
carries core tools + explicitly activated tools + recently used tools
(last 16, for continuity) — never the whole registry. The schema
budget is 96 tool schemas / ~32.000 estimated schema tokens. Core and
activated tools are never dropped to fit (metrics flag `over_budget`
instead); only the recent-tools tail is cut. `capability.search` is
always exposed so the model can always recover the on-demand
ecosystem. The intended flow is search, then activate exactly what the
task needs:

The schema cost counts what providers receive per tool: name,
description and input schema (counting only the schema under-estimated
it by ~40%, so the budget never bound). Besides browser/MCP/OpenAPI/
plugins, rarely used native packs are on demand (`always_loaded=False`):
`documents.*`, `rinari.*`, `lsp.*`, `pty.*`, `context.*`, `ssh.*`,
`artifact.metadata` and `artifact.export`. `artifact.read` stays core
because every spilled observation points at it. On-demand tools become
visible through `capability.search` with `load=true`,
`capability.activate`, a skill that requires them, a subagent definition
that names them, or by being called by exact name (the registry resolves
calls; the tool then stays visible as recently used). A destination-bound
remote operation keeps `ssh.inspect` core, since it is its only tool.

- `capability.search` — rank capabilities across native, plugin, MCP,
  OpenAPI and the browser fallback (typed connector first, browser
  DOM last; exact name matches win outright).
- `capability.activate` — expose tools by exact name, scope `turn`
  (decays after ~4 model rounds, `ttl_rounds` 1–32) or `session`.
- `capability.deactivate` — drop previously activated tools.

### Per-turn execution plan

Each turn's tool calls are planned into ordered groups: consecutive
read-only + idempotent + non-network calls may share a group, every
other call runs alone, and groups always run in order with
observations reported back in the original call order. Execution is
still serial (policy/approval/event ordering); the plan is computed
and traced as `execution_plan` on the model-invoked event, ready for
a concurrent executor.

### Always Loaded (core)
- `fs.*`
- `shell.*`
- `search.*`
- `artifact.*`
- `capability.search`
- `capability.activate`
- `capability.deactivate`
- `user.*`
- `context.*`
- `budget.*`
- `cancel.*`

### On Demand (lazy until activated)
- browser (`browser.*`)
- web
- git
- GitHub / GitLab
- databases
- containers
- cloud
- documents
- multimedia
- communication
- productivity
- MCP servers (`mcp.*`)
- OpenAPI services (`api.*`)
- plugins (`plugin.*`)

### Dynamic
- project-specific plugins
- user plugins
- temporary composed tools

---

## 64. Preferred Capability Order

When several interfaces can solve the same task:

```text
typed API
  ↓
native connector
  ↓
MCP / OpenAPI
  ↓
filesystem / structured local API
  ↓
browser DOM
  ↓
accessibility tree
  ↓
computer vision + coordinates
  ↓
raw shell escape hatch
```

Prefer the most structured, deterministic and least risky interface available.
# Structured desktop questions

`user.ask` requests clarification through a host-provided interaction channel.
It uses the common ToolRuntime and is available in PLAN without authorizing
filesystem mutations. Unsupported hosts omit it from model exposure.
The [interactive workspace contract](desktop/06-interactive-workspace.md)
defines question schemas, explicit replies, skips, cancellation and recovery.


## Implemented SSH inspection contract (2026-09-10)

`ssh.inspect({target_id, section})` reads a registered Linux destination through OpenSSH.
Default section: `hardware`. Sections: `system`, `cpu`, `memory`, `disks`, `gpu` (NVIDIA), or `hardware` to collect
all sections over one connection and report individual command outcomes. A unique
registered name or a static alias from ~/.ssh/config can identify a destination.
Static aliases reuse the configured identity and known_hosts with strict checking.
Match, Include and proxy configurations require a registered target or the shell;
the resolver never executes configuration directives.
Only fixed commands under
`/usr/bin` run; no model-controlled command string, paths, environment or connection flags.
Registered destinations use installation-owned identities and immutable
IP/port/user/Ed25519 host-key records; aliases use existing OpenSSH files.
Every call uses Tool Runtime network policy/approval, a network guard, bounded output,
cancellation and deadline. Strict host-key checking never learns a replacement key.
Transport or remote-command nonzero exits are failures, not successful inspection.

A destination-bound Engine operation registers only this tool with its target ID pinned
in the schema and handler; local shell/filesystem/extension/agent tools and execution
hooks are unavailable for that operation. This is an initial read-only Linux inspection
contract, not general remote shell or a PC runner. See `durable-operations.md` for dispatch.

### `ssh.run` (2026-10-08)

`ssh.run({target_id, script, shell?, timeout_s?})` runs a script on the same
registered destinations and static aliases, over the same pinned connection
(`StrictHostKeyChecking=yes`, no agent, no forwarding, `BatchMode`). The script
travels on stdin to `bash -s` (default) or `sh -s`: it is never parsed by a local
shell and needs no quoting, which replaces the fragile `shell.exec "ssh host '...'"`
(cmd → ssh → bash nesting). CRLF line endings are normalized to LF; scripts are
capped at 64 KiB; `timeout_s` defaults to 60 and accepts up to 600. Output is
bounded like `shell.exec` (stdout/stderr, `truncated`, full capture spilled to
artifacts) and streams live to the activity. A non-zero exit is the script's
answer (`ok=true`, `exit_code`); exit 255 with an OpenSSH diagnostic is a
transport failure (`AUTH_REQUIRED` for host key or authentication, otherwise
`NETWORK_ERROR`). A timeout or cancellation does not guarantee that the remote
side stopped.

Policy: it classifies as two actions, `network.outbound` (mode `send`, target the
destination host) and `shell.exec` (`ssh <host> '<script>`), so it is never freer
than running ssh through the shell: read-only denies it, a peer-originated turn
denies it, workspace allows it on the LAN and asks for an internet host, and every
shell rule applies.
An unknown destination is rejected before any approval is requested.
Destination-bound operations do not register it.

## Tool efficiency contract (2026-09-10, local implementation)

CLI inspection and Engine `tool.list` share the 106 built-in definitions. Inspection
does not imply a tool has its required session host, platform support or credentials.
Dynamic integrations are still loaded when constructing the session registry.

`capability.search` accepts `load=true` to activate matches in the same call.
`loaded` identifies activated schemas; `load_error` explains absent exposure or an
exceeded budget. `capability.activate` rejects an over-budget request before mutation.
No-match searches return no fabricated fallback tool. Optional integration loading
failures leave a source diagnostic instead of silently disappearing.

ToolRuntime preserves inherited deadlines and never automatically replays tools
with side effects. HTTP retries remain method-aware in the HTTP adapter, without
an additional outer retry. Long model observations remain valid JSON.

`fs.write` and `fs.patch` accept `expected_hash` (SHA-256, or `missing` for creation)
and replace a file atomically. Reads return a hash only when the content is complete.
`fs.read_lines` streams to the requested range and returns `N| text` rows in `text`;
unknown total line counts are null.
`fs.list` supports offset/limit/revision and reports a next offset. `fs.diff` refuses
to claim a complete comparison when either input exceeds its reading limit.

`process.output` accepts and returns separate stdout/stderr character cursors with
`max_chars` and `has_more`. `pty.write(submit_line=false)` sends literal input.
Web reads reuse bounded session snapshots for 30 seconds; `refresh=true` bypasses
reuse, and downloads always refresh. Network policy remains enforced on cache hits.

### Contracts and bounded composition

All 106 registered built-ins now expose input and output schemas. Output fields
are optional for alternative adapter success shapes and allow additive fields;
extension-owned schemas are never replaced. Known platform/service prerequisites
filter model exposure; inspection availability is not a permission grant.

Side-effecting built-ins may expose `request_id`: repeating the same arguments in
the same live runtime reuses a receipt; changed arguments conflict. Receipts retain
the last 256 requests and are not durable exactly-once guarantees across restarts.

- `fs.read` / `fs.stat` accept `paths` (1–16), authorize every path first, and use
  up to four pure read workers with ordered per-file results.
- `fs.patch(files=[{path, expected_hash?, edits:[{old_string,new_string}]}])`
  validates all files/permissions before mutation. Each file is bounded to 1 MiB.
  Failures trigger conflict-aware rollback with explicit restoration status;
  this is not an OS-level multi-file transaction.
- `fs.glob` / `search.files` share traversal, ignore and pagination semantics;
  `include_dirs=true` also matches folders.
  Literal and regex searches share bounded workers; regex runs in a cancellable
  subprocess, with ripgrep when available and a bounded Python fallback.
- `shell.exec` and `process.start` accept `argv` for literal arguments.
  `shell.exec(background=true)` starts the existing process service. Process cwd
  defaults to the session, stdin is closed, and `process.list(running_only=true)`
  filters completed handles.
  For SSH, keep the remote program as one literal argv element so the local
  shell never expands remote variables. For example,
  `{"argv":["ssh","logs-host","awk '{print $1}' /var/log/app.log"]}` sends
  `$1` unchanged to the remote command parser.
- PTY reads use the pump buffer with character cursors, avoiding competing reads
  from the descriptor. Termination escalates TERM to KILL. Windows sessions omit
  the POSIX PTY tools and use process tools; ConPTY is not implemented.
- Git status preserves NUL-delimited filenames/renames, diff returns patches by
  default, and log returns structured commits with pagination.
- Web results carry `source_id`; derived reads accept that ID and reuse the exact
  bounded snapshot. `web.sources` without URLs lists cached provenance without
  requests. Evicted IDs return a clear error. `web.search(queries=[...])` groups
  up to four queries. SSE supports explicit `last_event_id` resume.
- Browser launch reuses an active connection. Semantic snapshots expose backend
  node IDs usable for clicks/drag points. Existing console/network event drains
  remain incremental. Arbitrary page execution still uses browser policy.
- Memory updates accept optimistic `expected_version` (and user records expose a
  monotonic `revision` for Gateway panel compare-and-swap). Repeated identical
  episodic records deduplicate persistently. General chats do not inherit project
  memory. The Engine Protocol's optional `personal_memory_v1` capability exposes
  only user memory list/search/get/remember/update/forget to an authenticated
  owner panel; it is not a second storage authority.
- Context retrieval accepts a token budget; pins preserve absolute file origin,
  list pagination and missing-file state.
- Verification records include a bounded workspace metadata revision; a changed
  workspace invalidates older evidence. This detects freshness, not fabrication
  of model-declared evidence or adversarial preservation of file metadata.
- LSP avoids unchanged document notifications, bounds document reads, and observes
  cancellation/deadlines. Columns explicitly use UTF-16; callers can set
  `column_encoding=unicode` to convert a Unicode code-point input column.
  `lsp.diagnostics(include_state=true)` distinguishes waiting/received diagnostics
  and whether the reported version matches. Stale versioned notifications are rejected.
- Agent wait accepts several IDs and returns when one finishes; state mutations
  use the shared receipts. Skill activation/deactivation is repeat-safe.

General tool-call execution remains serial to preserve shared policy/budget/event
ordering; parallelism is limited to the authorized pure filesystem batch. Browser
console/network events are drained rather than backed by a durable cursor store.
Web snapshots and receipts expire with their live runtime. Dynamic MCP/plugin/
OpenAPI support still depends on configured services and credentials.

Partial failures preserve their data in model observations. Structured lists and
nested objects exceeding the inline limit spill to a complete JSON artifact;
redaction applies before both the preview and artifact are emitted. SSH failures
retain partial sections and classify authentication, host-key and transport errors.

## Channel tools and visual references (Gateway integration)

A trusted operation host may supply a channel binding. `artifact.import` imports an
existing authorized local file or a pinned SSH destination file by streaming, without
interpreting visual content. The record retains MIME, size, SHA-256 and a stable URI.
`channel.send_attachment`, `channel.reply` and `channel.delivery_get` are discovered
through capability.search and use the normal runtime for policy, cancellation,
classification and events. A channel host is mandatory and is removed from child
agent contexts. Remote jobs gain these capabilities without gaining local shell tools.

The host owns durable delivery fingerprints and receipts. Runtime deduplication is
not delivery persistence. On an uncertain outcome, query the receipt; never generate
a replacement or automatically send again. Receiving an image does not authorize video
generation. Visual provider content is materialized only at the adapter boundary from
validated, session-scoped image references; events/history contain references, not
base64. Original files remain distinct from reduced visual inputs.

## Peer messaging between sessions (Boards)

`session.peers` and `session.send` let the agent of one session message the
agent of another session of the same board. They exist only when a trusted
host binding is present (the session is a member of an enabled peer group
registered by the desktop through `session.peer_group.set`); subagents and
remote turns never receive them. Both are discovered through
`capability.search` and run through the normal runtime.

`session.send` is classified as `session.message` with the destination as
target: policy asks the owner per destination (`allow_session` binds exactly
to that target and lives only for the engine process), denies in read-only
and refuses a missing target. The engine, never the model, resolves the source
session, group, chain and hop. Limits: 32 000 chars, 5 sends per turn, 3 hops
per chain (`PEER_LOOP`), 20 deliveries per chain; identical sends in the same
turn are deduplicated.

A delivery is accepted, not executed: the receiver starts its own turn when
idle (queued otherwise) with the text wrapped as content from another agent,
`origin.kind = "peer"` persisted on the message and `memory_origin =
"automation"`. In that turn the runtime enforces a provenance ceiling in code:
`fs.write`, `shell.exec`, `process.local`, `browser.mutate`,
`network.outbound`, `mcp.call`, `state.write`, `git.local`, `agent.spawn`,
`agent.message` and `agent.synthesize` fail with `POLICY_DENIED` without
prompting. The owner turns a message into a real task by forwarding it
(`session.peer_message.forward`, origin `user`). See
`docs/desktop/09-peer-messaging.md` for the protocol contract.

### Browser connection reliability

Browser tools are discoverable with `capability.search` (`query: browser`,
`load: true`). Search before concluding that browser testing is unavailable.
An HTTP 200 is server evidence, not a functional or visual browser test.

`browser.launch` respects an explicit executable or configured CDP endpoint.
Automatic discovery checks PATH, then Windows installation directories and App
Paths registry entries for Edge/Chrome. It uses a dedicated session profile.
Each runtime gets a unique profile directory below its session directory. Code
creates a runtime per turn and closes its owned browser at turn end, including
when SessionEnd hooks fail. This avoids Chrome exit 21 from a prior runtime's
profile lock. External browser connections are disconnected without terminating
the external browser. A new turn starts fresh browser state.
Launch/connect results and failures include bounded diagnostics (selected
executable, last connection error and up to 16 KiB of browser stderr).

Idle CDP connections remain open independently of command timeouts. An explicit
`browser.connect` refreshes target sessions and invalidates old element IDs;
obtain a new snapshot before acting. Recovery never replays clicks or typing.
`browser.launch` cleans up a dead managed browser before relaunching, while
closing an external connection does not terminate its browser process.

Real local-browser regression (temporary profile, loopback page, 31-second idle,
keyboard, capture, console, reconnect and relaunch): set
`RINARI_TEST_REAL_BROWSER=1` and run
`uv run pytest tests/e2e/test_browser_windows.py -q -s`.


## Resultados recuperables y concurrencia del Engine

El contrato de entrega, configuración y catálogo con elegibilidad explícita está en [tool-results.md](tool-results.md). La serialización de resultados no recorta evidencia; el Engine proyecta resultados recuperables dentro del presupuesto de la ronda.
