"""`rinari eval` (commands.md 51): list, run, show, report, compare,
history, create, validate — over the built-in deterministic suites and
externally written case files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import typer

from rinari.cli.deps import app_context, is_json, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.evals import builtins
from rinari.evals.reports import compare, list_reports, load_report, new_run_id, save_report
from rinari.evals.runner import run_suite

eval_app = typer.Typer(
    help="Run and inspect deterministic evaluation suites.", no_args_is_help=True
)

TEMPLATE = '''"""External eval case file (scaffolded by `rinari eval create`).

Run it with:  rinari eval run --file {name}.py
Validate it:  rinari eval validate {name}.py
"""

from __future__ import annotations

from rinari.evals.scripted import answer, calls
from rinari.evals.spec import EvalCase

CASES: list[EvalCase] = [
    EvalCase(
        case_id="{suite}.{slug}",
        suite="{suite}",
        name="{slug}",
        description="Describe what property of the harness this case proves.",
        prompt="Describe the task the scripted model will 'perform'.",
        project=True,
        setup=lambda f: [f.file("example.txt", "v1\\n")],
        script=lambda f: [
            calls(("fs.read", {{"path": "example.txt"}})),
            answer("Done."),
        ],
        expectations=lambda run: [
            # Example: assert the inspect-before-edit trajectory.
            # _ok("read_ok", any(t["name"] == "fs.read" and t["ok"] for t in run.tools)),
        ],
    ),
]
'''


def _find_case(case_id: str):
    for case in builtins.all_cases():
        if case.case_id == case_id or case.case_id.split(".", 1)[-1] == case_id:
            return case
    return None


@eval_app.command("list")
@with_error_handling("eval.list")
def eval_list(
    ctx: typer.Context,
    suite: str = typer.Option(None, "--suite", "-s", help="Show one suite only."),
    case: bool = typer.Option(False, "--case", help="List case ids (machine readable)."),
) -> None:
    """List the built-in eval suites and cases."""
    suites = builtins.suites()
    if suite is not None and suite not in suites:
        known = ", ".join(sorted(suites))
        typer.echo(f"Unknown suite: {suite} (known: {known})", err=True)
        raise typer.Exit(2)
    selected = suites if suite is None else {suite: suites[suite]}
    if is_json(ctx) or case:
        rows = [
            {"suite": name, "case_id": c.case_id, "description": c.description}
            for name in sorted(selected)
            for c in selected[name]
        ]
        if is_json(ctx):
            emit_json(success_envelope("eval.list", rows))
            return
        for row in rows:
            typer.echo(row["case_id"])
        return
    for name in sorted(selected):
        typer.echo(f"{name} ({len(selected[name])} cases)")
        for c in selected[name]:
            typer.echo(f"  {c.case_id}  — {c.description}")


@eval_app.command("show")
@with_error_handling("eval.show")
def eval_show(ctx: typer.Context, case_id: str = typer.Argument(...)) -> None:
    """Show one case's metadata."""
    case = _find_case(case_id)
    if case is None:
        typer.echo(f"Unknown case: {case_id}", err=True)
        raise typer.Exit(7)
    data = {
        "case_id": case.case_id,
        "suite": case.suite,
        "name": case.name,
        "description": case.description,
        "profile": case.profile,
        "project": case.project,
        "window": case.window,
        "prompts": list(case.all_prompts()),
    }
    if is_json(ctx):
        emit_json(success_envelope("eval.show", data))
        return
    for key, value in data.items():
        typer.echo(f"{key}: {value}")


