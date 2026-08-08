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

import httpx


class ToolError(ValueError):
    pass


def _shell_for_platform() -> str:
    """Devuelve el shell correcto para ejecutar comandos.

    Windows: busca el bash de Git (MSYS) POR RUTA — nunca confía en 'bash'
    del PATH, porque Windows resuelve 'bash' a System32\\bash.exe (WSL),
    que falla si WSL2 no está habilitado (o es lentísimo).
    Unix: usa sh.
    """
    if sys.platform != "win32":
        return "sh"
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    # fallback: SHELL si es MSYS, o 'bash' (podría ser WSL — mejor que nada)
    env_shell = os.environ.get("SHELL", "")
    if env_shell and "git" in env_shell.lower():
        return env_shell
    return "bash"


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
    shell = _shell_for_platform()
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
    backup = None
    if path.is_file():
        backup = _backup_file(cwd, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result = {"ok": True, "path": str(path.relative_to(Path(cwd).resolve()))}
    if backup:
        result["backup"] = backup
    return result


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
    backup = None
    if path.is_file():
        backup = _backup_file(cwd, path)
    path.write_text(new_content, encoding="utf-8")

    # Línea del primer cambio (1-indexed): posición del texto nuevo insertado
    idx = new_content.find(new)
    line = new_content[:idx].count("\n") + 1 if idx >= 0 else 1
    result = {"ok": True, "path": str(path.relative_to(Path(cwd).resolve())), "line": line, "count": count}
    if backup:
        result["backup"] = backup
    return result


def _backup_file(cwd: str, path: Path) -> str:
    """Copia un archivo a .rinari-undo/ antes de modificarlo. Devuelve la ruta del backup."""
    import shutil
    import time

    undo_dir = Path(cwd).resolve() / ".rinari-undo"
    undo_dir.mkdir(exist_ok=True)
    # ruta relativa al cwd, con separadores codificados para el nombre
    rel = path.resolve().relative_to(Path(cwd).resolve())
    rel_enc = str(rel).replace("\\", "__").replace("/", "__")
    name = f"{int(time.time() * 1000)}__{rel_enc}"
    dest = undo_dir / name
    shutil.copy2(path, dest)
    return str(dest)


def undo_edit(args: dict, cwd: str) -> dict:
    """Restaura el último backup de .rinari-undo/ del directorio de trabajo."""
    import shutil

    undo_dir = Path(cwd).resolve() / ".rinari-undo"
    if not undo_dir.is_dir():
        return {"ok": False, "error": "No hay ediciones para deshacer (sin backups)"}
    backups = sorted(undo_dir.iterdir(), key=lambda p: p.name)
    if not backups:
        return {"ok": False, "error": "No hay ediciones para deshacer (sin backups)"}
    latest = backups[-1]
    rel_enc = latest.name.split("__", 1)[1]
    rel = rel_enc.replace("__", "/")
    target = Path(cwd).resolve() / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(latest, target)
    latest.unlink()  # consumido
    return {"ok": True, "path": str(target.relative_to(Path(cwd).resolve()))}


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

    shell = _shell_for_platform()
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


def web_search(args: dict, cwd: str) -> dict:
    """Busca en la web con DuckDuckGo Lite (sin API key).

    Devuelve hasta 'limit' resultados: {title, url, snippet}.
    """
    from urllib.parse import unquote, urlparse
    from html import unescape

    query = (args.get("query") or "").strip()
    limit = int(args.get("limit", 5))
    if not query:
        return {"ok": False, "error": "Se requiere 'query'"}

    params = {"q": query}
    # UA simple: el UA completo (con detalles de Windows) dispara anti-bot 202
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get("https://lite.duckduckgo.com/lite/", params=params, headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "error": f"DuckDuckGo respondió HTTP {resp.status_code}"}
        html = resp.content.decode("utf-8", errors="replace")
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Error de red: {e}"}

    results = []
    # Bloques: <a ... class='result-link'>TITLE</a> ... <td class='result-snippet'>SNIPPET</td>
    title_re = re.compile(r"class=['\"]result-link['\"][^>]*>(.*?)</a>", re.DOTALL)
    snippet_re = re.compile(r"class=['\"]result-snippet['\"]>(.*?)</td>", re.DOTALL)
    titles = title_re.findall(html)
    snippets = snippet_re.findall(html)

    # URLs: el href viene como //duckduckgo.com/l/?uddg=<encoded>
    url_re = re.compile(r"href=\"([^\"]*uddg=([^\"]+?))&amp;rut=", re.DOTALL)
    raw_urls = url_re.findall(html)

    for i, title in enumerate(titles):
        clean_title = re.sub(r"<[^>]+>", "", title).strip()
        url = ""
        if i < len(raw_urls):
            raw = raw_urls[i][1]
            decoded = unquote(raw)
            url = unescape(decoded)
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
        results.append({"title": clean_title, "url": url, "snippet": snippet[:300]})
        if len(results) >= limit:
            break
    return {"ok": True, "query": query, "results": results}


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
    """Estado del repo: rama, ahead/behind, staged/unstaged/untracked separados."""
    proc = _git(cwd, "status", "--porcelain=v1", "--branch")
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "no es un repo git").strip()[:300]}
    branch = _git(cwd, "branch", "--show-current").stdout.strip() or "detached"
    ahead = behind = 0
    for line in proc.stdout.splitlines():
        if line.startswith("## "):
            # "## main...origin/main [ahead 2, behind 1]"
            import re

            m = re.search(r"\[ahead (\d+)", line)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", line)
            if m:
                behind = int(m.group(1))
    staged, unstaged, untracked = [], [], []
    for line in proc.stdout.splitlines():
        if line.startswith("## "):
            continue
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:].strip()
        entry = {"status": xy.strip() or "?", "path": path}
        if xy[0] != " " and xy[0] != "?":
            staged.append(entry)
        if xy[1] != " ":
            unstaged.append(entry)
        if xy[0] == "?":
            untracked.append(entry)
    return {
        "ok": True,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "clean": not (staged or unstaged or untracked),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "counts": {"staged": len(staged), "unstaged": len(unstaged), "untracked": len(untracked)},
        "changes": staged + unstaged + untracked,  # compat con callers viejos
    }


