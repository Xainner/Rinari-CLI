"""Turn OpenAPI operations into namespaced ToolDefinitions (phase 5).

Every generated tool is `network.outbound` on its target URL and carries a
mutation risk derived from the HTTP method (overridable per operation).
Auth requirements are recorded as metadata + a pre-flight check; the actual
secret is resolved at call time from the process environment (never stored).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_REMOTE_DESTRUCTIVE,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    ToolDefinition,
)

from .spec import Operation, SpecDocument

API_NAMESPACE = "api"

# Mutation risk defaults by HTTP method.
_METHOD_RISK: dict[str, tuple[str, str]] = {
    "get": (RISK_LOW, SIDE_EFFECT_NONE),
    "head": (RISK_LOW, SIDE_EFFECT_NONE),
    "options": (RISK_LOW, SIDE_EFFECT_NONE),
    "post": (RISK_MEDIUM, SIDE_EFFECT_REMOTE_REVERSIBLE),
    "put": (RISK_MEDIUM, SIDE_EFFECT_REMOTE_REVERSIBLE),
    "patch": (RISK_MEDIUM, SIDE_EFFECT_REMOTE_REVERSIBLE),
    "delete": (RISK_HIGH, SIDE_EFFECT_REMOTE_DESTRUCTIVE),
}

_VALID_RISK = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)
_VALID_SIDE_EFFECTS = (
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    SIDE_EFFECT_REMOTE_DESTRUCTIVE,
)


def operation_tool_name(spec_name: str, operation: Operation) -> str:
    return f"api.{spec_name}.{operation.operation_id}"


def default_risk(method: str) -> tuple[str, str]:
    return _METHOD_RISK.get(method, (RISK_MEDIUM, SIDE_EFFECT_REMOTE_REVERSIBLE))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_") or "op"


def _parameter_schema(parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema")
    if not isinstance(schema, dict):
        schema = {"type": "string"}
    out = dict(schema)
    name = parameter.get("name")
    if name is not None:
        out["name"] = name
    out["in"] = parameter.get("in", "query")
    return out


def _build_input_schema(operation: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in operation.parameters:
        if not isinstance(parameter, dict) or parameter.get("in") not in (
            "path",
            "query",
            "header",
        ):
            continue
        pname = str(parameter.get("name") or "")
        if not pname:
            continue
        prop = _parameter_schema(parameter)
        properties[prop.pop("name")] = prop
        if parameter.get("required") and prop["in"] in ("path", "query"):
            required.append(pname)
    if operation.request_body is not None:
        content = operation.request_body.get("content") or {}
        json_schema = (content.get("application/json") or {}).get("schema")
        body: dict[str, Any] = (
            dict(json_schema) if isinstance(json_schema, dict) else {"type": "object"}
        )
        body["description"] = "Request body (application/json)."
        properties["body"] = body
        required.append("body")
    return {"type": "object", "properties": properties, "required": required}


def _resolve_risk(operation: Operation, overrides: dict[str, Any]) -> tuple[str, str]:
    risk, side_effect = default_risk(operation.method)
    override = overrides.get(operation.operation_id)
    if isinstance(override, dict):
        if override.get("risk") in _VALID_RISK:
            risk = override["risk"]
        if override.get("side_effects") in _VALID_SIDE_EFFECTS:
            side_effect = override["side_effects"]
    return risk, side_effect


def _auth_requirements(operation: Operation, spec: SpecDocument) -> list[dict[str, str]]:
    """List of required secrets (scheme, type, param) for this operation."""
    security = list(spec.global_security or [])
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in security:
        if not isinstance(group, dict):
            continue
        for scheme_name in group:
            if scheme_name in seen:
                continue
            seen.add(scheme_name)
            scheme = spec.security_schemes.get(scheme_name)
            if not isinstance(scheme, dict):
                continue
            stype = str(scheme.get("type") or "")
            if stype == "http" and str(scheme.get("scheme")) == "bearer":
                requirements.append({"scheme": scheme_name, "kind": "bearer"})
            elif stype == "apiKey":
                requirements.append(
                    {
                        "scheme": scheme_name,
                        "kind": "apiKey",
                        "name": str(scheme.get("name") or ""),
                        "in": str(scheme.get("in") or "header"),
                    }
                )
    return requirements


def spec_tool_definitions(
    spec_name: str,
    spec: SpecDocument,
    overrides: dict[str, Any] | None = None,
    make_handler: Callable[[str, Operation], Any] | None = None,
) -> list[ToolDefinition]:
    overrides = overrides or {}
    definitions: list[ToolDefinition] = []
    for operation in spec.operations:
        risk, side_effect = _resolve_risk(operation, overrides)
        auth = _auth_requirements(operation, spec)
        override = overrides.get(operation.operation_id)
        description = (
            operation.description
            or operation.summary
            or (f"{operation.method.upper()} {operation.path}")
        )
        if isinstance(override, dict) and override.get("description"):
            description = str(override["description"])
        definition = ToolDefinition(
            name=operation_tool_name(spec_name, operation),
            description=description.strip(),
            input_schema=_build_input_schema(operation),
            capabilities=("network.outbound",),
            risk=risk,
            side_effects=side_effect,
            idempotent=operation.method in ("get", "head", "options"),
            namespace=f"{API_NAMESPACE}.{spec_name}",
            manifest={
                "source": "openapi",
                "spec": spec_name,
                "method": operation.method,
                "path": operation.path,
                "operation_id": operation.operation_id,
                "auth": auth,
            },
            # Nivel C (Etapa B): OpenAPI tools are on-demand.
            always_loaded=False,
            handler=(make_handler(spec_name, operation) if make_handler is not None else None),
        )
        definitions.append(definition)
    return definitions


__all__ = [
    "API_NAMESPACE",
    "default_risk",
    "operation_tool_name",
    "spec_tool_definitions",
]
