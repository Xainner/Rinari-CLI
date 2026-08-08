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
        self._init_messages()

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
) -> str | None:
    """Ejecuta un comando de barra. Devuelve mensaje de respuesta (o None).

    config_dir: directorio del config.toml (para /model <modelo>); si es
    None, /model solo cambia de perfil (compat hacia atrás).
    compact_client: cliente LLM con .chat() para /compact (resumir contexto).
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
    if cmd == "help":
        return (
            "Comandos: /new (nueva conversación), /model <perfil o modelo>, "
            "/compact (resumir contexto), /todos (tareas), /save, /exit. "
            "Escribe tu mensaje para chatear."
        )
    raise ValueError(f"Comando desconocido: /{cmd}")


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