def git_diff(args: dict, cwd: str) -> dict:
    """Diff separado: staged, unstaged y untracked. NO muta el index."""
    max_lines = int(args.get("max_lines", 200))
    path = args.get("path") or ""
    path_args = ["--", path] if path else []

    # staged: index vs HEAD
    staged_proc = _git(cwd, "diff", "--cached", *path_args)
    # unstaged: working vs index
    unstaged_proc = _git(cwd, "diff", *path_args)
    # untracked: contenido de archivos sin trackear
    untracked_proc = _git(cwd, "ls-files", "--others", "--exclude-standard")
    untracked_files = [f for f in untracked_proc.stdout.splitlines() if f.strip()]
    if path:
        untracked_files = [f for f in untracked_files if f == path]

    untracked_content = ""
    for f in untracked_files:
        try:
            content = (Path(cwd) / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        untracked_content += f"--- {f}\n+++ {f} (untracked)\n"
        untracked_content += "".join(f"+{ln}\n" for ln in content.splitlines())
        untracked_content += "\n"

    sections = {
        "staged": staged_proc.stdout,
        "unstaged": unstaged_proc.stdout,
        "untracked": untracked_content,
    }
    combined = "\n".join(v for v in sections.values() if v.strip())
    lines = combined.splitlines()
    truncated = len(lines) > max_lines
    return {
        "ok": True,
        "diff": "\n".join(lines[:max_lines]),
        "truncated": truncated,
        "total_lines": len(lines),
        "staged": sections["staged"],
        "unstaged": sections["unstaged"],
        "untracked": sections["untracked"],
    }


def git_log(args: dict, cwd: str) -> dict:
    """Historial de commits recientes (hash corto, mensaje, autor, fecha)."""
    limit = int(args.get("limit", 10))
    proc = _git(
        cwd, "log", f"-{limit}",
        "--pretty=format:%h%x1f%s%x1f%an%x1f%ad", "--date=short",
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "sin commits aún").strip()[:300]}
    commits = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return {"ok": True, "commits": commits}


def git_branch(args: dict, cwd: str) -> dict:
    """Ramas locales: nombre, cuál es la actual, si está mergeada a HEAD."""
    proc = _git(cwd, "branch", "-v", "--no-color")
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "no es un repo git").strip()[:300]}
    current = _git(cwd, "branch", "--show-current").stdout.strip() or "detached"
    branches = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        marker, rest = line[0], line[1:].strip()
        parts = rest.split()
        if not parts:
            continue
        name = parts[0]
        branches.append({
            "name": name,
            "current": marker == "*",
            "hash": parts[1] if len(parts) > 1 else "",
        })
    return {"ok": True, "current": current, "branches": branches}


