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


def edit_file(args: dict, cwd: str) -> dict:
    """Edita un archivo reemplazando un bloque (old → new).

    Más quirúrgico que write_file: solo toca la parte indicada.
    - old debe ser único (o pasar count=N para N ocurrencias)
    - Devuelve la línea donde se aplicó el cambio
    """
    path = _resolve(cwd, args.get("path", ""))
    old = args.get("old", "")
    new = args.get("new", "")
    count = int(args.get("count", 1))
    if not old:
        return {"ok": False, "error": "Se requiere 'old' (texto a reemplazar)"}
    if not path.is_file():
        return {"ok": False, "error": f"Archivo no existe: {args.get('path')}"}

    content = path.read_text(encoding="utf-8", errors="replace")
    occurrences = content.count(old)
    if occurrences == 0:
        return {"ok": False, "error": f"No se encontró el texto en {args.get('path')}"}
    if occurrences < count:
        return {
            "ok": False,
            "error": f"Se encontraron {occurrences} ocurrencia(s), se pidieron {count}",
        }
    if count == 1 and occurrences > 1:
        return {
            "ok": False,
            "error": (
                f"Ambigüedad: el texto aparece {occurrences} veces — incluye más "
                f"contexto o usa count={occurrences}"
            ),
        }

    new_content = content.replace(old, new, count)
    path.write_text(new_content, encoding="utf-8")

    # Línea del primer cambio (1-indexed): posición del texto nuevo insertado
    idx = new_content.find(new)
    line = new_content[:idx].count("\n") + 1 if idx >= 0 else 1
    return {"ok": True, "path": str(path.relative_to(Path(cwd).resolve())), "line": line, "count": count}


