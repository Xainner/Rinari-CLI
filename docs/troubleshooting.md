# Troubleshooting

Guide de resolución de problemas comunes de Rinari. Todos los comandos se
ejecutan con `uv run rinari ...` (o `rinari` si el entorno ya está activo).

## Punto de partida

```bash
rinari doctor
rinari status
rinari version
```

`doctor` nunca muta credenciales ni estado; `status` muestra el contexto
operativo (proveedor, modelo, contexto, perfil, proyecto, branch).

## Config

### Error al arrancar: `Legacy inline profiles: ...`

Tu `~/.rinari/config.toml` usa el layout v1 (tablas `[default]` y
`[profile.<name>]` con `api_key` inline). Migración determinista con backup:

```bash
rinari config migrate --dry-run   # ver el plan sin tocar nada
rinari config migrate             # aplica: crea config.toml.bak-<stamp>
```

Después de migrar, recrea los proveedores con los comandos que imprime
(los valores de API key solo existen en el backup; `net`/`sat` usan
`--api-key-env <VAR>`).

### `Config key X unknown key(s)`

La clave no existe en el schema actual. Revisa `rinari config list` para las
claves válidas. Si el valor pertenece a un proveedor (endpoint, key), usa
`rinari providers add` en lugar de config.

### `User config ... is not valid TOML`

Corrija la sintaxis TOML a mano (el comando no reescribe archivos corruptos
para no borrar datos).

### `Unknown profile: X`

`config set profile <nombre>` debe apuntar a un perfil builtin (`safe`,
`read-only`, `workspace`, `full-access`) o a `~/.rinari/profiles/<nombre>.toml`.
`rinari profiles list` muestra los disponibles.

## Trust

### El proyecto no es confiado

Un proyecto sin trust no carga instrucciones, skills/plugins/MCP locales, ni
hooks. Verificar y añadir:

```bash
rinari trust status
rinari trust add
```

### `revalidation-required`

El fingerprint del proyecto cambió (rebase, limpieza de worktree, etc.).
Revise los cambios y re-autorice con `rinari trust status` / `trust add`.

## Providers y modelos

### `authentication required` / `authentication expired`

```bash
rinari providers list            # estado por proveedor
rinari providers add <tipo> --name <alias> --endpoint <url> --api-key-env <VAR>
rinari providers login <alias>
```

Cambiar de proveedor/modelo (`provider use` / `model use`) **nunca** borra
configuración previa; cada proveedor recuerda su último modelo.

## Red

Las acciones de red se rigen por policy (`network.mode` + reglas allow/deny),
no por el modelo:

```bash
rinari network status
rinari network test <host>
rinari network allow <host>      # corta el ask para ese host
rinari network history
```

## Sesiones

### `resume` muestra warnings

El reconciler re-verifica hechos duraderos (proyecto, branch, dirty,
proveedor, trust, cwd). Cada warning tiene `action` sugerida en el JSON
(`--json`). Nada se corrige en silencio excepto re-registrar la row del
proyecto.

### Perder contexto largo

El contexto se compacta por presión (`context.compact_at_percent`), nunca
recortando a la conversación persistida. Los eventos `ContextCompacted`
quedan en la traza; revise con `rinari logs`.

## Estado, logs y métricas

- Estado local: `$RINARI_HOME` (por defecto `~/.rinari`).
- Trace por sesión: `rinari logs tail`, `rinari logs search <tipo>`,
  `rinari trace`.
- Métricas agregadas: `rinari metrics all` / `metrics success` /
  `metrics latency`.

## Portabilidad

```bash
rinari export session            # documento portable de una sesión
rinari export config             # config con hojas secret-named redactadas
rinari import session <file>
```

Los secretos nunca se exportan; las hojas `[redacted]` se rechazan en import.

## Reglas generales

- `rinari doctor` primero; la salida es el contrato de diagnóstico.
- Los errores de machine tienen código propio (en `--json` el envelope incluye
  `code` y `retryable`); no escriba lógica sobre el texto.
- Ante error de sandbox/aprobar acciones destructivas: revise `rinari
  approvals history` y el perfil con `rinari permissions`.
- No hay comando `reset`; mover/reemplazar `$RINARI_HOME` borra el estado
  (exporte lo valioso antes).