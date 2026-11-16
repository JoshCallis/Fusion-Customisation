"""The macOS appearance, read in-process — for the Fusion add-ins.

`interface_style()` is what `defaults read -g AppleInterfaceStyle` prints ('Dark', or
'' in Light), and as fresh: CFPreferencesAppSynchronize goes back to cfprefsd, so a
change from System Settings, Auto or toggle_appearance.sh shows on the next read
(verified 2026-10-06 against `defaults write -g` from outside Fusion). It is not
NSAppearance, which stayed stale for 26 minutes (remap/RemapApp/main.swift).

Why not run `defaults`: forking Fusion costs ~50 ms with the GIL held, and Dim
Outside Active's tone poller did it every 0.5 s — two hitches a second in every orbit
(measured 2026-10-06). This read is ~0.02 ms.
"""
import ctypes

_UTF8 = 0x08000100
_cf = None


def _setup():
    global _cf, _any, _key
    cf = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    cf.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFPreferencesAppSynchronize.argtypes = [ctypes.c_void_p]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    _any = ctypes.c_void_p.in_dll(cf, 'kCFPreferencesAnyApplication')
    _key = cf.CFStringCreateWithCString(None, b'AppleInterfaceStyle', _UTF8)
    _cf = cf


def interface_style():
    """'Dark' or '' (Light). Raises only if CoreFoundation can't be reached."""
    if _cf is None:
        _setup()
    _cf.CFPreferencesAppSynchronize(_any)
    v = _cf.CFPreferencesCopyAppValue(_key, _any)
    if not v:
        return ''
    try:
        if _cf.CFGetTypeID(v) != _cf.CFStringGetTypeID():
            return ''
        buf = ctypes.create_string_buffer(64)
        return buf.value.decode() if _cf.CFStringGetCString(v, buf, 64, _UTF8) else ''
    finally:
        _cf.CFRelease(v)
