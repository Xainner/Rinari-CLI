"""Saneamiento del vault: propiedad del home y reconocimiento exacto.

Revisión del PR (P1): el plan no puede borrar nada que este home no pueda
demostrar como propio. Las entradas se clasifican así:

    live      el id existe en la state.db de este home (se conservan)
    retained  el id quedó retenido a propósito (keep_credentials)
    orphans   entrada del scope de este home sin proveedor vivo ni retenido
    unknown   con forma de rinari pero sin forma/scope que pruebe pertenencia
    foreign   cualquier otra cosa (otras aplicaciones)

Solo `orphans` se borra. `unknown` y `foreign` nunca.
"""

from __future__ import annotations

import json
import threading

from typer.testing import CliRunner

from rinari.application.credentials_gc import apply_cleanup, plan_cleanup
from rinari.application.credentials_vault import VaultCredential

SCOPE = "home-a"


def _cred(
    target: str,
    username: str = "",
    *,
    cred_type: int = 1,
    comment: str = "Stored using python-keyring",
    written: str = "2026-09-10T00:00:00",
) -> VaultCredential:
    return VaultCredential(
        target=target,
        username=username,
        comment=comment,
        last_written=written,
        persist=3,
        cred_type=cred_type,
    )


def _plan(entries, *, live=(), retained=()):
    return plan_cleanup(
        entries,
        live_provider_ids=set(live),
        retained_provider_ids=set(retained),
        scope=SCOPE,
    )


def test_plan_classifies_scoped_entries_of_this_home() -> None:
    entries = [
        _cred("rinari/home-a/providers/prov_live", "providers/prov_live"),
        _cred("providers/prov_live@rinari/home-a/providers/prov_live", "providers/prov_live"),
        _cred("rinari/home-a/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/home-a/staging/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/home-a/previous/providers/prov_dead", "providers/prov_dead"),
    ]
    plan = _plan(entries, live={"prov_live"})
    assert [entry.target for entry in plan.live] == [
        "rinari/home-a/providers/prov_live",
        "providers/prov_live@rinari/home-a/providers/prov_live",
    ]
    assert [entry.target for entry in plan.orphans] == [
        "rinari/home-a/providers/prov_dead",
        "rinari/home-a/staging/providers/prov_dead",
        "rinari/home-a/previous/providers/prov_dead",
    ]
    assert plan.unknown == []
    assert plan.foreign == []


def test_plan_classifies_rotation_slots_as_managed() -> None:
    """`gen/providers/<id>/gen-<n>` (stage_unique): propio del home y huérfana
    cuando el proveedor ya no vive; conservada mientras el id esté vivo (un
    candidato staged más reciente puede ser la única copia de una clave)."""
    entries = [
        _cred("rinari/home-a/gen/providers/prov_live/gen-1", "providers/prov_live"),
        _cred("rinari/home-a/gen/providers/prov_dead/gen-2", "providers/prov_dead"),
        _cred("rinari/home-a/gen/providers/other/gen-3", "providers/prov_dead"),
        _cred("rinari/home-b/gen/providers/prov_dead/gen-4", "providers/prov_dead"),
    ]
    plan = _plan(entries, live={"prov_live"})
    assert [entry.target for entry in plan.live] == [
        "rinari/home-a/gen/providers/prov_live/gen-1",
    ]
    assert [entry.target for entry in plan.orphans] == [
        "rinari/home-a/gen/providers/prov_dead/gen-2",
    ]
    # id no coincidente y scope ajeno quedan sin tocar (foreign/unknown):
    # nunca entran al borrado.
    non_deletable = [e.target for e in plan.foreign] + [e.target for e in plan.unknown]
    assert "rinari/home-a/gen/providers/other/gen-3" in non_deletable
    assert "rinari/home-b/gen/providers/prov_dead/gen-4" in non_deletable


def test_plan_recognizes_real_keyring_rotation_slot_username() -> None:
    """Formato real del backend keyring (review P1): el username lleva el slot
    completo `gen/providers/<id>/gen-<n>`, no solo `providers/<id>`."""
    entries = [
        _cred("rinari/home-a/gen/providers/prov_live/gen-1", "gen/providers/prov_live/gen-1"),
        _cred("rinari/home-a/gen/providers/prov_dead/gen-2", "gen/providers/prov_dead/gen-2"),
    ]
    plan = _plan(entries, live={"prov_live"})
    assert [entry.target for entry in plan.live] == [
        "rinari/home-a/gen/providers/prov_live/gen-1",
    ]
    assert [entry.target for entry in plan.orphans] == [
        "rinari/home-a/gen/providers/prov_dead/gen-2",
    ]
    assert plan.orphan_owners == {"rinari/home-a/gen/providers/prov_dead/gen-2": "prov_dead"}


