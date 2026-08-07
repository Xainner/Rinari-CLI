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