@eval_app.command("run")
@with_error_handling("eval.run")
def eval_run(
    ctx: typer.Context,
    suite: str = typer.Argument(None, help="Suite to run (default: all)."),
    case: str = typer.Option(None, "--case", "-c", help="Run a single case id."),
    file: str = typer.Option(None, "--file", help="Run a case file (see eval create)."),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop at the first failure."),
) -> None:
    """Run eval suites (deterministic; no network)."""
    with app_context(ctx) as c:
        layout = c.layout
        run_id = new_run_id()
        scratch = layout.dir("evals") / "scratch" / run_id
        scratch.mkdir(parents=True, exist_ok=True)

        if file:
            cases = _load_case_file(Path(file))
        elif case is not None:
            one = _find_case(case)
            if one is None:
                typer.echo(f"Unknown case: {case}", err=True)
                raise typer.Exit(7)
            cases = (one,)
        else:
            all_suites = builtins.suites()
            if suite is None:
                cases = tuple(c for group in all_suites.values() for c in group)
            elif suite not in all_suites:
                typer.echo(f"Unknown suite: {suite}", err=True)
                raise typer.Exit(2)
            else:
                cases = all_suites[suite]
        if not cases:
            typer.echo("No cases selected.", err=True)
            raise typer.Exit(2)

        def _progress(eval_case):
            if not is_json(ctx):
                typer.echo(f"• {eval_case.case_id} …", err=True)

        summary = run_suite(
            cases, base_dir=scratch, run_id=run_id, fail_fast=fail_fast, progress=_progress
        )
        report = {
            "version": "1",
            "run_id": run_id,
            "suite": suite or ("external" if file else "all"),
            **summary.to_dict(),
        }
        path = save_report(layout, report)
        if is_json(ctx):
            emit_json(success_envelope("eval.run", report, details={"report": str(path)}))
            code = 0 if summary.failed == 0 and summary.error == 0 else 1
            raise typer.Exit(code)
        for result in summary.results:
            marker = {"passed": "✓", "failed": "✗", "error": "!", "skipped": "·"}[result.status]
            typer.echo(f"{marker} {result.case_id} ({result.duration_ms:.0f} ms) {result.status}")
            for assertion in result.assertions:
                if assertion.outcome.value != "passed":
                    typer.echo(f"    - {assertion.name}: {assertion.detail}")
        typer.echo(
            f"{summary.passed} passed, {summary.failed} failed, "
            f"{summary.error} errors, {summary.skipped} skipped — report: {path}"
        )
        raise typer.Exit(0 if summary.failed == 0 and summary.error == 0 else 1)


@eval_app.command("report")
@with_error_handling("eval.report")
def eval_report(
    ctx: typer.Context,
    run_id: str = typer.Argument(None, help="Run id (default: most recent)."),
) -> None:
    """Render one run's report."""
    with app_context(ctx) as c:
        reports = list_reports(c.layout)
        if not reports:
            typer.echo("No eval runs recorded yet.", err=True)
            raise typer.Exit(7)
        target = run_id or reports[0]
        try:
            report = load_report(c.layout, target)
        except FileNotFoundError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(7) from exc
    if is_json(ctx):
        emit_json(success_envelope("eval.report", report))
        return
    typer.echo(f"run {report['run_id']} — {report.get('suite')}")
    for row in report.get("results", []):
        typer.echo(f"  {row['case_id']}  {row['status']}")
        for assertion in row.get("assertions", []):
            if assertion["outcome"] != "passed":
                typer.echo(f"     - {assertion['name']}: {assertion['detail']}")
    typer.echo(f"{report['passed']} passed, {report['failed']} failed, {report['errors']} errors")


@eval_app.command("compare")
@with_error_handling("eval.compare")
def eval_compare(
    ctx: typer.Context,
    a: str = typer.Argument(..., help="Baseline run id."),
    b: str = typer.Argument(..., help="Candidate run id."),
) -> None:
    """Compare two runs case-by-case."""
    with app_context(ctx) as c:
        ra = load_report(c.layout, a)
        rb = load_report(c.layout, b)
    diff = compare(None, ra, rb)
    if is_json(ctx):
        emit_json(success_envelope("eval.compare", diff))
        return
    typer.echo(f"{diff['a']} -> {diff['b']}")
    for row in diff["rows"]:
        typer.echo(f"  {row['case_id']}: {row['a']} -> {row['b']} ({row['delta']})")
    if diff["regressions"]:
        typer.echo("regressions: " + ", ".join(diff["regressions"]))
    if diff["fixes"]:
        typer.echo("fixes: " + ", ".join(diff["fixes"]))
    raise typer.Exit(1 if diff["regressions"] else 0)


