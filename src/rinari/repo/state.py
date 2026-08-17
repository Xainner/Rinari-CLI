"""Repository state detection: languages, frameworks, commands, generated hints.

Pure, deterministic, file-based analysis (no subprocess, no network). The
result feeds the prompt environment segment (phase 3 "Repository state")
and will feed the repository index later. Detection is intentionally
conservative: a hint is only emitted for markers that actually exist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCAN_ENTRY_LIMIT = 50_000

_GENERATED_DIRS = {
    "node_modules",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    ".cache",
}

_GENERATED_FILES = {
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
}

_LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "c++",
    ".cc": "c++",
    ".hpp": "c++",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".sh": "shell",
    ".ps1": "powershell",
    ".lua": "lua",
    ".zig": "zig",
}


@dataclass(frozen=True, slots=True)
class CommandHint:
    command: str
    source: str


@dataclass(slots=True)
class RepositorySummary:
    root: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    build: list[CommandHint] = field(default_factory=list)
    test: list[CommandHint] = field(default_factory=list)
    lint: list[CommandHint] = field(default_factory=list)
    typecheck: list[CommandHint] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    scanned_files: int = 0

    def to_prompt_dict(self) -> dict:
        def _first(hints: list[CommandHint]) -> str | None:
            return hints[0].command if hints else None

        return {
            "languages": ", ".join(self.languages) or "-",
            "frameworks": ", ".join(self.frameworks) or "-",
            "package_managers": ", ".join(self.package_managers) or "-",
            "build_command": _first(self.build),
            "test_command": _first(self.test),
            "lint_command": _first(self.lint),
            "typecheck_command": _first(self.typecheck),
            "generated_dirs": ", ".join(self.generated) or "-",
        }


def analyze_repository(root: str | Path) -> RepositorySummary:
    root_path = Path(root).expanduser().resolve()
    summary = RepositorySummary(root=str(root_path))
    if not root_path.is_dir():
        return summary

    ext_counts: dict[str, int] = {}
    entries_seen = 0
    for path in _walk(root_path):
        entries_seen += 1
        if entries_seen > SCAN_ENTRY_LIMIT:
            break
        if path.suffix in _LANGUAGE_EXTENSIONS:
            language = _LANGUAGE_EXTENSIONS[path.suffix]
            ext_counts[language] = ext_counts.get(language, 0) + 1

    js_deps = _read_js_deps(root_path / "package.json")
    python_deps = _read_python_deps(root_path / "pyproject.toml")

    summary.languages = [lang for lang, _c in sorted(ext_counts.items(), key=lambda kv: -kv[1])]
    summary.markers = sorted(_present_markers(root_path))
    summary.generated = sorted({entry.name for entry in _top_level_entries(root_path)})
    _package_managers(root_path, summary)
    _frameworks(root_path, js_deps, python_deps, summary)
    _build_test_commands(root_path, js_deps, summary)
    _lint_typecheck(root_path, summary)
    summary.scanned_files = entries_seen
    return summary


# -- scanning helpers -----------------------------------------------------------


def _walk(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name in _GENERATED_DIRS or child.name.endswith(".egg-info"):
                    continue
                stack.append(child)
            elif child.is_file():
                yield child


def _top_level_entries(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.name in _GENERATED_DIRS and p.is_dir())
    except OSError:
        return []


def _present_markers(root: Path) -> set[str]:
    names = {p.name for p in root.iterdir()} if root.is_dir() else set()
    return names


def _read_js_deps(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.update({k: str(v) for k, v in value.items()})
    return deps


def _read_python_deps(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _toml_dep_names(text)


def _toml_dep_names(text: str) -> list[str]:
    # Deliberately simple: we only need to know which *known* dependencies
    # appear (as `dep = "..."` entries or `"dep<spec>"` list items). We do not
    # need full TOML parsing for framework hints.
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("[", "#", "dependencies", "deps")):
            continue
        for dep in _KNOWN_PYTHON_DEPS:
            if re.search(rf"\b{re.escape(dep)}\b", stripped):
                names.append(dep)
    return names


_KNOWN_PYTHON_DEPS = (
    "django",
    "djangorestframework",
    "fastapi",
    "flask",
    "starlette",
    "pydantic",
    "sqlalchemy",
    "pytest",
    "hypothesis",
    "typer",
    "click",
    "httpx",
    "requests",
)


# -- specific detections ------------------------------------------------------------


def _package_managers(root: Path, summary: RepositorySummary) -> None:
    def has(name: str) -> bool:
        return (root / name).exists()

    managers: list[str] = []
    if has("pyproject.toml"):
        if has("uv.lock"):
            managers.append("uv")
        elif has("poetry.lock"):
            managers.append("poetry")
        else:
            managers.append("pip")
    if has("package-lock.json"):
        managers.append("npm")
    if has("yarn.lock"):
        managers.append("yarn")
    if has("pnpm-lock.yaml"):
        managers.append("pnpm")
    if has("Cargo.toml"):
        managers.append("cargo")
    if has("go.mod"):
        managers.append("go")
    if has("pom.xml"):
        managers.append("maven")
    if has("build.gradle") or has("build.gradle.kts"):
        managers.append("gradle")
    if has("Gemfile"):
        managers.append("bundler")
    summary.package_managers = managers


def _js_run(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _scripts(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts")
    return {k: str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


_JS_FRAMEWORKS = {
    "react": "react",
    "vue": "vue",
    "svelte": "svelte",
    "next": "next.js",
    "nuxt": "nuxt",
    "express": "express",
    "fastify": "fastify",
    "angular": "angular",
}


def _frameworks(
    root: Path, js_deps: dict[str, str], python_deps: list[str], summary: RepositorySummary
) -> None:
    frameworks: list[str] = []
    if (root / "manage.py").is_file():
        frameworks.append("django")
    for dep in python_deps:
        if dep == "flask":
            frameworks.append("flask")
        elif dep == "fastapi":
            frameworks.append("fastapi")
        elif dep == "django":
            frameworks.append("django")
    for dep in js_deps:
        if dep in _JS_FRAMEWORKS:
            frameworks.append(_JS_FRAMEWORKS[dep])
    if (root / "go.mod").is_file():
        frameworks.append("go-modules")
    if (root / "settings.gradle").is_file() or (root / "settings.gradle.kts").is_file():
        frameworks.append("gradle-project")
    # De-duplicate preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for f in frameworks:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    summary.frameworks = deduped


def _build_test_commands(root: Path, js_deps: dict[str, str], summary: RepositorySummary) -> None:
    js = _js_run(root)
    scripts = _scripts(root / "package.json")
    if "build" in scripts:
        summary.build.append(CommandHint(f"{js} run build", "package.json scripts.build"))
    if (root / "Cargo.toml").is_file():
        summary.build.append(CommandHint("cargo build", "Cargo.toml"))
    if (root / "go.mod").is_file():
        summary.build.append(CommandHint("go build ./...", "go.mod"))
    if (root / "pom.xml").is_file():
        summary.build.append(CommandHint("mvn -q -DskipTests package", "pom.xml"))
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        summary.build.append(CommandHint("./gradlew build", "build.gradle*"))
    if (root / "pyproject.toml").is_file() and (root / "uv.lock").exists():
        summary.build.append(CommandHint("uv build", "pyproject.toml [build-system]"))

    if "test" in scripts:
        summary.test.append(CommandHint(f"{js} run test", "package.json scripts.test"))
    if (root / "Cargo.toml").is_file():
        summary.test.append(CommandHint("cargo test", "Cargo.toml"))
    if (root / "go.mod").is_file():
        summary.test.append(CommandHint("go test ./...", "go.mod"))
    if (root / "pom.xml").is_file():
        summary.test.append(CommandHint("mvn -q test", "pom.xml"))
    if (root / "Gemfile").is_file() and (root / "spec").is_dir():
        summary.test.append(CommandHint("bundle exec rspec", "Gemfile + spec/"))
    if (
        (root / "pyproject.toml").is_file()
        or (root / "pytest.ini").is_file()
        or (root / "tests").is_dir()
    ):
        has_pytest = (
            "pytest" in _read_python_deps(root / "pyproject.toml")
            or (root / "pytest.ini").is_file()
        )
        if has_pytest or (root / "tests").is_dir():
            runner = "uv run pytest" if (root / "uv.lock").exists() else "python -m pytest"
            source = "pyproject.toml pytest dep or pytest.ini" if has_pytest else "tests/ directory"
            summary.test.append(CommandHint(runner, source))


def _lint_typecheck(root: Path, summary: RepositorySummary) -> None:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = ""
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "[tool.ruff]" in text:
            summary.lint.append(
                CommandHint(
                    "uv run ruff check ." if (root / "uv.lock").exists() else "ruff check .",
                    "pyproject.toml [tool.ruff]",
                )
            )
        if "[tool.mypy]" in text or re.search(r'["\']?mypy["\']?\s*=', text):
            summary.typecheck.append(
                CommandHint(
                    "uv run mypy ." if (root / "uv.lock").exists() else "mypy .",
                    "pyproject.toml [tool.mypy]/mypy dep",
                )
            )
    if (
        (root / "eslint.config.js").is_file()
        or (root / "eslint.config.mjs").is_file()
        or (root / ".eslintrc.json").is_file()
    ):
        summary.lint.append(
            CommandHint(f"{_js_run(root)} run lint || npx eslint .", "eslint config")
        )
    if (root / ".rubocop.yml").is_file():
        summary.lint.append(CommandHint("bundle exec rubocop", ".rubocop.yml"))

    scripts = _scripts(root / "package.json")
    if "typecheck" in scripts:
        summary.typecheck.append(
            CommandHint(f"{_js_run(root)} run typecheck", "package.json scripts.typecheck")
        )
    elif (root / "tsconfig.json").is_file():
        summary.typecheck.append(CommandHint(f"{_js_run(root)} run tsc --noEmit", "tsconfig.json"))
