"""Saneamiento del vault de credenciales del sistema.

Clasifica las entradas del vault en tres grupos y borra únicamente las
huérfanas (credenciales de proveedores que ya no existen). Nunca toca las
entradas de proveedores vivos ni las de otras aplicaciones.

Contexto: ver el informe CredWrite error 8 — las reescrituras del backend
dejaban huérfanas hasta saturar el vault.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from rinari.application.credentials_vault import VaultCredential

#: Marca del servicio del harness dentro del TargetName.
SERVICE_MARK = "rinari"
#: Prefijo del usuario con el que se registran las claves de proveedor.
PROVIDER_PREFIX = "providers/"

Deleter = Callable[[str, int], None]


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """Clasificación de las entradas del vault antes de borrar nada."""

    live: list[VaultCredential]
    orphans: list[VaultCredential]
    foreign: list[VaultCredential]

    def summary(self) -> dict[str, int]:
        return {
            "live": len(self.live),
            "orphans": len(self.orphans),
            "foreign": len(self.foreign),
        }


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted: int
    failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def owned_provider_id(entry: VaultCredential) -> str | None:
    """Provider id the entry belongs to, or None when it is not ours.

    Both the current format (``rinari/providers/<id>``), the displaced one
    (``providers/<id>@rinari``), the staging one and the legacy shared
    service (``rinari``) carry ``providers/<id>`` as the username.
    """
    if not entry.username.startswith(PROVIDER_PREFIX):
        return None
    if SERVICE_MARK not in entry.target:
        return None
    provider_id = entry.username[len(PROVIDER_PREFIX) :]
    return provider_id or None


def plan_cleanup(
    entries: Iterable[VaultCredential], live_provider_ids: set[str]
) -> CleanupPlan:
    live: list[VaultCredential] = []
    orphans: list[VaultCredential] = []
    foreign: list[VaultCredential] = []
    for entry in entries:
        provider_id = owned_provider_id(entry)
        if provider_id is None:
            foreign.append(entry)
        elif provider_id in live_provider_ids:
            live.append(entry)
        else:
            orphans.append(entry)
    return CleanupPlan(live=live, orphans=orphans, foreign=foreign)


def apply_cleanup(plan: CleanupPlan, *, delete: Deleter) -> CleanupReport:
    """Delete the orphaned entries; per-entry failures are reported, not raised."""
    deleted = 0
    failures: list[tuple[str, str]] = []
    for entry in plan.orphans:
        try:
            delete(entry.target, entry.cred_type)
        except Exception as error:
            # Deliberado: el fallo por entrada se reporta al llamador, no se oculta
            # ni aborta el resto del saneamiento.
            failures.append((entry.target, str(error)))
        else:
            deleted += 1
    return CleanupReport(deleted=deleted, failed=len(failures), failures=failures)
