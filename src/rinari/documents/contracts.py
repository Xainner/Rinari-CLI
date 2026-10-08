"""Contratos del servicio documental: errores, estados y verificación.

Una sola definición canónica en Python; el protocolo publica estas formas y
el escritorio genera sus tipos desde el schema, no a mano.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"

KINDS = ("pptx", "xlsx", "docx", "pdf")

MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


class DocumentErrorCode(StrEnum):
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    LICENSE_REQUIRED = "LICENSE_REQUIRED"
    FONT_UNAVAILABLE = "FONT_UNAVAILABLE"
    ENCRYPTED_INPUT = "ENCRYPTED_INPUT"
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
    ZIP_LIMIT_EXCEEDED = "ZIP_LIMIT_EXCEEDED"
    DOCUMENT_LIMIT_EXCEEDED = "DOCUMENT_LIMIT_EXCEEDED"
    UNSAFE_EXTERNAL_RESOURCE = "UNSAFE_EXTERNAL_RESOURCE"
    PRESERVATION_RISK = "PRESERVATION_RISK"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    CALCULATION_UNSUPPORTED = "CALCULATION_UNSUPPORTED"
    CALCULATION_BACKEND_UNAVAILABLE = "CALCULATION_BACKEND_UNAVAILABLE"
    RENDER_FAILED = "RENDER_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_SPEC = "INVALID_SPEC"
    NOT_FOUND = "NOT_FOUND"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    OUTPUT_QUOTA_EXCEEDED = "OUTPUT_QUOTA_EXCEEDED"


_RETRYABLE = {
    DocumentErrorCode.RENDER_FAILED,
    DocumentErrorCode.INTERRUPTED,
    DocumentErrorCode.CANCELLED,
}


class DocumentError(Exception):
    """Fallo documental con código estable, alcance y una acción posible."""

    def __init__(
        self,
        code: DocumentErrorCode,
        message: str,
        *,
        scope: str | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.scope = scope
        self.action = action
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.scope:
            value["scope"] = self.scope
        if self.action:
            value["action"] = self.action
        if self.details:
            value["details"] = self.details
        return value


# -- trabajos ------------------------------------------------------------------

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_PARTIAL = "partial"
JOB_FAILED = "failed"
JOB_CANCELLING = "cancelling"
JOB_CANCELLED = "cancelled"
JOB_INTERRUPTED = "interrupted"
JOB_ACTIVE = (JOB_QUEUED, JOB_RUNNING, JOB_CANCELLING)
JOB_TERMINAL = (JOB_SUCCEEDED, JOB_PARTIAL, JOB_FAILED, JOB_CANCELLED, JOB_INTERRUPTED)

PHASES = ("import", "inspect", "plan", "build", "calculate", "render", "validate", "publish")


# -- verificación ----------------------------------------------------------------

CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_PARTIAL = "partial"
CHECK_NOT_RUN = "not_run"
CHECK_NOT_APPLICABLE = "not_applicable"
CHECK_STATES = (CHECK_PASSED, CHECK_FAILED, CHECK_PARTIAL, CHECK_NOT_RUN, CHECK_NOT_APPLICABLE)
DIMENSIONS = ("structure", "content", "preservation", "formulas", "visual")


@dataclass(slots=True)
class Check:
    status: str
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"status": self.status}
        if self.reason:
            value["reason"] = self.reason
        if self.evidence:
            value["evidence"] = self.evidence
        if self.findings:
            value["findings"] = self.findings
        return value


@dataclass(slots=True)
class ValidationReport:
    """Evidencia por dimensión; nunca un `verified: true` suelto."""

    revision_id: str
    sha256: str
    checks: dict[str, Check] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        states = [check.status for check in self.checks.values()]
        if any(state == CHECK_FAILED for state in states):
            return CHECK_FAILED
        relevant = [s for s in states if s != CHECK_NOT_APPLICABLE]
        if relevant and all(s == CHECK_PASSED for s in relevant):
            return CHECK_PASSED
        return CHECK_PARTIAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision_id": self.revision_id,
            "sha256": self.sha256,
            "status": self.status,
            "checks": {name: check.to_dict() for name, check in self.checks.items()},
            "warnings": self.warnings,
        }


PRESERVE_STRICT = "preserve_strict"
PRESERVE_BEST_EFFORT = "preserve_best_effort"
REBUILD = "rebuild"
PRESERVATION_POLICIES = (PRESERVE_STRICT, PRESERVE_BEST_EFFORT, REBUILD)


def kind_of_name(name: str) -> str | None:
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return suffix if suffix in KINDS else None
