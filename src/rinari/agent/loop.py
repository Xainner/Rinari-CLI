"""Agent loop: modelo → tool_calls → ejecutar → observar → repetir.

El loop:
1. Envía el historial (system prompt + task + observaciones) al modelo
2. Si el modelo devuelve tool_calls → ejecutar cada una (con aprobación si
   no hay auto_approve), append resultados como mensajes role=tool
3. Si devuelve contenido final → terminar
4. Corta en max_iterations

Dependencias inyectadas (client, registry, approver) para testear sin red.
"""

from __future__ import annotations

import json
from typing import Callable

from rinari.agent.prompt import build_agent_messages
from rinari.agent.tools import ToolRegistry, is_dangerous
from rinari.client import LLMError


class AgentError(Exception):
    pass


def _default_approver(name: str, args: dict, cwd: str) -> bool:
    """Aprobador por defecto: pide confirmación en terminal para comandos."""
    from rich.console import Console

    console = Console()
    command = args.get("command", "")
    console.print(f"[yellow]⚠️ El agente quiere ejecutar:[/yellow] [bold]{command}[/bold]")
    answer = console.input("¿Aprobar? [y/N] ").strip().lower()
    return answer in ("y", "yes", "s", "si", "sí")


def run_agent(
    task: str,
    client,
    cwd: str,
    registry: ToolRegistry | None = None,
    auto_approve: bool = False,
    approver: Callable[[str, dict, str], bool] | None = None,
    max_iterations: int = 10,
    render_callback: Callable[[dict], None] | None = None,
) -> dict:
    """Ejecuta la tarea con el loop agéntico. Devuelve {status, final, steps, iterations}.

    client: objeto con .chat(messages, temperature) → str, y .chat_stream para
            streaming. En tests se inyecta un ScriptedClient.
    """
    registry = registry or ToolRegistry()
    approver = approver or _default_approver
    messages = build_agent_messages(task)
    steps: list[dict] = []
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        try:
            if hasattr(client, "chat_message"):
                response = client.chat_message(
                    messages,
                    tools=registry.openai_schemas(),
                )
            else:
                response = client.chat(
                    messages,
                    tools=registry.openai_schemas(),
                )
        except LLMError as e:
            steps.append({"type": "error", "message": str(e)})
            if render_callback:
                render_callback({"type": "error", "message": str(e)})
            return {
                "status": "error",
                "final": None,
                "steps": steps,
                "iterations": iterations,
            }

        parsed = _parse_response(response)

        if parsed["final"] is not None and not parsed["tool_calls"]:
            steps.append({"type": "final", "content": parsed["final"]})
            if render_callback:
                render_callback({"type": "final", "content": parsed["final"]})
            return {
                "status": "done",
                "final": parsed["final"],
                "steps": steps,
                "iterations": iterations,
            }

        if not parsed["tool_calls"]:
            # Respuesta sin contenido ni tools: terminar para no loopear
            steps.append({"type": "final", "content": parsed["final"] or ""})
            if render_callback:
                render_callback({"type": "final", "content": parsed["final"] or ""})
            return {
                "status": "done",
                "final": parsed["final"] or "",
                "steps": steps,
                "iterations": iterations,
            }

        # Ejecutar tool calls
        tool_messages: list[dict] = []
        for tc in parsed["tool_calls"]:
            name = tc.get("name", "")
            args = tc.get("arguments", {})
            steps.append({"type": "tool_call", "name": name, "arguments": args})
            if render_callback:
                render_callback({"type": "tool_call", "name": name, "arguments": args})

            allowed = auto_approve
            if not allowed and name == "run_command" and is_dangerous(args.get("command", "")):
                allowed = approver(name, args, cwd)
            elif not allowed:
                allowed = True  # read/write/search son seguros

            if not allowed:
                result = {"ok": False, "error": "Comando denegado por el usuario"}
                steps.append({"type": "tool_denied", "name": name, "arguments": args})
                if render_callback:
                    render_callback({"type": "tool_denied", "name": name, "arguments": args})
            else:
                try:
                    result = registry.execute(name, args, cwd)
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "error": str(e)}

            steps.append({"type": "tool_result", "name": name, "result": result})
            if render_callback:
                render_callback({"type": "tool_result", "name": name, "result": result})

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        messages.append(
            {
                "role": "assistant",
                "content": parsed.get("content") or "",
                "tool_calls": [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("arguments", {})),
                        },
                    }
                    for tc in parsed["tool_calls"]
                ],
            }
        )
        messages.extend(tool_messages)

    steps.append({"type": "max_iterations", "message": f"Se alcanzó el máximo de {max_iterations} iteraciones"})
    if render_callback:
        render_callback({"type": "max_iterations", "message": f"Se alcanzó el máximo de {max_iterations} iteraciones"})
    return {
        "status": "max_iterations",
        "final": None,
        "steps": steps,
        "iterations": iterations,
    }


def _parse_response(response) -> dict:
    """Interpreta la respuesta del modelo.

    Acepta:
    - dict con tool_calls (API de tools)
    - dict con contenido final
    - str plano (contenido final)
    """
    if isinstance(response, str):
        return {"final": response, "tool_calls": [], "content": response}

    if not isinstance(response, dict):
        return {"final": str(response), "tool_calls": [], "content": str(response)}

    content = response.get("content") or ""
    tool_calls = []
    for tc in response.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
        else:
            args = raw_args
        tool_calls.append({"id": tc.get("id", ""), "name": name, "arguments": args})

    # El modelo puede devolver tool_calls y contenido a la vez
    has_final = bool(content) and not tool_calls
    return {
        "final": content if has_final else (content or None),
        "tool_calls": tool_calls,
        "content": content,
    }
