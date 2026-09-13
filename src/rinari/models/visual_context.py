"""Media projection: canonical messages remain untouched."""

from dataclasses import replace

from rinari.models.types import ModelRequest


def retire_images(message):
    if not message.images:
        return message
    refs = ", ".join(dict.fromkeys(i.uri for i in message.images))
    return replace(
        message,
        images=(),
        retired_images=message.images,
        display_content=message.display_content
        if message.display_content is not None
        else message.content,
        content=(message.content or "")
        + "\n[Original images available via fs.read_image: "
        + refs
        + "]",
    )


def select_visual_context(request: ModelRequest, *, compact: bool = False) -> ModelRequest:
    """Retire superseded tool batches; compact older attachments only on compaction.

    An assistant tool-call message defines a batch, independently of user wording.
    No image count or inferred intent is used.
    """
    batch = -1
    batches = {}
    newest = None
    anchor = -1
    for index, msg in enumerate(request.messages):
        if msg.role == "assistant" and msg.tool_calls:
            batch = index
        if msg.role == "tool" and msg.images:
            batches[index] = batch
            newest = batch
        if msg.role == "user" and msg.images:
            anchor = index
    messages = []
    for index, msg in enumerate(request.messages):
        stale = index in batches and batches[index] != newest
        if compact and msg.role == "user" and msg.images and anchor >= 0 and index < anchor:
            stale = True
        messages.append(retire_images(msg) if stale else msg)
    return replace(request, messages=tuple(messages))


def last_owner_message(messages):
    """Original display text marks owner messages; legacy callers use role fallback."""
    owners = [
        i for i, m in enumerate(messages) if m.role == "user" and m.display_content is not None
    ]
    if owners:
        return owners[-1]
    return max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)


def prepare_visual_payload(request, constraints):
    """Enforce declared bounds, compacting historical media before network I/O.

    Current attachments and the latest tool batch are protected. If those alone
    exceed the bound the same explicit limit error is returned; nothing is sent.
    """
    from rinari.models.images import VisualPayloadLimitError, validate_visual_payload

    try:
        validate_visual_payload(request, constraints)
        return request
    except VisualPayloadLimitError:
        compacted = select_visual_context(request, compact=True)
        validate_visual_payload(compacted, constraints)
        return compacted
