"""Every outgoing HTTP client of the Engine, made with the owner's network settings.

`~/.rinari/network.json` (plan 0.4.0 #96, card «Red») chooses two things:

- **proxy**: ``system`` (default; environment variables and, on Windows, the
  system proxy, exactly what httpx reads with ``trust_env``), ``manual`` (one
  URL; local addresses and the owner's bypass list go direct) or ``none``;
- **certificates**: ``system`` (default; the OS store through ``truststore``,
  so a company network with TLS inspection works like the browser does),
  ``certifi`` (the bundle httpx ships, the behaviour before 0.4.0) or ``file``
  (only the PEM bundle the owner names).

`client()` / `async_client()` build a fresh client; `shared_client()` builds
the long-lived client the services share, which follows the file when it
changes. Every other keyword goes to httpx untouched. A caller that brings its
own ``transport``/``mounts`` (tests use ``httpx.MockTransport``) keeps it: the
proxy is then theirs, only ``verify`` is filled in. An explicit ``verify`` or
``proxy`` from the caller always wins.

A broken file never breaks the Engine: the factory falls back to the
defaults, logs it once, and `network.settings.get` reports the error.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import ssl
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from rinari.shared.paths import resolve_home

log = logging.getLogger(__name__)

SETTINGS_FILE = "network.json"
PROXY_MODES = ("system", "manual", "none")
CERT_MODES = ("system", "certifi", "file")
PROXY_SCHEMES = ("http", "https")
#: Always direct with a manual proxy: a local model server is never behind it.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

_lock = threading.Lock()
_settings_cache: dict[str, tuple[Any, NetworkSettings, str | None]] = {}
_ssl_cache: dict[tuple, ssl.SSLContext] = {}
_logged_errors: set[str] = set()


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    proxy: str = "system"
    proxy_url: str | None = None
    proxy_bypass: tuple[str, ...] = field(default_factory=tuple)
    certificates: str = "system"
    ca_file: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "proxy": {
                "mode": self.proxy,
                "url": self.proxy_url,
                "bypass": list(self.proxy_bypass),
            },
            "certificates": {"mode": self.certificates, "path": self.ca_file},
        }


# -- settings ----------------------------------------------------------------------


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Network setting '{name}' must be an object.")
    unknown = set(value) - keys
    if unknown:
        raise ValueError(f"Unknown network setting '{name}.{sorted(unknown)[0]}'.")
    return value


def _mode(value: Any, name: str, modes: tuple[str, ...]) -> str:
    mode = "system" if value is None else value
    if mode not in modes:
        raise ValueError(f"Unknown {name} mode {mode!r}; use one of: {', '.join(modes)}.")
    return mode


def _proxy_url(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError("The proxy URL must be a string like http://host:port.")
    text = value.strip()
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL as exc:
        raise ValueError(f"The proxy URL is not valid: {exc}.") from None
    if url.scheme not in PROXY_SCHEMES or not url.host:
        raise ValueError("The proxy URL must look like http://host:port (http or https).")
    return text


def _bypass(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("The proxy bypass list must be a list of host names.")
    hosts = []
    for item in value:
        host = item.strip()
        if not host:
            continue
        if "://" in host or "/" in host or any(ch.isspace() for ch in host):
            raise ValueError(
                f"Proxy bypass entry {item!r} must be a host (example.com, *.corp, 10.0.0.5)."
            )
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _ca_file(value: Any, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError("Certificates from a file need the path of a PEM file.")
        return None
    if not isinstance(value, str):
        raise ValueError("The certificate file path must be a string.")
    path = Path(value.strip()).expanduser()
    if not required:
        return str(path)
    if not path.is_file():
        raise ValueError(f"The certificate file does not exist: {path}")
    try:
        ssl.create_default_context(cafile=str(path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise ValueError(
            f"The certificate file is not a PEM bundle with at least one certificate: {path} "
            f"({exc})"
        ) from None
    return str(path.resolve())


def validate(value: Any) -> NetworkSettings:
    """Strict check of a network.json value; ValueError says what is wrong."""
    root = _object(value if value is not None else {}, "network", {"proxy", "certificates"})
    proxy = _object(root.get("proxy"), "proxy", {"mode", "url", "bypass"})
    certs = _object(root.get("certificates"), "certificates", {"mode", "path"})
    proxy_mode = _mode(proxy.get("mode"), "proxy", PROXY_MODES)
    proxy_url = _proxy_url(proxy.get("url"))
    if proxy_mode == "manual" and proxy_url is None:
        raise ValueError("A manual proxy needs its URL, like http://proxy.example.com:8080.")
    cert_mode = _mode(certs.get("mode"), "certificates", CERT_MODES)
    return NetworkSettings(
        proxy=proxy_mode,
        proxy_url=proxy_url,
        proxy_bypass=_bypass(proxy.get("bypass")),
        certificates=cert_mode,
        ca_file=_ca_file(certs.get("path"), required=cert_mode == "file"),
    )


def _home(home: str | os.PathLike[str] | None) -> Path:
    return Path(home) if home is not None else resolve_home(None)


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_settings(home: str | os.PathLike[str] | None) -> NetworkSettings:
    """network.json of `home`, validated; defaults when there is none."""
    path = _home(home) / SETTINGS_FILE
    if not path.exists():
        return NetworkSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"network.json cannot be read: {exc}") from None
    return validate(raw)


def settings_for(home: str | os.PathLike[str] | None) -> tuple[NetworkSettings, str | None]:
    """The settings in use and the error that made them the defaults, if any.

    Cached on the file's mtime and size: one `stat` per call.
    """
    path = _home(home) / SETTINGS_FILE
    key = _stat_key(path)
    cache_key = str(path)
    with _lock:
        cached = _settings_cache.get(cache_key)
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
    error = None
    try:
        value = load_settings(home) if key is not None else NetworkSettings()
    except ValueError as exc:
        value, error = NetworkSettings(), str(exc)
        if error not in _logged_errors:
            _logged_errors.add(error)
            log.warning("network.json ignored, using the defaults: %s", error)
    with _lock:
        _settings_cache[cache_key] = (key, value, error)
    return value, error


def save_settings(home: str | os.PathLike[str] | None, value: Any) -> NetworkSettings:
    """Validate and write network.json atomically; returns what was saved."""
    settings = validate(value)
    root = _home(home)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="network-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings.to_json(), stream, ensure_ascii=False, indent=2)
        os.replace(tmp, root / SETTINGS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    with _lock:
        _settings_cache.pop(str(root / SETTINGS_FILE), None)
    return settings


# -- TLS -------------------------------------------------------------------------------


def _env_ca() -> tuple[str | None, str | None]:
    return os.environ.get("SSL_CERT_FILE") or None, os.environ.get("SSL_CERT_DIR") or None


def _ssl_key(settings: NetworkSettings) -> tuple:
    if settings.certificates == "file":
        path = Path(settings.ca_file or "")
        return ("file", str(path), _stat_key(path))
    return (settings.certificates, *_env_ca())


def _system_context() -> ssl.SSLContext:
    import truststore

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Someone who already pointed SSL_CERT_FILE at the company's CA keeps it:
    # truststore tries these when the system store does not vouch for a chain.
    cafile, capath = _env_ca()
    with contextlib.suppress(OSError, ssl.SSLError, ValueError):
        if cafile and Path(cafile).is_file():
            context.load_verify_locations(cafile=cafile)
        if capath and Path(capath).is_dir():
            context.load_verify_locations(capath=capath)
    return context


def _build_context(settings: NetworkSettings) -> ssl.SSLContext:
    if settings.certificates == "certifi":
        # httpx's own default, SSL_CERT_FILE / SSL_CERT_DIR included.
        return httpx.create_ssl_context(verify=True, trust_env=True)
    if settings.certificates == "file" and settings.ca_file:
        return ssl.create_default_context(cafile=settings.ca_file)
    return _system_context()


def ssl_context(settings: NetworkSettings) -> ssl.SSLContext:
    """The verify context for `settings`, built once per value."""
    key = _ssl_key(settings)
    with _lock:
        context = _ssl_cache.get(key)
    if context is not None:
        return context
    try:
        context = _build_context(settings)
    except (OSError, ssl.SSLError, ValueError) as exc:
        # The file vanished or broke after it was saved: the system store.
        message = f"certificates ({settings.certificates}) unusable, using the system store: {exc}"
        if message not in _logged_errors:
            _logged_errors.add(message)
            log.warning(message)
        context = _system_context()
    with _lock:
        if len(_ssl_cache) > 16:
            _ssl_cache.clear()
        _ssl_cache.setdefault(key, context)
        return _ssl_cache[key]


# -- clients ---------------------------------------------------------------------------


def _kwargs(settings: NetworkSettings, kwargs: dict[str, Any]) -> dict[str, Any]:
    options = dict(kwargs)
    if "verify" not in options:
        options["verify"] = ssl_context(settings)
    own_routing = "transport" in options or "mounts" in options or "proxy" in options
    if settings.proxy == "none":
        options.setdefault("trust_env", False)
    elif settings.proxy == "manual" and settings.proxy_url and not own_routing:
        options["proxy"] = settings.proxy_url
        options["mounts"] = {
            f"all://{host}": None for host in (*LOCAL_HOSTS, *settings.proxy_bypass)
        }
    return options


def client(home: str | os.PathLike[str] | None = None, **kwargs: Any) -> httpx.Client:
    """A new `httpx.Client` with the network settings of `home` applied."""
    settings, _ = settings_for(home)
    return httpx.Client(**_kwargs(settings, kwargs))


def async_client(home: str | os.PathLike[str] | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """A new `httpx.AsyncClient` with the network settings of `home` applied."""
    settings, _ = settings_for(home)
    return httpx.AsyncClient(**_kwargs(settings, kwargs))


class SharedClient:
    """A long-lived client that follows network.json.

    Behaves as an `httpx.Client` (attributes are delegated). When the
    settings change, the next request gets a client built with the new ones;
    the old one keeps serving what it had in flight and closes with this one.
    """

    def __init__(self, home: str | os.PathLike[str] | None, kwargs: dict[str, Any]) -> None:
        self._home = home
        self._options = dict(kwargs)
        self._guard = threading.Lock()
        self._key: tuple | None = None
        self._client: httpx.Client | None = None
        self._retired: list[httpx.Client] = []
        self._closed = False

    def current(self) -> httpx.Client:
        settings, _ = settings_for(self._home)
        key = (settings, _ssl_key(settings))
        with self._guard:
            if self._client is None or key != self._key:
                if self._client is not None:
                    self._retired.append(self._client)
                self._client = httpx.Client(**_kwargs(settings, self._options))
                self._key = key
            return self._client

    def __getattr__(self, name: str) -> Any:
        # Own state is never delegated (half-built or being torn down).
        if name.startswith("__") or name in _OWN:
            raise AttributeError(name)
        return getattr(self.current(), name)

    def close(self) -> None:
        with self._guard:
            clients = [*self._retired, *([self._client] if self._client else [])]
            self._retired, self._client, self._closed = [], None, True
        for item in clients:
            with contextlib.suppress(Exception):
                item.close()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def __enter__(self) -> SharedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_OWN = frozenset({"_home", "_options", "_guard", "_key", "_client", "_retired", "_closed"})


def shared_client(home: str | os.PathLike[str] | None = None, **kwargs: Any) -> Any:
    """The services' long-lived client: an `httpx.Client` that follows network.json."""
    return SharedClient(home, kwargs)