def run_tests(args: dict, cwd: str) -> dict:
    """Detecta el framework de tests del repo y ejecuta la suite.

    - pytest si hay pyproject.toml con [tool.pytest] o pytest.ini o tests/
    - npm test si hay package.json con script test
    - --command overridea la detección
    """
    command = args.get("command")
    max_output = int(args.get("max_output", 4000))
    base = Path(cwd).resolve()
    framework = None

    if command:
        framework = "custom"
    else:
        # pytest: pyproject.toml con config pytest, pytest.ini, o dir tests/ con .py
        has_pytest_config = (
            (base / "pytest.ini").exists()
            or (base / "pyproject.toml").exists()
            and "[tool.pytest" in (base / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        )
        tests_dir = base / "tests"
        if has_pytest_config or (tests_dir.is_dir() and any(tests_dir.glob("test_*.py"))):
            framework = "pytest"
            # Proyecto uv (uv.lock) → uv run pytest usa el venv con deps
            if (base / "uv.lock").exists() or (base / ".venv").exists():
                command = "uv run pytest -q"
            else:
                command = "python -m pytest -q"
        elif (base / "package.json").exists():
            try:
                import json as _json

                pkg = _json.loads((base / "package.json").read_text(encoding="utf-8"))
                if "test" in (pkg.get("scripts") or {}):
                    framework = "npm"
                    command = "npm test"
            except (ValueError, OSError):
                pass

    if not command:
        return {
            "ok": False,
            "error": "No se encontró un framework de tests (pytest/npm). Pasa --command para usar uno custom.",
        }

    shell = os.environ.get("SHELL", "bash" if sys.platform == "win32" else "sh")
    try:
        proc = subprocess.run(
            [shell, "-c", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "framework": framework, "exit_code": -1, "output": "", "error": "Tests excedieron 300s"}
    output = proc.stdout + proc.stderr
    truncated = len(output) > max_output
    return {
        "ok": proc.returncode == 0,
        "framework": framework,
        "exit_code": proc.returncode,
        "output": output[:max_output],
        "truncated": truncated,
    }


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Ejecuta git con encoding seguro (MSYS emite bytes no-UTF8)."""
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def git_status(args: dict, cwd: str) -> dict:
    """Estado del repo: rama, limpio?, cambios (modificados/nuevos/borrados)."""
    proc = _git(cwd, "status", "--porcelain=v1")
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "no es un repo git").strip()[:300]}
    branch = _git(cwd, "branch", "--show-current").stdout.strip() or "detached"
    changes = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2].strip(), line[3:].strip()
        changes.append({"status": status or "?", "path": path})
    return {
        "ok": True,
        "branch": branch,
        "clean": len(changes) == 0,
        "changes": changes,
    }


def git_diff(args: dict, cwd: str) -> dict:
    """Diff de los cambios sin commitear (working tree + staged + untracked)."""
    max_lines = int(args.get("max_lines", 200))
    # Marca untracked como intent-to-add para que aparezcan en el diff
    _git(cwd, "add", "-N", ".")
    proc = _git(cwd, "diff", "HEAD")
    if proc.returncode != 0:
        # Repo sin commits: diff del working tree
        proc = _git(cwd, "diff")
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "no es un repo git").strip()[:300]}
    # Limpia el index (no toca el working tree)
    _git(cwd, "reset", "-q")
    diff = proc.stdout
    lines = diff.splitlines()
    truncated = len(lines) > max_lines
    return {
        "ok": True,
        "diff": "\n".join(lines[:max_lines]),
        "truncated": truncated,
        "total_lines": len(lines),
    }


def git_commit(args: dict, cwd: str) -> dict:
    """Hace git add -A + commit con el mensaje dado."""
    message = args.get("message", "").strip()
    if not message:
        return {"ok": False, "error": "Se requiere un mensaje de commit"}
    add = _git(cwd, "add", "-A")
    if add.returncode != 0:
        return {"ok": False, "error": (add.stderr or "git add falló").strip()[:300]}
    commit = _git(cwd, "commit", "-m", message)
    if commit.returncode != 0:
        return {"ok": False, "error": (commit.stderr or "git commit falló").strip()[:300]}
    return {"ok": True, "commit": commit.stdout.strip().splitlines()[-1] if commit.stdout.strip() else None}


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
            "name": "edit_file",
            "description": (
                "Edita un archivo reemplazando un bloque de texto (old → new) sin "
                "reescribir el archivo completo. El 'old' debe ser único en el archivo "
                "(usa count=N si hay repeticiones, o incluye más contexto). Devuelve la línea del cambio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relativo al cwd"},
                    "old": {"type": "string", "description": "Texto exacto a reemplazar (puede ser multilínea)"},
                    "new": {"type": "string", "description": "Texto de reemplazo"},
                    "count": {"type": "number", "description": "Cuántas ocurrencias reemplazar (default 1)"},
                },
                "required": ["path", "old", "new"],
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
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Estado del repositorio git: rama actual, si está limpio, y la "
                "lista de cambios (modificados, nuevos, borrados)."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Diff de los cambios sin commitear (working tree + staged). "
                "Usa max_lines para limitar la salida."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_lines": {"type": "number", "description": "Máximo de líneas del diff (default 200)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "Hace git add -A y commit de todos los cambios con el mensaje dado. "
                "MODIFICA EL HISTORIAL — requiere aprobación."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Mensaje del commit (conventional: feat:, fix:, docs:)"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Detecta y ejecuta la suite de tests del repo (pytest o npm test). "
                "Usa --command para un comando custom. Devuelve exit code y output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando custom (opcional, overridea la detección)"},
                    "max_output": {"type": "number", "description": "Máximo de chars del output (default 4000)"},
                },
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
    r"\bgit\s+commit\b",
    r"\bgit\s+checkout\s+--\b",
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
        "edit_file": edit_file,
        "search_files": search_files,
        "list_dir": list_dir,
        "git_status": git_status,
        "git_diff": git_diff,
        "git_commit": git_commit,
        "run_tests": run_tests,
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
