import pytest

from rinari.application.context import AppContext, build_app_context
from rinari.shared.clock import FakeClock
from rinari.shared.paths import ENV_HOME


@pytest.fixture
def app_ctx(tmp_path, monkeypatch) -> AppContext:
    home = tmp_path / "rinari-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    ctx = build_app_context(home=str(home), clock=FakeClock(start=1_700_000_000.0, step=1.0))
    yield ctx
    ctx.close()
