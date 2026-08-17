"""Verification services (phase 3): evidence, planning, completion gate."""

from __future__ import annotations

from rinari.verify.gate import (
    OUTCOMES,
    GateDecision,
    detect_failure_markers,
    evaluate_gate,
    is_false_success,
)
from rinari.verify.planner import VerificationPlan, plan_verification
from rinari.verify.records import VALIDATION_KINDS, VALIDATION_RESULTS, record_validation
from rinari.verify.service import VerificationService

__all__ = [
    "OUTCOMES",
    "VALIDATION_KINDS",
    "VALIDATION_RESULTS",
    "GateDecision",
    "VerificationPlan",
    "VerificationService",
    "detect_failure_markers",
    "evaluate_gate",
    "is_false_success",
    "plan_verification",
    "record_validation",
]
