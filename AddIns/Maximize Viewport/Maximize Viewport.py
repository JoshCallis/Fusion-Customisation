"""
+ Maximize Viewport
──────────────────
Pushes the Fusion canvas edge-to-edge by hiding chrome the public API cannot
touch, driving the live Qt widget tree and the AppKit NSWindow via ctypes:

  • the macOS title bar  → made BORDERLESS (Titled style bit dropped) for a
    true edge-to-edge window, traffic lights hidden.  This flips the window's
    AX subrole from AXStandardWindow to AXDialog, so every canvas-detector
    must accept AXDialog and disambiguate the (large) canvas from a (small)
    real dialog by SIZE: the slots/swipe scripts (slotlib.sh cad_aux_geoms),
    reanchor, FusionScrollFilter, and Cursor Probe's _cursor_is_over_fusion.
    (Do NOT fake the subrole by swizzling accessibilitySubrole — a Python IMP
    there deadlocks Fusion's main thread; see set_titlebar.)
  • the open-document tabs strip + QAT + notification  → Qt
  • the ribbon (Command Toolbar)                        → Fusion command
  • the parametric Design timeline (buttons + bar)      → Qt
  • the canvas is stretched to fill the space the timeline left.

Three checkbox toggles (ADD-INS panel, UTILITIES tab) — assign shortcuts so
they work even when their buttons are hidden:

  • Maximize Viewport     — master on/off (title bar + the loop)
  • Hide Ribbon + Tabs    — ribbon + document tabs + QAT
  • Hide Timeline         — the Design timeline (+ canvas fill)

Fusion re-creates / re-shows these widgets on resize, workspace switch and
document activate, so a ~0.5 s timer re-applies the current state (fired as a
custom event so the work runs on the GUI thread).  Framework paths are resolved
from the loaded dyld images, so it survives a Fusion auto-update.
"""

import adsk.core
import builtins
import ctypes
import ctypes.util
import json
import os
import platform
import threading
import time
import traceback

# Selects the Objective-C struct-return calling convention — see Qt._send_struct.
_ARM64 = (platform.machine() == 'arm64')

_app = None
_ui = None
_handlers = []

EVENT_ID = 'MaximizeViewportReapply'
CMD_MASTER   = 'MaximizeViewportTgl'
CMD_TOP      = 'MaximizeViewportTopTgl'
CMD_TIMELINE = 'MaximizeViewportTimelineTgl'

CMDS = [
    ('master',   CMD_MASTER,   'Maximize Viewport',   'Hide the title bar and run the edge-to-edge loop'),
    ('top',      CMD_TOP,      'Hide Ribbon + Tabs',  'Hide the ribbon, document tabs and quick-access toolbar'),
    ('timeline', CMD_TIMELINE, 'Hide Timeline',       'Hide the Design timeline and stretch the canvas down'),
]

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Maximize Viewport State.json')

# feature state (all on by default)
_enabled = True
_hide_top = True
_hide_timeline = True

_running = False
_event = None
_qt = None
_ribbon_dirty = True     # ribbon command needs (re)applying
_nudge_pending = False   # window needs a relayout nudge
_burst_until = 0.0       # poll fast until this monotonic time (kills palette flash)
_reveal_timeline_once = False  # one-shot: re-show the timeline on the next _apply (set by toggle)
_reveal_top_once = False  # one-shot: re-dock/show the top bar on the next _apply (set by toggle).
                          # The top bar, like the timeline, must only be TOUCHED on the reveal:
                          # calling restoreDockWidget every idle tick eventually flings the dock
                          # off-screen (to negative coords), which looked like the ribbon
                          # "re-hiding itself" a second after it was shown. Once shown, Fusion
                          # keeps it shown on its own, so a single reveal is enough.
# cached widget pointers (refreshed by the full _apply) so the burst can re-hide
# cheaply — O(1) instead of rescanning every widget on each fast tick.
_top_dock_ptr = None
_mains_ptrs = ()
_timeline_ptrs = ()
_canvas_ptr = None
_full_rect = None
_canvas_pin = None       # (canvas ptr, min height) currently pinned — see _pin_canvas


# Another add-in may ask for the timeline hidden on top of our own setting: the Hide
# Native Timeline add-in (its own hotkey), via builtins._fusion_hide_native_timeline —
# on builtins so every add-in reads one copy. It fires EVENT_ID when it flips.
_forced_last = False


def _hide_forced():
    return bool(getattr(builtins, '_fusion_hide_native_timeline', False))


def _tl_hidden():
    """Hide the Design timeline this tick? Our setting OR Hide Native Timeline's."""
    return _hide_timeline or _hide_forced()

TOP_NAMES = {
    "TopNavBar_2",
    "QTAMNotificationStandalone",
    "QTView:(102)ActiveDocumentsToolbar",
    "QAT:QTView:(103)QAT",
    "QAT:QTView:(103)QATRight",
}


