"""Actualización de campos (índice, referencias, numeración) con el Word instalado.

Un campo TOC escrito no es un índice: sus números de página los calcula Word
al paginar. Este adaptador abre la revisión en Word (macros desactivadas,
sin alertas), actualiza los índices y los campos de ese documento y guarda
una copia. Solo toca su propio documento; el PID de su Word queda apuntado
para que cancelar no termine ningún otro proceso.
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
$known = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = New-Object -ComObject Word.Application
# Solo es nuestro el proceso que no existía antes: el del usuario nunca se apunta.
Get-Process WINWORD -ErrorAction SilentlyContinue | Where-Object { $known -notcontains $_.Id } |
  ForEach-Object { if ($Pids) { Add-Content -Path $Pids -Value $_.Id } }
$before = $app.Documents.Count
try {
  $app.Visible = $false
  $app.DisplayAlerts = 0
  $app.AutomationSecurity = 3
  $d = $app.Documents.Open($In, $false, $false, $false)
  try {
    foreach ($toc in $d.TablesOfContents) { [void]$toc.Update() }
    [void]$d.Fields.Update()
    foreach ($toc in $d.TablesOfContents) { [void]$toc.UpdatePageNumbers() }
    $d.SaveAs2($Out, 16)
  } finally { $d.Close([ref]0) }
} finally {
  if ($before -eq 0 -and $app.Documents.Count -eq 0) { $app.Quit([ref]0) }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""


def available() -> bool:
    from rinari.documents.adapters.render import office_apps

    return sys.platform == "win32" and office_apps().get("docx", False)


def update(source: Path, out_dir: Path, *, timeout_s: float = 300.0) -> Path:
    if not available():
        raise DocumentError(
            DocumentErrorCode.BACKEND_UNAVAILABLE,
            "Fields need Microsoft Word to be updated",
            action="The table of contents is kept; Word updates it with F9",
        )
    script = out_dir / "fields.ps1"
    script.write_text(_SCRIPT, encoding="utf-8-sig")
    target = out_dir / "fields.docx"
    from rinari.documents.adapters.excel_calc import mark_before

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
            str((out_dir.parent / OWNED).resolve()),
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
            from rinari.documents.adapters.excel_calc import kill_owned

            kill_owned(out_dir.parent)
            raise DocumentError(DocumentErrorCode.RENDER_FAILED, "Word did not finish in time")
        time.sleep(0.2)
    stderr = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace")
    if process.returncode != 0 or not target.is_file():
        raise DocumentError(
            DocumentErrorCode.RENDER_FAILED,
            f"Word could not update the fields: {stderr.strip()[-300:] or process.returncode}",
        )
    return target


__all__ = ["available", "update"]
