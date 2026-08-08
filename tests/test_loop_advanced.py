"""Tests del loop agéntico avanzado: retry, verificación, plan, persistencia."""

import json

import pytest

from rinari.agent.loop import run_agent
from rinari.client import LLMError


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def tool_call_msg(tool_calls: list[dict], content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc.get("id", "call_1"),
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))},
            }
            for tc in tool_calls
        ],
    }


def final_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


class FlakyClient:
    """Falla N veces con LLMError y luego responde normal."""

    def __init__(self, failures: int, response: dict):
        self.failures = failures
        self.response = response
        self.calls = 0

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMError("error transitorio de red")
        return self.response

    def chat_stream(self, messages, temperature=0.7, max_tokens=None, tools=None):
        raise NotImplementedError


class PlanClient:
    """Primera llamada: solo texto (plan). Segunda: final."""

    def __init__(self, plan_text: str, final_text: str):
        self.plan_text = plan_text
        self.final_text = final_text
        self.calls = 0

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return final_msg(self.plan_text)
        return final_msg(self.final_text)

    def chat_stream(self, messages, temperature=0.7, max_tokens=None, tools=None):
        raise NotImplementedError


class ApproverRecorder:
    def __init__(self, approve: bool):
        self.approve = approve
        self.calls: list = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.approve


def test_retry_after_transient_llm_error(workdir):
    """Un LLMError transitorio se reintenta (max_retries) y se completa."""
    client = FlakyClient(failures=2, response=final_msg("finalmente"))
    result = run_agent(
        "tarea",
        client,
        cwd=workdir,
        auto_approve=True,
        max_retries=3,
        max_iterations=3,
    )
    assert result["status"] == "done"
    assert client.calls == 3  # 2 fallos + 1 éxito
    assert result["final"] == "finalmente"


def test_retry_exhausted_returns_error(workdir):
    """Si se agotan los reintentos, devuelve status error."""
    client = FlakyClient(failures=99, response=final_msg("nunca"))
    result = run_agent(
        "tarea",
        client,
        cwd=workdir,
        auto_approve=True,
        max_retries=2,
        max_iterations=3,
    )
    assert result["status"] == "error"
    assert client.calls == 3  # 1 intento + 2 reintentos


def test_retry_zero_disables(workdir):
    """max_retries=0 → sin reintentos, falla a la primera."""
    client = FlakyClient(failures=1, response=final_msg("nunca"))
    result = run_agent(
        "tarea",
        client,
        cwd=workdir,
        auto_approve=True,
        max_retries=0,
        max_iterations=3,
    )
    assert result["status"] == "error"
    assert client.calls == 1


def test_persists_session_to_history(workdir, monkeypatch, tmp_path):
    """Al terminar, la sesión del agente se guarda en el historial."""
    import rinari.agent.loop as loop_mod
    from rinari.history import History

    client = ScriptedClient([final_msg("hecho")])
    monkeypatch.setattr(History, "create_session", lambda self, profile: 999)
    saved = {}
    monkeypatch.setattr(History, "append_message", lambda self, sid, msg: saved.update({msg["role"]: msg["content"]}))
    monkeypatch.setattr(
        loop_mod,
        "save_agent_session",
        lambda task, messages, steps, status, history=None: {"saved": True},
    )
    result = run_agent(
        "tarea de prueba",
        client,
        cwd=workdir,
        auto_approve=True,
        persist=True,
    )
    assert result["status"] == "done"
    assert "saved" in result and result["saved"] is True


def test_verify_changes_runs_tests_after_edit(workdir, monkeypatch):
    """Tras una edición con edit_file, se ejecuta run_tests automáticamente."""
    from rinari.agent import tools as tools_mod

    calls = []
    original_execute = tools_mod.ToolRegistry.execute

    def spy_execute(self, name, args, cwd):
        calls.append(name)
        if name == "edit_file":
            return {"ok": True, "file": args.get("path", "")}
        if name == "run_tests":
            return {"ok": True, "exit_code": 0, "stdout": "5 passed"}
        return original_execute(self, name, args, cwd)

    monkeypatch.setattr(tools_mod.ToolRegistry, "execute", spy_execute)

    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "edit_file", "arguments": {"path": "a.py", "old": "x", "new": "y"}}]),
            final_msg("listo"),
        ]
    )
    result = run_agent(
        "cambia a.py",
        client,
        cwd=workdir,
        auto_approve=True,
        verify_changes=True,
    )
    assert result["status"] == "done"
    assert "edit_file" in calls
    assert "run_tests" in calls


def test_plan_first_asks_approval_before_executing(workdir):
    """Con plan_first, el plan se presenta y requiere aprobación para ejecutar."""
    approver = ApproverRecorder(approve=True)
    plan_approver = ApproverRecorder(approve=True)
    client = PlanClient(plan_text="Mi plan: 1) editar 2) testear", final_text="hecho")
    result = run_agent(
        "haz algo",
        client,
        cwd=workdir,
        auto_approve=False,
        approver=approver,
        plan_approver=plan_approver,
        plan_first=True,
        max_iterations=5,
    )
    assert result["status"] == "done"
    assert plan_approver.calls  # pidió aprobación del plan
    assert result["final"] == "hecho"
    assert result["plan"] == "Mi plan: 1) editar 2) testear"


def test_plan_first_denied_stops(workdir):
    """Si el usuario deniega el plan, el agente no ejecuta nada."""
    plan_approver = ApproverRecorder(approve=False)
    client = PlanClient(plan_text="Mi plan", final_text="hecho")
    result = run_agent(
        "haz algo",
        client,
        cwd=workdir,
        auto_approve=False,
        plan_approver=plan_approver,
        plan_first=True,
        max_iterations=5,
    )
    assert result["status"] == "plan_denied"
    assert result["plan"] == "Mi plan"


class ScriptedClient:
    """Cliente falso que responde con una secuencia predefinida."""

    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.requests: list[list[dict]] = []

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.requests.append(list(messages))
        if not self.responses:
            raise AssertionError("No hay más respuestas scripteadas")
        return self.responses.pop(0)

    def chat_stream(self, messages, temperature=0.7, max_tokens=None, tools=None):
        raise NotImplementedError
