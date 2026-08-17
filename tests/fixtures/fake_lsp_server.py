"""Deterministic fake LSP server over stdio (test fixture, not a tool).

Runs modes selected by argv:
  (default)      full capabilities: definition/references/symbols/hover/
                 signature/rename; publishes a diagnostic when the opened
                 text contains "boom"; "textDocument/hang" sleeps (timeout
                 tests).
  --minimal      advertises no providers (capability-gating tests).
  --crash        exits as soon as the first document is opened (crash tests).

Protocol: standard LSP framing (Content-Length + JSON-RPC).
"""

import json
import sys
import time


def read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if headers:
                break
            continue
        if b":" not in line:
            return None
        key, _, value = line.decode("ascii").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = headers.get("content-length")
    if not length or not length.isdigit():
        return None
    body = stream.read(int(length))
    if len(body) < int(length):
        return None
    return json.loads(body.decode("utf-8"))


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def respond(rid, result):
    write_message({"jsonrpc": "2.0", "id": rid, "result": result})


def err(rid, code, message):
    write_message({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _uri(params):
    return params["textDocument"]["uri"]


FULL_CAPS = {
    "textDocument": {
        "definitionProvider": True,
        "referencesProvider": True,
        "documentSymbolProvider": True,
        "hoverProvider": True,
        "signatureHelpProvider": True,
        "renameProvider": True,
    },
    "documentSymbolProvider": True,
}


def main():
    mode = sys.argv[1].lstrip("-") if len(sys.argv) > 1 else "full"
    stream = sys.stdin.buffer
    while True:
        msg = read_message(stream)
        if msg is None:
            return
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            respond(
                rid,
                {
                    "capabilities": FULL_CAPS if mode == "full" else {},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            )
            continue
        if method == "initialized":
            continue
        if method == "textDocument/didOpen":
            if mode == "crash":
                sys.stdout.flush()
                return
            if "boom" in params["textDocument"]["text"]:
                write_message(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": _uri(params),
                            "diagnostics": [
                                {
                                    "range": {
                                        "start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 4},
                                    },
                                    "severity": 1,
                                    "code": "E001",
                                    "message": "boom detected",
                                }
                            ],
                        },
                    }
                )
            continue
        if method in ("textDocument/didChange", "textDocument/didClose"):
            continue
        if method == "exit":
            return
        if method == "shutdown":
            respond(rid, None)
            continue

        if rid is None:
            continue

        if method == "textDocument/definition":
            respond(
                rid,
                {
                    "uri": _uri(params),
                    "range": {
                        "start": {"line": 9, "character": 4},
                        "end": {"line": 9, "character": 9},
                    },
                },
            )
        elif method == "textDocument/references":
            respond(
                rid,
                [
                    {
                        "uri": _uri(params),
                        "range": {
                            "start": {"line": 1, "character": 2},
                            "end": {"line": 1, "character": 7},
                        },
                    },
                    {
                        "uri": _uri(params) + ".more",
                        "range": {
                            "start": {"line": 2, "character": 1},
                            "end": {"line": 2, "character": 6},
                        },
                    },
                ],
            )
        elif method == "textDocument/documentSymbol":
            respond(
                rid,
                [
                    {
                        "name": "alpha",
                        "symbolKind": 12,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 9, "character": 0},
                        },
                        "selectionRange": {
                            "start": {"line": 0, "character": 6},
                            "end": {"line": 0, "character": 11},
                        },
                        "children": [
                            {
                                "name": "beta",
                                "symbolKind": 13,
                                "range": {
                                    "start": {"line": 1, "character": 4},
                                    "end": {"line": 8, "character": 4},
                                },
                                "selectionRange": {
                                    "start": {"line": 1, "character": 8},
                                    "end": {"line": 1, "character": 12},
                                },
                            }
                        ],
                    }
                ],
            )
        elif method == "textDocument/hover":
            respond(rid, {"contents": {"kind": "plaintext", "value": "alpha: int"}})
        elif method == "textDocument/signatureHelp":
            respond(
                rid,
                {
                    "signatures": [
                        {
                            "label": "alpha(x: int, y: str = 'a')",
                            "parameters": [
                                {"label": "x: int"},
                                {"label": "y: str = 'a'"},
                            ],
                        }
                    ],
                    "activeSignature": 0,
                },
            )
        elif method == "textDocument/rename":
            new_name = params.get("newName")
            if new_name == "bad":
                err(rid, -32602, "invalid name")
            else:
                respond(
                    rid,
                    {
                        "changes": {
                            _uri(params): [
                                {
                                    "range": {
                                        "start": {"line": 0, "character": 6},
                                        "end": {"line": 0, "character": 11},
                                    },
                                    "newText": new_name,
                                }
                            ]
                        }
                    },
                )
        elif method == "textDocument/hang":
            time.sleep(3)
            respond(rid, None)
        else:
            err(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