def git_stash(args: dict, cwd: str) -> dict:
    """Stash: push (guarda cambios) / list / pop (restaura el último)."""
    action = args.get("action", "list")
    if action == "push":
        proc = _git(cwd, "stash", "push", "-u")
    elif action == "pop":
        proc = _git(cwd, "stash", "pop")
    elif action == "list":
        proc = _git(cwd, "stash", "list")
    else:
        return {"ok": False, "error": f"Acción inválida: {action}. Usa push|list|pop"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "stash falló").strip()[:300]}
    if action == "list":
        stashes = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(":", 2)
            stashes.append({"ref": parts[0].strip(), "desc": parts[-1].strip() if len(parts) > 1 else ""})
        return {"ok": True, "stashes": stashes}
    return {"ok": True, "output": (proc.stdout or "").strip()}


def git_checkout(args: dict, cwd: str) -> dict:
    """Cambia de rama (o crea con -b)."""
    branch = args.get("branch", "").strip()
    if not branch:
        return {"ok": False, "error": "Se requiere el nombre de la rama"}
    create = bool(args.get("create", False))
    cmd = ["checkout", "-b", branch] if create else ["checkout", branch]
    proc = _git(cwd, *cmd)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "checkout falló").strip()[:300]}
    return {"ok": True, "branch": branch}


def git_pull(args: dict, cwd: str) -> dict:
    """git pull del upstream (peligrosa: requiere aprobación)."""
    proc = _git(cwd, "pull")
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "pull falló").strip()[:300]}
    return {"ok": True, "output": (proc.stdout or "").strip()}


def git_push(args: dict, cwd: str) -> dict:
    """git push del upstream (peligrosa: requiere aprobación)."""
    proc = _git(cwd, "push")
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "push falló").strip()[:300]}
    return {"ok": True, "output": (proc.stdout or "").strip()}


def _gh_remote_info(cwd: str) -> tuple[str, str] | None:
    """Extrae (owner, repo) del remote origin (https o ssh)."""
    proc = _git(cwd, "remote", "get-url", "origin")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    url = proc.stdout.strip()
    # https://github.com/owner/repo.git | git@github.com:owner/repo.git
    url = url.removesuffix(".git")
    if "github.com/" in url:
        parts = url.split("github.com/", 1)[1].split("/")
    elif "github.com:" in url:
        parts = url.split("github.com:", 1)[1].split("/")
    else:
        return None
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _gh_headers() -> dict:
    """Headers de la API de GitHub con el token de GITHUB_TOKEN si existe."""
    import os

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_create_pr(args: dict, cwd: str) -> dict:
    """Crea un PR en GitHub desde la rama actual (requiere GITHUB_TOKEN)."""
    import os

    title = (args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Se requiere 'title' para el PR"}
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN no está definido (export GITHUB_TOKEN=...)"}
    remote = _gh_remote_info(cwd)
    if remote is None:
        return {"ok": False, "error": "No se pudo determinar owner/repo del remote origin"}
    owner, repo = remote
    branch = _git(cwd, "branch", "--show-current").stdout.strip()
    if not branch:
        return {"ok": False, "error": "No hay rama actual (detached HEAD)"}
    base = args.get("base") or "main"
    payload = {
        "title": title,
        "head": branch,
        "base": base,
    }
    body = args.get("body")
    if body:
        payload["body"] = body
    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=_gh_headers(),
            json=payload,
            timeout=30,
        )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"No se pudo conectar a GitHub: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"GitHub respondió {resp.status_code}: {resp.text[:300]}"}
    data = resp.json()
    return {
        "ok": True,
        "number": data.get("number"),
        "url": data.get("html_url"),
        "title": data.get("title"),
    }


