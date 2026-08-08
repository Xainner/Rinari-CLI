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
import subprocess
from typing import Callable

from rinari.agent.prompt import build_agent_messages
from rinari.agent.tools import ToolRegistry, is_dangerous
from rinari.client import LLMError
from rinari.history import History


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


def _default_plan_approver(task: str, plan: str) -> bool:
    """Aprobador por defecto del plan: pide confirmación en terminal."""
    from rich.console import Console

    console = Console()
    console.print("\n[bold cyan]📋 Plan del agente:[/bold cyan]")
    console.print(plan)
    answer = console.input("\n¿Aprobar el plan y ejecutar? [y/N] ").strip().lower()
    return answer in ("y", "yes", "s", "si", "sí")


def load_hooks(config_dir=None) -> dict:
    """Carga los hooks de ~/.rinari/hooks.toml (estilo Claude Code).

    Formato:
        [pre_tool]
        edit_file = "python lint.py"   # por tool
        "*" = "echo hola"              # para todas
        [post_tool]
        edit_file = "python format.py"

    Devuelve {"pre_tool": {tool: cmd}, "post_tool": {tool: cmd}}.
    """
    import tomllib
    from pathlib import Path

    path = Path(config_dir) / "hooks.toml" if config_dir else Path.home() / ".rinari" / "hooks.toml"
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return {k: v for k, v in data.items() if k in ("pre_tool", "post_tool")}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def run_agent(
    task: str,
    client,
    cwd: str,
    registry: ToolRegistry | None = None,
    auto_approve: bool = False,
    approver: Callable[[str, dict, str], bool] | None = None,
    max_iterations: int = 10,
    max_retries: int = 2,
    reminder_threshold: int = 3,
    render_callback: Callable[[dict], None] | None = None,
    messages: list[dict] | None = None,
    mcp_servers: dict | None = None,
    persist: bool = False,
    verify_changes: bool = False,
    plan_first: bool = False,
    plan_approver: Callable[[str, str], bool] | None = None,
    history=None,
    hooks: dict | None = None,
) -> dict:
    """Ejecuta la tarea con el loop agéntico. Devuelve {status, final, steps, iterations, messages}.

    client: objeto con .chat(messages, temperature) → str, y .chat_stream para
            streaming. En tests se inyecta un ScriptedClient.
    messages: historial previo (modo interactivo). Si es None, se construye
              desde cero con build_agent_messages(task).
    mcp_servers: dict[str, MCPServer] — expone las tools de esos servidores
                 MCP como tools dinámicas del agente.
    max_retries: reintentos ante LLMError transitorios (0 = sin retry).
    persist: guarda la sesión en el historial SQLite al terminar.
    verify_changes: tras cada edit_file/write_file, ejecuta run_tests.
    plan_first: la primera respuesta se trata como plan y requiere aprobación
                antes de continuar (plan_approver recibe (task, plan)).
    """
    registry = registry or ToolRegistry(delegate=True)
    approver = approver or _default_approver
    plan_approver = plan_approver or _default_plan_approver
    hooks = hooks if hooks is not None else load_hooks()
    if messages is None:
        messages = build_agent_messages(task, cwd=cwd)
    else:
        messages = list(messages) + [{"role": "user", "content": f"Tarea: {task}"}]
    steps: list[dict] = []
    iterations = 0
    plan: str | None = None

    # Bridge MCP: conecta bajo demanda y resuelve tools
    mcp_bridge = MCPToolBridge(mcp_servers) if mcp_servers else None
    mcp_schemas = mcp_bridge.schemas() if mcp_bridge else []

    def _all_schemas() -> list[dict]:
        return registry.openai_schemas() + mcp_schemas

    def _run_hooks(event: str, tool_name: str) -> list[dict]:
        """Ejecuta los hooks pre/post para una tool. Nunca bloquea el loop."""
        entries = (hooks or {}).get(event, {})
        cmds = [cmd for key, cmd in entries.items() if key == "*" or key == tool_name]
        results = []
        for cmd in cmds:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                results.append({
                    "type": "hook",
                    "event": event,
                    "tool": tool_name,
                    "ok": proc.returncode == 0,
                    "output": (proc.stdout or proc.stderr).strip()[:300],
                })
            except Exception as e:  # noqa: BLE001
                results.append({"type": "hook", "event": event, "tool": tool_name, "ok": False, "output": str(e)})
        for r in results:
            steps.append(r)
            if render_callback:
                render_callback(r)
        return results

    def _execute_tool(name: str, args: dict) -> dict:
        """Ejecuta tool nativa o MCP según el nombre."""
        if name == "delegate_task":
            return _run_delegate(args)
        _run_hooks("pre_tool", name)
        if mcp_bridge and mcp_bridge.has_tool(name):
            result = mcp_bridge.call(name, args)
        else:
            result = registry.execute(name, args, cwd)
        _run_hooks("post_tool", name)
        return result

    def _run_delegate(args: dict) -> dict:
        """Ejecuta una subtarea en un subagente (un nivel, sin recursión)."""
        task = (args.get("task") or "").strip()
        if not task:
            return {"ok": False, "error": "delegate_task requiere 'task'"}
        context = (args.get("context") or "").strip()
        sub_messages = None
        if context:
            sub_messages = [{"role": "system", "content": f"Contexto del agente padre:\n{context}"}]
        sub_registry = ToolRegistry(delegate=False)  # el subagente NO delega
        sub = run_agent(
            task,
            client,
            cwd,
            registry=sub_registry,
            auto_approve=auto_approve,
            approver=approver,
            max_iterations=max(3, min(max_iterations // 2, 8)),
            render_callback=render_callback,
            messages=sub_messages,
            mcp_servers=mcp_servers,
            persist=False,
            verify_changes=False,
            hooks=hooks,
        )
        return {
            "ok": sub["status"] == "done",
            "status": sub["status"],
            "final": sub.get("final"),
            "iterations": sub.get("iterations"),
        }

    def _llm_call() -> tuple[dict | str, int]:
        """Llama al modelo con reintentos ante errores transitorios.

        Cuando quedan pocas iteraciones (reminder_threshold), inyecta un
        aviso al modelo para que priorice cerrar la tarea (estilo Codex).
        """
        attempts = max_retries + 1
        remaining = max_iterations - iterations
        call_messages = messages
        if 0 < remaining <= reminder_threshold:
            # inyectar en el mensaje system EXISTENTE (al inicio) — algunos
            # servidores (llama.cpp) rechazan system messages al final
            call_messages = list(messages)
            reminder = (
                f"⚠️ IMPORTANTE: te quedan {remaining} iteración(es) de "
                "presupuesto. Prioriza COMPLETAR la tarea ahora: si estás "
                "cerca de terminar, da tu respuesta final; si te falta mucho, "
                "termina con lo esencial y reporta qué quedó pendiente."
            )
            for i, m in enumerate(call_messages):
                if m.get("role") == "system":
                    merged = dict(m)
                    merged["content"] = f"{m.get('content', '')}\n\n{reminder}"
                    call_messages[i] = merged
                    break
            else:
                call_messages.insert(0, {"role": "system", "content": reminder})
        for attempt in range(attempts):
            try:
                if hasattr(client, "chat_message"):
                    return client.chat_message(
                        call_messages,
                        tools=_all_schemas(),
                    ), 0
                return client.chat(
                    call_messages,
                    tools=_all_schemas(),
                ), 0
            except LLMError as e:
                last_error = e
                if attempt < attempts - 1:
                    steps.append({"type": "retry", "attempt": attempt + 1, "error": str(e)})
                    if render_callback:
                        render_callback({"type": "retry", "attempt": attempt + 1, "error": str(e)})
        raise last_error

    def _finalize(status: str, final: str | None) -> dict:
        """Arma el resultado final y opcionalmente persiste la sesión."""
        tool_count = sum(1 for s in steps if s.get("type") == "tool_result")
        result = {
            "status": status,
            "final": final,
            "steps": steps,
            "iterations": iterations,
            "messages": messages,
            "plan": plan,
            "tool_count": tool_count,
        }
        if persist:
            saved = save_agent_session(task, messages, steps, status, history=history)
            result["saved"] = saved is not None
        return result

    # Planificación explícita: primera llamada presenta plan y pide aprobación
    if plan_first:
        iterations += 1
        try:
            if hasattr(client, "chat_message"):
                plan_response = client.chat_message(messages, tools=_all_schemas())
            else:
                plan_response = client.chat(messages, tools=_all_schemas())
        except LLMError as e:
            steps.append({"type": "error", "message": str(e)})
            if render_callback:
                render_callback({"type": "error", "message": str(e)})
            return _finalize("error", None)
        parsed_plan = _parse_response(plan_response)
        plan = parsed_plan["final"] or parsed_plan["content"] or ""
        if not plan:
            plan = "El agente no presentó un plan."
        steps.append({"type": "plan", "content": plan})
        if render_callback:
            render_callback({"type": "plan", "content": plan})
        approved = plan_approver(task, plan)
        if not approved:
            messages.append({"role": "assistant", "content": plan})
            steps.append({"type": "plan_denied", "content": plan})
            if render_callback:
                render_callback({"type": "plan_denied", "content": plan})
            return _finalize("plan_denied", None)
        messages.append({"role": "assistant", "content": plan})
        messages.append(
            {"role": "user", "content": "Plan aprobado. Procede a ejecutarlo paso a paso."}
        )

    while iterations < max_iterations:
        iterations += 1
        try:
            response, _ = _llm_call()
        except LLMError as e:
            steps.append({"type": "error", "message": str(e)})
            if render_callback:
                render_callback({"type": "error", "message": str(e)})
            return _finalize("error", None)

        parsed = _parse_response(response)

        if parsed["final"] is not None and not parsed["tool_calls"]:
            messages.append({"role": "assistant", "content": parsed["final"]})
            steps.append({"type": "final", "content": parsed["final"]})
            if render_callback:
                render_callback({"type": "final", "content": parsed["final"]})
            return _finalize("done", parsed["final"])

        if not parsed["tool_calls"]:
            # Respuesta sin contenido ni tools: terminar para no loopear
            messages.append({"role": "assistant", "content": parsed["final"] or ""})
            steps.append({"type": "final", "content": parsed["final"] or ""})
            if render_callback:
                render_callback({"type": "final", "content": parsed["final"] or ""})
            return _finalize("done", parsed["final"] or "")

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
                    result = _execute_tool(name, args)
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "error": str(e)}

            # Verificación automática: tras editar, correr los tests
            if verify_changes and result.get("ok") and name in ("edit_file", "write_file"):
                test_result = registry.execute("run_tests", {}, cwd)
                steps.append({"type": "verify", "name": "run_tests", "result": test_result})
                if render_callback:
                    render_callback({"type": "verify", "name": "run_tests", "result": test_result})

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

        # Sanear el contenido: evitar que el modelo imite formatos raros
        # (@url:`...` de docs de PowerShell) vistos en tool results previos
        for tm in tool_messages:
            tm["content"] = _sanitize_tool_content(tm["content"])

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
    return _finalize("max_iterations", None)


def save_agent_session(
    task: str,
    messages: list[dict],
    steps: list[dict],
    status: str,
    history=None,
    profile: str = "default",
) -> dict | None:
    """Guarda la sesión del agente en el historial SQLite.

    Devuelve el id de la sesión creada o None si falla (nunca rompe el loop).
    """
    try:
        hist = history or History()
        session_id = hist.create_session(profile)
        hist.append_message(session_id, {"role": "user", "content": task})
        # Último mensaje relevante como respuesta (final o error)
        last = None
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                last = m["content"]
                break
        if last:
            hist.append_message(session_id, {"role": "assistant", "content": last})
        return {"session_id": session_id}
    except Exception:  # noqa: BLE001 — persistencia nunca debe romper el agente
        return None


class MCPToolBridge:
    """Conecta servidores MCP y expone sus tools como tools del agente.

    Conecta bajo demanda (lazy): listar tools al iniciar, ejecutar al llamar.
    Cada servidor abre su propia conexión stdio.
    """

    def __init__(self, servers: dict):
        self._servers = servers or {}
        self._by_name: dict[str, tuple[str, str]] = {}  # tool_name -> (server_name, mcp_name)
        self._connections: dict[str, MCPConnection] = {}
        self._schemas: list[dict] = []
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        from rinari.mcp import MCPConnection, build_openai_schemas

        for server_name, server in self._servers.items():
            try:
                conn = MCPConnection(server)
                conn.__enter__()
                tools = conn.list_tools()
                self._connections[server_name] = conn
                for t in tools:
                    self._by_name[t.name] = (server_name, t.name)
                self._schemas.extend(build_openai_schemas(tools))
            except Exception as e:  # noqa: BLE001
                # Servidor caído: no bloquea al agente, se reporta como error al llamar
                self._by_name[f"__mcp_error_{server_name}"] = (server_name, "")
                self._schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"__mcp_error_{server_name}",
                            "description": f"Servidor MCP '{server_name}' no disponible: {e}",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                )
        self._loaded = True

    def schemas(self) -> list[dict]:
        self._load()
        return self._schemas

    def has_tool(self, name: str) -> bool:
        self._load()
        return name in self._by_name and not name.startswith("__mcp_error_")

    def call(self, name: str, arguments: dict) -> dict:
        self._load()
        entry = self._by_name.get(name)
        if entry is None:
            return {"ok": False, "error": f"Tool MCP desconocida: {name}"}
        server_name, mcp_name = entry
        if name.startswith("__mcp_error_"):
            return {"ok": False, "error": f"Servidor MCP '{server_name}' no disponible"}
        conn = self._connections.get(server_name)
        if conn is None:
            return {"ok": False, "error": f"Servidor MCP '{server_name}' no conectado"}
        try:
            result = conn.call_tool(mcp_name, arguments)
            return {"ok": True, "result": result}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"MCP '{mcp_name}': {e}"}


def _sanitize_tool_content(content: str) -> str:
    """Limpia formatos raros de tool results antes de volver al modelo.

    Convierte '@url:`https://x`' (sintaxis de docs de PowerShell) en la URL
    desnuda — el modelo tiende a imitar ese formato en comandos posteriores
    (curl "@url:`...`"), lo que rompe la ejecución.
    """
    import re

    # @url:`https://example.com` → https://example.com
    content = re.sub(r"@url:`([^`]+)`", r"\1", content)
    # @url:https://example.com → https://example.com
    content = re.sub(r"@url:([a-zA-Z][a-zA-Z0-9+.-]*://[^\s`\"'\)\]]+)", r"\1", content)
    return content


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
