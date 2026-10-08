"""Recálculo certificado con el Excel instalado (Windows, COM).

Instancia propia y aislada: si la instancia que COM entrega ya tiene libros
abiertos (el usuario trabajando), se rechaza en lugar de recalcular o cerrar
su trabajo. Macros desactivadas, sin actualizar vínculos ni eventos. El PID
del Excel que el trabajo arrancó (y solo ese: uno que ya existía no se
apunta) queda en `owned.pids`; cancelar termina ese proceso y ningún otro.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

OWNED = "owned.pids"

_SCRIPT = r"""
param([string]$In, [string]$Out, [string]$Pids)
$ErrorActionPreference = 'Stop'
$known = @(Get-Process EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = New-Object -ComObject Excel.Application
# Solo es nuestro el proceso que no existía antes: el del usuario nunca se apunta.
Get-Process EXCEL -ErrorAction SilentlyContinue | Where-Object { $known -notcontains $_.Id } |
  ForEach-Object { if ($Pids) { Add-Content -Path $Pids -Value $_.Id } }
if ($app.Workbooks.Count -gt 0) {
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
  Write-Error 'SHARED_INSTANCE'
  exit 3
}
try {
  $app.Visible = $false
  $app.DisplayAlerts = $false
  $app.AskToUpdateLinks = $false
  $app.EnableEvents = $false
  $app.AutomationSecurity = 3
  $wb = $app.Workbooks.Open($In, 0, $false)
  try {
    $app.CalculateFull()
    $wb.SaveAs($Out, 51)
  } finally { $wb.Close($false) }
} finally {
  if ($app.Workbooks.Count -eq 0) { $app.Quit() }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""


def available() -> bool:
    from rinari.documents.adapters.render import office_apps

    return sys.platform == "win32" and office_apps().get("xlsx", False)


def calculate(source: Path, out_dir: Path, *, timeout_s: float = 300.0) -> Path:
    if not available():
        raise DocumentError(
            DocumentErrorCode.CALCULATION_BACKEND_UNAVAILABLE,
            "No certified calculation backend: Microsoft Excel is not installed",
            action="Formulas are kept; their results stay pending",
        )
    script = out_dir / "calculate.ps1"
    script.write_text(_SCRIPT, encoding="utf-8-sig")
    target = out_dir / "calculated.xlsx"
    pids = out_dir.parent / OWNED
    kwargs: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            str(source.resolve()),
            str(target.resolve()),
            str(pids.resolve()),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    deadline = time.monotonic() + timeout_s
    while process.poll() is None:
        if time.monotonic() > deadline:
            process.kill()
            kill_owned(out_dir.parent)
            raise DocumentError(
                DocumentErrorCode.CALCULATION_BACKEND_UNAVAILABLE, "Excel did not finish in time"
            )
        time.sleep(0.2)
    stderr = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace")
    if "SHARED_INSTANCE" in stderr:
        raise DocumentError(
            DocumentErrorCode.CALCULATION_BACKEND_UNAVAILABLE,
            "Excel could not be isolated: the instance has the user's workbooks open",
            action="Close Excel and retry; Rinari never recalculates in your session",
        )
    if process.returncode != 0 or not target.is_file():
        raise DocumentError(
            DocumentErrorCode.CALCULATION_BACKEND_UNAVAILABLE,
            "Excel could not calculate the workbook: "
            f"{stderr.strip()[-300:] or process.returncode}",
        )
    return target


def kill_owned(workdir: Path) -> None:
    """Termina solo los procesos que este trabajo apuntó como suyos."""
    path = workdir / OWNED
    if sys.platform != "win32" or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").split():
        if line.isdigit() and int(line) > 0:
            subprocess.run(
                ["taskkill", "/PID", line, "/T", "/F"],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )


__all__ = ["available", "calculate", "kill_owned"]
