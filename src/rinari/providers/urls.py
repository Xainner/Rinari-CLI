"""Compose API paths while retaining gateway prefixes."""

from urllib.parse import urlsplit, urlunsplit


def api_url(base: str, resource: str, *, version: str | None = None) -> str:
    parts = urlsplit(base)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Provider endpoint must be an HTTP(S) base URL without credentials, query or fragment"
        )
    path = parts.path.rstrip("/")
    if version and not path.endswith("/" + version):
        path += "/" + version
    return urlunsplit((parts.scheme, parts.netloc, path + "/" + resource.lstrip("/"), "", ""))
