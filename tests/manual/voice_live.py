"""Opt-in: do the personality levels show, and does Rinari say "I"? (plan 0.4.0 #87, #95)

Costs money (or plan quota) on the owner's provider account. Nothing runs
without an explicit saved model and a call limit; `--dry-run` prints the
estimate and touches no credentials or network.

    uv run python tests/manual/voice_live.py --dry-run
    uv run python tests/manual/voice_live.py --model ALIAS --max-calls 70

Every ask is one turn of the real agent loop (system prompt, soul, level and
tools as in the app) in a fresh conversation of a temporary Rinari home. The
provider credential reaches that home through an environment variable for
the length of the run, as in `context_continuity_live.py`.
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
from rinari.evals import voice_live
from rinari.evals.continuity_live import CallBudget
from rinari.models.router import ModelRouter
from rinari.runtime.model_caller import ModelCaller
from rinari.soul.store import SoulStore

SECRET_ENV = "RINARI_VOICE_LIVE_SECRET"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="Saved model alias or ID (required unless --dry-run)")
    parser.add_argument("--runs", type=int, default=1, help="Repetitions of every ask")
    parser.add_argument("--max-calls", type=int, help="Hard limit on provider calls (required)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Estimate only; no credentials, no calls"
    )
    parser.add_argument("--out", type=Path, help="Write the JSON report here too")
    args = parser.parse_args()

    estimate = voice_live.plan(runs=args.runs)
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
    os.environ[SECRET_ENV] = source.providers.resolve_secret(provider) or ""
    try:
        with tempfile.TemporaryDirectory(prefix="rinari-voice-live-") as directory:
            ctx = build_app_context(Path(directory) / "state")
            services = build_services(ctx, user_home=Path(directory))
            try:
                if provider.auth_method == "external-cli":
                    services.providers.set_external_runtimes_enabled(True)
                target = services.providers.add(
                    AddProviderInput(
                        alias="live",
                        provider_type=provider.type,
                        auth_method=provider.auth_method,
                        endpoint=provider.endpoint,
                        settings=provider.settings,
                        secret_env=SECRET_ENV if provider.auth_method != "external-cli" else None,
                    )
                )
                chosen = services.models.add(
                    "live",
                    model.provider_model_id,
                    "live",
                    capabilities=model.capabilities,
                    settings=model.settings,
                )
                services.providers.use("live")
                caller = CallBudget(
                    ModelCaller(
                        ModelRouter(services.providers, services.models), target, chosen.id
                    ),
                    args.max_calls,
                )
                souls = SoulStore(ctx.home)

                ask = voice_live.agent_ask(services, directory, caller)

                result = voice_live.run(ask, souls.set_character_intensity, runs=args.runs)
                result.update(model=model.alias, provider=provider.alias, calls=caller.calls)
                text = json.dumps(result, ensure_ascii=False, indent=2)
                print(text)
                if args.out:
                    args.out.write_text(text, encoding="utf-8")
            finally:
                ctx.close()
    finally:
        os.environ.pop(SECRET_ENV, None)
        source_ctx.close()


if __name__ == "__main__":
    main()
