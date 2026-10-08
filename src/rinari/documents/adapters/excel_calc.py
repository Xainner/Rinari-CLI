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
$app = $null
# Tras cancelar otro trabajo, COM puede tardar en aceptar un Office nuevo.
for ($try = 0; $try -lt 4 -and -not $app; $try++) {
  try { $app = New-Object -ComObject Excel.Application
  } catch { Start-Sleep -Milliseconds 750 }
}
if (-not $app) { throw 'COM_UNAVAILABLE' }
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
    mark_before(out_dir.parent)
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


BEFORE = "office.before"
_AUTOMATION_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='EXCEL.EXE' OR Name='WINWORD.EXE' OR "
    "Name='POWERPNT.EXE'\" | Where-Object { $_.CommandLine -match 'automation|Embedding' } | "
    "ForEach-Object { $_.ProcessId }"
)


def automation_pids() -> set[int]:
    """Procesos de Office abiertos por COM (`/automation -Embedding`), nunca los del usuario."""
    if sys.platform != "win32":
        return set()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _AUTOMATION_QUERY],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return {int(line) for line in result.stdout.split() if line.isdigit()}


def mark_before(workdir: Path) -> None:
    """Antes de lanzar Office: qué procesos de automatización ya existían."""
    if sys.platform != "win32":
        return
    try:
        (workdir / BEFORE).write_text(
            " ".join(str(p) for p in sorted(automation_pids())), encoding="utf-8"
        )
    except Exception:
        return


def kill_owned(workdir: Path) -> None:
    """Termina solo los procesos que este trabajo arrancó.

    Los apuntados en `owned.pids` y, si el trabajo se canceló mientras Office
    arrancaba (antes de poder apuntarlo), los procesos de automatización que
    no existían cuando el trabajo los fue a lanzar. Un Office del usuario no
    se abre en modo automatización: nunca entra.
    """
    if sys.platform != "win32":
        return
    pids: set[int] = set()
    path = workdir / OWNED
    if path.is_file():
        pids |= {
            int(x) for x in path.read_text(encoding="utf-8", errors="ignore").split() if x.isdigit()
        }
    before = workdir / BEFORE
    if before.is_file():
        known = {int(x) for x in before.read_text(encoding="utf-8").split() if x.isdigit()}
        pids |= automation_pids() - known
    for pid in sorted(pids):
        if pid > 0:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )


__all__ = ["available", "calculate", "kill_owned"]