def test_plan_rejects_malformed_rotation_slot_usernames() -> None:
    """Formas casi idénticas que no son slots administrados no se tocan."""
    entries = [
        _cred("rinari/home-a/gen/providers/../../etc/gen-1", "gen/providers/../../etc/gen-1"),
        _cred("rinari/home-a/gen/providers/prov_dead", "gen/providers/prov_dead"),
        _cred("rinari/home-a/gen/providers/prov_dead/genX", "gen/providers/prov_dead/genX"),
    ]
    plan = _plan(entries, live=set())
    deletable = plan.live + plan.retained + plan.orphans
    assert deletable == []


def test_plan_ignores_targets_that_merely_contain_the_service_name() -> None:
    entries = [
        _cred("unrelated-rinari-backup", "providers/prov_dead"),
        _cred("rinari-backup/providers/prov_dead", "providers/prov_dead"),
        _cred("xrinari/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/providers/prov_dead/extra", "providers/prov_dead"),
    ]
    plan = _plan(entries)
    assert plan.orphans == []
    assert len(plan.foreign) == len(entries)


def test_plan_does_not_own_entries_from_another_scope() -> None:
    entries = [
        _cred("rinari/home-b/providers/prov_other", "providers/prov_other"),
        _cred("providers/prov_other@rinari/home-b/providers/prov_other", "providers/prov_other"),
    ]
    plan = _plan(entries)
    assert plan.orphans == []
    assert len(plan.unknown) == len(entries)


def test_plan_keeps_unverifiable_legacy_formats_as_unknown() -> None:
    entries = [
        _cred("rinari", "providers/prov_dead"),
        _cred("providers/prov_dead@rinari", "providers/prov_dead"),
        _cred("rinari/providers/prov_dead", "providers/prov_dead"),
    ]
    plan = _plan(entries)
    assert plan.orphans == []
    assert len(plan.unknown) == len(entries)


def test_plan_protects_live_and_retained_ids_in_any_format() -> None:
    entries = [
        _cred("rinari", "providers/prov_live"),
        _cred("providers/prov_kept@rinari", "providers/prov_kept"),
        _cred("rinari/home-b/providers/prov_remote", "providers/prov_remote"),
    ]
    plan = _plan(entries, live={"prov_live"}, retained={"prov_kept"})
    assert [entry.target for entry in plan.live] == ["rinari"]
    assert [entry.target for entry in plan.retained] == ["providers/prov_kept@rinari"]
    assert plan.orphans == []
    assert [entry.target for entry in plan.unknown] == ["rinari/home-b/providers/prov_remote"]


def test_plan_requires_the_generic_credential_type() -> None:
    entries = [
        _cred("rinari/home-a/providers/prov_dead", "providers/prov_dead", cred_type=2),
    ]
    plan = _plan(entries)
    assert plan.orphans == []
    assert len(plan.foreign) == 1


def test_plan_requires_target_and_username_identifiers_to_match() -> None:
    entries = [
        _cred("rinari/home-a/providers/prov_one", "providers/prov_two"),
        _cred("providers/prov_one@rinari/home-a/providers/prov_two", "providers/prov_one"),
    ]
    plan = _plan(entries)
    assert plan.orphans == []
    assert len(plan.foreign) == len(entries)


def test_plan_summary_counts_every_group() -> None:
    entries = [
        _cred("rinari/home-a/providers/prov_live", "providers/prov_live"),
        _cred("rinari/home-a/providers/prov_kept", "providers/prov_kept"),
        _cred("rinari/home-a/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/home-b/providers/prov_other", "providers/prov_other"),
        _cred("XboxLive"),
    ]
    plan = _plan(entries, live={"prov_live"}, retained={"prov_kept"})
    assert plan.summary() == {
        "live": 1,
        "retained": 1,
        "orphans": 1,
        "unknown": 1,
        "foreign": 1,
    }


