"""Saneamiento del vault de credenciales: clasificación y borrado.

Las huérfanas aparecen cuando el backend reescribe entradas (ver
`test_credentials_keyring`) y nadie las retira; en Windows terminan llenando
el Administrador de credenciales. El saneamiento clasifica cada entrada como
viva, huérfana o ajena y solo borra las huérfanas.
"""

from __future__ import annotations

from rinari.application.credentials_gc import apply_cleanup, plan_cleanup
from rinari.application.credentials_vault import VaultCredential


def _cred(
    target: str,
    username: str = "",
    *,
    comment: str = "Stored using python-keyring",
    written: str = "2026-09-10T00:00:00",
) -> VaultCredential:
    return VaultCredential(
        target=target,
        username=username,
        comment=comment,
        last_written=written,
        persist=3,
        cred_type=1,
    )


def test_plan_classifies_live_orphan_and_foreign_entries() -> None:
    entries = [
        _cred("providers/prov_live@rinari", "providers/prov_live"),  # viva, formato desplazado
        _cred("rinari/providers/prov_live", "providers/prov_live"),  # viva, formato actual
        _cred("providers/prov_dead@rinari", "providers/prov_dead"),  # huérfana
        _cred("rinari", "providers/prov_old"),  # huérfana, formato legado
        _cred("XboxLive"),  # ajena
        _cred("providers/prov_foreign", "providers/prov_foreign"),  # parece nuestra, sin servicio
    ]
    plan = plan_cleanup(entries, live_provider_ids={"prov_live"})

    assert [entry.target for entry in plan.live] == [
        "providers/prov_live@rinari",
        "rinari/providers/prov_live",
    ]
    assert [entry.target for entry in plan.orphans] == [
        "providers/prov_dead@rinari",
        "rinari",
    ]
    assert [entry.target for entry in plan.foreign] == [
        "XboxLive",
        "providers/prov_foreign",
    ]
    assert plan.summary() == {"live": 2, "orphans": 2, "foreign": 2}


def test_plan_treats_stale_staging_entries_as_orphans() -> None:
    entries = [
        _cred("rinari/staging/providers/prov_dead", "providers/prov_dead"),
        _cred("rinari/staging/providers/prov_live", "providers/prov_live"),
    ]
    plan = plan_cleanup(entries, live_provider_ids={"prov_live"})
    assert [entry.target for entry in plan.orphans] == ["rinari/staging/providers/prov_dead"]
    assert [entry.target for entry in plan.live] == ["rinari/staging/providers/prov_live"]


def test_apply_deletes_only_orphans() -> None:
    plan = plan_cleanup(
        [
            _cred("providers/prov_live@rinari", "providers/prov_live"),
            _cred("providers/prov_dead@rinari", "providers/prov_dead"),
            _cred("XboxLive"),
        ],
        live_provider_ids={"prov_live"},
    )
    deleted: list[str] = []

    def delete(target: str, cred_type: int) -> None:
        deleted.append(target)

    report = apply_cleanup(plan, delete=delete)

    assert deleted == ["providers/prov_dead@rinari"]
    assert (report.deleted, report.failed) == (1, 0)
    assert report.failures == []


def test_apply_records_failures_without_raising() -> None:
    plan = plan_cleanup(
        [_cred("providers/prov_dead@rinari", "providers/prov_dead")],
        live_provider_ids=set(),
    )

    def delete(target: str, cred_type: int) -> None:
        raise PermissionError("access denied")

    report = apply_cleanup(plan, delete=delete)

    assert (report.deleted, report.failed) == (0, 1)
    assert report.failures[0][0] == "providers/prov_dead@rinari"
    assert "access denied" in report.failures[0][1]


def test_cleanup_command_reports_a_plan_or_unsupported(tmp_path, monkeypatch) -> None:
    """El comando responde con el plan (Windows) o con «unsupported» (otras plataformas)."""
    import json

    from typer.testing import CliRunner

    from rinari.cli.main import app
    from rinari.shared.paths import ENV_HOME

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.chdir(work)

    result = CliRunner().invoke(app, ["--json", "secrets", "cleanup"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "secrets.cleanup"
    assert payload["data"]["status"] in {"plan", "unsupported"}