def _is_topbar(nm):
    """The dock + every toolbar/tab/QAT/ribbon widget inside it — the things
    that make up the top bar Fusion flashes back on command activation. Matched
    by prefix/substring so it survives Fusion's per-session instance numbers
    (the -0 / (103) / (104) suffixes). NOTE: 'CMD:QTView' is the command ribbon;
    'CmdDg:QTView' is the command DIALOG the user needs — startswith keeps them
    apart so we never clamp the dialog."""
    return (nm.startswith("TopNavBar_2")
            or nm.startswith("QTApplicationBar")
            or nm.startswith("QTABTabBar")
            or nm.startswith("QAT")            # QATToolBar-0, QATRightToolBar-0, QAT:QTView:(103)*
            or nm.startswith("CMD:QTView")
            or "ActiveDocumentsToolbar" in nm)


# ── state persistence ─────────────────────────────────────────────────────────

def _load_state():
    global _enabled, _hide_top, _hide_timeline
    try:
        with open(_STATE_FILE) as f:
            d = json.load(f)
        _enabled = bool(d.get('enabled', True))
        _hide_top = bool(d.get('hide_top', True))
        _hide_timeline = bool(d.get('hide_timeline', True))
    except Exception:
        pass


def _save_state():
    try:
        with open(_STATE_FILE, 'w') as f:
            json.dump({'enabled': _enabled, 'hide_top': _hide_top, 'hide_timeline': _hide_timeline}, f, indent=2)
    except Exception:
        pass


# ── ctypes / Qt + AppKit bindings ─────────────────────────────────────────────

def _find_image(substr):
    libc = ctypes.CDLL(None)
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    b = substr.encode()
    for i in range(libc._dyld_image_count()):
        n = libc._dyld_get_image_name(i)
        if n and b in n:
            return n.decode()
    return None


# NSPoint / NSSize / NSRect, defined ONCE. fill_top_gap and nudge used to declare them
# inside the method, so every call (every _apply tick) made three new ctypes classes and
# a new objc_msgSend prototype for them — none ever freed: 27,000 calls had left ~110,000
# type objects on the heap, and a full garbage collection of Fusion's Python took 4 s,
# paid out as random 100+ ms hitches in whichever add-in tripped it (2026-10-08).
class _NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _NSSize(ctypes.Structure):
    _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]


class _NSRect(ctypes.Structure):
    _fields_ = [("o", _NSPoint), ("s", _NSSize)]


