"""Opt-in: does a real model continue as well from compacted history as from full history?

Costs money on the owner's provider account. Nothing runs without an explicit
saved model and a call limit; `--dry-run` prints the estimate and touches no
credentials or network.

    uv run python tests/manual/context_continuity_live.py --dry-run
    uv run python tests/manual/context_continuity_live.py --model ALIAS --max-calls 15

Synthetic history only, in a temporary Rinari home. The provider credential is
passed to the temporary home through an environment variable for the length
of the run, as in `context_compaction_smoke.py`.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.context.settings import save
from rinari.evals import continuity_live
from rinari.models.router import ModelRouter
from rinari.runtime.model_caller import ModelCaller
from rinari.storage.records import SessionRecord


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="Saved model alias or ID (required unless --dry-run)")
    parser.add_argument("--runs", type=int, default=1, help="Repetitions of every scenario")
    parser.add_argument("--max-calls", type=int, help="Hard limit on provider calls (required)")
    parser.add_argument(
        "--window", type=int, default=32_000, help="Context window to compact against"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Estimate only; no credentials, no calls"
    )
    parser.add_argument("--out", type=Path, help="Write the JSON report here too")
    args = parser.parse_args()

    estimate = continuity_live.plan(runs=args.runs)
    print(json.dumps({"plan": estimate}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return
    if not args.model or not args.max_calls:
        sys.exit("--model and --max-calls are required for a real run (see --dry-run).")
    if args.max_calls < estimate["min_calls"]:
        sys.exit(
            f"--max-calls {args.max_calls} is below the {estimate['min_calls']} calls a run needs."
        )

    source_ctx = build_app_context(Path.home() / ".rinari")
    source = build_services(source_ctx)
    model = source.models.resolve(args.model)
    provider = source.providers.get(model.provider_id)
    os.environ["RINARI_CONTINUITY_LIVE_SECRET"] = source.providers.resolve_secret(provider) or ""
    try:
        with tempfile.TemporaryDirectory(prefix="rinari-continuity-live-") as directory:
            ctx = build_app_context(Path(directory) / "state")
            services = build_services(ctx, user_home=Path(directory))
            try:
                target = services.providers.add(
                    AddProviderInput(
                        alias="live",
                        provider_type=provider.type,
                        auth_method=provider.auth_method,
                        endpoint=provider.endpoint,
                        settings=provider.settings,
                        secret_env="RINARI_CONTINUITY_LIVE_SECRET",
                    )
                )
                chosen = services.models.add(
                    "live",
                    model.provider_model_id,
                    "live",
                    capabilities=model.capabilities,
                    settings=model.settings,
                )
                save(
                    services,
                    {
                        "enabled": True,
                        "compact_at_percent": 80,
                        "model_id": None,
                        "model_windows": {chosen.id: args.window},
                    },
                )
                caller = continuity_live.CallBudget(
                    ModelCaller(
                        ModelRouter(services.providers, services.models), target, chosen.id
                    ),
                    args.max_calls,
                )

                def session_for(scenario):
                    root = str(Path(directory) / f"project-{scenario.key}")
                    Path(root).mkdir(exist_ok=True)
                    record = SessionRecord(
                        id=f"live-{scenario.key}-{caller.calls}",
                        kind="PROJECT" if scenario.seed else "CHAT",
                        title=scenario.description,
                        project_id=None,
                        project_root_snapshot=root if scenario.seed else None,
                        created_cwd=root,
                        current_cwd=root,
                        provider_id=target.id,
                        model_id=chosen.id,
                        profile_id="",
                        mode="plan",
                        state="active",
                        compact_state=None,
                        created_at="2026-09-23",
                        updated_at="2026-09-23",
                        last_active_at="2026-09-23",
                    )
                    ctx.session_repo.insert(record)
                    return record, root

                result = continuity_live.run(
                    services, session_for, caller, chosen.id, runs=args.runs
                )
                result.update(model=model.alias, provider=provider.alias, calls=caller.calls)
                prices = model.settings or {}
                if "input_price_per_mtok" in prices and "output_price_per_mtok" in prices:
                    tokens_in = sum(
                        (r["full"]["input_tokens"] or 0)
                        + ((r["compacted"] or {}).get("input_tokens") or 0)
                        for r in result["runs"]
                    )
                    tokens_out = sum(
                        (r["full"]["output_tokens"] or 0)
                        + ((r["compacted"] or {}).get("output_tokens") or 0)
                        for r in result["runs"]
                    )
                    # Answers only: summary calls are not included. Marked estimated.
                    result["estimated_answer_cost_usd"] = round(
                        tokens_in / 1e6 * float(prices["input_price_per_mtok"])
                        + tokens_out / 1e6 * float(prices["output_price_per_mtok"]),
                        6,
                    )
                text = json.dumps(result, ensure_ascii=False, indent=2)
                print(text)
                if args.out:
                    args.out.write_text(text, encoding="utf-8")
            finally:
                ctx.close()
    finally:
        os.environ.pop("RINARI_CONTINUITY_LIVE_SECRET", None)
        source_ctx.close()


if __name__ == "__main__":
    main()
