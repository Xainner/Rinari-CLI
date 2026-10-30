"""Human verification checks (plan 0.4.0 #45)."""

from __future__ import annotations

from rinari.browser import human_check


def test_a_check_that_clears_on_its_own_is_not_reported() -> None:
    answers = iter([{"kind": "cloudflare", "url": "https://a/"}, None])
    now = [0.0]
    found = human_check.detect(
        lambda _expr: next(answers),
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
    )
    assert found is None


def test_a_check_still_there_after_settling_is_reported() -> None:
    now = [0.0]
    found = human_check.detect(
        lambda _expr: {"kind": "turnstile", "url": "https://a/login"},
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
    )
    assert found == {"kind": "turnstile", "url": "https://a/login"}
    assert now[0] >= human_check.SETTLE_S


def test_a_failing_probe_never_breaks_the_tool() -> None:
    def boom(_expr):
        raise RuntimeError("detached")

    assert human_check.detect(boom) is None
    assert human_check.probe(lambda _expr: {"kind": "something-else"}) is None
    assert human_check.probe(lambda _expr: "nope") is None


def test_cancelling_while_settling_stops_looking() -> None:
    assert (
        human_check.detect(
            lambda _expr: {"kind": "hcaptcha", "url": ""},
            cancelled=lambda: True,
            sleep=lambda _s: None,
        )
        is None
    )


def test_the_probe_only_counts_visible_unanswered_checks() -> None:
    # The expression itself is browser code; these pin the rules it encodes.
    probe = human_check.PROBE
    assert "size=invisible" in probe
    assert "cf-turnstile-response" in probe
    assert "g-recaptcha-response" in probe
    assert "h-captcha-response" in probe
    assert "_cf_chl_opt" in probe


def test_the_message_asks_the_owner_and_never_to_solve_it() -> None:
    text = human_check.message({"kind": "recaptcha", "url": "https://a/"})
    assert "reCAPTCHA" in text
    assert "must not try to solve, bypass or retry" in text
    assert "user.ask" in text
