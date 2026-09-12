"""Versioned data contracts for built-ins; extension schemas remain authoritative.

Fields are optional when adapters have alternative success shapes. Unknown fields
are allowed for additive protocol evolution; these schemas describe data, not the
outer ToolResult envelope.
"""

from __future__ import annotations

FIELDS = {
    "artifact.read": (
        "uri:s text:s start_byte:i end_byte:i size_bytes:i next_start_byte:i? truncated:b"
    ),
    "artifact.metadata": "uri:s name:s size_bytes:i mime_type:s modified_ns:i",
    "fs.read": "path:s text:s size_bytes:i sha256:s? truncated:b",
    "fs.read_lines": "path:s lines:a total_lines:i? next_line:i?",
    "fs.write": "path:s bytes_written:i sha256:s",
    "fs.patch": "path:s replacements:i sha256:s",
    "fs.list": "path:s entries:a total_entries:i revision:s next_offset:i?",
    "fs.glob": "root:s pattern:s matches:a truncated:b next_offset:i?",
    "fs.search_text": "root:s pattern:s matches:a files_searched:i truncated:b",
    "fs.stat": "path:s type:s size_bytes:i modified:s executable:b",
    "fs.diff": "a:s b:s diff:s truncated:b changes:i",
    "git.status": "branch:s? dirty:b files:a total_files:i truncated:b",
    "git.diff": "diff:s truncated:b",
    "git.log": "entries:a truncated:b",
    "git.show": "ref:s stat:s truncated:b",
    "git.branch": "current:s? branches:a",
    "shell.exec": "exit_code:i stdout:s stderr:s truncated:b handle:s running:b",
    "process.start": "handle:s pid:i? running:b",
    "process.wait": "handle:s exit_code:i? timed_out:b",
    "process.output": "handle:s stdout:s stderr:s cursor:o has_more:b running:b exit_code:i?",
    "process.signal": "handle:s signal:s",
    "process.list": "processes:a",
    "pty.start": "handle:s command:s running:b",
    "pty.read": "handle:s output:s running:b exit_code:i?",
    "pty.write": "handle:s written:i",
    "pty.resize": "handle:s rows:i columns:i",
    "pty.terminate": "handle:s exit_code:i?",
    "search.files": "root:s pattern:s matches:a truncated:b next_offset:i?",
    "search.regex": "root:s pattern:s matches:a files_searched:i truncated:b",
    "search.symbols": "root:s query:s matches:a truncated:b",
    "search.references": "root:s name:s matches:a truncated:b",
    "search.hybrid": "root:s query:s matches:a truncated:b",
    "lsp.hover": "contents:s?",
    "lsp.signature": "signatures:a active_signature:i? active_parameter:i?",
    "lsp.rename": "changes:o document_changes:a?",
    "memory.remember": "id:s",
    "memory.recall": "scope:s records:a count:i",
    "memory.update": "id:s",
    "memory.forget": "id:s scope:s forgotten:b",
    "memory.episodic": "id:s session_ref:s summary_chars:i",
    "context.retrieve": "query:s results:a count:i",
    "context.pin": "source:s ref:s label:s",
    "context.unpin": "source:s ref:s unpinned:b",
    "context.list_pins": "count:i pins:a",
    "skills.list": "skills:a",
    "skills.show": "name:s version:s body:s required_tools:a optional_tools:a",
    "skills.activate": "name:s version:s active:b",
    "skills.deactivate": "name:s active:b removed:b",
    "capability.search": "query:s results:a matched:b loaded:a load_error:s? diagnostics:a",
    "capability.activate": "activated:a scope:s reason:s",
    "capability.deactivate": "deactivated:a",
    "agent.spawn": "agent_id:s agent:s state:s limits:o",
    "agent.wait": "agent_id:s state:s status:s timed_out:b",
    "agent.result": "agent_id:s state:s",
    "agent.message": "agent_id:s accepted:b",
    "agent.cancel": "agent_id:s cancelled:b",
    "agent.synthesize": "conflicts:a",
    "user.ask": "request_id:s status:s answers:o",
    "verify.record": "id:s kind:s result:s summary:s",
    "verify.plan": "checks:a",
    "verify.evaluate": "outcome:s reasons:a",
    "ssh.inspect": "target_id:s section:s revision:s output:s sections:o",
    "web.search": "query:s results:a",
    "web.fetch": "url:s text:s sha256:s truncated:b",
    "web.open": "url:s title:s description:s text:s links:a provenance:o",
    "web.links": "url:s links:a total:i",
    "web.find": "url:s pattern:s hits:a truncated:b",
    "web.extract_text": "url:s text:s characters:i truncated:b",
    "web.extract_markdown": "url:s markdown:s characters:i truncated:b",
    "web.extract_metadata": "url:s title:s description:s language:s? charset:s? meta:o",
    "web.download": "path:s bytes:i sha256:s content_type:s truncated:b provenance:o",
    "web.cite": "citation:o",
    "web.sources": "sources:a",
    "http.request": "url:s status:i headers:o text:s truncated:b",
    "http.sse": "url:s events:a count:i stopped_early:b timed_out:b elapsed_ms:n",
    "browser.status": "state:s endpoint:s? managed:b targets:i",
    "browser.launch": "endpoint:s",
    "browser.connect": "connected:s",
    "browser.close": "closed:a",
    "browser.tabs": "tabs:a",
    "browser.tabs_close": "closed:s",
    "browser.open": "target_id:s",
    "browser.navigate": "url:s frame_id:s? loader_id:s?",
    "browser.snapshot": "html:s bytes:i truncated:b",
    "browser.a11y": "nodes:a total:i truncated:b",
    "browser.screenshot": "path:s bytes:i sha256:s",
    "browser.click": "clicked:o",
    "browser.fill": "filled:s chars:i",
    "browser.type": "typed:i",
    "browser.select": "selected:s",
    "browser.check": "checked:b",
    "browser.scroll": "scrolled:o",
    "browser.drag": "from:o to:o",
    "browser.evaluate": "truncated:b",
    "browser.console": "events:a",
    "browser.network": "events:a",
    "browser.cookies": "cookies:a",
    "browser.set_cookie": "set:s",
    "browser.upload": "selector:s file:s",
    "browser.download": "path:s bytes:i sha256:s",
}
ARRAY_RESULTS = {"lsp.definition", "lsp.references", "lsp.symbols", "lsp.diagnostics"}
TYPES = {"s": "string", "i": "integer", "n": "number", "b": "boolean", "a": "array", "o": "object"}


def output_schema(name: str) -> dict | None:
    if name == "lsp.diagnostics":
        return {"type": ["array", "object"]}
    if name in ARRAY_RESULTS:
        return {"type": "array", "items": {"type": "object"}}
    if name == "agent.status":
        return {"type": ["object", "array"]}
    if name not in FIELDS:
        return None
    props = {}
    for field in FIELDS[name].split():
        key, kind = field.split(":")
        typ = TYPES[kind.rstrip("?")]
        props[key] = {"type": [typ, "null"] if kind.endswith("?") else typ}
    return {"type": "object", "properties": props, "additionalProperties": True}
