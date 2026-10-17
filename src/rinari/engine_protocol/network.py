"""Ajustes → Avanzado → Red (plan 0.4.0 #96): proxy, certificates and «Probar conexión».

`network.settings.get` returns the saved settings with the defaults filled in
plus `effective` (what the clients use right now: the proxy seen in the
environment or the OS, the certificate source, and the error that made a
broken network.json fall back to the defaults). `network.settings.set` takes
`proxy` and/or `certificates` (a missing one keeps its saved value), validates
and writes `~/.rinari/network.json`; the next request uses it, no restart.
`network.test` reaches every enabled provider through the same factory.
"""

from __future__ import annotations

from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

_SET_KEYS = frozenset({"proxy", "certificates"})
_TEST_KEYS = frozenset({"provider_ids"})


def _home(services: Any) -> Any:
    return services.ctx.home


def _view(services: Any) -> dict[str, Any]:
    from rinari.net import effective, settings_for

    settings, _ = settings_for(_home(services))
    return {**settings.to_json(), "effective": effective(_home(services))}


def settings_get(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise EngineProtocolError(INVALID_PARAMS, "network.settings.get takes no parameters.")
    return _view(services)


def settings_set(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    from rinari.net import NetworkSettings, load_settings, save_settings

    unknown = set(params) - _SET_KEYS
    if unknown:
        raise EngineProtocolError(INVALID_PARAMS, f"Unknown params: {', '.join(sorted(unknown))}")
    if not params:
        raise EngineProtocolError(INVALID_PARAMS, "Give 'proxy' and/or 'certificates'.")
    try:
        current = load_settings(_home(services)).to_json()
    except ValueError:
        current = NetworkSettings().to_json()  # a broken file is replaced, not merged
    try:
        save_settings(_home(services), {**current, **params})
    except ValueError as exc:
        raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
    return _view(services)


def run_test(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    from rinari.net.probe import probe_providers

    unknown = set(params) - _TEST_KEYS
    if unknown:
        raise EngineProtocolError(INVALID_PARAMS, f"Unknown params: {', '.join(sorted(unknown))}")
    wanted = params.get("provider_ids")
    if wanted is not None and (
        not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted)
    ):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'provider_ids' must be a list of ids.")
    records = services.providers.list(include_disabled=False)
    if wanted is not None:
        records = [record for record in records if record.id in wanted or record.alias in wanted]
    return {
        "results": probe_providers(records, _home(services)),
        "effective": _view(services)["effective"],
    }