# -- what is in use ----------------------------------------------------------------------


def _redact(url: str | None) -> str | None:
    """`url` without its user and password: a proxy URL may carry them."""
    if not url:
        return url
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rpartition("@")[2]))


def effective(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """What the clients use right now (for Settings → Avanzado → Red)."""
    settings, error = settings_for(home)
    if settings.proxy == "none":
        proxy: dict[str, Any] = {
            "mode": "none",
            "http": None,
            "https": None,
            "source": None,
            "bypass": [],
        }
    elif settings.proxy == "manual":
        url = _redact(settings.proxy_url)
        proxy = {
            "mode": "manual",
            "http": url,
            "https": url,
            "source": "settings",
            "bypass": [*LOCAL_HOSTS, *settings.proxy_bypass],
        }
    else:
        environment = urllib.request.getproxies_environment()
        found = urllib.request.getproxies()
        proxy = {
            "mode": "system",
            "http": _redact(found.get("http") or found.get("all")),
            "https": _redact(found.get("https") or found.get("all")),
            "source": "environment" if environment else ("os" if found else None),
            "bypass": [host.strip() for host in (found.get("no") or "").split(",") if host.strip()],
        }
    cafile, capath = _env_ca()
    if settings.certificates == "certifi":
        certificates = {"mode": "certifi", "source": cafile or capath or "certifi"}
    elif settings.certificates == "file":
        certificates = {"mode": "file", "source": settings.ca_file}
    else:
        certificates = {"mode": "system", "source": "os", "extra": cafile or capath}
    return {"proxy": proxy, "certificates": certificates, "error": error}
