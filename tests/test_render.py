"""Tests para el rendering de streaming con rich."""

from rinari.render import (
    DeltaAccumulator,
    ToolSpinner,
    render_tool_result,
    strip_ansi,
)


def test_accumulator_builds_text():
    acc = DeltaAccumulator()
    acc.add("Hola")
    acc.add(" mundo")
    assert acc.text == "Hola mundo"


def test_accumulator_handles_empty():
    acc = DeltaAccumulator()
    acc.add("")
    assert acc.text == ""


def test_strip_ansi_removes_codes():
    assert strip_ansi("\x1b[32mverde\x1b[0m") == "verde"


def test_accumulator_reset():
    acc = DeltaAccumulator()
    acc.add("hola")
    acc.reset()
    assert acc.text == ""


def test_tool_spinner_start_stop():
    """El spinner arranca (activo) y se detiene limpiamente."""
    spinner = ToolSpinner()
    assert not spinner.is_active()
    spinner.start("ejecutando...")
    assert spinner.is_active()
    spinner.stop()
    assert not spinner.is_active()
    # stop doble no rompe
    spinner.stop()


def test_tool_spinner_start_after_stop():
    """Se puede reutilizar tras detenerse."""
    spinner = ToolSpinner()
    spinner.start("uno")
    spinner.stop()
    spinner.start("dos")
    assert spinner.is_active()
    spinner.stop()


def test_render_tool_result_error_no_crash():
    """Resultado con error se renderiza sin romper."""
    render_tool_result({"ok": False, "error": "algo salió mal"})


def test_render_tool_result_stdout_no_crash():
    """Resultado con stdout se renderiza en panel sin romper."""
    render_tool_result({"ok": True, "stdout": "hola\nmundo\n", "exit_code": 0})


def test_render_tool_result_empty_no_crash():
    """Resultado sin stdout no imprime nada (no rompe)."""
    render_tool_result({"ok": True, "stdout": "", "exit_code": 0})


def test_stream_live_accumulates_and_renders():
    """stream_live acumula todos los deltas y termina con el texto completo."""
    from io import StringIO

    from rich.console import Console

    from rinari.render import DeltaAccumulator

    acc = DeltaAccumulator()
    console = Console(file=StringIO(), width=80, force_terminal=True)
    events = ["ho", "la ", "mundo", {"tool_calls": []}]
    acc.stream_live(iter(events), console=console)
    assert acc.text == "hola mundo"


def test_stream_live_ignores_tool_call_events():
    """Los eventos dict (tool_calls) no aportan texto."""
    from io import StringIO

    from rich.console import Console

    from rinari.render import DeltaAccumulator

    acc = DeltaAccumulator()
    console = Console(file=StringIO(), width=80, force_terminal=True)
    acc.stream_live(iter([{"tool_calls": []}, "solo texto"]), console=console)
    assert acc.text == "solo texto"


def test_stream_live_empty_events_no_crash():
    """Sin eventos no rompe y no acumula nada."""
    from io import StringIO

    from rich.console import Console

    from rinari.render import DeltaAccumulator

    acc = DeltaAccumulator()
    console = Console(file=StringIO(), width=80, force_terminal=True)
    acc.stream_live(iter([]), console=console)
    assert acc.text == ""
