"""Lógica del REPL de chat.

ChatSession mantiene el estado de la conversación (messages + perfil).
parse_command / run_command manejan los comandos de barra (/new, /model, ...).
El loop interactivo de TTY vive en cli.py; aquí solo la lógica testeable.
"""

from __future__ import annotations

import json

from rinari.history import History
from rinari.identity import build_chat_prompt

COMMANDS = {"new", "model", "exit", "save", "help"}


def parse_command(text: str) -> tuple[str | None, str | None]:
    """Separa '/comando args' de un mensaje normal."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, None
    parts = stripped[1:].split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


class ChatSession:
    """Estado de una conversación de chat."""

    def __init__(self, history: History | None, profile: str, session_id: int | None = None):
        self.history = history
        self.profile = profile
        self.session_id = session_id
        self.messages: list[dict] = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.tool_calls = 0
        self._init_messages()

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Acumula tokens usados por una llamada al modelo."""
        self.total_prompt_tokens += max(0, int(prompt_tokens))
        self.total_completion_tokens += max(0, int(completion_tokens))

    def _init_messages(self) -> None:
        if self.session_id is not None and self.history is not None:
            self.messages = self.history.get_messages(self.session_id)
        else:
            self.messages = [{"role": "system", "content": build_chat_prompt()}]

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def persist(self) -> None:
        """Guarda los mensajes en el historial si hay sesión activa."""
        if self.history is None or self.session_id is None:
            return
        # Solo persistimos mensajes nuevos: simplificación — reescribimos
        # mensajes que aún no están (el REPL maneja el caso).
        self.history.append_message(self.session_id, self.messages[-1])

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": build_chat_prompt()}]
        self.session_id = None


def run_command(
    cmd: str,
    args: str,
    session: ChatSession,
    config_dir=None,
    compact_client=None,
    cwd: str | None = None,
) -> str | None:
    """Ejecuta un comando de barra. Devuelve mensaje de respuesta (o None).

    config_dir: directorio del config.toml (para /model <modelo>); si es
    None, /model solo cambia de perfil (compat hacia atrás).
    compact_client: cliente LLM con .chat() para /compact (resumir contexto).
    cwd: directorio de trabajo (para /init).
    """
    cmd = cmd.lower()
    if cmd == "new":
        session.reset()
        return "🧹 Conversación nueva."
    if cmd == "model":
        name = args.strip()
        if not name:
            if config_dir is None:
                raise ValueError("Uso: /model <nombre> — falta el nombre del perfil")
            from rinari.config import load_config

            cfg = load_config(config_dir)
            try:
                cur = cfg.get_profile(session.profile)
            except Exception:  # noqa: BLE001
                cur = None
            active = cur.model if cur else "?"
            return (
                f"Modelos en '{session.profile}': activo [bold]{active}[/bold]. "
                f"Usa [bold]/model <nombre-del-modelo>[/bold] para cambiar de modelo, "
                f"o [bold]/model <perfil>[/bold] para cambiar de perfil."
            )
        if config_dir is not None:
            from rinari.config import load_config, set_profile_model

            cfg = load_config(config_dir)
            # ¿es un perfil existente? → cambia de perfil (prioridad)
            try:
                cfg.get_profile(name)
                session.profile = name
                return f"🔀 Perfil cambiado a '{name}'."
            except Exception:  # noqa: BLE001 — no es perfil → es modelo
                set_profile_model(config_dir, session.profile, name)
                return f"🧠 Modelo de '{session.profile}' cambiado a '{name}'."
        session.profile = name
        return f"🔀 Perfil cambiado a '{name}'."
    if cmd == "exit":
        raise SystemExit(0)
    if cmd == "save":
        if session.history is not None and session.session_id is not None:
            return f"💾 Conversación guardada (sesión {session.session_id})."
        return "ℹ️ Nada que guardar — sin sesión activa."
    if cmd == "compact":
        return _cmd_compact(session, compact_client)
    if cmd == "todos":
        return _cmd_todos(session, args)
    if cmd == "cost":
        return (
            f"💰 Uso de la sesión: [bold]{session.total_prompt_tokens}[/bold] prompt + "
            f"[bold]{session.total_completion_tokens}[/bold] completion "
            f"= [bold]{session.total_tokens}[/bold] tokens totales"
        )
    if cmd == "rewind":
        return _cmd_rewind(session, args)
    if cmd == "status":
        from rinari.config import load_config

        model = "?"
        try:
            cfg = load_config(config_dir)
            cur = cfg.get_profile(session.profile)
            model = cur.model
        except Exception:  # noqa: BLE001
            pass
        return (
            f"📊 Sesión: perfil [bold]{session.profile}[/bold] · modelo [bold]{model}[/bold]\n"
            f"   Mensajes: {len(session.messages)} · tools: {getattr(session, 'tool_calls', 0)}\n"
            f"   Tokens: {session.total_prompt_tokens} prompt + "
            f"{session.total_completion_tokens} completion "
            f"= {session.total_tokens} totales"
        )
    if cmd == "init":
        from pathlib import Path

        return _cmd_init(str(Path(cwd) if cwd else Path.cwd()))
    if cmd == "help":
        return (
            "Comandos: /new (nueva conversación), /model <perfil o modelo>, "
            "/compact (resumir contexto), /todos (tareas), /cost (tokens), "
            "/rewind [N] (volver N pasos atrás), /undo (deshacer edición), "
            "/status (dashboard de sesión), /init (generar RINARI.md), /save, /exit. "
            "Escribe tu mensaje para chatear."
        )
    raise ValueError(f"Comando desconocido: /{cmd}")


