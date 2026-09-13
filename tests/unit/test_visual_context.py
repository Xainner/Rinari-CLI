from types import SimpleNamespace

from rinari.models.types import ChatMessage, ModelRequest, ToolCall
from rinari.models.visual_context import select_visual_context


def image(n):
    return SimpleNamespace(uri=f"artifact://session/media/{n}.png", sha256=str(n))


def test_user_images_are_not_cut_by_a_fixed_count():
    request = ModelRequest(
        model="test",
        messages=tuple(ChatMessage(role="user", images=(image(n),)) for n in range(12)),
    )
    assert select_visual_context(request) == request
    compacted = select_visual_context(request, compact=True)
    assert sum(len(m.images) for m in compacted.messages) == 1
    assert "artifact://session/media/0.png" in compacted.messages[0].content
    assert sum(len(m.images) for m in request.messages) == 12
    assert select_visual_context(compacted, compact=True) == compacted


def test_tool_batch_is_preserved_and_superseded_batch_retires():
    request = ModelRequest(
        model="test",
        messages=(
            ChatMessage.assistant("", (ToolCall("old", "fs.read_image"),)),
            ChatMessage(role="tool", tool_call_id="old", images=(image(0),)),
            ChatMessage.assistant(
                "", tuple(ToolCall(str(n), "fs.read_image") for n in range(1, 7))
            ),
            *(
                ChatMessage(role="tool", tool_call_id=str(n), images=(image(n),))
                for n in range(1, 7)
            ),
        ),
    )
    projected = select_visual_context(request)
    assert not projected.messages[1].images
    assert "artifact://session/media/0.png" in projected.messages[1].content
    assert sum(len(m.images) for m in projected.messages) == 6
    assert request.messages[1].images


def test_explicit_recovery_can_load_an_old_original():
    request = ModelRequest(
        model="test",
        messages=(
            ChatMessage.user("follow up"),
            ChatMessage.assistant("", (ToolCall("reload", "fs.read_image"),)),
            ChatMessage(role="tool", tool_call_id="reload", images=(image(0),)),
        ),
    )
    assert select_visual_context(request).messages[-1].images
