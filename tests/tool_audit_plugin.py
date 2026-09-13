"""Optional runtime coverage ledger: pytest -p tests.tool_audit_plugin.

Records names and result codes only. No arguments, outputs or credentials.
Success means the runtime was exercised, not that an external integration was live.
"""

import json
import threading
from collections import Counter, defaultdict
from pathlib import Path

_lock = threading.Lock()
_results = defaultdict(Counter)
_original = None


def pytest_addoption(parser):
    parser.addoption("--tool-audit", default=None)


def pytest_configure(config):
    from rinari.tools.runtime import ToolRuntime

    global _original
    _original = ToolRuntime.execute

    def execute(self, tool_name, *args, **kwargs):
        try:
            result = _original(self, tool_name, *args, **kwargs)
        except Exception as exc:
            with _lock:
                _results[tool_name][f"raised:{type(exc).__name__}"] += 1
            raise
        with _lock:
            _results[tool_name]["ok" if result.ok else result.error.code.value] += 1
        return result

    ToolRuntime.execute = execute


def pytest_sessionfinish(session, exitstatus):
    from rinari.tools.catalog import builtin_catalog
    from rinari.tools.runtime import ToolRuntime

    ToolRuntime.execute = _original
    destination = session.config.getoption("--tool-audit")
    if destination:
        inventory = builtin_catalog().names()
        report = {
            "pytest_exitstatus": int(exitstatus),
            "catalog_count": len(inventory),
            "scope": "ToolRuntime invocations in automated tests; mocks may be present",
            "tools": {name: dict(_results.get(name, {})) for name in inventory},
        }
        Path(destination).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
