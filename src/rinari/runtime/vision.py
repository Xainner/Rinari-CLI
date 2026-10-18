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


#: Tools that exist to look at an image: without a way to see it, the turn
#: stops (the owner sets up vision). Any other tool's image (a capture, a
#: created image, #11 and #28) is a by-product: it goes back with a note.
SEEING_TOOLS = frozenset({"fs.read_image", "fs.read_pdf_pages"})


def needs_sight(message) -> bool:
    """Whether this message's images must be seen for the turn to go on."""
    return message.role != "tool" or getattr(message, "name", None) in SEEING_TOOLS


def visual_status(caller):
    """Shared host preflight; compatibility for injected/test callers."""
    method = getattr(caller, "visual_decision", None)
    if method:
        return method()
    return resolve_visual_route("automatic", caller.capabilities().vision)