def github_list_prs(args: dict, cwd: str) -> dict:
    """Lista los PRs del repo (state: open|closed|all)."""
    import os

    remote = _gh_remote_info(cwd)
    if remote is None:
        return {"ok": False, "error": "No se pudo determinar owner/repo del remote origin"}
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN no está definido (export GITHUB_TOKEN=...)"}
    owner, repo = remote
    state = args.get("state") or "open"
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=_gh_headers(),
            params={"state": state, "per_page": args.get("limit", 10)},
            timeout=30,
        )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"No se pudo conectar a GitHub: {e}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"GitHub respondió {resp.status_code}: {resp.text[:300]}"}
    pulls = []
    for p in resp.json():
        pulls.append({
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "url": p.get("html_url"),
        })
    return {"ok": True, "pulls": pulls}


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
                "Estado del repositorio git: rama actual, ahead/behind del "
                "upstream, y cambios separados por staged/unstaged/untracked "
                "con conteos."
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
                "Diff de los cambios sin commitear, separado en staged, "
                "unstaged y untracked. NO modifica el index. Usa path para "
                "un archivo puntual y max_lines para limitar la salida."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Archivo específico a diffear (opcional)"},
                    "max_lines": {"type": "number", "description": "Máximo de líneas del diff (default 200)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": (
                "Historial de commits recientes: hash corto, mensaje, autor "
                "y fecha. Usa limit para cuántos traer (default 10)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "number", "description": "Número de commits (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": (
                "Lista las ramas locales: nombre, hash y cuál es la actual."
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
            "name": "git_stash",
            "description": (
                "Maneja el stash: push guarda los cambios (incluye untracked), "
                "list los enumera, pop restaura el último. MODIFICA EL ESTADO "
                "DEL REPO con push/pop — requiere aprobación."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "push | list | pop", "enum": ["push", "list", "pop"]},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": (
                "Cambia a otra rama (o la crea con create=true). Fallará si "
                "hay cambios sin commitear que choquen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Nombre de la rama"},
                    "create": {"type": "boolean", "description": "Crear la rama con -b (default false)"},
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": (
                "git pull del upstream. TRAE CAMBIOS REMOTOS — requiere aprobación."
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
            "name": "git_push",
            "description": (
                "git push del upstream. SUBE CAMBIOS AL REMOTO — requiere aprobación."
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
            "name": "github_create_pr",
            "description": (
                "Crea un Pull Request en GitHub desde la rama actual hacia "
                "base (default main). Requiere GITHUB_TOKEN en el entorno. "
                "SUBE DATOS AL REMOTO — requiere aprobación."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Título del PR"},
                    "body": {"type": "string", "description": "Descripción del PR (opcional)"},
                    "base": {"type": "string", "description": "Rama base (default: main)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_prs",
            "description": (
                "Lista los Pull Requests del repo (state: open|closed|all). "
                "Requiere GITHUB_TOKEN en el entorno."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "description": "open | closed | all (default open)"},
                    "limit": {"type": "number", "description": "Máximo de PRs (default 10)"},
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
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Busca en la web usando DuckDuckGo (sin API key). Devuelve "
                "resultados con título, url y snippet. Útil para documentación, "
                "errores conocidos, sintaxis, noticias."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Búsqueda (en español o inglés)"},
                    "limit": {"type": "number", "description": "Máximo de resultados (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bgit\s+push\b",
    r"\bgit\s+pull\b",
    r"github_create_pr",
    r"\bgit\s+stash\s+(push|pop|apply|drop)\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+commit\b",
    r"\bgit\s+checkout\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+rebase\b",
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
        "git_log": git_log,
        "git_branch": git_branch,
        "git_stash": git_stash,
        "git_checkout": git_checkout,
        "git_pull": git_pull,
        "git_push": git_push,
        "run_tests": run_tests,
        "web_search": web_search,
        "github_create_pr": github_create_pr,
        "github_list_prs": github_list_prs,
        }

    _DELEGATE_DEF = {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Delega una subtarea a un subagente: resúmenes, análisis de "
                "archivos, investigaciones puntuales. El subagente tiene sus "
                "propias tools (lee/escribe/git) pero NO puede delegar a su vez "
                "ni ejecutar comandos sin aprobación. Devuelve el resultado final."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "La subtarea a resolver (instrucción autocontenida)"},
                    "context": {"type": "string", "description": "Contexto adicional para el subagente (opcional)"},
                },
                "required": ["task"],
            },
        },
    }

    def __init__(self, delegate: bool = False):
        self._delegate = delegate

    def openai_schemas(self) -> list[dict]:
        if self._delegate:
            return TOOL_DEFS + [self._DELEGATE_DEF]
        return TOOL_DEFS

    def execute(self, name: str, args: dict, cwd: str) -> dict:
        if name == "delegate_task":
            if not self._delegate:
                raise ToolError("delegate_task no está disponible en este contexto")
            raise ToolError("delegate_task debe ejecutarse vía el agent loop")
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
