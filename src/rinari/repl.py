"""Lógica del REPL de chat.

ChatSession mantiene el estado de la conversación (messages + perfil).
parse_command / run_command manejan los comandos de barra (/new, /model, ...).
El loop interactivo de TTY vive en cli.py; aquí solo la lógica testeable.
"""

from __future__ import annotations

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


def run_command(cmd: str, args: str, session: ChatSession, config_dir=None) -> str | None:
    """Ejecuta un comando de barra. Devuelve mensaje de respuesta (o None).

    config_dir: directorio del config.toml (para /model <modelo>); si es
    None, /model solo cambia de perfil (compat hacia atrás).
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
    if cmd == "help":
        return (
            "Comandos: /new (nueva conversación), /model <perfil o modelo>, "
            "/save, /exit. Escribe tu mensaje para chatear."
        )
    raise ValueError(f"Comando desconocido: /{cmd}")
