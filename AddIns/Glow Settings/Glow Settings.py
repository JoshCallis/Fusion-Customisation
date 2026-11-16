"""
Glow Settings
─────────────
One command, "Glow Settings" (id FusionGlowSettingsCmd): opens a palette of live
sliders for the edge glow the Browser shows round Fusion's window in suspend mode
(bin/fusion_glow.m in ~/Code/Scripts).

  - One look per appearance — Light / Dark / Extra Dark / Extra Light (the same four
    contrast_theme/ and Control Room use). config/fusion_glow.json is keyed by them
    (light / dark / xdark / xlight); the glow picks the one for the appearance it is
    in. The palette's mode tabs pick which one the sliders edit; they open on the
    current appearance.
  - Fade (seconds, on / off) is the one setting shared by all four: the file's
    top-level "fade", outside the per-mode looks; the mode tabs don't apply to it.
  - Every slider writes config/fusion_glow.json at once; the running glow re-reads
    that file by mtime on its next tick, so a change shows within ~150 ms. Nothing
    here talks to the glow process itself — the file is the whole hand-off.
  - Preview starts the glow while the palette is open (it normally only runs in
    suspend mode), and stops it again on close — unless suspend mode is on, which
    owns it then (Shared State.json "suspend", fvs § Suspend mode: peek).
  - The status line reads the screen's contrast the way the glow does (the display
    gamma table, see the glow's header): ×1.21 in Dark / Extra Dark, which is when
    Contrast compensation and Compensation ramp matter at all. For a mode other than
    the current one, its level comes from contrast_theme/config.json instead.

Bind it from Control Room ▸ Fusion like any other command.
"""
import adsk
import adsk.core
import ctypes
import json
import os
import subprocess
import traceback

CMD_ID     = 'FusionGlowSettingsCmd'
CMD_NAME   = 'Glow Settings'
CMD_DESC   = 'Tune the suspend-mode edge glow round Fusion\'s window'
PALETTE_ID = 'FusionGlowSettingsPalette'
PANEL_ID   = 'SolidScriptsAddinsPanel'

CONFIG     = '/Users/josh/Code/Scripts/config/fusion_glow.json'
EXTRA      = '/Users/josh/Code/Scripts/fusion_ui/extra.json'
CONTRAST   = '/Users/josh/Code/Scripts/contrast_theme/config.json'
MODES      = ('light', 'dark', 'xdark', 'xlight')
GLOW_BIN   = '/Users/josh/bin/fusion_glow'
SHARED     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'Shared State.json')
HTML       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'palette.html')

# The glow's own compiled-in defaults (bin/fusion_glow.m § The look) — what Reset puts back.
DEFAULTS = {
    'reach': 120, 'falloff': 1.6, 'corner': 8, 'alpha': 0.22,
    'rgb': [120, 180, 255], 'contrast_comp': 0.6, 'contrast_toe': 0.08,
}
FADE_DEFAULT = 0.25     # bin/fusion_glow.m D_FADE — top-level, not per mode

_app = None
_ui = None
_handlers = []
_preview = False


# ── config ────────────────────────────────────────────────────────────────────
def _read_file():
    try:
        with open(CONFIG, 'r', encoding='utf-8') as f:
            got = json.load(f)
    except Exception:
        got = {}
    return got if isinstance(got, dict) else {}


def _read_fade():
    v = _read_file().get('fade')
    return float(v) if isinstance(v, (int, float)) else FADE_DEFAULT


def _read_all():
    """{mode: full look} for all four. Flat top-level keys (the old single-look file)
    are the base every mode starts from — the glow reads it the same way."""
    got = _read_file()
    base = dict(DEFAULTS)
    base.update({k: v for k, v in got.items() if k in DEFAULTS})
    out = {}
    for m in MODES:
        cfg = dict(base)
        own = got.get(m)
        if isinstance(own, dict):
            cfg.update({k: v for k, v in own.items() if k in DEFAULTS})
        out[m] = cfg
    return out


def _write_all(looks, fade=None):
    """Atomic, so the glow never reads half a file. Always the per-mode shape, plus
    the shared top-level fade (kept as it was unless given)."""
    out = {'fade': _read_fade() if fade is None else fade}
    out.update({m: looks[m] for m in MODES})
    tmp = CONFIG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)
        f.write('\n')
    os.replace(tmp, CONFIG)


def _mode_now():
    """light / dark / xdark / xlight — read as contrast_theme/main.swift and
    remap/server.py contrast_mode_now() read it (`defaults`, not a cached appearance)."""
    try:
        style = subprocess.run(['/usr/bin/defaults', 'read', '-g', 'AppleInterfaceStyle'],
                               capture_output=True, text=True).stdout.strip()
    except Exception:
        style = ''
    try:
        with open(EXTRA, 'r', encoding='utf-8') as f:
            extra = bool(json.load(f).get('on'))
    except Exception:
        extra = False
    if style == 'Dark':
        return 'xdark' if extra else 'dark'
    return 'xlight' if extra else 'light'


def _contrast_levels():
    """contrast_theme/config.json's level per mode (percent), 0 when it is off."""
    try:
        with open(CONTRAST, 'r', encoding='utf-8') as f:
            c = json.load(f)
        on = c.get('enabled', True)
        return {m: (float(c.get(m, 0)) if on else 0.0) for m in MODES}
    except Exception:
        return {m: 0.0 for m in MODES}


