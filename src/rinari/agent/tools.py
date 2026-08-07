"""Tool registry para el modo agente.

Herramientas disponibles para el modelo (OpenAI function calling):
- run_command : ejecuta un comando en el cwd (bash en Windows, sh en Unix)
- read_file   : lee un archivo (con límite de líneas)
- write_file  : crea/sobrescribe un archivo
- search_files: regex dentro de archivos
- list_dir    : lista un directorio

Seguridad: paths se validan contra escape del cwd; run_command acepta
aprobación externa (approval) para comandos peligrosos.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path


class ToolError(ValueError):
    pass


def _resolve(cwd: str | Path, path: str) -> Path:
    """Resuelve un path relativo al cwd y valida que no se escape."""
    base = Path(cwd).resolve()
    target = (base / path).resolve()
    if base not in target.parents and target != base:
        raise ToolError(f"Path '{path}' está fuera del directorio de trabajo")
    return target


def run_command(args: dict, cwd: str) -> dict:
    """Ejecuta un comando y devuelve stdout/stderr/exit_code.

    Windows: mata el ÁRBOL de procesos en timeout (taskkill /T) — el
    bash.exe de MSYS deja hijos (sleep, etc.) que mantienen los pipes
    abiertos si solo matamos el padre.
    """
    command = args.get("command", "")
    timeout = float(args.get("timeout", 30))
    shell = os.environ.get("SHELL", "bash" if sys.platform == "win32" else "sh")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            [shell, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            creationflags=creationflags,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return {
                "stdout": (stdout or "")[-8000:],
                "stderr": (stderr or "")[-4000:],
                "exit_code": proc.returncode,
                "error": None,
            }
        except subprocess.TimeoutExpired:
            # Matar el árbol completo de procesos
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                import signal

                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            stdout, stderr = proc.communicate(timeout=10)
            return {
                "stdout": (stdout or "")[-8000:],
                "stderr": (stderr or "")[-4000:],
                "exit_code": -1,
                "error": f"Comando excedió el timeout de {timeout}s",
            }
    except FileNotFoundError as e:
        return {"stdout": "", "stderr": "", "exit_code": -1, "error": f"Shell no encontrado: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"stdout": "", "stderr": "", "exit_code": -1, "error": str(e)}


def read_file(args: dict, cwd: str) -> dict:
    path = _resolve(cwd, args.get("path", ""))
    max_lines = int(args.get("max_lines", 500))
    if not path.is_file():
        return {"exists": False, "error": f"Archivo no existe: {args.get('path')}"}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    shown = lines[:max_lines]
    truncated = total > max_lines
    return {
        "exists": True,
        "content": "\n".join(shown),
        "total_lines": total,
        "truncated": truncated,
    }


def write_file(args: dict, cwd: str) -> dict:
    path = _resolve(cwd, args.get("path", ""))
    content = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path.relative_to(Path(cwd).resolve()))}


def search_files(args: dict, cwd: str) -> dict:
    pattern = args.get("pattern", "")
    search_path = args.get("path", ".")
    limit = int(args.get("limit", 50))
    base = Path(cwd).resolve()
    target = (base / search_path).resolve() if search_path != "." else base
    if base not in target.parents and target != base:
        raise ToolError(f"Path '{search_path}' está fuera del directorio de trabajo")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"error": f"Regex inválida: {e}", "matches": []}

    matches = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith((".git", "node_modules", "__pycache__", ".venv"))]
        for fname in files:
            fpath = Path(root) / fname
            try:
                for lineno, line in enumerate(
                    fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if rx.search(line):
                        rel = fpath.relative_to(base)
                        matches.append({"file": str(rel), "line": lineno, "content": line[:200]})
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
            except (OSError, UnicodeDecodeError):
                continue
    return {"matches": matches, "truncated": False}


def list_dir(args: dict, cwd: str) -> dict:
    path = args.get("path", ".")
    base = Path(cwd).resolve()
    target = (base / path).resolve() if path != "." else base
    if base not in target.parents and target != base:
        raise ToolError(f"Path '{path}' está fuera del directorio de trabajo")
    if not target.is_dir():
        return {"error": f"No es un directorio: {path}", "entries": []}
    entries = []
    for p in sorted(target.iterdir()):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        entries.append(
            {
                "name": str(rel),
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else None,
            }
        )
    return {"entries": entries}


TOOL_DEFS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Ejecuta un comando shell en el directorio de trabajo. "
                "Usa bash (git-bash en Windows). Devuelve stdout, stderr y exit code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando a ejecutar"},
                    "timeout": {"type": "number", "description": "Timeout en segundos (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lee un archivo de texto (UTF-8). Devuelve contenido, total de líneas y si fue truncado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al cwd"},
                    "max_lines": {"type": "number", "description": "Máximo de líneas (default 500)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Crea o sobrescribe un archivo con contenido completo (UTF-8).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al cwd"},
                    "content": {"type": "string", "description": "Contenido completo del archivo"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Busca una regex dentro de archivos del proyecto. Devuelve file, línea y contenido.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex a buscar"},
                    "path": {"type": "string", "description": "Directorio a buscar (default '.')"},
                    "limit": {"type": "number", "description": "Máximo de matches (default 50)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Lista un directorio del proyecto (nombres, tipo, tamaño).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relativo al cwd (default '.')"}},
                "required": [],
            },
        },
    },
]

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bcurl\b.*\|\s*(ba|z)?sh\b",
    r"\bsudo\b",
    r"\bshutdown\b|\breboot\b",
    r"\brm\s+[^|]*/\.\*",
]

DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


def is_dangerous(command: str) -> bool:
    """Detecta comandos potencialmente destructivos (para aprobación)."""
    return bool(DANGEROUS_RE.search(command))


class ToolRegistry:
    """Registro de herramientas: ejecuta por nombre + expone schemas OpenAI."""

    _TOOLS = {
        "run_command": run_command,
        "read_file": read_file,
        "write_file": write_file,
        "search_files": search_files,
        "list_dir": list_dir,
    }

    def openai_schemas(self) -> list[dict]:
        return TOOL_DEFS

    def execute(self, name: str, args: dict, cwd: str) -> dict:
        fn = self._TOOLS.get(name)
        if fn is None:
            raise ToolError(f"Herramienta desconocida: {name}")
        try:
            result = fn(args, cwd)
            if not isinstance(result, dict):
                result = {"result": result}
            return result
        except ToolError as e:
            return {"error": str(e)}
