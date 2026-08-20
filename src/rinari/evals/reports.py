"""Eval run reports: persist, load, render and compare (harness.md: reports)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

REPORT_VERSION = "1"


def report_dir(layout) -> Path:
    path = layout.dir("evals") / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_run_id() -> str:
    import secrets

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"run_{stamp}-{secrets.token_hex(2)}"


def save_report(layout, report: dict[str, Any]) -> Path:
    path = report_dir(layout) / f"{report['run_id']}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def load_report(layout, run_id: str) -> dict[str, Any]:
    path = report_dir(layout) / f"{run_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No eval run {run_id!r} (looked in {path.parent})")
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports(layout) -> list[str]:
    directory = report_dir(layout)
    return sorted((p.stem for p in directory.glob("run_*.json")), reverse=True)


def compare(reports: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Diff two runs case-by-case (new passes, regressions, unchanged)."""

    def by_case(report):
        return {r["case_id"]: r for r in report.get("results", [])}

    ca, cb = by_case(a), by_case(b)
    ids = sorted(set(ca) | set(cb))
    rows = []
    for cid in ids:
        ra, rb = ca.get(cid), cb.get(cid)
        if ra is None:
            rows.append({"case_id": cid, "a": "missing", "b": rb["status"], "delta": "new"})
        elif rb is None:
            rows.append({"case_id": cid, "a": ra["status"], "b": "missing", "delta": "removed"})
        else:
            delta = "same" if ra["status"] == rb["status"] else f"{ra['status']}->{rb['status']}"
            rows.append(
                {
                    "case_id": cid,
                    "a": ra["status"],
                    "b": rb["status"],
                    "delta": delta,
                }
            )
    regressions = [
        r["case_id"]
        for r in rows
        if r["a"] in ("passed", "skipped") and r["b"] in ("failed", "error")
    ]
    fixes = [r["case_id"] for r in rows if r["a"] in ("failed", "error") and r["b"] == "passed"]
    return {
        "a": a.get("run_id"),
        "b": b.get("run_id"),
        "regressions": regressions,
        "fixes": fixes,
        "rows": rows,
    }
