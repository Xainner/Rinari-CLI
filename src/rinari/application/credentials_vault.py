"""Inventario nativo del Administrador de credenciales de Windows.

Motivo (informe CredWrite error 8): el store del harness acumulaba entradas
huérfanas hasta llenar el vault, y `keyring` no expone enumeración. Aquí se
usa `advapi32` vía ctypes, sin dependencias nuevas.

Solo metadatos: el blob (el secreto) nunca se lee ni se devuelve.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from rinari.shared.errors import CredentialStoreUnavailableError

CRED_TYPE_GENERIC = 1
ERROR_NOT_FOUND = 1168

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class VaultCredential:
    """Metadatos de una credencial del vault (nunca el secreto)."""

    target: str
    username: str
    comment: str
    last_written: str
    persist: int
    cred_type: int


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_byte)),
    ]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32() -> ctypes.WinDLL:
    if sys.platform != "win32":
        raise CredentialStoreUnavailableError(
            "The native credential vault is only available on Windows",
            hint="On macOS and Linux the OS keychain needs no cleanup pass.",
        )
    library = ctypes.WinDLL("advapi32", use_last_error=True)
    library.CredEnumerateW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))),
    ]
    library.CredEnumerateW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def _filetime_to_iso(value: _FILETIME) -> str:
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    if not ticks:
        return ""
    moment = _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def enumerate_credentials() -> list[VaultCredential]:
    """Metadata for every generic credential of the current user."""
    library = _advapi32()
    count = wintypes.DWORD(0)
    pointer = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
    if not library.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return []
        raise CredentialStoreUnavailableError(
            f"Could not read the Windows credential vault (winerror {error})",
            hint="Retry from an interactive desktop session of the same user.",
        )
    try:
        entries: list[VaultCredential] = []
        for index in range(count.value):
            item = pointer[index].contents
            entries.append(
                VaultCredential(
                    target=item.TargetName or "",
                    username=item.UserName or "",
                    comment=item.Comment or "",
                    last_written=_filetime_to_iso(item.LastWritten),
                    persist=int(item.Persist),
                    cred_type=int(item.Type),
                )
            )
        return entries
    finally:
        library.CredFree(pointer)


def delete_credential(target: str, cred_type: int = CRED_TYPE_GENERIC) -> None:
    """Delete one credential by TargetName; failures are raised, not hidden."""
    library = _advapi32()
    if not library.CredDeleteW(target, cred_type, 0):
        error = ctypes.get_last_error()
        raise OSError(f"CredDelete failed for {target!r} (winerror {error})")
