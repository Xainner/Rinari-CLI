"""Web capability (phase 5): bounded HTTP + HTML extraction + keyless search."""

from rinari.web.client import (
    DEFAULT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    SEARCH_ENDPOINT,
    Fetched,
    WebRequestError,
    fetch,
    search,
)
from rinari.web.html import MAX_LINKS, Page, parse_page

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_LINKS",
    "MAX_RESPONSE_BYTES",
    "SEARCH_ENDPOINT",
    "Fetched",
    "Page",
    "WebRequestError",
    "fetch",
    "parse_page",
    "search",
]