class _Bind:
    def __init__(self):
        qtw = ctypes.CDLL(_find_image("QtWidgets.framework/QtWidgets"))
        qtc = ctypes.CDLL(_find_image("QtCore.framework/QtCore"))

        class QWidgetList(ctypes.Structure):
            _fields_ = [("d", ctypes.c_void_p), ("ptr", ctypes.c_void_p), ("size", ctypes.c_longlong)]
        class QString(ctypes.Structure):
            _fields_ = [("d", ctypes.c_void_p), ("ptr", ctypes.c_void_p), ("size", ctypes.c_longlong)]
        class QRect(ctypes.Structure):
            _fields_ = [("x1", ctypes.c_int32), ("y1", ctypes.c_int32), ("x2", ctypes.c_int32), ("y2", ctypes.c_int32)]
        self.QRect = QRect

        self._allWidgets = qtw["_ZN12QApplication10allWidgetsEv"]; self._allWidgets.restype = QWidgetList; self._allWidgets.argtypes = []
        self._objectName = qtc["_ZNK7QObject10objectNameEv"]; self._objectName.restype = QString; self._objectName.argtypes = [ctypes.c_void_p]
        self._metaClassName = qtc["_ZNK11QMetaObject9classNameEv"]; self._metaClassName.restype = ctypes.c_char_p; self._metaClassName.argtypes = [ctypes.c_void_p]
        self._setVisible = qtw["_ZN7QWidget10setVisibleEb"]; self._setVisible.restype = None; self._setVisible.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        self._setGeometry = qtw["_ZN7QWidget11setGeometryERK5QRect"]; self._setGeometry.restype = None; self._setGeometry.argtypes = [ctypes.c_void_p, ctypes.POINTER(QRect)]
        # QMainWindow dock-layout membership — the robust lever for the top bar:
        # removing it from the dock layout means Fusion's relayout has nothing to
        # re-show, so it can't flash back. Loaded defensively: a missing symbol
        # leaves the feature off rather than bricking the whole bind.
        self._removeDock = self._restoreDock = None
        try:
            self._removeDock = qtw["_ZN11QMainWindow16removeDockWidgetEP11QDockWidget"]; self._removeDock.restype = None; self._removeDock.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self._restoreDock = qtw["_ZN11QMainWindow17restoreDockWidgetEP11QDockWidget"]; self._restoreDock.restype = None; self._restoreDock.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        except Exception:
            self._removeDock = self._restoreDock = None

        # Size clamp — the lever that actually kills the command-activation flash.
        # removeDockWidget keeps the top bar out at idle, but Fusion re-shows the
        # dock for ~1 frame when a command/environment activates, and that re-show
        # races (and beats) any "re-hide it" watcher by a frame. setFixedSize(0,0)
        # is a STICKY constraint that also forces an immediate resize, so the bar
        # is 0×0 even when Fusion re-shows it → nothing to paint, no race. Plain
        # setMaximumSize does NOT work here (Fusion's custom toolbar/dock layout
        # ignores max-only hints); setFixed* works because it also resizes now.
        self._setFixedW = self._setFixedH = None
        self._setMinW = self._setMinH = self._setMaxW = self._setMaxH = None
        try:
            self._setFixedW = qtw["_ZN7QWidget13setFixedWidthEi"]; self._setFixedW.restype = None; self._setFixedW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._setFixedH = qtw["_ZN7QWidget14setFixedHeightEi"]; self._setFixedH.restype = None; self._setFixedH.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._setMinW = qtw["_ZN7QWidget15setMinimumWidthEi"]; self._setMinW.restype = None; self._setMinW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._setMinH = qtw["_ZN7QWidget16setMinimumHeightEi"]; self._setMinH.restype = None; self._setMinH.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._setMaxW = qtw["_ZN7QWidget15setMaximumWidthEi"]; self._setMaxW.restype = None; self._setMaxW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._setMaxH = qtw["_ZN7QWidget16setMaximumHeightEi"]; self._setMaxH.restype = None; self._setMaxH.argtypes = [ctypes.c_void_p, ctypes.c_int]
        except Exception:
            self._setFixedW = self._setFixedH = None

        self.objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        self.objc.objc_getClass.restype = ctypes.c_void_p; self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p; self.objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def all_widgets(self):
        lst = self._allWidgets()
        if not lst.ptr or not (0 < lst.size < 200000):
            return []
        arr = (ctypes.c_void_p * lst.size).from_address(lst.ptr)
        return [arr[i] for i in range(lst.size) if arr[i]]

    def name(self, w):
        qs = self._objectName(w)
        if qs.ptr and 0 < qs.size < 4096:
            return bytes((ctypes.c_char * (qs.size * 2)).from_address(qs.ptr)).decode("utf-16-le", "replace")
        return ""

    # metaObject() callables by function address. Building the CFUNCTYPE per call was
    # most of what a full _apply cost (two passes over every widget, ~56 ms a tick);
    # there are only a few distinct metaObject() implementations.
    _MO_PROTO = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
    _mo_calls = {}

    def cls(self, w):
        try:
            vt = ctypes.c_void_p.from_address(w).value
            fn = ctypes.c_void_p.from_address(vt).value
            call = self._mo_calls.get(fn)
            if call is None:
                call = self._mo_calls[fn] = self._MO_PROTO(fn)
            mo = call(w)
            return (self._metaClassName(mo) or b"").decode("utf-8", "replace")
        except Exception:
            return ""

    def rect(self, w):
        try:
            data = ctypes.c_void_p.from_address(w + 0x20).value
            ii = (ctypes.c_int32 * 9).from_address(data)
            return (ii[5], ii[6], ii[7], ii[8])
        except Exception:
            return None

    def parent(self, w):
        try:
            d = ctypes.c_void_p.from_address(w + 8).value
            return ctypes.c_void_p.from_address(d + 16).value if d else None
        except Exception:
            return None

    def set_visible(self, w, v):
        self._setVisible(w, v)

    def remove_dock(self, mains, dock):
        # idempotent: removing an already-removed dock is a no-op, so calling
        # this every tick keeps it out of the layout even if Fusion re-docks it.
        if not self._removeDock:
            return
        for mw in mains:
            self._removeDock(mw, dock)

    def add_dock(self, mains, dock):
        # restoreDockWidget puts it back exactly where it was before removal.
        if not self._restoreDock:
            return
        for mw in mains:
            self._restoreDock(mw, dock)

    def set_geometry(self, w, x1, y1, x2, y2):
        r = self.QRect(x1, y1, x2, y2)
        self._setGeometry(w, ctypes.byref(r))

    def clamp_zero(self, w):
        # pin to 0×0 (sticky) so a re-shown top-bar widget paints nothing
        if self._setFixedW:
            self._setFixedW(w, 0); self._setFixedH(w, 0)

    def unclamp(self, w):
        # release the 0×0 pin so the widget can size to its content again
        if self._setMinW:
            self._setMinW(w, 0); self._setMinH(w, 0)
            self._setMaxW(w, 0xFFFFFF); self._setMaxH(w, 0xFFFFFF)  # QWIDGETSIZE_MAX

    def min_height(self, w, h):
        # sticky floor on the canvas height (0 releases it) — see _pin_canvas
        if self._setMinH:
            self._setMinH(w, h)

    def _send(self, recv, selname, restype=ctypes.c_void_p, argtypes=None, args=()):
        msg = self.objc.objc_msgSend
        msg.restype = restype
        msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + (argtypes or [])
        return msg(recv, self.objc.sel_registerName(selname.encode()), *args)

    # ── struct-returning / struct-taking sends ────────────────────────────────
    # objc_msgSend_stret DOES NOT EXIST on arm64 — asking libobjc for it raises
    # "symbol not found", which is how this add-in broke on Apple Silicon. arm64
    # has no separate struct-return entry point: NSRect is four doubles, i.e. a
    # homogeneous float aggregate, so it comes back in d0–d3 from plain
    # objc_msgSend (anything too big instead uses the x8 indirect-result
    # register — still plain objc_msgSend). On x86-64 the hidden-pointer stret
    # variant is still required, so keep both paths: Fusion ships an x86_64
    # slice and could be run under Rosetta.
    #
    # Both build a PRIVATE prototype rather than touching self.objc.objc_msgSend.
    # ctypes caches that attribute as one shared object, so setting a struct
    # restype on it would silently corrupt every other caller — _send() included.
    # The prototypes, built once per struct type (see _NSRect).
    _protos = {}

    def _proto(self, key, make):
        fn = self._protos.get(key)
        if fn is None:
            fn = self._protos[key] = make()
        return fn

    def _send_struct(self, recv, selname, R):
        sel = self.objc.sel_registerName(selname)
        if _ARM64:
            fn = self._proto(('ret', R), lambda: ctypes.CFUNCTYPE(
                R, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", self.objc)))
            return fn(recv, sel)
        fn = self._proto(('stret', R), lambda: ctypes.CFUNCTYPE(
            None, ctypes.POINTER(R), ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend_stret", self.objc)))
        out = R()
        fn(ctypes.byref(out), recv, sel)
        return out

    def _send_rect(self, recv, selname, R, rect, flag):
        # Passing a struct BY VALUE is plain objc_msgSend on both architectures.
        fn = self._proto(('rect', R), lambda: ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, R, ctypes.c_bool)(
            ("objc_msgSend", self.objc)))
        fn(recv, self.objc.sel_registerName(selname), rect, flag)

    def main_nswindow(self):
        app = self._send(self.objc.objc_getClass(b"NSApplication"), "sharedApplication")
        wins = self._send(app, "windows")
        n = self._send(wins, "count", restype=ctypes.c_ulong)
        for i in range(n):
            w = self._send(wins, "objectAtIndex:", argtypes=[ctypes.c_ulong], args=(i,))
            vis = self._send(w, "isVisible", restype=ctypes.c_bool)
            style = self._send(w, "styleMask", restype=ctypes.c_ulong)
            t = self._send(w, "title")
            tu = self._send(t, "UTF8String", restype=ctypes.c_char_p) if t else b""
            if vis and (style & 0xe) == 0xe and "Autodesk Fusion" in (tu or b"").decode(errors="replace"):
                return w
        return None

    def ensure_key(self, w, force=False):
        # Showing the top UI makes one of Fusion's toolbar QNSPanels grab the key
        # window, so keyboard/hotkeys/the "S" search go to that panel instead of
        # the canvas until you click. Our window stays mainWindow though, so the
        # canvas should be key. force=True (used on a toggle) restores key to the
        # canvas even when a panel currently holds it; the loop stays conservative
        # (acts only when nothing is key) so it never steals key from a real
        # command dialog. Never act unless Fusion is the active app.
        if not w:
            return
        app = self._send(self.objc.objc_getClass(b"NSApplication"), "sharedApplication")
        active = self._send(app, "isActive", restype=ctypes.c_bool)
        kw = self._send(app, "keyWindow")
        is_key = self._send(w, "isKeyWindow", restype=ctypes.c_bool)
        is_main = self._send(w, "isMainWindow", restype=ctypes.c_bool)
        if not active:
            return
        # normal/loop: only when nothing is key.  force: when a panel stole key
        # but the canvas is still the main window (so it's safe to reclaim).
        act = (not is_key) if (force and is_main) else (kw == 0 or kw is None)
        if act and self._send(w, "canBecomeKeyWindow", restype=ctypes.c_bool):
            self._send(w, "makeKeyWindow")

    def set_titlebar(self, w, hidden):
        # BORDERLESS when hiding: drop the Titled bit for a true no-title-bar,
        # edge-to-edge window (no 28px blue band). This makes macOS report the
        # window subrole as AXDialog instead of AXStandardWindow — that is fine:
        # the slots/swipe scripts identify Fusion's canvas by SIZE, not subrole
        # (slotlib.sh cad_aux_geoms), so window management survives this AND
        # survives yabai restarts. This is a plain styleMask change (safe); do
        # NOT ever fake the subrole by swizzling accessibilitySubrole — a Python
        # IMP there deadlocks Fusion's main thread.
        cur = self._send(w, "styleMask", restype=ctypes.c_ulong)
        new = (cur & ~0x1 & ~0x8000) if hidden else (cur | 0x1)
        if new != cur:
            self._send(w, "setStyleMask:", argtypes=[ctypes.c_ulong], args=(new,))
        for btn in (0, 1, 2):  # close / miniaturize / zoom
            b = self._send(w, "standardWindowButton:", argtypes=[ctypes.c_long], args=(btn,))
            if b:
                self._send(b, "setHidden:", argtypes=[ctypes.c_bool], args=(hidden,))

    def nudge(self, w):
        P, S, R = _NSPoint, _NSSize, _NSRect
        fr = self._send_struct(w, b"frame", R)
        self._send_rect(w, b"setFrame:display:", R, R(P(fr.o.x, fr.o.y), S(fr.s.w, fr.s.h - 1)), True)
        self._send_rect(w, b"setFrame:display:", R, R(P(fr.o.x, fr.o.y), S(fr.s.w, fr.s.h)), True)

    def fill_top_gap(self, w, threshold=60.0):
        # The slots/window-manager positions Fusion via the Accessibility API,
        # which macOS clamps a few px below the menu bar (and more at startup,
        # ~title-bar height). A TITLED window hides that strip behind its title
        # bar, but our BORDERLESS window shows desktop through it. Close it by
        # extending the window's TOP up to the screen top via a Cocoa setFrame
        # (Cocoa isn't AX-clamped). Scoped to a SMALL gap so we only ever undo
        # the clamp — never override an intentional partial/bottom slot (those
        # leave a gap far larger than `threshold`). Cocoa coords: origin.y is the
        # window BOTTOM, so raising the top = growing height, bottom unchanged.
        P, S, R = _NSPoint, _NSSize, _NSRect
        scr = self._send(w, "screen")
        if not scr:
            return
        fr = self._send_struct(w, b"frame", R)
        sf = self._send_struct(scr, b"frame", R)
        gap = (sf.o.y + sf.s.h) - (fr.o.y + fr.s.h)   # screen top − window top
        if 0.5 < gap <= threshold:
            self._send_rect(w, b"setFrame:display:", R,
                            R(P(fr.o.x, fr.o.y), S(fr.s.w, fr.s.h + gap)), True)


# ── ribbon (Fusion command) ───────────────────────────────────────────────────

def _set_ribbon(hidden):
    cid = 'HideCommandToolbarCommand' if hidden else 'ShowCommandToolbarCommand'
    cd = _ui.commandDefinitions.itemById(cid)
    if cd:
        try:
            cd.execute()
        except Exception:
            pass


# ── core apply / restore ──────────────────────────────────────────────────────

def _owner_main(qt, w):
    """Walk parents to the owning Nu::QTMainWindow (for re-docking on restore)."""
    p = qt.parent(w)
    for _ in range(20):
        if not p:
            break
        if qt.cls(p) == "Nu::QTMainWindow":
            return p
        p = qt.parent(p)
    return None


def _quick(qt):
    """Burst-time re-hide using cached pointers — re-remove the top dock (and
    re-hide the timeline) the instant Fusion re-shows them after a palette opens.
    Note: the 0×0 top-bar clamp is NOT re-applied here — setFixedSize is sticky,
    so it survives Fusion's re-shows on its own.

    CRITICAL: every cached pointer is validated against the LIVE widget set before
    it is touched. Opening a document or clicking Home destroys and rebuilds the
    canvas / dock / timeline widgets, so the cached pointers from the last _apply
    become dangling — dereferencing one (e.g. qt.rect on a freed canvas) is a
    SIGSEGV that try/except cannot catch and hard-crashes Fusion. We run on the
    GUI thread (fired via custom event), so nothing is freed mid-tick: a fresh
    QApplication::allWidgets() snapshot is authoritative for the duration of this
    call. If a cached pointer is no longer live we skip it and ask for a full
    rescan, which re-finds the rebuilt widgets safely."""
    global _ribbon_dirty
    try:
        live = set(qt.all_widgets())
        if not live:
            return
        # a rebuilt tree means our cache is stale: skip the cheap path this tick
        # and let the next _apply re-find everything from scratch.
        if (_top_dock_ptr is not None and _top_dock_ptr not in live) or \
           (_canvas_ptr is not None and _canvas_ptr not in live):
            _ribbon_dirty = True
            return
        if _hide_top and _top_dock_ptr in live:
            mains = [m for m in _mains_ptrs if m in live]
            if mains:
                qt.remove_dock(mains, _top_dock_ptr)
        if _tl_hidden() and _timeline_ptrs:
            for w in _timeline_ptrs:
                if w in live:
                    qt.set_visible(w, False)
        # keep the canvas full so a flashing timeline can't reflow/shift the
        # viewport — it just overlays briefly instead of pushing geometry
        if _tl_hidden() and _canvas_ptr in live and _full_rect:
            if qt.rect(_canvas_ptr) != _full_rect:
                qt.set_geometry(_canvas_ptr, _full_rect[0], _full_rect[1], _full_rect[2], _full_rect[3])
    except Exception:
        pass


def _apply(qt):
    global _ribbon_dirty, _nudge_pending, _top_dock_ptr, _mains_ptrs, _timeline_ptrs, _canvas_ptr, _full_rect, _reveal_timeline_once, _reveal_top_once
    win = qt.main_nswindow()
    if win:
        qt.set_titlebar(win, True)
        qt.fill_top_gap(win)   # close the AX menu-bar clamp gap above the borderless window

    # Each widget's name and class read ONCE — both passes below need them.
    widgets = [(w, qt.name(w), qt.cls(w)) for w in qt.all_widgets()]
    ws = bg = canvas = top_dock = None
    mains = []
    topbar = []
    for w, nm, c in widgets:
        if nm == "Main--QTWorkspaceWindow":
            ws = w
        elif nm == "backgroundWidget":
            bg = w
        elif nm == "TopNavBar_2":
            top_dock = w
        elif c == "QTCanvas":
            canvas = w
        elif c == "Nu::QTMainWindow":
            mains.append(w)
        if _is_topbar(nm):
            topbar.append(w)
    full = qt.rect(bg) if bg else None
    thr = (full[3] - 60) if full else None

    # Top bar: pull it out of the dock layout entirely (not just hide it) so the
    # environment/palette relayout has nothing to re-show — no flash. The HIDE side
    # is idempotent per tick, so it self-heals if Fusion ever re-docks it. The SHOW
    # side is ONE-SHOT (only on _reveal_top_once): calling restoreDockWidget every
    # idle tick eventually flings the dock to negative coords (off-screen), which is
    # the "ribbon re-hides itself a second after I show it" bug. Once shown, Fusion
    # keeps the bar docked on its own — same contract as the timeline below.
    if top_dock is not None and mains:
        if _hide_top:
            qt.remove_dock(mains, top_dock)
        elif _reveal_top_once:
            owner = _owner_main(qt, top_dock)
            if owner is not None:
                qt.add_dock([owner], top_dock)
                qt.set_visible(top_dock, True)

    # Pin the top bar to 0×0 when hidden (the sticky clamp that kills the
    # command-activation flash — re-asserted every tick so a re-shown dock paints
    # nothing). Release it only ONCE on reveal; re-touching it every tick while
    # shown is unnecessary and keeps us from fighting Fusion's own layout.
    for w in topbar:
        if _hide_top:
            qt.clamp_zero(w)
        elif _reveal_top_once:
            qt.unclamp(w)

    # Timeline ownership: only TOUCH timeline widgets when we are actively hiding
    # them (or, one-shot, to reveal them right after a toggle). When the timeline
    # is meant to be visible, leave it ENTIRELY to Fusion — re-show()ing it every
    # tick fought Fusion's own per-context management (it hides the parent
    # assembly's nodes + the overflow scrollbar when you step into a component),
    # which showed up as a half-second scrollbar flash and overlaid timeline
    # items. Fusion never re-hides the Design timeline on its own, so a single
    # reveal on the toggle is enough — no periodic force-show needed.
    timeline = []
    reveal = _reveal_timeline_once
    for w, nm, c in widgets:
        if nm in TOP_NAMES:
            # hide every tick; show only once on reveal (same one-shot contract as
            # the dock — avoids re-poking these widgets while the bar is shown)
            if _hide_top:
                qt.set_visible(w, False)
            elif _reveal_top_once:
                qt.set_visible(w, True)
            continue
        is_tl = False
        if c.startswith("Na::") and "Timeline" in c:
            is_tl = True
        elif ws is not None and thr is not None and qt.parent(w) == ws:
            r = qt.rect(w)
            if r and r[1] >= thr and c not in ("QTCanvas", "WebView") and not c.startswith("QTPalette"):
                is_tl = True
        if is_tl:
            timeline.append(w)
            if _tl_hidden():
                qt.set_visible(w, False)
            elif reveal:
                qt.set_visible(w, True)
            continue
    _reveal_timeline_once = False
    _reveal_top_once = False

    # refresh the burst cache (pointers are stable for the session; the full
    # _apply re-finds them each idle tick in case a workspace switch recreates them)
    _top_dock_ptr = top_dock
    _mains_ptrs = tuple(mains)
    _timeline_ptrs = tuple(timeline)
    _canvas_ptr = canvas
    _full_rect = full

    # canvas: only force-fill when the timeline is hidden; otherwise Fusion owns it
    _pin_canvas(qt, canvas, full)
    if canvas is not None and full is not None and _tl_hidden():
        if qt.rect(canvas) != full:
            qt.set_geometry(canvas, full[0], full[1], full[2], full[3])

    if _ribbon_dirty:
        _set_ribbon(_hide_top); _ribbon_dirty = False
    if _nudge_pending and win:
        qt.nudge(win); _nudge_pending = False

    # heal the borderless-window key-focus loss (keyboard / hotkeys / S search)
    if win:
        qt.ensure_key(win)


def _pin_canvas(qt, canvas, full):
    """Pin the canvas's MINIMUM height to the full height while the timeline is hidden.
    Opening a command dialog (an Edit Feature, say) relays out the workspace and Fusion
    sets the canvas 50px short — the timeline's strip — for a frame before the fill
    below/_quick stretches it back: the viewport jumped and the bar's gap showed, which
    read as the timeline popping up. QWidget::setGeometry clamps to the minimum size, so
    with the floor in place Fusion's short geometry lands full height (measured: 1439
    held through a whole edit, vs one 1389 frame without it). The canvas's parent
    (QTWorkspaceWindow) has no QLayout, so the floor never propagates to the window's
    minimum size. Re-pinned when the height changes (window resize) — before the fill,
    so a smaller full rect isn't clamped by the old floor — released when the timeline
    shows or the add-in stops."""
    global _canvas_pin
    want = None
    if canvas is not None and full is not None and _tl_hidden():
        want = (canvas, full[3] - full[1] + 1)
    if want == _canvas_pin:
        return
    if _canvas_pin is not None and (want is None or want[0] != _canvas_pin[0]):
        if _canvas_pin[0] in set(qt.all_widgets()):      # the old canvas may be gone
            qt.min_height(_canvas_pin[0], 0)
    if want is not None:
        qt.min_height(*want)
    _canvas_pin = want


def _restore(qt):
    _pin_canvas(qt, None, None)
    win = qt.main_nswindow()
    if win:
        qt.set_titlebar(win, False)
    widgets = qt.all_widgets()
    ws = bg = top_dock = None
    for w in widgets:
        nm = qt.name(w)
        if nm == "Main--QTWorkspaceWindow":
            ws = w
        elif nm == "backgroundWidget":
            bg = w
        elif nm == "TopNavBar_2":
            top_dock = w
    full = qt.rect(bg) if bg else None
    thr = (full[3] - 60) if full else None
    # re-dock the top bar we pulled out of the layout
    if top_dock is not None:
        owner = _owner_main(qt, top_dock)
        if owner is not None:
            qt.add_dock([owner], top_dock)
    for w in widgets:
        nm = qt.name(w); c = qt.cls(w)
        if _is_topbar(nm):
            qt.unclamp(w)            # release the 0×0 pin so the bar sizes back
        if nm in TOP_NAMES or (c.startswith("Na::") and "Timeline" in c):
            qt.set_visible(w, True); continue
        if ws is not None and thr is not None and qt.parent(w) == ws:
            r = qt.rect(w)
            if r and r[1] >= thr and c not in ("QTCanvas", "WebView") and not c.startswith("QTPalette"):
                qt.set_visible(w, True)
    _set_ribbon(False)
    if win:
        qt.nudge(win)


# ── event plumbing ────────────────────────────────────────────────────────────

class ReapplyHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _qt, _forced_last, _nudge_pending, _reveal_timeline_once, _fired_at
        _fired_at = 0.0
        try:
            if _qt is None:
                _qt = _Bind()
            if not _enabled:
                return
            # Hide Native Timeline's request came or went: relayout, and when it went (and our own
            # setting shows the bar) reveal it once — same one-shot as the toggle.
            forced = _hide_forced()
            if forced != _forced_last:
                _forced_last = forced
                _nudge_pending = True
                if not _tl_hidden():
                    _reveal_timeline_once = True
                _apply(_qt)
                return
            # during a burst (just after a palette opened) use the cheap cached
            # re-hide so we can poll fast without rescanning every widget
            if time.monotonic() < _burst_until:
                _quick(_qt)
            else:
                _apply(_qt)
        except Exception:
            pass


# When the last tick was fired and not yet handled, or 0. While Fusion's main thread is
# busy (a long recompute, a slow script) nothing is handled, and firing on regardless
# queued one full rescan per half second — all of them run back to back afterwards,
# stretching the stall by seconds. One outstanding tick is enough.
_fired_at = 0.0


def _worker():
    global _fired_at
    while _running:
        now = time.monotonic()
        if not _fired_at or now - _fired_at > 5.0:
            _fired_at = now
            try:
                _app.fireCustomEvent(EVENT_ID)
            except Exception:
                _fired_at = 0.0
        # During a burst (just after a command/palette opened or closed) poll
        # every frame so the chrome Fusion re-showed is re-hidden before it can
        # flash; otherwise idle at the lazy 0.5 s rate.
        time.sleep(0.016 if time.monotonic() < _burst_until else 0.5)


class DocActivatedHandler(adsk.core.DocumentEventHandler):
    """A fresh document brings the ribbon back; mark it for re-hide."""
    def notify(self, args):
        global _ribbon_dirty
        if _enabled:
            _ribbon_dirty = True


class CommandRelayoutHandler(adsk.core.ApplicationCommandEventHandler):
    """Starting/ending a command relays out the workspace and re-shows the hidden
    chrome. The flashy case is environment/feature command activation (entering a
    sketch, Extrude, Move): Fusion flips the TopNavBar_2 dock — document tabs, QAT
    and the command ribbon — back to visible for ~1 frame. Re-hiding it (even
    synchronously here) loses that race by a frame, which is why the *real* fix is
    the sticky 0×0 clamp on the top-bar widgets in _apply/_quick — a re-shown 0×0
    widget paints nothing, no race. This handler just arms the fast burst so the
    clamp/dock-removal is re-asserted promptly if a workspace relayout disturbs
    it, and re-asserts once synchronously now as cheap insurance."""
    def notify(self, args):
        global _burst_until
        if _enabled:
            if _hide_top and _qt is not None:
                try:
                    _quick(_qt)            # re-assert clamp + dock removal now
                except Exception:
                    pass
            _burst_until = time.monotonic() + 2.0


# ── toggle commands ───────────────────────────────────────────────────────────

class ToggleExecute(adsk.core.CommandEventHandler):
    def __init__(self, which):
        super().__init__()
        self.which = which

    def notify(self, args):
        # Flip our own authoritative flag rather than trusting cmd_def.isChecked
        # (which is not reliably updated when the execute event fires for a
        # checkbox command), then force the checkbox to match.
        global _enabled, _hide_top, _hide_timeline, _ribbon_dirty, _nudge_pending, _qt, _reveal_timeline_once, _reveal_top_once
        try:
            cmd_id = {'master': CMD_MASTER, 'top': CMD_TOP, 'timeline': CMD_TIMELINE}[self.which]
            if self.which == 'master':
                _enabled = not _enabled; new = _enabled; _ribbon_dirty = True; _nudge_pending = True
            elif self.which == 'top':
                # nudge the window on every top toggle: re-showing the bar means
                # re-docking it, and the dock doesn't lay out (stays 0×0/offscreen)
                # until a relayout is forced — otherwise the bar "comes up" but
                # never actually paints.
                _hide_top = not _hide_top; new = _hide_top; _ribbon_dirty = True; _nudge_pending = True
            else:  # timeline
                _hide_timeline = not _hide_timeline; new = _hide_timeline; _nudge_pending = True
            # arm the one-shot reveals whenever the timeline / top bar should now be
            # visible, so the next _apply shows each once and then hands it back to
            # Fusion (re-touching them every tick is what broke them — see _apply).
            if _enabled and not _tl_hidden():
                _reveal_timeline_once = True
            if _enabled and not _hide_top:
                _reveal_top_once = True
            cd = _ui.commandDefinitions.itemById(cmd_id)
            if cd:
                try:
                    cd.isChecked = new
                except Exception:
                    pass
            _save_state()
            if _qt is None:
                _qt = _Bind()
            if _enabled:
                _apply(_qt)
            else:
                _restore(_qt)
            try:
                _qt.ensure_key(_qt.main_nswindow(), force=True)
            except Exception:
                pass
        except Exception:
            if _ui:
                _ui.messageBox(traceback.format_exc())


class ToggleCreated(adsk.core.CommandCreatedEventHandler):
    def __init__(self, which):
        super().__init__()
        self.which = which

    def notify(self, args):
        h = ToggleExecute(self.which)
        args.command.execute.add(h)
        _handlers.append(h)


# ── lifecycle ─────────────────────────────────────────────────────────────────

def run(context):
    global _app, _ui, _event, _running, _ribbon_dirty, _nudge_pending
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        _load_state()
        _ribbon_dirty = True
        _nudge_pending = True

        ws = _ui.workspaces.itemById('FusionSolidEnvironment')
        panel = ws.toolbarPanels.itemById('SolidScriptsAddinsPanel')
        init = {'master': _enabled, 'top': _hide_top, 'timeline': _hide_timeline}
        for which, cmd_id, name, desc in CMDS:
            cmd_def = _ui.commandDefinitions.itemById(cmd_id)
            if not cmd_def:
                cmd_def = _ui.commandDefinitions.addCheckBoxDefinition(cmd_id, name, desc, init[which])
            try:
                cmd_def.isChecked = init[which]
            except Exception:
                pass
            on_created = ToggleCreated(which)
            cmd_def.commandCreated.add(on_created)
            _handlers.append(on_created)
            if not panel.controls.itemById(cmd_id):
                ctrl = panel.controls.addCommand(cmd_def)
                if ctrl:
                    ctrl.isPromoted = True

        try:
            _event = _app.registerCustomEvent(EVENT_ID)
        except Exception:
            _app.unregisterCustomEvent(EVENT_ID)
            _event = _app.registerCustomEvent(EVENT_ID)
        reapply = ReapplyHandler()
        _event.add(reapply)
        _handlers.append(reapply)

        doc_h = DocActivatedHandler()
        _app.documentActivated.add(doc_h)
        _handlers.append(doc_h)

        relayout_h = CommandRelayoutHandler()
        _ui.commandStarting.add(relayout_h)
        _ui.commandTerminated.add(relayout_h)
        _handlers.append(relayout_h)

        _running = True
        threading.Thread(target=_worker, daemon=True).start()

    except Exception:
        if _ui:
            _ui.messageBox('Maximize Viewport start failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _running
    try:
        _running = False
        if _qt is not None:
            _pin_canvas(_qt, None, None)     # a reload must not leave the floor behind
        _handlers.clear()
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
    except Exception:
        if _ui:
            _ui.messageBox('Maximize Viewport stop failed:\n{}'.format(traceback.format_exc()))
