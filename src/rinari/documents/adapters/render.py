"""Render de documentos a PDF y de PDF a imágenes, con el backend disponible.

- PDF → PNG: pypdfium2, el renderer que ya usa el Engine.
- PPTX/DOCX/XLSX → PDF: Office local (COM por PowerShell) si está instalado,
  o LibreOffice si se instaló. Ninguno de los dos se descarga solo.

Office se abre con macros forzadas a desactivadas, sin actualizar vínculos y
en solo lectura. Se cierra únicamente el documento propio; la aplicación solo
se cierra si no tenía nada más abierto, así no se toca el trabajo del usuario.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

OWNED = "owned.pids"
_PROGIDS = {
    "pptx": "PowerPoint.Application",
    "docx": "Word.Application",
    "xlsx": "Excel.Application",
}

# Cada script recibe la entrada y la salida como argumentos; nunca texto
# interpolado en el código. AutomationSecurity 3 = msoAutomationSecurityForceDisable.
_SCRIPTS = {
    "pptx": r"""
param([string]$In, [string]$Out, [string]$Pids)
$ErrorActionPreference = 'Stop'
$known = @(Get-Process POWERPNT -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = $null
# Tras cancelar otro trabajo, COM puede tardar en aceptar un Office nuevo.
for ($try = 0; $try -lt 4 -and -not $app; $try++) {
  try { $app = New-Object -ComObject PowerPoint.Application
  } catch { Start-Sleep -Milliseconds 750 }
}
if (-not $app) { throw 'COM_UNAVAILABLE' }
# Solo es nuestro el proceso que no existía antes: el del usuario nunca se apunta.
Get-Process POWERPNT -ErrorAction SilentlyContinue | Where-Object { $known -notcontains $_.Id } |
  ForEach-Object { if ($Pids) { Add-Content -Path $Pids -Value $_.Id } }
$before = $app.Presentations.Count
$visible = $app.Visible
try {
  $app.AutomationSecurity = 3
  $p = $app.Presentations.Open($In, -1, 0, 0)
  try { $p.SaveAs($Out, 32) } finally { $p.Close() }
} finally {
  if ($before -eq 0 -and $app.Presentations.Count -eq 0 -and $visible -ne -1) { $app.Quit() }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
""",
    "docx": r"""
param([string]$In, [string]$Out, [string]$Pids)
$ErrorActionPreference = 'Stop'
$known = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = $null
# Tras cancelar otro trabajo, COM puede tardar en aceptar un Office nuevo.
for ($try = 0; $try -lt 4 -and -not $app; $try++) {
  try { $app = New-Object -ComObject Word.Application
  } catch { Start-Sleep -Milliseconds 750 }
}
if (-not $app) { throw 'COM_UNAVAILABLE' }
# Solo es nuestro el proceso que no existía antes: el del usuario nunca se apunta.
Get-Process WINWORD -ErrorAction SilentlyContinue | Where-Object { $known -notcontains $_.Id } |
  ForEach-Object { if ($Pids) { Add-Content -Path $Pids -Value $_.Id } }
$before = $app.Documents.Count
try {
  $app.Visible = $false
  $app.DisplayAlerts = 0
  $app.AutomationSecurity = 3
  $d = $app.Documents.Open($In, $false, $true, $false)
  try { $d.ExportAsFixedFormat($Out, 17) } finally { $d.Close([ref]0) }
} finally {
  if ($before -eq 0 -and $app.Documents.Count -eq 0) { $app.Quit([ref]0) }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
""",
    "xlsx": r"""
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
$before = $app.Workbooks.Count
try {
  $app.Visible = $false
  $app.DisplayAlerts = $false
  $app.AskToUpdateLinks = $false
  $app.EnableEvents = $false
  $app.AutomationSecurity = 3
  $wb = $app.Workbooks.Open($In, 0, $true)
  try { $wb.ExportAsFixedFormat(0, $Out) } finally { $wb.Close($false) }
} finally {
  if ($before -eq 0 -and $app.Workbooks.Count -eq 0) { $app.Quit() }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
""",
}


def office_apps() -> dict[str, bool]:
    """Qué aplicaciones de Office registran su servidor COM en este equipo."""
    if sys.platform != "win32" or os.environ.get("RINARI_DOCUMENTS_NO_OFFICE"):
        return {kind: False for kind in _PROGIDS}
    import winreg

    found = {}
    for kind, progid in _PROGIDS.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"):
                found[kind] = True
        except OSError:
            found[kind] = False
    return found


def libreoffice_path() -> str | None:
    configured = os.environ.get("RINARI_SOFFICE")
    if configured and Path(configured).is_file():
        return configured
    if os.environ.get("RINARI_DOCUMENTS_NO_LIBREOFFICE"):
        return None
    candidates = [shutil.which("soffice"), shutil.which("libreoffice")]
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            if base:
                candidates.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return next((c for c in candidates if c and Path(c).is_file()), None)


def office_renderers(kind: str) -> list[str]:
    """Backends capaces de convertir `kind` a PDF, en orden de preferencia."""
    backends = []
    if office_apps().get(kind):
        backends.append("office-com")
    if kind in _PROGIDS and libreoffice_path():
        backends.append("libreoffice")
    return backends


def to_pdf(
    source: Path, kind: str, out_dir: Path, *, timeout_s: float = 180.0
) -> tuple[bytes, str]:
    """Convierte un documento Office a PDF con el primer backend disponible."""
    if kind == "pdf":
        return source.read_bytes(), "native"
    backends = office_renderers(kind)
    if not backends:
        raise DocumentError(
            DocumentErrorCode.BACKEND_UNAVAILABLE,
            f"No renderer for {kind}: install Microsoft Office or LibreOffice",
            action="Install LibreOffice or Office to get previews",
        )
    errors: list[str] = []
    for backend in backends:
        try:
            if backend == "office-com":
                return _office(source, kind, out_dir, timeout_s), "office-com"
            return _libreoffice(source, out_dir, timeout_s), "libreoffice"
        except DocumentError as exc:
            errors.append(f"{backend}: {exc.message}")
    raise DocumentError(DocumentErrorCode.RENDER_FAILED, "; ".join(errors))


def _run(
    argv: list[str], timeout_s: float, *, owner: Path | None = None
) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        if owner is not None:
            from rinari.documents.adapters.excel_calc import kill_owned

            kill_owned(owner)
        raise DocumentError(DocumentErrorCode.RENDER_FAILED, "The renderer timed out") from exc


def _office(source: Path, kind: str, out_dir: Path, timeout_s: float) -> bytes:
    script = out_dir / f"render-{kind}.ps1"
    script.write_text(_SCRIPTS[kind], encoding="utf-8-sig")
    target = out_dir / "rendered.pdf"
    target.unlink(missing_ok=True)
    from rinari.documents.adapters.excel_calc import mark_before

    mark_before(out_dir.parent)
    result = _run(
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
        timeout_s,
        owner=out_dir.parent,
    )
    if result.returncode != 0 or not target.is_file():
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise DocumentError(
            DocumentErrorCode.RENDER_FAILED,
            f"Office could not export the file: {detail[-300:] or result.returncode}",
        )
    return target.read_bytes()


def _libreoffice(source: Path, out_dir: Path, timeout_s: float) -> bytes:
    binary = libreoffice_path()
    assert binary is not None
    with tempfile.TemporaryDirectory(prefix="rinari-lo-profile-") as profile:
        # Perfil propio por trabajo: no comparte bloqueo ni ajustes con una
        # instancia que el usuario tenga abierta.
        result = _run(
            [
                binary,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                f"-env:UserInstallation={Path(profile).resolve().as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(source),
            ],
            timeout_s,
        )
    target = out_dir / f"{source.stem}.pdf"
    if result.returncode != 0 or not target.is_file():
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise DocumentError(
            DocumentErrorCode.RENDER_FAILED, f"LibreOffice could not convert: {detail[-300:]}"
        )
    return target.read_bytes()


def pdf_page_count(data: bytes) -> int:
    import pypdfium2 as pdfium

    from rinari.artifacts.attachments import _PDFIUM_LOCK

    with _PDFIUM_LOCK, contextlib.closing(pdfium.PdfDocument(data)) as document:
        return len(document)


def pdf_to_png(
    data: bytes, pages: list[int] | None = None, *, max_side: int = 1600
) -> list[dict[str, Any]]:
    """Páginas (1-based) a PNG con su tamaño; todas si `pages` es None."""
    import pypdfium2 as pdfium

    from rinari.artifacts.attachments import _PDFIUM_LOCK

    out: list[dict[str, Any]] = []
    with _PDFIUM_LOCK, contextlib.closing(pdfium.PdfDocument(data)) as document:
        count = len(document)
        wanted = pages or list(range(1, count + 1))
        for number in wanted:
            if not 1 <= number <= count:
                raise DocumentError(
                    DocumentErrorCode.INVALID_SPEC, f"Page {number} is outside 1..{count}"
                )
            with contextlib.closing(document[number - 1]) as page:
                width, height = page.get_size()
                scale = min(4.0, max_side / max(width, height))
                with (
                    contextlib.closing(page.render(scale=scale)) as bitmap,
                    contextlib.closing(bitmap.to_pil()) as image,
                ):
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG", optimize=True)
                    out.append(
                        {
                            "page": number,
                            "png": buffer.getvalue(),
                            "width": image.width,
                            "height": image.height,
                        }
                    )
    return out
