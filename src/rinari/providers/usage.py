"""Remote account quotas. Independent of Rinari's execution budgets."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

import httpx

from rinari.providers.catalog import product_for


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.isoformat() if parsed.tzinfo else None
        except ValueError:
            pass
    return None


def percent(value):
    if isinstance(value, Decimal):
        value = float(value)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value))
        return format(decimal, "f") if decimal.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def window(key, value, *, scope="account", label=None):
    used = percent(value.get("percent", value.get("used_percent")))
    return {
        "id": key,
        "label": label or key,
        "scope": scope,
        "used_percent": used,
        "remaining_percent": None if used is None else 100 - used,
        "resets_at": timestamp(value.get("resetsAt", value.get("reset_at"))),
        "duration_seconds": value.get("limit_window_seconds")
        if type(value.get("limit_window_seconds")) is int and value["limit_window_seconds"] > 0
        else None,
    }


class ProviderUsageService:
    def __init__(self, providers, *, clock=time.time):
        self.providers = providers
        self.clock = clock
        self.lock = threading.RLock()

    def get(self, ref, *, refresh=False):
        with self.lock:
            snapshot = self._get(ref, refresh=refresh)
            row = self.providers._ctx.db.query_one(
                """SELECT count(*) AS calls,
                   sum(json_extract(payload_json, '$.usage.input_tokens')) AS input_tokens,
                   sum(json_extract(payload_json, '$.usage.output_tokens')) AS output_tokens,
                   count(json_extract(payload_json, '$.usage.input_tokens')) AS measured_input,
                   count(json_extract(payload_json, '$.usage.output_tokens')) AS measured_output
                   FROM session_events WHERE type = 'ModelInvoked'
                   AND json_extract(payload_json, '$.provider_id') = ?""",
                (snapshot["provider_id"],),
            )
            snapshot["local_usage"] = {
                "calls": row["calls"],
                "input_tokens": row["input_tokens"]
                if row["measured_input"] == row["calls"]
                else None,
                "output_tokens": row["output_tokens"]
                if row["measured_output"] == row["calls"]
                else None,
            }
            from rinari.providers.catalog import DASHBOARDS

            snapshot["dashboard_url"] = DASHBOARDS.get(snapshot["product_id"])
            return snapshot

    def _get(self, ref, *, refresh):
        provider = self.providers.get(ref)
        product = product_for(provider)
        now = self.clock()
        base = {
            "provider_id": provider.id,
            "product_id": product,
            "status": "unsupported",
            "windows": [],
            "balances": [],
            "fetched_at": None,
            "source": None,
            "detail": "Remote account usage is not exposed by this integration.",
            "retry_at": None,
        }
        if product in ("ollama", "lmstudio"):
            return {**base, "detail": "Remote quotas do not apply to local providers."}
        if product not in ("opencode-go", "openrouter", "deepseek", "chatgpt"):
            return base
        try:
            secret = self.providers.resolve_secret(provider)
        except Exception:
            return {
                **base,
                "status": "needs_auth",
                "detail": "Reconnect this provider to read account usage.",
            }
        if not secret:
            return {
                **base,
                "status": "needs_auth",
                "detail": "Connect this provider to read account usage.",
            }
        # Includes actual credential fingerprint: env rotation also invalidates the cache.
        identity = [
            provider.id,
            product,
            provider.endpoint,
            self.providers.credential_ref(provider),
            hashlib.sha256(secret.encode()).hexdigest(),
        ]
        key = "provider.usage." + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        repository = self.providers._ctx.config_repo
        cached = repository.get_json(key) or {}
        snapshot = cached.get("snapshot", base)
        age = now - cached.get("fetched", 0)
        invalidated = repository.get_json(f"provider.usage.invalidated.{provider.id}") or 0
        refresh = refresh or invalidated > cached.get("fetched", 0)
        if cached and (now < cached.get("retry", 0) or (not refresh and age < 60)):
            return {
                **snapshot,
                "status": "stale"
                if age > 300 and snapshot.get("fetched_at") and snapshot["status"] != "needs_auth"
                else snapshot["status"],
            }
        client = self.providers._client or httpx.Client(timeout=10, follow_redirects=False)
        headers = {"Authorization": f"Bearer {secret}", "User-Agent": "Rinari/1"}
        if product == "chatgpt":
            from rinari.providers.adapters.subscriptions import chatgpt_headers

            headers = chatgpt_headers(secret)
        failures = []
        retry = now + min(900, 60 * 2 ** min(cached.get("failures", 0), 4))

        def fetch(url):
            nonlocal retry
            try:
                response = client.get(url, headers=headers, timeout=10, follow_redirects=False)
                if response.status_code >= 400:
                    delay = min(900, 60 * 2 ** min(cached.get("failures", 0), 4))
                    after = response.headers.get("Retry-After")
                    if after:
                        try:
                            seconds = float(after)
                            if math.isfinite(seconds):
                                delay = max(delay, seconds)
                        except ValueError:
                            with contextlib.suppress(ValueError, TypeError, OverflowError):
                                delay = max(delay, parsedate_to_datetime(after).timestamp() - now)
                    retry = max(retry, now + delay)
                    failures.append(
                        "needs_auth"
                        if response.status_code == 401
                        else f"HTTP {response.status_code}"
                    )
                    return None
                value = response.json(parse_float=Decimal)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (httpx.HTTPError, ValueError):
                failures.append("Usage service unavailable or invalid response")
                return None

        result = {**base, "windows": [], "balances": [], "status": "available", "detail": ""}
        try:
            if product == "opencode-go":
                result["source"] = "https://opencode.ai/zen/go/v1/usage"
                data = fetch(result["source"])
                usage = (data or {}).get("usage")
                if isinstance(usage, dict):
                    for name, value in usage.items():
                        if isinstance(value, dict):
                            result["windows"].append(window(name, value))
            elif product == "openrouter":
                result["source"] = "https://openrouter.ai/api/v1/credits"
                credits = (fetch(result["source"]) or {}).get("data")
                if isinstance(credits, dict):
                    total, used = (
                        amount(credits.get("total_credits")),
                        amount(credits.get("total_usage")),
                    )
                    if total is not None and used is not None:
                        result["balances"].append(
                            {
                                "scope": "account",
                                "label": "Credit balance",
                                "currency": "USD",
                                "remaining": str(Decimal(total) - Decimal(used)),
                                "limit": total,
                                "unlimited": False,
                            }
                        )
                limits = (fetch("https://openrouter.ai/api/v1/key") or {}).get("data")
                if isinstance(limits, dict) and "limit" in limits:
                    result["balances"].append(
                        {
                            "scope": "key",
                            "label": "Key spending limit",
                            "currency": "USD",
                            "remaining": amount(limits.get("limit_remaining")),
                            "limit": amount(limits.get("limit")),
                            "unlimited": limits["limit"] is None,
                        }
                    )
            elif product == "deepseek":
                result["source"] = "https://api.deepseek.com/user/balance"
                data = fetch(result["source"])
                infos = (data or {}).get("balance_infos")
                if isinstance(infos, list):
                    for item in infos:
                        if not isinstance(item, dict) or not isinstance(item.get("currency"), str):
                            continue
                        result["balances"].append(
                            {
                                "scope": "account",
                                "label": "Balance",
                                "currency": item["currency"],
                                "remaining": amount(item.get("total_balance")),
                                "granted": amount(item.get("granted_balance")),
                                "topped_up": amount(item.get("topped_up_balance")),
                                "limit": None,
                                "unlimited": False,
                            }
                        )
            elif product == "chatgpt":
                result["source"] = "https://chatgpt.com/backend-api/wham/usage"
                data = fetch(result["source"])
                if data:
                    groups = [("default", data.get("rate_limit"))]
                    additional = data.get("additional_rate_limits")
                    for group in additional if isinstance(additional, list) else []:
                        if isinstance(group, dict):
                            groups.append(
                                (
                                    str(
                                        group.get(
                                            "limit_name", group.get("metered_feature", "additional")
                                        )
                                    ),
                                    group.get("rate_limit"),
                                )
                            )
                    for name, group in groups:
                        if isinstance(group, dict):
                            for name_window in ("primary_window", "secondary_window"):
                                value = group.get(name_window)
                                if isinstance(value, dict):
                                    result["windows"].append(
                                        window(
                                            name + ":" + name_window,
                                            value,
                                            scope=name,
                                            label=name + " · " + name_window,
                                        )
                                    )
                    credits = data.get("credits")
                    if isinstance(credits, dict):
                        result["balances"].append(
                            {
                                "scope": "account",
                                "label": "Credits",
                                "currency": "credits",
                                "remaining": amount(credits.get("balance")),
                                "limit": None,
                                "unlimited": credits.get("unlimited") is True,
                            }
                        )
        finally:
            if self.providers._client is None:
                client.close()
        valid = bool(result["windows"] or result["balances"])
        if not valid and not failures:
            failures.append("No usable quota data returned.")
        incomplete = any(w["used_percent"] is None for w in result["windows"]) or any(
            b["remaining"] is None and not b["unlimited"] for b in result["balances"]
        )
        if valid:
            result.update(
                fetched_at=timestamp(now),
                status="partial" if failures or incomplete else "available",
                detail="; ".join(failures),
            )
        else:
            result = {
                **snapshot,
                "status": "needs_auth"
                if "needs_auth" in failures
                else "stale"
                if snapshot.get("fetched_at")
                else "error",
                "detail": "; ".join(failures) or "No usable quota data returned.",
            }
        result["retry_at"] = timestamp(retry) if failures else None
        value = {
            "snapshot": result,
            "fetched": now if valid else cached.get("fetched", 0),
            "retry": retry if failures else now + 5,
            "failures": cached.get("failures", 0) + 1 if failures else 0,
        }
        with self.providers._ctx.db.transaction():
            repository.set_json(key, value, updated_at=timestamp(now))
        return result