def test_apply_deletes_only_orphans_and_reports_skips() -> None:
    entries = [
        _cred("rinari/home-a/providers/prov_live", "providers/prov_live"),
        _cred("rinari/home-a/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/home-a/providers/prov_race", "providers/prov_race"),
        _cred("rinari/home-b/providers/prov_other", "providers/prov_other"),
    ]
    plan = _plan(entries, live={"prov_live"})
    deleted: list[str] = []

    def still_orphan(provider_id: str) -> bool:
        # `prov_race` se dio de alta justo antes del borrado.
        return provider_id != "prov_race"

    report = apply_cleanup(
        plan,
        delete=lambda target, cred_type: deleted.append(target),
        still_orphan=still_orphan,
    )

    assert deleted == ["rinari/home-a/providers/prov_dead"]
    assert (report.deleted, report.skipped, report.failed) == (1, 1, 0)


def test_apply_records_failures_without_raising() -> None:
    plan = _plan([_cred("rinari/home-a/providers/prov_dead", "providers/prov_dead")])

    def delete(target: str, cred_type: int) -> None:
        raise PermissionError("access denied")

    report = apply_cleanup(plan, delete=delete)
    assert (report.deleted, report.failed) == (0, 1)
    assert report.failures[0][0] == "rinari/home-a/providers/prov_dead"


def test_cleanup_command_uses_the_inventory_through_a_fake(tmp_path, monkeypatch) -> None:
    from rinari.cli.main import app
    from rinari.shared.paths import ENV_HOME

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.chdir(work)

    inventory = [
        _cred("rinari/home-a/providers/prov_dead", "providers/prov_dead"),
        _cred("XboxLive"),
    ]
    monkeypatch.setattr("rinari.cli.commands.secrets.enumerate_credentials", lambda: inventory)

    result = CliRunner().invoke(app, ["--json", "secrets", "cleanup"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    data = payload["data"]
    assert data["status"] == "plan"
    # El home temporal no es el de las entradas sintéticas: nada es huérfano.
    assert data["summary"]["orphans"] == 0
    assert data["summary"]["unknown"] == 1
    assert data["summary"]["foreign"] == 1


def test_cleanup_command_reports_unsupported_when_the_vault_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    from rinari.cli.main import app
    from rinari.shared.errors import CredentialStoreUnavailableError
    from rinari.shared.paths import ENV_HOME

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.chdir(work)

    def unavailable():
        raise CredentialStoreUnavailableError("sin vault nativo")

    monkeypatch.setattr("rinari.cli.commands.secrets.enumerate_credentials", unavailable)

    result = CliRunner().invoke(app, ["--json", "secrets", "cleanup"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "unsupported"
    assert payload["data"]["supported"] is False


def test_cleanup_apply_respects_the_shared_credential_lock(tmp_path, monkeypatch) -> None:
    """Revisión (P1): la limpieza no puede correr mientras hay un alta en curso."""
    from rinari.cli.main import app
    from rinari.shared.locking import file_lock
    from rinari.shared.paths import ENV_HOME, home_identifier

    home = tmp_path / "rinari-home"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.chdir(work)

    scope = home_identifier(home)
    target = f"rinari/{scope}/providers/prov_dead"
    inventory = [_cred(target, "providers/prov_dead")]
    monkeypatch.setattr("rinari.cli.commands.secrets.enumerate_credentials", lambda: inventory)
    deleted: list[str] = []
    monkeypatch.setattr(
        "rinari.cli.commands.secrets.delete_credential",
        lambda entry_target, cred_type: deleted.append(entry_target),
    )

    # `main()` activa el modo JSON global al ver --json antes del subcomando;
    # el CliRunner no pasa por esa ruta, así que se replica aquí.
    monkeypatch.setattr("rinari.cli.deps._global_json", True)

    # El lock se mantiene desde otro hilo: en el mismo hilo es reentrante.
    ready = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock(home / "credentials.lock"):
            ready.set()
            release.wait(5)

    keeper = threading.Thread(target=holder)
    keeper.start()
    assert ready.wait(5)
    try:
        blocked = CliRunner().invoke(
            app, ["--json", "secrets", "cleanup", "--apply", "--lock-timeout", "0.3"]
        )
    finally:
        release.set()
        keeper.join(5)
    assert blocked.exit_code == 8, blocked.output
    envelope = json.loads(blocked.stdout)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "CREDENTIAL_STORE_BUSY"
    assert envelope["error"]["retryable"] is True
    assert deleted == []

    free = CliRunner().invoke(app, ["--json", "secrets", "cleanup", "--apply"])
    assert free.exit_code == 0, free.output
    assert json.loads(free.stdout)["data"]["deleted"] == 1
    assert deleted == [target]
