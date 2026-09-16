"""Graphic control plane (computer use): grants, backends and observations.

This package coordinates what the browser stack does not own: explicit
user-issued grants over an authorized graphic target, backend isolation,
observation vigencia and the action dispatch ledger. It never copies
sessions, models, artifacts, approvals or policy: those stay with the
existing engine services.

Safety posture: the default backend is a fake. No OS input or screen
capture exists in this build; a real Windows backend must be selected,
reviewed and lab-approved first (see docs/adr/0001-windows-desktop-backend.md).
"""
