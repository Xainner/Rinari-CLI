"""Raw Win32 seams for the lab Windows backend (ctypes stdlib only).

User-mode calls exclusively: no elevation, no drivers, no hooks, no DLL
injection. Every function is synchronous and raises OSError-flavoured
ComputerError mapping at the backend layer, never raw ctypes errors.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - lab only
    raise ImportError("win32 seams require Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]  # noqa: RUF012


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


SendInput = user32.SendInput
SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SendInput.restype = wintypes.UINT

user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.FindWindowExW.argtypes = (
    wintypes.HWND,
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
)
user32.FindWindowExW.restype = wintypes.HWND
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
try:
    _get_dpi = user32.GetDpiForWindow
    _get_dpi.argtypes = (wintypes.HWND,)
    _get_dpi.restype = wintypes.UINT
except AttributeError:  # pre-1607 SDK headers
    _get_dpi = None


def _check(result, what: str) -> None:
    if not result:
        raise OSError(what + " failed: " + str(ctypes.get_last_error()))


def enum_windows_for_pid(pid: int) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    _check(user32.GetWindowRect(hwnd, ctypes.byref(rect)), "GetWindowRect")
    return (rect.left, rect.top, rect.right, rect.bottom)


def dpi_scale(hwnd: int) -> float:
    if _get_dpi is None:
        return 1.0
    return max(1.0, float(_get_dpi(hwnd)) / 96.0)


def virtual_screen() -> tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def to_absolute(x: float, y: float) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    return (
        int((x - vx) * 65535 / max(1, vw)),
        int((y - vy) * 65535 / max(1, vh)),
    )


def get_foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def focus_window(hwnd: int, timeout_s: float = 2.0) -> bool:
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    end = time.monotonic() + timeout_s
    ours = kernel32.GetCurrentThreadId()
    while time.monotonic() < end:
        if get_foreground() == hwnd:
            return True
        foreground = get_foreground()
        if foreground and foreground != hwnd:
            owner = wintypes.DWORD()
            theirs = user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
            attached = bool(theirs) and bool(user32.AttachThreadInput(ours, theirs, True))
        else:
            theirs, attached = 0, False
        try:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
        finally:
            if attached:
                user32.AttachThreadInput(ours, theirs, False)
    return get_foreground() == hwnd


def _send(*events: INPUT) -> None:
    sent = SendInput(len(events), (INPUT * len(events))(*events), ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError("SendInput delivered " + str(sent) + " of " + str(len(events)))


def _mouse(flags: int, x: int = 0, y: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, u=_INPUT_UNION(mi=MOUSEINPUT(dx=x, dy=y, flags=flags)))


def mouse_click_screen(sx: float, sy: float) -> None:
    ax, ay = to_absolute(sx, sy)
    move = _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay)
    down = _mouse(MOUSEEVENTF_LEFTDOWN)
    up = _mouse(MOUSEEVENTF_LEFTUP)
    _send(move)
    _send(down)
    try:
        _send(up)
    except OSError:
        try:
            _send(up)
        finally:
            raise


def mouse_release_all() -> None:
    _send(
        _mouse(MOUSEEVENTF_LEFTUP),
        _mouse(MOUSEEVENTF_MIDDLEUP),
        _mouse(MOUSEEVENTF_RIGHTUP),
    )


def _key(scan: int, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(ki=KEYBDINPUT(wScan=scan, dwFlags=flags)))


def _vkey(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    return INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))


def type_unicode(text: str, cancelled=None, pacing_s: float = 0.0) -> None:
    for char in text:
        point = ord(char)
        if point > 0xFFFF:
            raise OSError("non-BMP character refused: U+" + format(point, "X"))
        if cancelled is not None and cancelled():
            raise TimeoutError("cancelled")
        _send(_key(point, False))
        try:
            if cancelled is not None and cancelled():
                raise TimeoutError("cancelled")
            _send(_key(point, True))
        except BaseException:
            try:
                _send(_key(point, True))
            finally:
                raise
        if pacing_s > 0:
            time.sleep(pacing_s)


def press_enter() -> None:
    _send(_vkey(VK_RETURN, False))
    _send(_vkey(VK_RETURN, True))


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def read_text(hwnd: int) -> str:
    """Best-effort readable text: window title, else first edit-like child."""
    direct = _window_text(hwnd)
    if direct:
        return direct
    for cls in ("Edit", "RichEditD2DPT", "RICHEDIT50W", "RichEdit20W"):
        child = user32.FindWindowExW(hwnd, None, cls, None)
        if child:
            text = _window_text(int(child))
            if text:
                return text
    return ""


def visible_windows() -> list[tuple[int, str, int]]:
    """(hwnd, title, owner pid) for visible top-level windows. Lab-only helper."""
    found: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _param):
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            found.append((int(hwnd), _window_text(hwnd), owner.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_exe(pid: int) -> str:
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if int(snap) == -1:
        return ""
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return str(entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return ""
    finally:
        kernel32.CloseHandle(snap)


def terminate_pid(pid: int, timeout_s: float = 10.0) -> bool:
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        return kernel32.WaitForSingleObject(handle, int(timeout_s * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
FLASHW_ALL = 0x0003
FLASHW_TIMERNOFG = 0x000C


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def set_topmost(hwnd: int, top: bool) -> None:
    user32.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.SetWindowPos.restype = wintypes.BOOL
    anchor = HWND_TOPMOST if top else HWND_NOTOPMOST
    _check(
        user32.SetWindowPos(hwnd, anchor, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW),
        "SetWindowPos",
    )


def flash(hwnd: int) -> None:
    user32.FlashWindowEx.argtypes = (ctypes.POINTER(FLASHWINFO),)
    user32.FlashWindowEx.restype = wintypes.BOOL
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
    user32.FlashWindowEx(ctypes.byref(info))


CF_UNICODETEXT = 13


def get_clipboard_text() -> str:
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return str(ctypes.wstring_at(locked))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    GMEM_MOVEABLE = 0x0002
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc failed")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()
