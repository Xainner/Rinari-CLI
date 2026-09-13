import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rinari.models.execution import destination_slot, validate
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest, Usage
from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import SystemClock


@pytest.mark.parametrize("limit", [1, 3])
def test_destination_shared_across_sessions_and_callers(tmp_path, limit):
    (tmp_path / "model-execution.json").write_text(
        json.dumps({"max_concurrency": 8, "providers": {"p": limit}})
    )
    active = peak = 0
    lock = threading.Lock()

    def call(_):
        nonlocal active, peak
        with destination_slot(tmp_path, "p"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=9) as executor:
        list(executor.map(call, range(12)))
    assert peak == limit


def test_cancelled_waiter_never_dispatches_or_blocks_next(tmp_path):
    (tmp_path / "model-execution.json").write_text(json.dumps({"max_concurrency": 1}))
    token = CancellationToken()
    entered = threading.Event()

    def wait():
        entered.set()
        with destination_slot(tmp_path, "p", token.throw_if_cancelled):
            pytest.fail("cancelled request dispatched")

    with ThreadPoolExecutor() as pool, destination_slot(tmp_path, "p"):
        pending = pool.submit(wait)
        entered.wait(1)
        token.cancel()
        with pytest.raises(Exception, match="cancelled"):
            pending.result(timeout=1)
    with destination_slot(tmp_path, "p"):
        pass


def test_destination_and_installations_are_independent(tmp_path):
    (tmp_path / "model-execution.json").write_text(json.dumps({"max_concurrency": 1}))
    with (destination_slot(tmp_path, "p"), destination_slot(tmp_path, "other"),
          destination_slot(tmp_path / "another-install", "p")):
        pass


def test_budget_reservations_are_atomic_across_children():
    parent = BudgetMeter(TurnBudgetLimits(max_model_calls=3), SystemClock())
    children = [parent.spawn_child() for _ in range(10)]

    def reserve(child):
        try:
            child.reserve_model_call()
            child.note_usage(Usage(2, 3))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor() as executor:
        assert sum(executor.map(reserve, children)) == 3
    assert (parent.model_calls, parent.input_tokens, parent.output_tokens) == (3, 6, 9)


def test_generation_inherits_model_and_explicit_request_wins(tmp_path):
    router = ModelRouter.__new__(ModelRouter)
    router.adapter = lambda p: SimpleNamespace(default_max_tokens=None)
    router._providers = SimpleNamespace(_ctx=SimpleNamespace(home=tmp_path))
    provider = SimpleNamespace(settings={"generation": {"max_tokens": 10000}})
    model = SimpleNamespace(
        id="m", provider_model_id="remote", settings={"generation": {"max_tokens": 18000}}
    )
    request = ModelRequest(model="m", messages=(ChatMessage.user("hi"),))
    assert router.generation_request(provider, model, request).max_tokens == 18000
    (tmp_path / "model-execution.json").write_text(json.dumps({"models": {"m": 22000}}))
    assert router.generation_request(provider, model, request).max_tokens == 22000
    assert (
        router.generation_request(provider, model, replace(request, max_tokens=99)).max_tokens == 99
    )


def test_unconfigured_generation_omits_vision_cap(tmp_path):
    router = ModelRouter.__new__(ModelRouter)
    router.adapter = lambda p: SimpleNamespace(default_max_tokens=None)
    router._providers = SimpleNamespace(_ctx=SimpleNamespace(home=tmp_path))
    result = router.generation_request(
        SimpleNamespace(settings={}),
        SimpleNamespace(id="m", provider_model_id="remote", settings={}),
        ModelRequest(model="m", messages=(ChatMessage.user("image"),)),
    )
    assert result.max_tokens is None


@pytest.mark.parametrize(
    "value", [{"max_concurrency": 0}, {"providers": []}, {"models": {"m": -1}}]
)
def test_invalid_policy_rejected(value):
    with pytest.raises(ValueError):
        validate(value)
