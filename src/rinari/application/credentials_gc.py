"""Saneamiento del vault de credenciales del sistema.

Clasifica cada entrada del vault y borra únicamente las **huérfanas**: entradas
del namespace de este home cuyo proveedor ya no existe ni quedó retenido.

Reglas de propiedad (revisión del PR):

* Nada se borra si este home no puede demostrar que la entrada es suya: las
  formas sin scope (``rinari``, ``rinari/providers/<id>``) y las de otros homes
  se reportan como ``unknown`` y jamás entran al borrado.
* Se reconocen solo las formas exactas que administra el store -vigente,
  staging y anterior, con scope, más sus compuestos de Windows- verificando
  tipo genérico y correspondencia entre target y username.
* Antes de borrar cada entrada se revalida contra la base: un alta reciente
  (o un id retenido) la saca del borrado aunque el plan la marcara.

Contexto: ver el informe CredWrite error 8.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rinari.application.credentials import split_part_service
from rinari.application.credentials_vault import VaultCredential
from rinari.shared.locking import file_lock

#: Marca del servicio del harness. Solo se acepta en formas exactas.
SERVICE = "rinari"
PROVIDER_PREFIX = "providers/"
#: Credenciales genéricas: cualquier otro tipo no es nuestro.
CRED_TYPE_GENERIC = 1

#: Tipos de entrada gestionados por el store con scope de home.
SCOPED_KINDS = ("current", "staging", "previous", "gen")
#: Infijos de cada forma con scope, en el orden en que se prueban.
_SCOPED_INFIXES = (
    ("current", "providers"),
    ("staging", "staging/providers"),
    ("previous", "previous/providers"),
    ("gen", "gen/providers"),
)

#: Infijo de las referencias frescas de rotación (`gen/providers/<id>/gen-<n>`,
#: generadas por `stage_unique`); deben coincidir con el prefijo del slot.
_GEN_PREFIX = "gen/providers/"

Deleter = Callable[[str, int], None]
OwnershipCheck = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class ManagedTarget:
    """Entrada del vault con una de las formas que administra el store."""

    kind: str  # current | staging | previous | unscoped | legacy
    provider_id: str
    scope: str | None


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """Clasificación de las entradas del vault antes de borrar nada."""

    live: list[VaultCredential]
    retained: list[VaultCredential]
    orphans: list[VaultCredential]
    unknown: list[VaultCredential]
    foreign: list[VaultCredential]
    #: target -> provider id de cada huérfana (para revalidar antes de borrar).
    orphan_owners: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        return {
            "live": len(self.live),
            "retained": len(self.retained),
            "orphans": len(self.orphans),
            "unknown": len(self.unknown),
            "foreign": len(self.foreign),
        }


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted: int
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def parse_managed_target(target: str, username: str, scope: str) -> ManagedTarget | None:
    r"""Reconoce solo las formas administradas, y dice de qué scope son.

    Acepta ``rinari/<scope>/providers/<id>`` (y sus variantes staging/anterior)
    más el compuesto ``<username>@<service>`` que deja el backend de Windows,
    exigiendo que el id del username coincida con el del target. El formato
    legado ``rinari`` y el intermedio ``rinari/providers/<id>`` se reconocen
    como *sin scope*: son de rinari, pero su pertenencia no puede probarse.
    Los slots de rotación de keyring llevan el slot completo en el username:
    ``gen/providers/<id>/gen-<n>`` (review P1: el prefijo ``providers/`` puro
    no los reconoce y quedaban fuera del saneo administrado).
    """
    service = target
    if "@" in target:
        head, _, tail = target.partition("@")
        if head != username.partition("@")[0]:
            return None
        service = tail
    # A part of a long secret (`<service>#part-<n>`) is classified, kept or
    # removed together with the entry it completes.
    service = split_part_service(service)

    # Slots de rotación (keyring real): username == "gen/providers/<id>/gen-<n>".
    gen_marker_user = f"gen/{PROVIDER_PREFIX}"
    if username.startswith(gen_marker_user):
        parts = username.split("/")
        # gen / providers / <id> / gen-<n>
        if len(parts) == 4 and parts[1] == "providers":
            provider_id, slot_suffix = parts[2], parts[3]
            if (
                provider_id
                and slot_suffix.startswith("gen-")
                and not any(p in (".", "..") for p in parts)
                and provider_id not in (".", "..")
            ):
                # La pertenencia al home se prueba por el SERVICE NAME del
                # target: `rinari/<scope>/gen/providers/<id>/gen-<n>`.
                expected_service = f"{SERVICE}/{scope}/{_GEN_PREFIX}{provider_id}/{slot_suffix}"
                if service == expected_service:
                    return ManagedTarget(kind="gen", provider_id=provider_id, scope=scope)
        return None

    if not username.startswith(PROVIDER_PREFIX):
        return None
    provider_id = username[len(PROVIDER_PREFIX) :]
    if not provider_id:
        return None
    if any(part in (".", "..") for part in provider_id.split("/")):
        return None

    if service == SERVICE:
        return ManagedTarget(kind="legacy", provider_id=provider_id, scope=None)
    if service == f"{SERVICE}/providers/{provider_id}":
        return ManagedTarget(kind="unscoped", provider_id=provider_id, scope=None)

    prefix = f"{SERVICE}/"
    if not service.startswith(prefix):
        return None
    for kind, infix in _SCOPED_INFIXES:
        if kind == "gen":
            # Slots de rotación: manejo aparte más abajo (el servicio lleva
            # el sufijo `gen-<n>` que el bucle de sufijos exactos no cubre).
            continue
        suffix = f"/{infix}/{provider_id}"
        if not service.endswith(suffix):
            continue
        owner = service[len(prefix) : -len(suffix)]
        if owner and "/" not in owner:
            return ManagedTarget(kind=kind, provider_id=provider_id, scope=owner)
    # Rotación (`stage_unique`, keyring con scope): `rinari/<scope>/gen/providers/<id>/gen-<n>`.
    gen_marker = f"{prefix}{scope}/{_GEN_PREFIX}"
    if service.startswith(gen_marker):
        rest = service[len(gen_marker) :]
        parts = rest.split("/")
        if len(parts) == 2 and parts[0] == provider_id and parts[1].startswith("gen-"):
            return ManagedTarget(kind="gen", provider_id=provider_id, scope=scope)
    return None


def owned_provider_id(target: str, username: str, scope: str) -> str | None:
    """Id del proveedor si la entrada es de este home; None si no lo es."""
    managed = parse_managed_target(target, username, scope)
    if managed is None or managed.kind not in SCOPED_KINDS:
        return None
    if managed.scope != scope:
        return None
    return managed.provider_id


def plan_cleanup(
    entries: Iterable[VaultCredential],
    *,
    live_provider_ids: set[str],
    retained_provider_ids: set[str] | frozenset[str] = frozenset(),
    scope: str,
) -> CleanupPlan:
    live: list[VaultCredential] = []
    retained: list[VaultCredential] = []
    orphans: list[VaultCredential] = []
    unknown: list[VaultCredential] = []
    foreign: list[VaultCredential] = []
    orphan_owners: dict[str, str] = {}

    for entry in entries:
        if entry.cred_type != CRED_TYPE_GENERIC:
            foreign.append(entry)
            continue
        managed = parse_managed_target(entry.target, entry.username, scope)
        if managed is None:
            foreign.append(entry)
            continue
        if managed.provider_id in live_provider_ids:
            live.append(entry)
        elif managed.provider_id in retained_provider_ids:
            retained.append(entry)
        elif managed.kind in SCOPED_KINDS and managed.scope == scope:
            orphans.append(entry)
            orphan_owners[entry.target] = managed.provider_id
        else:
            unknown.append(entry)

    return CleanupPlan(
        live=live,
        retained=retained,
        orphans=orphans,
        unknown=unknown,
        foreign=foreign,
        orphan_owners=orphan_owners,
    )


def apply_cleanup_locked(
    plan: CleanupPlan,
    *,
    delete: Deleter,
    lock_path: Path,
    still_orphan: OwnershipCheck | None = None,
    timeout: float = 5.0,
) -> CleanupReport:
    """Aplica el saneamiento tomando el lock compartido de credenciales.

    Los escritores del vault (altas y rotaciones) toman el mismo lock: sin él,
    la limpieza puede borrar el target de un alta que aún no confirmó su fila
    en la base, y la revalidación no alcanza porque la fila todavía no existe.
    """
    with file_lock(lock_path, timeout=timeout):
        return apply_cleanup(plan, delete=delete, still_orphan=still_orphan)


def apply_cleanup(
    plan: CleanupPlan,
    *,
    delete: Deleter,
    still_orphan: OwnershipCheck | None = None,
) -> CleanupReport:
    """Borra las huérfanas; fallos y saltos se reportan, no se esconden.

    ``still_orphan`` revalida el id justo antes de borrar (altas concurrentes);
    por defecto solo se cuenta con la clasificación del plan.
    """
    deleted = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    for entry in plan.orphans:
        if still_orphan is not None:
            provider_id = plan.orphan_owners.get(entry.target)
            if provider_id is None or not still_orphan(provider_id):
                skipped += 1
                continue
        try:
            delete(entry.target, entry.cred_type)
        except Exception as error:
            # Deliberado: el fallo por entrada se reporta al llamador, no se oculta
            # ni aborta el resto del saneamiento.
            failures.append((entry.target, str(error)))
        else:
            deleted += 1
    return CleanupReport(deleted=deleted, skipped=skipped, failed=len(failures), failures=failures)
