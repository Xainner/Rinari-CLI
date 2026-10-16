"""Media projection: canonical messages remain untouched."""

from dataclasses import replace

from rinari.models.types import ModelRequest


def retire_images(message, *, unseen=False):
    """The message without its pixels and a note saying what happened.

    `unseen`: the model never got them (it is set up without vision), so it
    must not describe them or read them again (plan 0.4.0 #14). Otherwise it
    already looked at them earlier and can read them again if it needs to.
    """
    if not message.images:
        return message
    refs = ", ".join(dict.fromkeys(i.uri for i in message.images))
    if unseen:
        note = (
            "\n[These images were not shown to you: the current model is set up without "
            "vision. Do not describe their content and do not read them again: " + refs + "]"
        )
    else:
        note = (
            "\n[You already looked at these images earlier; read them again with "
            "fs.read_image only if you need another look: " + refs + "]"
        )
    return replace(
        message,
        images=(),
        retired_images=message.images,
        display_content=message.display_content
        if message.display_content is not None
        else message.content,
        content=(message.content or "") + note,
    )


def select_visual_context(request: ModelRequest, *, compact: bool = False) -> ModelRequest:
    """Retire tool images the model already looked at; compact older attachments
    only on compaction.

    An assistant tool-call message defines a batch, independently of user
    wording. A tool batch's images go to the model once: when an assistant
    message follows them, the model already answered having seen them, and
    resending them on every later call only weighed on the context (one
    session sent 247 images for 33 distinct ones, plan 0.4.0 #61d/#66f). They
    stay readable with `fs.read_image`. No image count or inferred intent is
    used.
    """
    batch = -1
    batches = {}
    newest = None
    anchor = -1
    last_assistant = -1
    for index, msg in enumerate(request.messages):
        if msg.role == "assistant":
            last_assistant = index
            if msg.tool_calls:
                batch = index
        if msg.role == "tool" and msg.images:
            batches[index] = batch
            newest = batch
        if msg.role == "user" and msg.images:
            anchor = index
    messages = []
    for index, msg in enumerate(request.messages):
        stale = index in batches and (batches[index] != newest or last_assistant > index)
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
    # Harness notes (turn context, loop reminders) travel as user messages
    # after the owner's; they are never the owner's message.
    return max(
        (
            i
            for i, m in enumerate(messages)
            if m.role == "user" and (m.origin or {}).get("kind") != "harness"
        ),
        default=-1,
    )


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
