"""Lógica del REPL de chat.

ChatSession mantiene el estado de la conversación (messages + perfil).
parse_command / run_command manejan los comandos de barra (/new, /model, ...).
El loop interactivo de TTY vive en cli.py; aquí solo la lógica testeable.
"""

from __future__ import annotations

from qwencli.history import History

SYSTEM_PROMPT = (
    "Eres un asistente útil. Respondes en el idioma del usuario. "
    "Sé directo, claro y conciso."
)

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
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

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
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.session_id = None


def run_command(cmd: str, args: str, session: ChatSession) -> str | None:
    """Ejecuta un comando de barra. Devuelve mensaje de respuesta (o None)."""
    cmd = cmd.lower()
    if cmd == "new":
        session.reset()
        return "🧹 Conversación nueva."
    if cmd == "model":
        name = args.strip()
        if not name:
            raise ValueError("Uso: /model <nombre> — falta el nombre del perfil")
        session.profile = name
        return f"🔀 Perfil cambiado a '{name}'."
    if cmd == "exit":
        raise SystemExit(0)
    if cmd == "save":
        if session.history is not None and session.session_id is not None:
            return f"💾 Conversación guardada (sesión {session.session_id})."
        return "ℹ️ Nada que guardar — sin sesión activa."
    if cmd == "help":
        return (
            "Comandos: /new (nueva conversación), /model <perfil>, "
            "/save, /exit. Escribe tu mensaje para chatear."
        )
    raise ValueError(f"Comando desconocido: /{cmd}")
