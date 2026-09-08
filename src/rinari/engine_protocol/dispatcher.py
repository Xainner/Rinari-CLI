"""Request dispatcher: envelopes, duplicate IDs, unknown methods, error mapping."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import failure, success
from rinari.shared.errors import RinariError

Handler = Callable[[dict[str, Any]], dict[str, Any]]

_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')


def _extract_id(line: str) -> Any:
    match = _ID_RE.search(line)
    return match.group(1) if match else None


class EngineDispatcher:
    """Routes one NDJSON line to a registered handler.

    Duplicate request IDs are rejected without executing the handler.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._seen_ids: set[Any] = set()

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def dispatch(self, line: str) -> dict[str, Any] | None:
        if line.strip() == "":
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return failure(_extract_id(line), errors.BROKEN_FRAME, "Request is not valid JSON.")
        if not isinstance(obj, dict):
            return failure(None, errors.MALFORMED_REQUEST, "Request must be a JSON object.")
        request_id = obj.get("id")
        if not isinstance(request_id, (str, int)) or request_id == "":
            return failure(
                None, errors.MALFORMED_REQUEST, "Request 'id' must be a non-empty string."
            )
        method = obj.get("method")
        if not isinstance(method, str) or not method:
            return failure(
                request_id, errors.MALFORMED_REQUEST, "Request 'method' must be a string."
            )
        params = obj.get("params", {})
        if not isinstance(params, dict):
            return failure(request_id, errors.INVALID_PARAMS, "Request 'params' must be an object.")
        if request_id in self._seen_ids:
            return failure(
                request_id,
                errors.DUPLICATE_REQUEST_ID,
                f"Duplicate request id: {request_id!r}.",
            )
        handler = self._handlers.get(method)
        if handler is None:
            return failure(
                request_id,
                errors.UNKNOWN_METHOD,
                f"Unknown method: {method}.",
                details={"method": method},
            )
        self._seen_ids.add(request_id)
        try:
            return success(request_id, handler(params))
        except EngineProtocolError as err:
            return failure(request_id, err.code, err.message, err.retryable, err.details)
        except RinariError as err:
            mapped = errors.from_rinari_error(err)
            return failure(
                request_id, mapped.code, mapped.message, mapped.retryable, mapped.details
            )
        except Exception as err:  # defensive: never break the stdio frame stream
            return failure(request_id, errors.ENGINE_ERROR, f"{type(err).__name__}: {err}")