@eval_app.command("history")
@with_error_handling("eval.history")
def eval_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", help="Max runs to show."),
) -> None:
    """List recorded eval runs (newest first)."""
    with app_context(ctx) as c:
        reports = list_reports(c.layout)[:limit]
    if is_json(ctx):
        rows = [load_report(c.layout, rid) for rid in reports]
        emit_json(
            success_envelope(
                "eval.history",
                [
                    {
                        "run_id": r["run_id"],
                        "suite": r.get("suite"),
                        "passed": r["passed"],
                        "failed": r["failed"],
                        "errors": r["errors"],
                        "skipped": r["skipped"],
                    }
                    for r in rows
                ],
            )
        )
        return
    if not reports:
        typer.echo("No eval runs recorded yet.")
        return
    for rid in reports:
        report = load_report(c.layout, rid)
        typer.echo(
            f"{rid}  {report.get('suite') or '-'}  "
            f"{report['passed']}p/{report['failed']}f/{report['errors']}e"
        )


@eval_app.command("create")
@with_error_handling("eval.create")
def eval_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Case file name (without .py)."),
    suite: str = typer.Option("custom", "--suite", help="Suite key for the scaffold."),
    out: Path = typer.Option(None, "--out", help="Output directory (default: cwd)."),
) -> None:
    """Scaffold an external eval case file."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "case"
    target_dir = out or Path.cwd()
    target = target_dir / f"{name}.py"
    if target.exists():
        typer.echo(f"Already exists: {target}", err=True)
        raise typer.Exit(2)
    target.write_text(TEMPLATE.format(name=name, suite=suite, slug=slug), encoding="utf-8")
    if is_json(ctx):
        emit_json(success_envelope("eval.create", {"path": str(target)}))
        return
    typer.echo(f"created {target} (validate: rinari eval validate {target})")


@eval_app.command("validate")
@with_error_handling("eval.validate")
def eval_validate(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Case file to validate."),
) -> None:
    """Validate an external case file (imports it, checks the case shape)."""
    from rinari.evals.spec import EvalCase

    errors: list[str] = []
    cases: list = []
    try:
        module = _load_case_file(path)
        cases = module
    except Exception as exc:
        errors.append(f"load failed: {exc}")
    for case in cases:
        if not isinstance(case, EvalCase):
            errors.append(f"not an EvalCase: {case!r}")
            continue
        if not case.case_id or not case.suite or not case.name:
            errors.append(f"{getattr(case, 'case_id', '?')}: missing id/suite/name")
        if not (case.prompt.strip() or case.prompts):
            errors.append(f"{case.case_id}: empty prompt")
        if case.script is None and case.script_lanes is None and case.project:
            pass  # answering with no tool calls is a valid case
    if is_json(ctx):
        emit_json(
            success_envelope(
                "eval.validate",
                {"valid": not errors, "cases": len(cases), "errors": errors},
            )
        )
        if errors:
            raise typer.Exit(1)
        return
    if errors:
        for e in errors:
            typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"OK: {len(cases)} valid case(s) in {path}")


def _load_case_file(path: Path) -> list:
    if not path.is_file():
        typer.echo(f"File not found: {path}", err=True)
        raise typer.Exit(2)
    spec = importlib.util.spec_from_file_location(f"rinari_eval_case_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cases = getattr(module, "CASES", getattr(module, "cases", []))
    if callable(cases):
        cases = cases()
    cases = list(cases)
    if not cases:
        raise RuntimeError(f"{path} defines no CASES")
    return cases