def _cmd_init(cwd: str) -> str:
    """Genera RINARI.md analizando el repo (estilo codex /init).

    Detecta el stack (pyproject.toml / package.json / Cargo.toml / go.mod),
    el framework de tests, y el propósito desde el README. No sobrescribe
    un RINARI.md existente (usa /init --force para regenerarlo).
    """
    from pathlib import Path

    root = Path(cwd)
    target = root / "RINARI.md"
    if target.exists():
        return "ℹ️ Ya existe RINARI.md — no se sobrescribe (usa /init --force)."

    name = root.name
    sections: list[str] = ["# RINARI", ""]
    sections.append(f"Proyecto: {name}")

    # Stack de Python
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        import tomllib

        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            proj = data.get("project", {})
            proj_name = proj.get("name") or name
            deps = proj.get("dependencies", [])
            sections.append("")
            sections.append("## Stack")
            sections.append(f"- Python (proyecto: {proj_name})")
            if deps:
                sections.append(f"- Dependencias: {', '.join(d[:40] for d in deps[:8])}")
            if (root / "tests").exists() or any(root.glob("test_*.py")):
                sections.append("- Tests: pytest")
        except Exception:  # noqa: BLE001
            sections.append("")
            sections.append("## Stack")
            sections.append("- Python")

    # Stack de Node
    pkg = root / "package.json"
    if pkg.exists():
        import json

        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            sections.append("")
            sections.append("## Stack")
            sections.append(f"- Node/JavaScript (proyecto: {data.get('name') or name})")
            test_script = (data.get("scripts") or {}).get("test", "")
            if test_script:
                sections.append(f"- Tests: {test_script}")
        except Exception:  # noqa: BLE001
            sections.append("")
            sections.append("## Stack")
            sections.append("- Node/JavaScript")

    # Propósito desde el README
    for readme in ("README.md", "README.rst", "README"):
        rp = root / readme
        if rp.exists():
            first_lines = [l for l in rp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()][:6]
            if first_lines:
                sections.append("")
                sections.append("## Propósito")
                sections.extend(f"- {l.strip()[:100]}" for l in first_lines[:4])
            break

    sections.append("")
    sections.append("## Convenciones")
    sections.append("- _Documenta aquí las reglas del proyecto (tests, estilo, comandos).")
    sections.append("")

    target.write_text("\n".join(sections), encoding="utf-8")
    return f"✨ RINARI.md generado: {target.name} — edítalo con las convenciones del proyecto."


