"""OpenAPI tools runtime (phase 5).

Decision record (2026-08-17, see TODO.md "Registro de decisiones"):

- v1 spec format: **JSON** OpenAPI 3.x (`.json`/`.yaml` requires a prior
  conversion; no YAML dependency is added — the loader reports it clearly).
- Generated operations become namespaced `ToolDefinition`s
  (`api.<spec>.<operationId|method_path>`) subject to normal policy:
  every call is `network.outbound` on the target URL; mutation risk
  derives from the HTTP method by default and supports per-operation
  overrides stored with the spec.
- Auth: security schemes (http bearer, apiKey header/query) are *detected*,
  never invented. Credentials resolve from the process environment via
  `env://VAR` references stored in the spec record; missing secret ->
  AUTH_REQUIRED, no fallback.
- Calls go through the session's httpx factory + NetworkGuard, like web/http.
"""

from rinari.openapi.service import ApiService
from rinari.openapi.spec import (
    ApiSpecError,
    Operation,
    SpecDocument,
    load_spec_file,
    load_spec_url,
    validate_spec,
)
from rinari.openapi.tools import (
    API_NAMESPACE,
    operation_tool_name,
    spec_tool_definitions,
)

__all__ = [
    "API_NAMESPACE",
    "ApiService",
    "ApiSpecError",
    "Operation",
    "SpecDocument",
    "load_spec_file",
    "load_spec_url",
    "operation_tool_name",
    "spec_tool_definitions",
    "validate_spec",
]