# ── the glow process ──────────────────────────────────────────────────────────
def _glow_running():
    return subprocess.run(['/usr/bin/pgrep', '-x', 'fusion_glow'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _suspended():
    try:
        with open(SHARED, 'r', encoding='utf-8') as f:
            return bool(json.load(f).get('suspend'))
    except Exception:
        return False


def _set_preview(on):
    global _preview
    _preview = bool(on)
    try:
        if on:
            if not _glow_running():
                subprocess.Popen([GLOW_BIN, str(os.getpid())], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        elif not _suspended():          # suspend mode owns the glow while it's on
            subprocess.run(['/usr/bin/pkill', '-x', 'fusion_glow'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ── screen contrast, as the glow measures it ──────────────────────────────────
def _contrast():
    """k = (T(1) − T(0)) / (T(1) + T(0)) from the main display's gamma table."""
    try:
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        arr = ctypes.c_float * 256
        r, g, b = arr(), arr(), arr()
        cnt = ctypes.c_uint32(0)
        err = cg.CGGetDisplayTransferByTable(ctypes.c_uint32(cg.CGMainDisplayID()),
                                             ctypes.c_uint32(256), r, g, b, ctypes.byref(cnt))
        if err or cnt.value < 2:
            return 1.0
        t0, t1 = r[0], r[cnt.value - 1]
        if t1 + t0 <= 1e-4:
            return 1.0
        return max(1.0, (t1 - t0) / (t1 + t0))
    except Exception:
        return 1.0


def _status():
    return {'contrast': round(_contrast(), 3), 'running': _glow_running(),
            'suspended': _suspended(), 'preview': _preview, 'mode': _mode_now(),
            'levels': _contrast_levels()}


# ── palette ───────────────────────────────────────────────────────────────────
class _FromHTML(adsk.core.HTMLEventHandler):
    def notify(self, args):
        try:
            a = args.action
            data = json.loads(args.data) if args.data else {}
            if a == 'get':
                args.returnData = json.dumps({'looks': _read_all(), 'status': _status(),
                                              'defaults': DEFAULTS, 'fade': _read_fade()})
            elif a == 'set':                # {mode, values}
                m = data.get('mode')
                if m in MODES:
                    looks = _read_all()
                    looks[m].update({k: v for k, v in (data.get('values') or {}).items()
                                     if k in DEFAULTS})
                    _write_all(looks)
                args.returnData = json.dumps({'ok': True})
            elif a == 'fade':               # {value}: seconds, all modes
                v = data.get('value')
                if isinstance(v, (int, float)):
                    _write_all(_read_all(), max(0.0, min(5.0, float(v))))
                args.returnData = json.dumps({'ok': True})
            elif a == 'reset':              # {mode}: that mode back to the defaults
                looks = _read_all()
                m = data.get('mode')
                if m in MODES:
                    looks[m] = dict(DEFAULTS)
                    _write_all(looks)
                args.returnData = json.dumps({'looks': looks})
            elif a == 'copy':               # {mode}: that mode's look onto all four
                looks = _read_all()
                m = data.get('mode')
                if m in MODES:
                    looks = {o: dict(looks[m]) for o in MODES}
                    _write_all(looks)
                args.returnData = json.dumps({'looks': looks})
            elif a == 'preview':
                _set_preview(data.get('on'))
                args.returnData = json.dumps({'status': _status()})
            elif a == 'status':
                args.returnData = json.dumps({'status': _status()})
        except Exception:
            args.returnData = json.dumps({'error': traceback.format_exc()})


class _Closed(adsk.core.UserInterfaceGeneralEventHandler):
    def notify(self, args):
        _set_preview(False)


def _show_palette():
    pal = _ui.palettes.itemById(PALETTE_ID)
    if pal is None:
        pal = _ui.palettes.add(PALETTE_ID, 'Glow Settings', HTML, True, True, True, 340, 640, True)
        pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateFloating
        pal.setPosition(1000, 160)          # clear of the dock on the left
        h = _FromHTML()
        pal.incomingFromHTML.add(h)
        _handlers.append(h)
        c = _Closed()
        pal.closed.add(c)
        _handlers.append(c)
    pal.isVisible = True


class _Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _show_palette()
        except Exception:
            _ui.messageBox('Glow Settings:\n' + traceback.format_exc())


class _Created(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = _Execute()
        args.command.execute.add(h)
        _handlers.append(h)


# ── lifecycle ─────────────────────────────────────────────────────────────────
def run(context):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        cd = _ui.commandDefinitions.itemById(CMD_ID)
        if not cd:
            cd = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        h = _Created()
        cd.commandCreated.add(h)
        _handlers.append(h)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cd)
        adsk.autoTerminate(False)
    except Exception:
        if _ui:
            _ui.messageBox('Glow Settings run() failed:\n' + traceback.format_exc())


def stop(context):
    try:
        pal = _ui.palettes.itemById(PALETTE_ID)
        if pal:
            pal.deleteMe()
        _set_preview(False)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        ctrl = panel.controls.itemById(CMD_ID) if panel else None
        if ctrl:
            ctrl.deleteMe()
        _handlers.clear()
    except Exception:
        pass
