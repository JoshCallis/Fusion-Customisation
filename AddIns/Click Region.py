"""
Click Region
────────────
WHERE the user's last physical click landed — the piece of context a selection
event doesn't carry.

Clicking a feature in the TIMELINE selects its geometry and fires exactly the same
activeSelectionChanged as a viewport pick, so the "a click acts" add-ins treated it
as one: Click To Descend descended into the owning component, and the Browser's
poll jumped down / opened the sketch.  Scrubbing the timeline therefore dragged the
active component around behind you.

Qt knows precisely what is under the cursor, so ask it — QApplication::widgetAt(),
the same binding the Cursor Probe add-in uses for its orbit-centre gate.  The 3D
viewport is a QTCanvas; the timeline (and every other Fusion panel) is not.

The answer is memoised in /tmp against the event tap's click stamp
(/tmp/fusion_click_cmd), so every add-in asking about the SAME physical click gets
the SAME answer: Click Actions asks ~90 ms after the click (cursor almost always
still on the spot that was clicked), the Browser asks from its ~350 ms poll, by
which time the cursor may have moved on.

FAIL OPEN — anything we can't determine (Qt symbols moved by a Fusion update, no
widget under the cursor, no click stamp) reads as "canvas", i.e. exactly the
behaviour these add-ins had before this gate existed.

MAIN THREAD ONLY: reading the Qt widget tree from a background thread races widget
deletion and segfaults Fusion.  Call this from event handlers, never from a timer
thread.
"""

import ctypes, os

CANVAS_CLASS = 'QTCanvas'          # Qt class of Fusion's 3D viewport widget

_CLICK_FILE = '/tmp/fusion_click_cmd'     # in:  "<epoch_ms> <cmd 0|1>" (FusionScrollFilter)
_MEMO_FILE  = '/tmp/fusion_click_widget'  # out: "<epoch_ms> <qt class or -|?>"
_DRAG_FILE  = '/tmp/fusion_click_drag'    # in:  "<epoch_ms>" of a DRAGGED click (FusionScrollFilter)


# ── Qt / CoreGraphics binding ────────────────────────────────────────────────
class _QPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32)]


class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


def _dyld_image(substr):
    """Path of the loaded Mach-O image whose name contains `substr`, or None."""
    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype  = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    needle = substr.encode()
    for i in range(libc._dyld_image_count()):
        name = libc._dyld_get_image_name(i)
        if name and needle in name:
            return name.decode()
    return None


class _Bind:
    """QApplication::widgetAt + the cursor position, bound by mangled symbol."""

    def __init__(self):
        qtw = ctypes.CDLL(_dyld_image('QtWidgets.framework/QtWidgets'))
        qtc = ctypes.CDLL(_dyld_image('QtCore.framework/QtCore'))
        self._widget_at = qtw['_ZN12QApplication8widgetAtERK6QPoint']
        self._widget_at.restype  = ctypes.c_void_p
        self._widget_at.argtypes = [ctypes.POINTER(_QPoint)]
        self._meta_class_name = qtc['_ZNK11QMetaObject9classNameEv']
        self._meta_class_name.restype  = ctypes.c_char_p
        self._meta_class_name.argtypes = [ctypes.c_void_p]

        self._cg = ctypes.CDLL('/System/Library/Frameworks/'
                               'CoreGraphics.framework/CoreGraphics')
        self._cg.CGEventCreate.restype  = ctypes.c_void_p
        self._cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        self._cg.CGEventGetLocation.restype  = _CGPoint
        self._cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        self._cg.CFRelease.argtypes = [ctypes.c_void_p]

    def cursor_pos(self):
        """Cursor position in global Quartz coords — the same top-left origin Qt's
        global widget coordinates use, so the two need no conversion."""
        ev = self._cg.CGEventCreate(None)
        try:
            loc = self._cg.CGEventGetLocation(ev)
        finally:
            self._cg.CFRelease(ev)
        return loc.x, loc.y

    def cls(self, w):
        """Qt class name of a QObject (metaObject() is the first virtual slot —
        the same read Cursor Probe and Maximize Viewport rely on)."""
        vt = ctypes.c_void_p.from_address(w).value
        fn = ctypes.c_void_p.from_address(vt).value
        mo = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(fn)(w)
        return (self._meta_class_name(mo) or b'').decode('utf-8', 'replace')

    def class_at_cursor(self):
        x, y = self.cursor_pos()
        p = _QPoint(int(x), int(y))
        w = self._widget_at(ctypes.byref(p))
        return self.cls(w) if w else ''


_bind = None      # None = not tried yet, False = binding failed


def _qt():
    global _bind
    if _bind is None:
        try:
            _bind = _Bind()
        except Exception:
            _bind = False
    return _bind or None


# ── Per-click memo ───────────────────────────────────────────────────────────
def _click_ms():
    """Epoch ms of the last physical left-down over Fusion; 0 when unknown."""
    try:
        with open(_CLICK_FILE) as f:
            return int(f.read().split()[0])
    except Exception:
        return 0


def _read_memo(click_ms):
    """The class already recorded for THIS click ('' = none found), or None when
    the memo is for a different click."""
    try:
        with open(_MEMO_FILE) as f:
            ms_str, cls = f.read().split(None, 1)
        if int(ms_str) == click_ms:
            cls = cls.strip()
            return '' if cls in ('-', '?') else cls
    except Exception:
        pass
    return None


def click_widget_class():
    """Qt class of the widget the last physical click landed on; '' when unknown.

    Evaluated once per click and memoised, so callers that ask late agree with
    callers that asked in the selection event itself."""
    ms = _click_ms()
    if not ms:
        return ''
    cached = _read_memo(ms)
    if cached is not None:
        return cached
    cls = ''
    qt = _qt()
    if qt is not None:
        try:
            cls = qt.class_at_cursor()
        except Exception:
            cls = ''
    try:
        with open(_MEMO_FILE, 'w') as f:
            f.write('{} {}'.format(ms, cls or '-'))
    except Exception:
        pass
    return cls


def click_was_drag():
    """True when the last physical left-click was a DRAG — the cursor moved between
    down and up: a window/box select, or dragging geometry around.

    A rubber-band select fires exactly the same activeSelectionChanged as a pick, so
    the "a click acts" add-ins descended into / jumped down to whatever the band
    caught — dragging a selection window over an assembly yanked you into a
    component.  A drag is a selection gesture, never a navigation one.

    Only the event tap can tell the two apart (it sees both ends of the gesture), so
    it stamps the DOWN timestamp of a moved click into /tmp/fusion_click_drag; a
    click matches its own stamp, a plain click leaves an older one behind.

    FAIL OPEN — no stamp, no click, unreadable file → False ("not a drag"), i.e.
    exactly the behaviour these add-ins had before this gate existed."""
    ms = _click_ms()
    if not ms:
        return False
    try:
        with open(_DRAG_FILE) as f:
            return int(f.read().split()[0]) == ms
    except Exception:
        return False


def click_over_canvas(cls=None):
    """True unless the last physical click positively landed on a Fusion widget that
    ISN'T the 3D canvas (the timeline, a panel, a toolbar).  Unknown → True, so the
    click-acts add-ins behave exactly as they did before whenever the Qt read fails.

    Pass `cls` when you already called click_widget_class() (e.g. to log it)."""
    if cls is None:
        cls = click_widget_class()
    return (not cls) or cls == CANVAS_CLASS
