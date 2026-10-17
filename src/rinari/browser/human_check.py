"""Human verification checks on a page (plan 0.4.0 #45).

Cloudflare Turnstile, hCaptcha, reCAPTCHA and the «Checking your browser»
interstitial exist to keep automated browsers out, and Rinari's browser is
one: a site that rejects it is doing its job. Rinari never disguises the
browser nor tries to solve the check. What it does is notice it, so the
agent stops and asks the owner instead of retrying in circles.

Only a check that is **visible and unanswered** counts. reCAPTCHA v3 and
other invisible widgets sit on many pages without asking anything, and a
widget that already handed out its token was passed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

#: Returns `{kind, url}` while the page shows a check, `null` otherwise.
PROBE = r"""(() => {
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width >= 20 && box.height >= 20 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const answered = (selector) =>
    Array.from(document.querySelectorAll(selector))
      .some((el) => String(el.value || '').length > 0);
  const frames = Array.from(document.querySelectorAll('iframe'));
  const framesFrom = (pattern) =>
    frames.filter((frame) =>
      pattern.test(frame.src || '') && !/[?&]size=invisible/.test(frame.src || ''));
  const found = (kind) => ({ kind, url: location.href });
  const title = String(document.title || '').trim().toLowerCase();
  if (
    window._cf_chl_opt ||
    document.getElementById('challenge-form') ||
    document.getElementById('challenge-running') ||
    /^(just a moment|un momento|checking your browser|attention required)/.test(title)
  ) return found('cloudflare');
  const turnstile = Array.from(document.querySelectorAll('.cf-turnstile')).concat(
    framesFrom(/^https:\/\/challenges\.cloudflare\.com\//));
  if (turnstile.some(visible) && !answered('[name="cf-turnstile-response"]')) {
    return found('turnstile');
  }
  const hcaptcha = Array.from(
    document.querySelectorAll('.h-captcha:not([data-size="invisible"])')).concat(
    framesFrom(/^https:\/\/([a-z0-9-]+\.)?hcaptcha\.com\//));
  if (hcaptcha.some(visible) && !answered('[name="h-captcha-response"]')) {
    return found('hcaptcha');
  }
  const recaptcha = framesFrom(/\/recaptcha\/(api2|enterprise)\/(anchor|bframe)/);
  if (recaptcha.some(visible) && !answered('[name="g-recaptcha-response"]')) {
    return found('recaptcha');
  }
  return null;
})()"""

#: How long a check may take to clear on its own. A managed Turnstile or the
#: interstitial pass by themselves in a real browser within a few seconds;
#: only one still there after this is a check for the owner.
SETTLE_S = 4.0
_POLL_S = 0.5

NAMES = {
    "cloudflare": "Cloudflare's browser check",
    "turnstile": "Cloudflare Turnstile",
    "hcaptcha": "hCaptcha",
    "recaptcha": "reCAPTCHA",
}


def probe(evaluate: Callable[[str], Any]) -> dict[str, str] | None:
    """One look at the page. Any failure reads as «no check»: the probe
    never turns a working tool into a broken one."""
    try:
        value = evaluate(PROBE)
    except Exception:  # best effort by design
        return None
    if isinstance(value, dict) and value.get("kind") in NAMES:
        return {"kind": str(value["kind"]), "url": str(value.get("url") or "")}
    return None


def detect(
    evaluate: Callable[[str], Any],
    *,
    cancelled: Callable[[], bool] | None = None,
    settle_s: float = SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str] | None:
    """The check the page still shows after giving it `settle_s` to clear."""
    found = probe(evaluate)
    if found is None:
        return None
    deadline = clock() + settle_s
    while clock() < deadline:
        if cancelled is not None and cancelled():
            return None
        sleep(_POLL_S)
        found = probe(evaluate)
        if found is None:
            return None
    return found


def message(found: dict[str, str]) -> str:
    """What the model reads: stop, do not solve it, ask the owner."""
    name = NAMES.get(found["kind"], "a human verification check")
    return (
        f"This page asks for a human verification ({name}). Your browser is automated, "
        "so the site rejects it, and you must not try to solve, bypass or retry it. Stop "
        "using the browser for this site and ask the owner (user.ask) to do this step "
        "themselves: they can open the page in their own browser from the card in the "
        "chat, or in Chrome/Edge with Rinari, and tell you when it is done."
    )


__all__ = ["NAMES", "PROBE", "SETTLE_S", "detect", "message", "probe"]
