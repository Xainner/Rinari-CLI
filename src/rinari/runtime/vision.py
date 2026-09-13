"""Pure visual route policy, independent of consent, tools and transports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VisualDecision:
    route: str  # conversation | dedicated | unavailable
    fallback: bool = False
    reason: str = ""

    @property
    def available(self):
        return self.route != "unavailable"


def resolve_visual_route(mode, main_vision, dedicated_available=False, *, main_rejected=False):
    if mode not in {"automatic", "dedicated", "conversation"}:
        return VisualDecision("unavailable", reason="Invalid visual mode; review Vision settings")
    if mode == "conversation":
        return VisualDecision("conversation")
    if dedicated_available:
        return VisualDecision("dedicated")
    if mode == "automatic" and main_vision is True and not main_rejected:
        return VisualDecision("conversation")
    return VisualDecision(
        "unavailable",
        reason=(
            "Configure an auxiliary visual model, declare vision support, "
            "or select Native in Vision settings"
        ),
    )


def visual_status(caller):
    """Shared host preflight; compatibility for injected/test callers."""
    method = getattr(caller, "visual_decision", None)
    if method:
        return method()
    return resolve_visual_route("automatic", caller.capabilities().vision)