def _cmd_rewind(session: ChatSession, args: str) -> str:
    """Vuelve la conversación a un checkpoint anterior (estilo codex /rewind).

    Antes de cada mensaje del usuario se guarda un checkpoint; /rewind [N]
    restaura el estado N turnos atrás (default 1).
    """
    checkpoints = getattr(session, "checkpoints", None)
    if not checkpoints:
        raise ValueError("Nada que rebobinar — sin checkpoints de conversación.")
    try:
        steps = int(args.strip() or "1")
    except ValueError:
        raise ValueError("Uso: /rewind [N] — N = cuántos pasos atrás (default 1)")
    if steps < 1:
        raise ValueError("N debe ser >= 1.")
    if steps > len(checkpoints):
        raise ValueError(
            f"Solo hay {len(checkpoints)} checkpoint(s) — no se puede rebobinar {steps}."
        )
    for _ in range(steps):
        session.messages = checkpoints.pop()
    return f"↩️ Rebobinado {steps} paso(s): la conversación volvió a un punto anterior."


def _cmd_compact(session: ChatSession, compact_client) -> str:
    """Resume la conversación con el modelo y reemplaza el historial."""
    if compact_client is None:
        raise ValueError("Uso: /compact — este chat no soporta resumir contexto.")
    if len(session.messages) < 4:
        return "ℹ️ La conversación es corta — no hace falta compactar."
    try:
        summary = compact_client.chat(
            [
                {"role": "system", "content": (
                    "Resume la conversación siguiente en un párrafo conciso, "
                    "conservando decisiones, datos y acuerdos importantes. "
                    "Responde SOLO con el resumen."
                )},
                {"role": "user", "content": json.dumps(
                    session.messages, ensure_ascii=False
                )},
            ]
        )
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Fallo al resumir: {e}") from e
    summary = summary.strip()
    if not summary:
        raise ValueError("El modelo devolvió un resumen vacío.")
    session.messages = [
        {"role": "system", "content": build_chat_prompt()},
        {"role": "user", "content": f"[Resumen de la conversación anterior]\n{summary}\n\nContinúa desde aquí."},
    ]
    return "🧹 Contexto compactado: la conversación quedó resumida y seguimos con el resumen."


def _cmd_todos(session: ChatSession, args: str) -> str:
    """Gestiona la lista de tareas del agente (/todos, add, done, help)."""
    todos = getattr(session, "todos", None)
    if todos is None:
        todos = []
        session.todos = todos
    parts = args.strip().split(None, 1)
    action = parts[0].lower() if parts else ""
    if action == "add":
        text = (parts[1] if len(parts) > 1 else "").strip()
        if not text:
            return "Uso: /todos add <descripción>"
        todos.append({"text": text, "done": False})
        return f"✅ Tarea agregada ({len(todos)}): {text}"
    if action == "done":
        try:
            idx = int(parts[1]) - 1
            if not (0 <= idx < len(todos)):
                return f"❌ No existe la tarea {parts[1]}."
            todos[idx]["done"] = True
            return f"✓ Tarea completada: {todos[idx]['text']}"
        except (IndexError, ValueError):
            return "Uso: /todos done <número>"
    if action == "rm":
        try:
            idx = int(parts[1]) - 1
            removed = todos.pop(idx)
            return f"🗑️ Tarea eliminada: {removed['text']}"
        except (IndexError, ValueError):
            return "Uso: /todos rm <número>"
    if action == "help" or (parts and action not in ("list", "")):
        return "Uso: /todos (listar) · /todos add <texto> · /todos done <n> · /todos rm <n>"
    if not todos:
        return "📋 Lista de tareas vacía."
    lines = ["📋 Tareas:"]
    for i, t in enumerate(todos, 1):
        mark = "[x]" if t["done"] else "[ ]"
        lines.append(f"  {i}. {mark} {t['text']}")
    return "\n".join(lines)


def load_skill(name: str, config_dir=None) -> str | None:
    """Carga un prompt de ~/.rinari/commands/<name>.md (skills custom).

    Busca primero en config_dir/commands/, luego en ~/.rinari/commands/.
    Devuelve el contenido del archivo o None si no existe.
    """
    from pathlib import Path

    candidates: list[Path] = []
    if config_dir is not None:
        candidates.append(Path(config_dir) / "commands" / f"{name}.md")
    candidates.append(Path.home() / ".rinari" / "commands" / f"{name}.md")
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except OSError:
                return None
    return None
