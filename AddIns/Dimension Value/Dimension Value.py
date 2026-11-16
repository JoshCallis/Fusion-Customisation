"""
Dimension Value — add-in.

A bigger, multi-line replacement for Fusion's inline dimension box, which can also
turn what you type into a named user parameter in one gesture.

Press the hotkey with a sketch dimension selected and a dialog opens under the cursor,
prefilled with the dimension's current expression:

    value / expression   ← autocompletes against the document's user parameters
    parameter name       ← optional; filling it in creates the parameter and points
                           the dimension at it instead of at a bare number

Architecture is the same as the Params (8731) and Browser (8732) windows: this module
runs a local HTTP server (server.py, port 8734) and a ~70 ms custom-event tick that
does all the Fusion API work on the main thread — pushing the parameter list out and
draining commands the window posted back. The window itself is FusionValue.app.

The same window doubles as the NEW PARAMETER dialog (a second command, so it takes its
own key). Nothing is selected and no dimension is involved: the value box is empty, the
name field is compulsory, and the panel opens in the middle of the screen because there
is no dimension on the canvas for it to sit beside. Where the new parameter is FILED is
decided by the Browser window, which owns folders.json — see _file_param_in_browser.

Scope for the dimension half is deliberately SKETCH DIMENSIONS ONLY. Command-dialog value fields (extrude
distance and friends) are not reachable from the API — an add-in cannot read or write
another command's inputs — so covering those would mean reading Qt widgets and
synthesising keystrokes. Everything here is pure API, which is what makes it safe to
leave running all the time.
"""

import adsk.core, adsk.fusion
import importlib.util, json, math, os, re, subprocess, sys, threading, time, traceback, types
import urllib.request

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []

# The per-component bounds record. Loaded the way every add-in here loads a sibling
# module — a plain import cannot reach the AddIns root from inside a command folder.
_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


# The process-wide "someone is already in the model" latch — read its header before
# changing anything about the sync tick. It is why the sync below can decline to run.
tickguard = _load('tickguard', 'Tick Guard.py')

try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    # No bounds module means no bound datum, which is exactly the state before it
    # existed. Never a reason for the value box to stop opening.
    bounds = None

# Linking a driven dimension to a user parameter — shared with Parameters, which keeps
# the pair's names in step afterwards.
drivenlink = _load('drivenlink', 'Driven Link.py')

# "Make parameter"'s checks — shared with the Extrude and feature panels, whose live
# commands make theirs (see that file for why they can't come here).
madeparams = _load('madeparams', 'Made Params.py')

# Every typed number becomes a hidden number parameter on commit (read its header);
# _params_snapshot marks them so the window shows each as its value.
numbers = _load('numbers', 'Number Params.py')

try:
    # Only for a new plane-box sketch opening where Cycle Visibility was (see
    # _open_sketch_dialog / _do_create_sketch). Missing, it opens at the entry defaults.
    fvs = _load('fvs', 'Visibility Snapshot.py')
except Exception:
    fvs = None

try:
    # The Section Sketch engine: whether a dimension runs through material (Span or
    # Thickness), the section references a gap limit is drawn between, and flexible
    # ranges (§ Flex). Missing, the box opens exactly as it did before it existed.
    section = _load('section', 'Section.py')
except Exception:
    section = None

# Which datum line puts a plane where you clicked — pure arithmetic, beside planes.py in
# fusion_ui (loaded by path for the same reason Bounds.py loads planes.py).
PLANE_GUESS_PY = '/Users/josh/Code/Scripts/fusion_ui/plane_guess.py'
try:
    plane_guess = types.ModuleType('plane_guess')
    plane_guess.__file__ = PLANE_GUESS_PY
    with open(PLANE_GUESS_PY, 'r', encoding='utf-8') as _f:
        exec(compile(_f.read(), PLANE_GUESS_PY, 'exec'), plane_guess.__dict__)
except Exception:
    plane_guess = None      # the box still opens; a click just suggests nothing

CMD_ID   = 'DimensionValueCmd'
CMD_NAME = 'Dimension: Value / New Parameter'
CMD_DESC = ('Open the big value box for the selected sketch dimension. Type an '
            'expression, and optionally a name to turn it into a user parameter.')

# Same dialog, no dimension: just make a user parameter. Its own command so it gets its
# own key in Fusion's keymap (⇧⌘N), and so it works with nothing selected at all.
NEWPARAM_CMD_ID   = 'NewParameterCmd'
NEWPARAM_CMD_NAME = 'Parameter: New'
NEWPARAM_CMD_DESC = ('Open the value box to create a user parameter. It is filed into '
                     'whichever browser group is open, or into the active component.')
# Same box on a DRIVEN dimension: its value is the geometry's, so the box only names it,
# with the stepper, and commit makes the user parameter that tracks it (Driven Link.py).
# DimensionValueCmd does the same on a driven dimension; this is the key that says so
# up front and complains when what is selected isn't one. The id is older than this
# add-in's ownership of it (Parameters.py had it, with a plain input box) — kept, since
# the keymap binds by id.
LINK_CMD_ID   = 'FusionParamsLinkDrivenCmd'
LINK_CMD_NAME = 'Parameter: Link Driven Dimension'
LINK_CMD_DESC = ('Name the selected driven dimension as a user parameter that tracks it, '
                 'in the value box\'s name stepper.')
# Josh models in the RENDER workspace, and a shortcut only dispatches to a command the
# current environment carries — so the link command is registered into Render's hidden
# mirror rather than depending on Timeline Toggle having run first (see the long note in
# Delete Selection.py, where the same omission made Cmd+D silently inert).
RENDER_WS_ID    = 'FusionRenderEnvironment'
RENDER_TAB_ID   = 'DesignCmdsTab'
RENDER_PANEL_ID = 'DC_ParamsLinkDriven'
# The plane box lives here too, for the same reason the New Component panel does: a
# plane line is an expression after its first word, so it wants this autocomplete. The
# KEY belongs to the Smart Sketch add-in; this name is only for what it says in a
# message box.
SKETCH_CMD_NAME = 'Sketch: New'
# Same box again, for the Project Bounds add-in's key: the sketch that is open already
# has a plane, so this opens on ONE axis — whichever one that plane already runs along —
# rather than the whole vocabulary. Name is only for message boxes; the key is theirs.
PROJECT_BOUNDS_CMD_NAME = 'Sketch: Project Bounds'
PANEL_ID = 'SolidScriptsAddinsPanel'

# The Browser's "go in" actions (whichever of Go In / Go In Deep is on F — both count).
# Going into a selected sketch dimension means editing its value, so this add-in claims
# that key whenever a dimension is selected and leaves it alone otherwise. Browser.py
# declines to forward it in the same case — see the matching guard in its
# HotkeyStartingHandler; both sides check, because the order the two commandStarting
# handlers run in is not defined.
GOIN_CMD_IDS = ('BrowserGoInCmd', 'BrowserGoInDeepCmd')

# Fusion's own inline dimension value field — the little box that appears the moment you
# click a dimension into place, and again when you double-click one. It is a COMMAND of
# its own (found by tracing commandStarting, 2026-09-07), which is what makes replacing
# it possible at all: vetoing it before it starts leaves the dimension exactly where it
# was placed and lets this window take the value instead. That is what this add-in
# exists for — "a bigger, multi-line replacement for Fusion's inline dimension box" —
# and until now it was only ever reachable by pressing a key at a dimension afterwards.
#
# Set False to hand the little box back.
INLINE_EDIT_CMD_ID = 'SketchEditDimensionCmdDef'
REPLACE_INLINE_BOX = True
EVENT_ID = 'DimensionValueSyncEvent'

# What the root component is called as a parameter owner — the name stepper's prefix
# (TopLevel…) and its owner dropdown — instead of its real name, which is the
# document's and changes with it. Browser.py tells its window the same name.
ROOT_OWNER = 'Top Level'


def _owner_name(design, comp):
    """`comp`'s name as a parameter owner: ROOT_OWNER for the root."""
    if comp is None:
        return ''
    return ROOT_OWNER if comp == design.rootComponent else comp.name

# The dimension tool itself. The inline value box above opens in TWO situations that look
# identical from inside its commandStarting — a dimension just placed, and an existing one
# double-clicked — and only the first has a dimension that this dialog brought into being.
# The tool still running is what tells them apart: placing happens inside SketchDimension,
# double-clicking happens at idle. It decides whether cancelling takes the dimension with
# it (see _apply_pending_delete). Same id the Smart Dimension add-in starts.
DIMENSION_CMD = 'SketchDimension'

EDITOR_DIR = '/Users/josh/Code/Scripts/fusion_value'
SERVER_PY  = os.path.join(EDITOR_DIR, 'server.py')
APP_PATH   = '/Applications/FusionValue.app'
APP_PROC   = 'FusionValue'
# Failures are a HUD cross that opens the message when clicked, never a dialog — the
# convention Sketch Clipboard / Master Sketches set. See _hud.
HUD_FLASH  = '/Users/josh/bin/hud_flash'

# The Browser window owns folders.json (it is the single writer), so a new parameter is
# filed by asking that window to do it rather than by editing the file from here — two
# writers would race and the window, holding the whole overlay in memory, would win.
BROWSER_URL   = 'http://127.0.0.1:8732'
FILE_PARAM_ID = 'BrowserFileParamCmd'
# A parameter made from nothing has no sketch to sit beside, so the window works out
# where it goes from what is open and selected in the tree — knowledge that only exists
# there. See newParamContainer() in fusion_browser/index.html.
PLACE_PARAM_ID = 'BrowserPlaceNewParamCmd'
# Filing a new component's bounds into a "Bounds" folder in the browser. That window is
# the single writer of folders.json, so the filing is asked for rather than done here.
FILE_BOUNDS_ID = 'BrowserFileBoundsCmd'
# Told to the Browser right after an existing parameter is edited here, so its row shows
# the new value (and the new name) on the keystroke rather than a poll and a half later.
EDITED_PARAM_ID = 'BrowserParamEditedCmd'

# The Parameters window's server. Parameters are OWNED there — that add-in is the single
# writer of userParameters, and it is the one that knows a driven-dimension alias has to
# take its dimension's parameter along when it is renamed. So an edit made in this dialog
# is posted to it rather than applied here, exactly as the Browser posts its own inline
# edits. Validation still happens here, where the Fusion API is on the main thread.
PARAMS_URL = 'http://127.0.0.1:8731'

NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_httpd      = None
_server_mod = None
_stop_evt   = threading.Event()
_custom_evt = None
_active     = False
_starting_handler = None

# The dimension this session is editing, held as an entityToken rather than as the
# object: the window is open across many ticks, and a Fusion object handle does not
# reliably survive a recompute in between. Re-resolved at commit time.
# A dimension session carries {'mode': 'dim', 'token', 'unit', 'occToken', 'compId'}; a parameter
# session carries {'mode': 'param'} and nothing else, because there is no target.
_session = None

# A sketch waiting to be opened for edit, as (entityToken, not before this time).
#
# NOT done in the same tick as the commit. Closing the session makes the window hide
# itself and hand focus back to Fusion, and that activation lands a beat later — right
# on top of a queued SketchActivate, which then does nothing at all. It survived every
# test driven over HTTP (where the window was never frontmost) and failed the first time
# the key was pressed for real. So the entry waits for the window to be gone.
_pending_enter = None
_ENTER_DELAY = 0.30

# A validated dimension edit waiting for the running command to be out of the way.
#
# WHY IT WAITS. This dialog is opened by vetoing Fusion's own inline value box, which
# happens while the DIMENSION TOOL is still running — so anything written from here
# lands inside that command's transaction. Click away or start another command and the
# transaction commits and all is well; press Escape and Fusion cancels the command and
# takes the edit with it. The parameter would be created, the dimension would show it,
# and one Escape later both were gone. So the command is terminated first (which
# COMMITS it, unlike Escape) and the write happens a tick or two afterwards, on its own.
_pending_write = None
_WRITE_DELAY = 0.12

# A dimension waiting to be deleted, as {'token', 'due'}. The cancel half of the above.
#
# CANCELLING A DIMENSION CANCELS THE DIMENSION. This window replaces the box Fusion shows
# the instant a dimension is placed, so backing out of it means backing out of the
# placement — otherwise Escape leaves behind exactly the thing you just said you did not
# want, sized to wherever the cursor happened to be. Only for a dimension this dialog
# opened ON placement (_session['created']); one you double-clicked to edit was there
# before and stays there.
#
# It waits for the same reason a write does, and the delete is deliberately NOT left to
# Fusion's own Escape: terminating the tool COMMITS the placement, so the dimension is
# real by the time this runs and deleting it is a plain, undoable edit rather than a race
# with a transaction that may or may not roll back.
_pending_delete = None


def _hud(kind, message=None):
    """Tick on success; a cross that opens `message` if clicked. Never a dialog. Safe
    from inside commandStarting too — it is a spawned process, not a modal."""
    try:
        args = [HUD_FLASH, kind]
        if message:
            args.append(message)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


# ── Server helpers ───────────────────────────────────────────────────────────
def _load_server():
    global _server_mod
    if _server_mod is None:
        spec = importlib.util.spec_from_file_location('fusion_value_server', SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _server_mod = mod
    return _server_mod


def _start_server():
    global _httpd
    mod = _load_server()
    if _httpd is None:
        try:
            _httpd = mod.make_server()
            threading.Thread(target=_httpd.serve_forever, daemon=True).start()
        except OSError:
            _httpd = None            # port taken — another instance already serving


def _stop_server():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None


_SYNC_PERIOD = 0.07

# Set when an event has been fired and not yet handled, so ticks cannot stack up
# behind a busy main thread and then drain as one long beachball. Holds the
# monotonic time it was fired; the 5 s staleness reset is a backstop so a notify
# lost to an add-in reload cannot stop the poller for good.
_tick_pending = 0.0

_PARAMS_PERIOD = 0.5      # how often an open dialog's parameter list is refreshed
_params_pushed_at = 0.0


def _poller():
    global _tick_pending
    while not _stop_evt.is_set():
        now = time.monotonic()
        if not _tick_pending or now - _tick_pending > 5.0:
            _tick_pending = now
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                _tick_pending = 0.0
        _stop_evt.wait(_SYNC_PERIOD)


def _app_running():
    try:
        return subprocess.run(['pgrep', '-x', APP_PROC],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _launch_app():
    """Start the window process (hidden). Cheap to call when it is already up."""
    try:
        if not _app_running():
            subprocess.Popen(['open', '-a', APP_PATH])
    except Exception:
        pass


def _ensure_window():
    """Make sure something is actually listening before a session is armed.

    Three states, and only the first is the happy one:
      polling recently  → fine, leave it alone
      process gone      → launch it
      process up but NOT polling → wedged. Seen for real: an instance whose page
                          never came up sat there looking healthy to pgrep while
                          every keypress vanished into it. Restart rather than arm a
                          session nothing will ever render.

    The relaunched page adopts an already-open session on its first poll, so arming
    immediately after this still produces a dialog.
    """
    mod = _server_mod
    if mod is not None and mod.window_alive():
        return
    if _app_running():
        try:
            subprocess.run(['pkill', '-x', APP_PROC], timeout=1)
        except Exception:
            pass
        # WAIT for it to actually go. Relaunching while the old process is still dying
        # produces an app that is running by pgrep but whose page never comes up — which
        # is the exact wedged state this function exists to clear, so racing here just
        # recreates it. Observed twice with a ~1 s gap; fine with 2 s.
        # Bounded, and only ever reached on the already-broken path.
        for _ in range(40):
            if not _app_running():
                break
            time.sleep(0.05)
    _launch_app()


def _kill_app():
    try:
        subprocess.Popen(['pkill', '-x', APP_PROC])
    except Exception:
        pass


# ── Reading the model ────────────────────────────────────────────────────────
def _selected_dimension(design):
    """The sketch dimension the hotkey should act on, or None.

    Selection-driven and nothing else. A "most recently created dimension" fallback was
    considered and left out on purpose: pressing the key with nothing selected would
    then silently edit some dimension elsewhere in the sketch, which is exactly the kind
    of surprise this is supposed to remove.
    """
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            try:
                ent = sel.item(i).entity
                if isinstance(ent, adsk.fusion.SketchDimension):
                    return ent
            except Exception:
                pass
    except Exception:
        pass
    return None


def _sketch_dir_to_world(sketch, vx, vy):
    """A direction given in sketch coordinates, expressed in world coordinates."""
    try:
        (_origin, xa, ya, _za) = sketch.transform.getAsCoordinateSystem()
        return adsk.core.Vector3D.create(xa.x * vx + ya.x * vy,
                                         xa.y * vx + ya.y * vy,
                                         xa.z * vx + ya.z * vy)
    except Exception:
        return None


def _point_of(ent):
    """The sketch-space Point3D of a dimensioned entity, if it has an obvious one."""
    try:
        if isinstance(ent, adsk.fusion.SketchPoint):
            return ent.geometry
        if isinstance(ent, adsk.fusion.SketchLine):
            return ent.startSketchPoint.geometry
    except Exception:
        pass
    return None


def _position_of(ent):
    """Where a dimensioned entity SITS, in sketch space.

    A line's MIDPOINT rather than an end, so which side of the origin the dimension is
    happening on does not depend on which direction the line happened to be drawn in.
    """
    try:
        if isinstance(ent, adsk.fusion.SketchPoint):
            return ent.geometry
        if isinstance(ent, adsk.fusion.SketchLine):
            g = ent.geometry
            return adsk.core.Point3D.create(
                (g.startPoint.x + g.endPoint.x) / 2.0,
                (g.startPoint.y + g.endPoint.y) / 2.0,
                (g.startPoint.z + g.endPoint.z) / 2.0)
        if isinstance(ent, (adsk.fusion.SketchCircle, adsk.fusion.SketchArc)):
            return ent.centerSketchPoint.geometry
    except Exception:
        pass
    return None


def _line_dir(ent):
    """(dx, dy) along a sketch line, unnormalised, or None if it is not a line."""
    try:
        if isinstance(ent, adsk.fusion.SketchLine):
            g = ent.geometry
            return (g.endPoint.x - g.startPoint.x, g.endPoint.y - g.startPoint.y)
    except Exception:
        pass
    return None


def _sketch_point_to_world(sketch, p):
    """A sketch-space point in world coordinates."""
    try:
        q = adsk.core.Point3D.create(p.x, p.y, p.z)
        q.transformBy(sketch.transform)
        return q
    except Exception:
        return None


def _measure_dir(dim):
    """(vx, vy) in SKETCH space: the direction this dimension actually measures along.

    NOT the vector between the two picked entities — that is only the same thing when
    both of them are points. Fusion measures a point-to-line distance PERPENDICULAR to
    the line, so a dimension from the origin to a horizontal line runs vertically no
    matter how far along that line the pick landed. Taking the vector between the picks
    there would report the line's own direction, which is the one axis the dimension is
    certainly NOT measuring.
    """
    try:
        if isinstance(dim, adsk.fusion.SketchOffsetDimension):
            # Distance between two parallel lines: measured perpendicular to them.
            d = _line_dir(dim.line)
            return (-d[1], d[0]) if d else None

        if isinstance(dim, adsk.fusion.SketchLinearDimension):
            o = dim.orientation
            if o == adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation:
                return (1.0, 0.0)
            if o == adsk.fusion.DimensionOrientations.VerticalDimensionOrientation:
                return (0.0, 1.0)

            # Aligned: whatever it measures between the two picks.
            e1, e2 = dim.entityOne, dim.entityTwo
            d1, d2 = _line_dir(e1), _line_dir(e2)
            d = d1 or d2
            if d is not None:                    # a line is involved: perpendicular to it
                return (-d[1], d[0])
            p1, p2 = _point_of(e1), _point_of(e2)
            if p1 is not None and p2 is not None:
                return (p2.x - p1.x, p2.y - p1.y)
    except Exception:
        pass
    return None                                  # radial / angular / unhandled


# Fusion's internal length unit is centimetres, so this is a micron: far below anything
# that could be a deliberate offset, far above floating-point noise.
_END_TOL = 1e-4


def _end_of(dim, axis):
    """'high' | 'low' | '' — which side of the origin along `axis` this is happening on.

    The dimensioned entity FURTHEST from the origin decides, not the midpoint of the
    two: dimensioning a line at +700 back to the origin is a question about the high
    end of Y, and averaging the two picks would only dilute that.

    Phase 1 asks nothing more than which side of zero. Once components carry bounds this
    becomes the sharper question Josh actually posed — nearer the back bound or the front
    one — which differs from the sign of the coordinate whenever the bounds are lopsided.
    """
    if not axis:
        return ''
    try:
        sketch = dim.parentSketch
        if sketch is None:
            return ''
        idx = {'X': 0, 'Y': 1, 'Z': 2}[axis]
        best, seen = 0.0, False
        for attr in ('entityOne', 'entityTwo', 'line'):
            try:
                ent = getattr(dim, attr, None)
                if ent is None:
                    continue
                p = _position_of(ent)
                if p is None:
                    continue
                w = _sketch_point_to_world(sketch, p)
                if w is None:
                    continue
                v = (w.x, w.y, w.z)[idx]
                seen = True
                if abs(v) > abs(best):
                    best = v
            except Exception:
                continue
        if not seen or abs(best) < _END_TOL:
            return ''
        return 'high' if best > 0 else 'low'
    except Exception:
        return ''


def _datum_of(dim, axis):
    """'zero' when one side of the dimension sits ON the origin of `axis`, else ''.

    The DATUM is what the dimension is measured FROM, and it decides which parameters
    share its frame of reference: a dimension starting at the origin wants values
    measured from the origin, not values measured in from a bound. See
    conflictsWithFrame() in the window.

    Returns 'zero' when a side sits on the origin, or the name of a bound face —
    'front', 'back', 'left', 'right', 'top', 'bottom' — when it sits on one of those.
    A component with no bounds record has no faces to sit on, so this can only ever say
    'zero' there, and the window does no frame filtering. That is the behaviour that was
    correct before bounds existed, which is why nothing changes until they are set.

    The origin is checked FIRST. A bound can legitimately sit at zero (a component whose
    front face is the world origin), and in that case the dimension really is measured
    from the origin — every origin-framed parameter is meaningful there, whereas reading
    it as a bound datum would filter them all out.
    """
    if not axis:
        return ''
    try:
        coords = _axis_coords(dim, axis)
        for v in coords:
            if abs(v) < _END_TOL:
                return 'zero'
        for v in coords:
            face = _face_at(dim, axis, v)
            if face:
                return face
    except Exception:
        pass
    return ''


def _datum_faces(dim, axis):
    """The bound faces sitting where the dimension's ORIGIN side is — [] unless the
    datum is 'zero' and a face of the component is at zero too.

    _datum_of checks the origin first, so a dimension starting on a face that happens to
    sit at zero reads as 'zero' and the face is lost. It is BOTH datums at once, and a
    parameter measured in from that face (an Inset from Top) is as right there as an
    origin-framed one — the window must not filter either out.
    """
    if not axis:
        return []
    try:
        out = []
        for v in _axis_coords(dim, axis):
            if abs(v) < _END_TOL:
                face = _face_at(dim, axis, v)
                if face and face not in out:
                    out.append(face)
        return out
    except Exception:
        return []


def _axis_coords(dim, axis):
    """World coordinates along `axis` of the dimension's entities."""
    sketch = dim.parentSketch
    if sketch is None:
        return []
    idx = {'X': 0, 'Y': 1, 'Z': 2}[axis]
    coords = []
    for attr in ('entityOne', 'entityTwo', 'line'):
        try:
            ent = getattr(dim, attr, None)
            if ent is None:
                continue
            p = _position_of(ent)
            if p is None:
                continue
            w = _sketch_point_to_world(sketch, p)
            if w is None:
                continue
            coords.append((w.x, w.y, w.z)[idx])
        except Exception:
            continue
    return coords


def _face_at(dim, axis, v):
    """The bound face of the dimension's own component at world `v` on `axis`, or ''."""
    if bounds is None:
        return ''
    sketch = dim.parentSketch
    comp = sketch.parentComponent if sketch is not None else None
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None or comp is None:
        return ''
    return bounds.face_at(design, comp, axis, v)


def _symmetric_axis(dim, axis):
    """Is the measured axis symmetric on the component this dimension lives in?

    It decides two things in the window. The span words (Width/Height/Depth) are only
    part of the vocabulary on a symmetric axis; and on one, the two faces are the same
    distance away, so "from the right" and "from the width" name the SAME bound and must
    not be read as two different frames.
    """
    if not axis or bounds is None:
        return False
    try:
        sketch = dim.parentSketch
        if sketch is None:
            return False
        design = adsk.fusion.Design.cast(app.activeProduct)
        return bool(bounds.is_symmetric(design, sketch.parentComponent, axis))
    except Exception:
        return False


# cos(45°): a dimension's direction is "within 45° of" an axis when that axis's own
# component is at least this — 45° itself counts (the window's ParamNaming.fits()
# reads the boundary as belonging to both neighbouring axes). A small negative
# epsilon keeps an exact 45° dimension from missing both pools to floating-point
# rounding.
_POOL_COS45 = math.cos(math.pi / 4) - 1e-9


def _situation_of(dim):
    """(axis, end, datum, axes, ends, dir_known) — the world axis this dimension
    measures NEAREST along (axis/end/datum, unchanged from before — the old-convention
    family resolver reads these three and no more), which side of the origin it sits
    on, and what it is measured FROM; axes/ends are the FULLER answer the structured
    convention's 45°-pool matching wants — every axis the direction is within 45° of
    (45° itself included, so an exactly-diagonal dimension is in both neighbouring
    pools), each with its own end. Any may be '' / [] / {} when untellable.

    This is the SITUATION the autocomplete resolves a parameter family against: given
    (Y, high) the family EdgeOffset resolves to EdgeBackOffsetY. See fusion_ui/naming.py.
    The structured convention (fusion_ui/param_naming.py) reads axes/ends/datum instead,
    through ParamNaming.fits() — window-side, see fusion_value's namingSituation().

    Getting it wrong costs a wrong suggestion, never a wrong commit — what the family
    resolves to is written into the box as its real name, where it can be read and
    corrected. So '' ("no opinion") is always a safe answer, and the single-axis
    threshold below is deliberately tight: a dimension running at 30 degrees to
    everything is not "along X" for the OLD convention's purposes, and guessing there
    would rank a whole family wrongly. The 45°-pool threshold is a different, wider
    question — "which axes is this closer to than the others" — answered independently.
    """
    try:
        sketch = dim.parentSketch
        if sketch is None:
            return '', '', '', [], {}, False
        d = _measure_dir(dim)
        if d is None:
            return '', '', '', [], {}, False
        vx, vy = d
        n = (vx * vx + vy * vy) ** 0.5
        if n < 1e-9:
            return '', '', '', [], {}, False
        world = _sketch_dir_to_world(sketch, vx / n, vy / n)
        if world is None:
            return '', '', '', [], {}, False

        comps = (('X', abs(world.x)), ('Y', abs(world.y)), ('Z', abs(world.z)))
        best = max(comps, key=lambda c: c[1])
        axis = best[0] if best[1] >= 0.9 else ''
        axes = [a for a, v in comps if v >= _POOL_COS45]
        ends = {a: _end_of(dim, a) for a in axes}
        datum = _datum_of(dim, axis)
        return axis, _end_of(dim, axis), datum, axes, ends, True
    except Exception:
        return '', '', '', [], {}, False


def _owning_occurrence(design, sketch):
    """The occurrence whose component owns `sketch`, or None for the root component.

    Prefers the ACTIVE occurrence when it is the right component, so that with several
    instances of the same component the parameter is filed under the one being worked
    in. design.activeOccurrence raises while a sketch nested in an occurrence is open
    for edit, hence the guard.
    """
    try:
        comp = sketch.parentComponent
        if comp is None or comp == design.rootComponent:
            return None
        try:
            occ = design.activeOccurrence
            if occ is not None and occ.component == comp:
                return occ
        except Exception:
            pass
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        return occs.item(0) if occs and occs.count else None
    except Exception:
        return None


def _doc_key():
    """Stable per-document key: the dataFile id, or a name-based one when unsaved.

    The same key the Browser and Params windows file their per-document records
    under. It travels with the parameter list because the tag store is keyed by it:
    two documents can each have a "Thickness" and mean a different axis by it.
    """
    try:
        doc = app.activeDocument
        if not doc:
            return ''
        try:
            df = doc.dataFile
            if df and df.id:
                return df.id
        except Exception:
            pass
        return '__unsaved__:' + (doc.name or '')
    except Exception:
        return ''


# The parameter objects and which of them are driven links, kept between snapshots.
# userParameters.item(i) recounts the whole collection on every call, so a fresh walk of
# 500+ parameters is quadratic — and the snapshot was rebuilt on every 70 ms tick while
# the dialog was up, which beachballed the viewport through a whole Create Sketch plane
# pick (2026-10-09). The objects survive edits and renames (values are read fresh off
# them); an add or delete shows in the count or isValid. A link verdict is keyed on the
# name and expression it was judged from.
_pcache = {'key': None, 'objs': [], 'links': {}}


def _user_param_objs(design):
    ups = design.userParameters
    key = (_doc_key(), ups.count)
    c = _pcache
    if key == c['key']:
        try:
            if all(p.isValid for p in c['objs']):
                return c['objs']
        except Exception:
            pass
    c['key'], c['objs'], c['links'] = key, list(ups), {}
    return c['objs']


def _params_snapshot(design):
    out = []
    try:
        links = _pcache['links']
        for p in _user_param_objs(design):
            try:
                row = {'name': p.name, 'expression': p.expression,
                       'unit': p.unit, 'comment': p.comment or ''}
                # A driven-dimension link's expression is its case-flipped twin, which
                # reads as the parameter naming itself: the window shows `measured`.
                lk = (row['name'], row['expression'])
                if lk not in links:
                    links[lk] = drivenlink.linked_dimension(design, p) is not None
                if links[lk]:
                    row['linked'] = True
                    row['measured'] = drivenlink.measured_text(design, p)
                out.append(row)
            except Exception:
                pass
    except Exception:
        pass
    return out


# ── Opening a session ────────────────────────────────────────────────────────
def _open_dialog(created=False, dim=None):
    """Arm a dialog session for the selected dimension (or `dim`). Runs on the main
    thread.

    `created` says the dimension is being placed right now rather than edited after the
    fact — the dialog brought it into being, so cancelling has to take it away again.
    Only the inline-box veto can know that; see HotkeyStartingHandler.

    A DRIVEN dimension opens the same box, but its value is the geometry's to set: the
    name is required and commit links it (Driven Link.py) instead of writing a value.
    """
    mod = _server_mod
    if mod is None:
        return
    global _session
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        if dim is None:
            dim = _selected_dimension(design)
        if dim is None:
            # Nothing to edit. Deliberately silent: a message box here would be a modal
            # interruption for a key that was simply pressed at the wrong moment.
            return
        driven = not dim.isDriving if hasattr(dim, 'isDriving') else False

        param = dim.parameter
        sketch = dim.parentSketch
        occ = _owning_occurrence(design, sketch)
        context = (occ.component.name if occ is not None
                   else (design.rootComponent.name if design.rootComponent else ''))
        if sketch is not None:
            context = '%s / %s' % (context, sketch.name) if context else sketch.name

        _axis, _end, _datum, _axes, _ends, _dir_known = _situation_of(dim)
        _symmetric = _symmetric_axis(dim, _axis)
        # The dimension's own component (not the occurrence, which has no bounds of its
        # own) — the name stepper's auto-prefixed first feature level, and the per-axis
        # symmetry that decides whether Inset offers a low-end anchor word.
        _param_comp = occ.component if occ is not None else design.rootComponent
        _param_comp_name = _owner_name(design, _param_comp)
        _param_symmetry = ({a: bounds.is_symmetric(design, _param_comp, a)
                            for a in bounds.AXES}
                           if bounds is not None and _param_comp is not None else {})

        _session = {
            'mode': 'dim',
            'token': dim.entityToken,
            'unit': param.unit if param else '',
            'occToken': occ.entityToken if occ is not None else '',
            # The component's id as well: the Browser files the parameter under THAT,
            # since an occurrence token dies the moment the component is reparented.
            'compId': _comp_id_of(occ),
            # Placed just now, so cancel deletes it — see _pending_delete.
            'created': bool(created),
            # Commit links it rather than writing a value — see _write_dimension.
            'driven': driven,
        }

        _ensure_window()
        mod.open_session(
            expression=(param.expression if param else ''),
            unit=(param.unit if param else ''),
            dim_name=(param.name if param else 'dimension'),
            axis=_axis, end=_end, datum=_datum, symmetric=_symmetric,
            datum_faces=(_datum_faces(dim, _axis) if _datum == 'zero' else []),
            axes=_axes, ends=_ends, dir_known=_dir_known,
            context=context,
            is_driven=driven,
            params=_params_snapshot(design),
            mode='dim', place='cursor', name_required=driven,
            component=_param_comp_name, symmetry=_param_symmetry,
            component_path=_component_path(design, occ),
            components=_all_component_names(design),
            created=bool(created),
            proposal=_proposal(design, dim, _axis, _datum, _param_comp),
            flex=_flex_of(design, param),
        )
    except Exception:
        _session = None
        if mod:
            mod.set_debug(last_error=traceback.format_exc())


# ── What a new name should be ────────────────────────────────────────────────
# The notes: "it asks for the name of the feature — it doesn't need to know whether
# it's an inset or an outset, or the direction, or the anchor; it already knows."
# So the stepper is handed a type and a direction and only asks for the feature word.
#   measured in from a bound face          -> Inset, anchored to that face
#   measured out from the origin           -> Outset (symmetric axis) / Offset, on its axis
#   between two things, through material   -> Thickness, no axis
#   between two things, across empty space -> Span, on its axis
# Through material or not is a point-in-body test at the middle of what is measured
# (Section.is_material), against the section's captured bodies — so it still works in a
# sketch, where every body is rolled back.

def _measured_ends(dim):
    """The two world points a dimension measures between, or None."""
    sketch = dim.parentSketch
    pts = []
    for attr in ('entityOne', 'entityTwo', 'line'):
        try:
            ent = getattr(dim, attr, None)
        except Exception:
            ent = None
        if ent is None:
            continue
        p = _position_of(ent)
        w = _sketch_point_to_world(sketch, p) if p is not None else None
        if w is not None:
            pts.append((w.x, w.y, w.z))
    return pts[:2] if len(pts) >= 2 else None


def _proposal(design, dim, axis, datum, comp):
    """{type, anchors, axes} for a dimension being named, or None (the stepper asks)."""
    try:
        if not axis:
            return None
        if datum and datum != 'zero' and bounds is not None and datum in bounds.DIRECTIONS:
            return {'type': 'Inset', 'anchors': [datum.capitalize()], 'axes': []}
        if datum == 'zero':
            sym = bounds is not None and comp is not None and bounds.is_symmetric(design, comp, axis)
            return {'type': 'Outset' if sym else 'Offset', 'anchors': [], 'axes': [axis]}
        ends = _measured_ends(dim)
        if section is None or ends is None:
            return None
        mid = tuple((ends[0][i] + ends[1][i]) / 2.0 for i in range(3))
        if section.is_material(design, mid):
            return {'type': 'Thickness', 'anchors': [], 'axes': []}
        return {'type': 'Span', 'anchors': [], 'axes': [axis]}
    except Exception:
        return None


def _flex_of(design, param):
    """The flexible range the box opens showing: the user parameter the dimension is
    set to, if it is set to exactly one, else the dimension's own."""
    if section is None or param is None:
        return None
    try:
        target = design.userParameters.itemByName((param.expression or '').strip()) or param
        return section.read_flex(target)
    except Exception:
        return None


def _axis_of_param(design, p):
    """The world axis a user parameter's dimensions measure along, or '' — read off the
    first sketch dimension that uses it."""
    try:
        for dp in p.dependentParameters:
            try:
                dim = adsk.fusion.SketchDimension.cast(dp.createdBy)
            except Exception:
                dim = None
            if dim is None:
                continue
            ax = _situation_of(dim)[0]
            if ax:
                return ax
    except Exception:
        pass
    return ''


def _open_link_dialog():
    """Link Driven Dimension's key: the value box on the selected DRIVEN dimension.
    Main thread. Nothing selected: nothing happens. Something selected that is not a
    driven dimension: the HUD cross, which says why when clicked."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        return
    try:
        sel = ui.activeSelections
        ents = [sel.item(i).entity for i in range(sel.count)]
    except Exception:
        ents = []
    if not ents:
        return
    dim = drivenlink.driven_dimension(ents)
    if dim is None:
        picked_a_dim = any(adsk.fusion.SketchDimension.cast(e) for e in ents)
        _hud('fail',
             'That dimension is driving, not driven.\n\nIt already is a parameter, so '
             'other expressions can reference it as it stands. Toggle it to a driven '
             '(reference) dimension if you want it to follow the geometry instead.'
             if picked_a_dim else
             'Link Driven Dimension: select a driven (reference) dimension.')
        return
    _open_dialog(dim=dim)


def _open_edit_param_dialog(design, name, filed=None):
    """Arm a session that edits the user parameter `name`. Main thread.

    Reached from the Browser window (Enter / F on a parameter row), which posts
    {'action': 'openparam'} to this add-in's server rather than opening its own inline
    box. Both fields come prefilled: the value is what you came to change, and the name
    is there so the same gesture can rename it.
    """
    mod = _server_mod
    global _session
    try:
        p = design.userParameters.itemByName(name)
        if p is None:
            return
        _comp = design.activeComponent or design.rootComponent
        _comp_name = _owner_name(design, _comp)
        _comp_symmetry = ({a: bounds.is_symmetric(design, _comp, a) for a in bounds.AXES}
                          if bounds is not None and _comp is not None else {})

        # A driven-dimension link opens like the driven dimension itself: its value is
        # the geometry's, shown to read (the measurement, not the twin's name it is
        # literally made of), and the commit renames it only — see _do_commit_edit_param.
        linked = drivenlink.linked_dimension(design, p) is not None
        _session = {'mode': 'editparam', 'name': name, 'unit': p.unit or '',
                    'linked': linked}
        _ensure_window()
        mod.open_session(
            expression=(drivenlink.measured_text(design, p) if linked
                        else (p.expression or '')),
            unit=(p.unit or ''),
            dim_name='', axis='', end='', datum='', symmetric=False, context='',
            is_driven=linked,
            params=_params_snapshot(design),
            mode='editparam', place='center', name_required=True, name=name,
            component=_comp_name, symmetry=_comp_symmetry,
            component_path=_component_path(design, _active_occurrence(design)),
            components=_all_component_names(design),
            filed=filed,
            flex=(section.read_flex(p) if section is not None else None),
        )
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())


def _do_retarget_param(design, name):
    """Swing the already-open session onto an existing parameter, picked from the
    Name field's own autosuggest. Main thread.

    Unlike _open_edit_param_dialog this touches only `_session` — the window is
    already up and has already put the parameter's name and expression in its own
    fields, so there is nothing to push back. It only has to be told which
    parameter a COMMIT should now land on, since _do_commit_edit_param reads that
    from here rather than from anything the commit itself carries.

    Silently does nothing if the parameter is not found (a recompute since the
    list was fetched, say) — the dialog is left as it was rather than losing
    whatever was typed over a session it cannot arm.
    """
    global _session
    try:
        p = design.userParameters.itemByName(name)
        if p is None:
            return
        _session = {'mode': 'editparam', 'name': name, 'unit': p.unit or '',
                    'linked': drivenlink.linked_dimension(design, p) is not None}
    except Exception:
        mod = _server_mod
        if mod is not None:
            mod.set_debug(last_error=traceback.format_exc())


def _open_new_param_dialog():
    """Arm a session that makes a user parameter and nothing else. Main thread.

    Takes no selection and needs no document state beyond a design being open, which is
    the point: it is the "I want a parameter" key, pressable at any moment.
    """
    mod = _server_mod
    if mod is None:
        return
    global _session
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        _comp = design.activeComponent or design.rootComponent
        _comp_name = _owner_name(design, _comp)
        _comp_symmetry = ({a: bounds.is_symmetric(design, _comp, a) for a in bounds.AXES}
                          if bounds is not None and _comp is not None else {})

        _session = {'mode': 'param'}
        _ensure_window()
        mod.open_session(
            expression='',
            unit='',                       # inferred from the expression at commit time
            dim_name='',
            axis='',
            end='',
            datum='',
            symmetric=False,
            context='',
            is_driven=False,
            params=_params_snapshot(design),
            mode='param', place='center', name_required=True,
            component=_comp_name, symmetry=_comp_symmetry,
            component_path=_component_path(design, _active_occurrence(design)),
            components=_all_component_names(design),
        )
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())


# ── Applying what the window posted back ─────────────────────────────────────
def _post_ui_action(action_id, arg):
    """Hand an action to the Browser window. Best-effort by design.

    With the dock closed there is no window to run it, and a parameter simply lands
    unfiled at the top level — which is exactly where a parameter created any other way
    starts out, so nothing is worse than the status quo.
    """
    try:
        body = json.dumps({'id': action_id, 'arg': arg}).encode('utf-8')
        req = urllib.request.Request(BROWSER_URL + '/uiaction', data=body,
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=0.4).read()
    except Exception:
        pass


def _component_path(design, occ):
    """Component names from the root down to `occ`'s component — every component that
    could own a new parameter, for the name stepper's owner dropdown. Just the root when
    `occ` is None (the root is active). Never raises: a path it cannot walk is cut
    short, not lost."""
    names = []
    try:
        o = occ
        while o is not None:
            names.append(o.component.name)
            o = o.assemblyContext
    except Exception:
        pass
    try:
        root = ROOT_OWNER if design.rootComponent else ''
    except Exception:
        root = ''
    if root:
        names.append(root)
    names.reverse()
    return names


def _all_component_names(design):
    """Every component in the design, root first, then the rest by name — what the
    name stepper's owner search offers. Never raises."""
    try:
        root = design.rootComponent
        top = root.name if root else ''
        names = sorted({c.name for c in design.allComponents if c.name != top},
                       key=str.lower)
        return ([ROOT_OWNER] if root else []) + names
    except Exception:
        return []


def _active_occurrence(design):
    try:
        return design.activeOccurrence
    except Exception:
        return None


def _comp_id_of(occ):
    try:
        return occ.component.id if occ is not None else ''
    except Exception:
        return ''


def _file_param_in_browser(name, occ_token, comp_id=''):
    """File the new parameter beside the component the dimension's sketch lives in.

    The container is named here because the add-in is the side that knows which sketch
    the dimension belonged to; the window does not. Named by component id — the token
    is kept for an older window, and is what the window falls back to without an id.
    """
    _post_ui_action(FILE_PARAM_ID, {'name': name, 'token': occ_token, 'compId': comp_id})


def _place_param_in_browser(name, expression, unit):
    """File a from-nothing parameter wherever the browser tree says it belongs.

    The opposite of the above: here the add-in knows nothing useful and the WINDOW picks
    — the open group, the one holding the selection, the last one opened, or failing all
    of that the component you are in. The expression and unit ride along so the row can
    appear on the keystroke rather than on the Parameters window's next poll.
    """
    _post_ui_action(PLACE_PARAM_ID,
                    {'name': name, 'expression': expression, 'unit': unit})


# The three units the Params window offers, in the order a bare expression is tried
# against them. Length first because that is what almost every parameter is; '' last
# because a unitless parameter is the one thing no expression can be mistaken for.
def _infer_unit(design, expr):
    """The unit to create a parameter in, judged from what was typed.

    There is no unit picker on this dialog (it has two fields and that is the point), so
    the unit is whichever of the three Fusion will actually accept the expression in.
    "12" and "Width/2" come out as length, "30 deg" as an angle, "Count + 1" as a plain
    number — each because it is the first one that parses.
    """
    um = design.unitsManager
    for unit in (um.defaultLengthUnits, 'deg', ''):
        try:
            if um.isValidExpression(expr, unit):
                return unit
        except Exception:
            pass
    return None


def _resolve(design, token):
    """The entity a token names, or None.

    findEntityByToken hands back a BaseVector — sized and indexable, but neither a list
    nor a tuple, so an isinstance check against those quietly treats the container
    itself as the entity and every commit fails with "no longer available". Duck-typing
    on len()/[0] is what actually matches the API.
    """
    try:
        found = design.findEntityByToken(token)
    except Exception:
        return None
    if found is None:
        return None
    try:
        return found[0] if len(found) else None
    except TypeError:
        return found            # already a bare entity


def _do_preview(design, expr, unit):
    mod = _server_mod
    if mod is None:
        return
    try:
        um = design.unitsManager
        # A dimension names its own unit. A from-nothing parameter has none yet, so the
        # preview is evaluated in the same unit the commit would create it in — which is
        # what makes "30 deg" read back as an angle rather than as nothing at all.
        u = unit if unit else _infer_unit(design, expr)
        if u is None or not um.isValidExpression(expr, u):
            mod.set_preview(expr, '')
            return
        mod.set_preview(expr, um.formatInternalValue(um.evaluateExpression(expr, u), u, True))
    except Exception:
        mod.set_preview(expr, '')


def _do_commit_new_param(design, expr, name):
    """Create a user parameter from what was typed, and let the browser file it.

    The name is not optional here — without one there is nothing to make — but the
    window refuses an empty or unusable name before it ever gets this far, so reaching
    these checks means the name went bad between typing it and committing it (another
    parameter took it on a recompute, say).
    """
    mod = _server_mod
    global _session
    if not name:
        mod.set_error('Give the parameter a name.')
        return
    if not NAME_RE.match(name):
        mod.set_error('"%s" is not a valid parameter name.' % name)
        return
    if design.allParameters.itemByName(name) is not None:
        mod.set_error('A parameter named "%s" already exists.' % name)
        return

    unit = _infer_unit(design, expr)
    if unit is None:
        mod.set_error('"%s" is not a valid expression.' % expr)
        return
    made = []
    try:
        # "Shelf + 10": the 10 is a number parameter. A plain "10" is this parameter's
        # own value — it already is one.
        expr, _left, made = numbers.convert(design, expr, unit, keep_whole=True)
        design.userParameters.add(
            name, adsk.core.ValueInput.createByString(expr), unit, '')
    except Exception as e:
        numbers.drop(design, made)
        mod.set_error('Could not create "%s": %s' % (name, e))
        return

    _place_param_in_browser(name, expr, unit)
    _session = None
    mod.close_session()


def _do_make_param(design, expr, name):
    """"Make a parameter from this": a new user parameter out of part of the value box.

    The same checks and the same creation as _do_commit_new_param, but the session is
    left exactly as it was — the window still has an expression to finish, and it puts
    the new name into it in place of what was selected once it hears back (set_made).
    The fresh parameter list travels in the same answer, so the name is already a chip
    by the time it lands in the box rather than a word waiting for the next tick.

    Not behind the Extrude or a feature panel: the server routes those to the live
    command, whose next preview would roll a parameter made here away (Made Params.py).
    """
    mod = _server_mod
    # A number chip given a name: the new parameter takes its value, and the number
    # parameter goes once nothing names it (the commit's leftovers, Number Params.py).
    if numbers.is_number_name((expr or '').strip()):
        p = design.userParameters.itemByName(expr.strip())
        if p is not None:
            expr = p.expression
    unit, why = madeparams.check(design, name, expr)
    if why:
        mod.set_error(why)
        return
    made = []
    try:
        expr, _left, made = numbers.convert(design, expr, unit, keep_whole=True)
        design.userParameters.add(
            name, adsk.core.ValueInput.createByString(expr), unit, '')
    except Exception as e:
        numbers.drop(design, made)
        mod.set_error('Could not create "%s": %s' % (name, e))
        return
    _place_param_in_browser(name, expr, unit)
    mod.set_made(name, _params_snapshot(design))


def _do_edit_made(design, name, expr, new_name):
    """"Edit parameter" from a chip: apply it now (no command of ours is previewing),
    tell the Browser, and answer the way a made parameter does — the session carries on,
    the window swaps the new name into its fields (set_made's `edited`)."""
    mod = _server_mod
    why = madeparams.check_edit(design, name, expr, new_name)
    if why:
        mod.set_error(why)
        return
    try:
        madeparams.apply_edit(design, name, expr, new_name)
    except Exception as e:
        mod.set_error('Could not edit "%s": %s' % (name, e))
        return
    madeparams.notify_browser_edit(name, new_name, expr)
    mod.set_made(new_name or name, _params_snapshot(design),
                 {'old': name, 'new': new_name})


def _do_make_info(design):
    """The active component, its path and every component — what the window's name
    stepper offers as the parent of a parameter made from a selection."""
    mod = _server_mod
    try:
        comp = design.activeComponent or design.rootComponent
        symmetry = ({a: bounds.is_symmetric(design, comp, a) for a in bounds.AXES}
                    if bounds is not None and comp is not None else {})
        mod.set_make_info({
            'component': _owner_name(design, comp),
            'componentPath': _component_path(design, _active_occurrence(design)),
            'components': _all_component_names(design),
            'symmetry': symmetry,
        })
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())


def _set_param_via_params_window(name, expression, new_name=''):
    """Post an edit to the Parameters add-in's server, which owns userParameters.

    Returns True if it was accepted for application. It applies on that add-in's own
    tick a moment later — the same deal the Browser's inline edit has always had, and
    why the row is updated optimistically rather than waited for.
    """
    body = {'name': name}
    if expression is not None:            # None: a link's rename — see _do_commit_edit_param
        body['expression'] = expression
    if new_name and new_name != name:
        body['newName'] = new_name
    try:
        req = urllib.request.Request(PARAMS_URL + '/set',
                                     data=json.dumps(body).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        return b'"ok": true' in urllib.request.urlopen(req, timeout=0.6).read().lower()
    except Exception:
        return False


def _do_commit_edit_param(design, expr, name):
    """Apply the dialog to the parameter it was opened on.

    Everything checkable is checked HERE, on the main thread with the Fusion API to
    hand, and only then handed to the Parameters window — so a bad expression or a
    taken name keeps the dialog open with the reason, instead of being swallowed a
    tick later by an add-in that has no way to tell this window about it.
    """
    mod = _server_mod
    global _session
    old_name = _session.get('name', '')
    p = design.userParameters.itemByName(old_name)
    if p is None:
        mod.set_error('The parameter "%s" no longer exists.' % old_name)
        return

    if not name:
        mod.set_error('Give the parameter a name.')
        return
    renaming = name != old_name
    if renaming:
        if not NAME_RE.match(name):
            mod.set_error('"%s" is not a valid parameter name.' % name)
            return
        if design.allParameters.itemByName(name) is not None:
            mod.set_error('A parameter named "%s" already exists.' % name)
            return

    # A driven-dimension link: what the box showed was the measurement, and writing it
    # back would cut the link. Only the name is the commit; the expression stays the
    # twin's (Parameters renames the twin with it — flip_case).
    linked = bool(_session.get('linked')) and \
        drivenlink.linked_dimension(design, p) is not None
    if linked:
        expr = (p.expression or '').strip()
    unit = _session.get('unit') or ''
    try:
        if not design.unitsManager.isValidExpression(expr, unit or
                                                     design.unitsManager.defaultLengthUnits):
            mod.set_error('"%s" is not a valid expression.' % expr)
            return
    except Exception:
        mod.set_error('"%s" is not a valid expression.' % expr)
        return

    if not _set_param_via_params_window(old_name, None if linked else expr, name):
        mod.set_error('The Parameters window would not take that edit.')
        return

    _post_ui_action(EDITED_PARAM_ID, {'name': old_name,
                                      'newName': name if renaming else '',
                                      'expression': expr})
    _session = None
    mod.close_session()


def _do_commit(design, expr, name, mode='', extra=None):
    """Set the dimension, creating a user parameter first when a name was given.

    `extra` is what the Section Sketch half of the box adds: `flex` ({flexible, min,
    max} — stored on the parameter the dimension ends up set to), `doppel` (the name
    exists already but differs only by axis: both get their axes, unasked) and `axis`
    (this dimension's). Flexible with limits and NO value is a gap limit instead: see
    _commit_gap.

    Every refusal is reported back to the window, which stays open showing it — losing
    a typed expression to a silent failure is the one outcome worth engineering against.

    `mode` is what the WINDOW believes it is showing. It only matters when this add-in
    has forgotten the session (a reload with the dialog open): a parameter needs no
    target to create, so that one can still go through on the window's word alone.
    """
    mod = _server_mod
    global _session
    if mod is None:
        return
    if (_session or {}).get('mode') == 'param' or (not _session and mode == 'param'):
        _do_commit_new_param(design, expr, name)
        _store_flex(design, name, extra)
        return
    if (_session or {}).get('mode') == 'editparam':
        _do_commit_edit_param(design, expr, name)
        _store_flex(design, name, extra)
        return
    if not _session:
        # No target armed, but the window is showing a session — the two got out of step,
        # which is what reloading this add-in with the dialog open does (stop() clears
        # _session; the window knows nothing about it). This used to `return` silently,
        # so Enter simply did nothing at all: no commit, no error, no clue. Say so
        # instead, which is the whole point of the refusal path.
        mod.set_error('This dialog lost track of its dimension (the add-in reloaded). '
                      'Press Esc and try again.')
        return
    try:
        unit = _session.get('unit') or design.unitsManager.defaultLengthUnits

        dim = _resolve(design, _session['token'])
        if dim is None or not isinstance(dim, adsk.fusion.SketchDimension):
            mod.set_error('That dimension is no longer available.')
            return
        param = dim.parameter
        if param is None:
            mod.set_error('That dimension has no editable parameter.')
            return
        extra = extra or {}
        flex = extra.get('flex')
        if not expr and flex and flex.get('flexible', True) and (flex.get('min') or flex.get('max')):
            err = _commit_gap(design, dim, flex)
            if err:
                mod.set_error(err)
                return
            _session = None
            mod.close_session()
            return

        # A driven dimension's value is the geometry's: the box only names it, so the
        # expression is not read at all and the name is the whole commit.
        driven = bool(_session.get('driven'))
        if driven:
            if not name:
                mod.set_error('Give the parameter a name.')
                return
        elif not design.unitsManager.isValidExpression(expr, unit):
            mod.set_error('"%s" is not a valid expression.' % expr)
            return

        reuse = ''
        if name:
            if not NAME_RE.match(name):
                mod.set_error('"%s" is not a valid parameter name.' % name)
                return
            if design.allParameters.itemByName(name) is not None:
                if not extra.get('doppel'):
                    mod.set_error('A parameter named "%s" already exists.' % name)
                    return
                name, reuse, err = _doppelganger(design, name, extra.get('axis', ''))
                if err:
                    mod.set_error(err)
                    return

        # Everything that can be checked has been. If a command is still running, the
        # write has to wait for it to be out of the way — see _pending_write. The
        # session stays armed and the dialog stays up until the write actually lands,
        # so a failure can still be reported into it.
        global _pending_write
        pending = {'token': _session['token'], 'expr': reuse or expr,
                   'name': '' if reuse else name,
                   'unit': unit, 'occToken': _session.get('occToken', ''),
                   'compId': _session.get('compId', ''), 'link': driven,
                   'flex': flex, 'due': time.time() + _WRITE_DELAY}
        running = ui.activeCommand
        if running and running != 'SelectCommand':
            _pending_write = pending
            try:
                # TERMINATE, which commits the command, rather than leaving it for the
                # user to Escape and roll our work back with it.
                ui.terminateActiveCommand()
            except Exception:
                pass
            return

        err = _write_dimension(design, pending)
        if err:
            mod.set_error(err)
            return
        _session = None
        mod.close_session()
    except Exception:
        mod.set_error('Something went wrong applying that value.')
        mod.set_debug(last_error=traceback.format_exc())


# A typed literal zero — "0", "0.0", "0 mm". Only a literal: an expression that merely
# EVALUATES to zero names a parameter, and swapping it for a constraint would cut that
# link without a word.
_ZERO_RE = re.compile(r'^\s*[+-]?(0+(\.0*)?|\.0+)\s*[A-Za-z"\']*\s*$')
# Sketch-space slope below which an aligned dimension counts as running along an axis.
_ORTHO_TOL = 1e-6


def _zero_axis(dim):
    """'x' | 'y' | None — the sketch axis a point-to-point dimension measures along, when
    it is one of the two. Anything else (a line involved, an aligned one at an angle) is
    not a question a zero can answer with a constraint."""
    lin = adsk.fusion.SketchLinearDimension.cast(dim)
    if lin is None:
        return None
    try:
        p1 = adsk.fusion.SketchPoint.cast(lin.entityOne)
        p2 = adsk.fusion.SketchPoint.cast(lin.entityTwo)
        if p1 is None or p2 is None:
            return None
        o = lin.orientation
        if o == adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation:
            return 'x'
        if o == adsk.fusion.DimensionOrientations.VerticalDimensionOrientation:
            return 'y'
        # Aligned: it measures along the line between the two, so it is orthographic
        # only when that line already runs along an axis.
        a, b = p1.geometry, p2.geometry
        dx, dy = abs(b.x - a.x), abs(b.y - a.y)
        if dx < 1e-9 and dy < 1e-9:
            return None
        if dy <= _ORTHO_TOL * dx:
            return 'x'
        if dx <= _ORTHO_TOL * dy:
            return 'y'
    except Exception:
        pass
    return None


def _zero_to_constraint(dim):
    """A zero between two points, turned into the constraint it means. Returns None when
    this is not such a case (write the value as usual), else '' or an error.

        0 along x  →  vertical points     (they share an x)
        0 along y  →  horizontal points   (they share a y)
        0 along the OTHER axis of a pair already locked one way  →  coincident

    A zero dimension is a constraint wearing a number — and Fusion will not hold one
    at zero anyway. The dimension is made driven before the constraint goes in, so the
    two never fight; it is deleted only once the constraint has taken, and made driving
    again if it did not.

    A zero measured from a LINE (an offset dimension — what Smart Dimension makes against
    a bound) goes to _zero_offset_to_constraint.
    """
    off = adsk.fusion.SketchOffsetDimension.cast(dim)
    if off is not None:
        return _zero_offset_to_constraint(off)
    axis = _zero_axis(dim)
    if axis is None:
        return None
    lin = adsk.fusion.SketchLinearDimension.cast(dim)
    p1, p2 = lin.entityOne, lin.entityTwo
    sketch = dim.parentSketch
    gc = sketch.geometricConstraints

    def pair(a, b):
        try:
            return (a == p1 and b == p2) or (a == p2 and b == p1)
        except Exception:
            return False

    # What already holds these two together. A vertical-points lock shares x; horizontal
    # shares y; coincident shares both.
    have = {}
    for i in range(gc.count):
        try:
            c = gc.item(i)
            vp = adsk.fusion.VerticalPointsConstraint.cast(c)
            hp = adsk.fusion.HorizontalPointsConstraint.cast(c)
            co = adsk.fusion.CoincidentConstraint.cast(c)
            if vp is not None and pair(vp.pointOne, vp.pointTwo):
                have['x'] = vp
            elif hp is not None and pair(hp.pointOne, hp.pointTwo):
                have['y'] = hp
            elif co is not None and pair(co.point, co.entity):
                have['both'] = co
        except Exception:
            continue

    other = 'y' if axis == 'x' else 'x'

    def make():
        if 'both' in have or axis in have:
            pass                                    # already true: only the dim goes
        elif other in have:
            # Locked the other way already, and now this way too: that is one point.
            have[other].deleteMe()
            try:
                gc.addCoincident(p1, p2)
            except Exception:
                # Put the lock back rather than leave the pair looser than it was.
                (gc.addVerticalPoints if other == 'x' else gc.addHorizontalPoints)(p1, p2)
                raise
        elif axis == 'x':
            gc.addVerticalPoints(p1, p2)
        else:
            gc.addHorizontalPoints(p1, p2)

    return _swap_for_constraint(dim, make, 'the two points')


def _zero_offset_to_constraint(dim):
    """A zero from a line, turned into the constraint it means:

        point at 0 from a line  →  coincident (on the line or its extension)
        line  at 0 from a line  →  collinear

    Same driven-first / delete-last handling as _zero_to_constraint."""
    try:
        line = adsk.fusion.SketchLine.cast(dim.line)
        ent = dim.entityTwo
    except Exception:
        return None
    pt = adsk.fusion.SketchPoint.cast(ent)
    ln = adsk.fusion.SketchLine.cast(ent)
    if line is None or (pt is None and ln is None):
        return None
    gc = dim.parentSketch.geometricConstraints

    def same(a, b):
        try:
            return a == b
        except Exception:
            return False

    def already():
        for i in range(gc.count):
            try:
                c = gc.item(i)
                co = adsk.fusion.CoincidentConstraint.cast(c)
                cl = adsk.fusion.CollinearConstraint.cast(c)
                if pt is not None and co is not None and \
                        same(co.point, pt) and same(co.entity, line):
                    return True
                if ln is not None and cl is not None and (
                        (same(cl.lineOne, line) and same(cl.lineTwo, ln)) or
                        (same(cl.lineOne, ln) and same(cl.lineTwo, line))):
                    return True
            except Exception:
                continue
        return False

    def make():
        if already():
            return                                  # already true: only the dim goes
        if pt is not None:
            gc.addCoincident(pt, line)
        else:
            gc.addCollinear(ln, line)

    return _swap_for_constraint(dim, make, 'it to the line' if pt is not None
                                else 'the two lines')


def _swap_for_constraint(dim, make, what):
    """Make `dim` driven, run `make` (which adds the constraint), then delete `dim`.
    Returns '' or an error — the dim made driving again if the constraint would not go."""
    try:
        driving = dim.isDriving
        if driving:
            dim.isDriving = False
    except Exception:
        driving = False
    try:
        make()
    except Exception as e:
        try:
            if driving:
                dim.isDriving = True
        except Exception:
            pass
        return 'Could not constrain %s instead: %s' % (what, e)
    try:
        dim.deleteMe()
    except Exception:
        pass                                        # constraint holds; a spare driven dim
    return ''


def _write_dimension(design, job):
    """Apply a validated edit. Returns '' or the reason it could not be applied — and,
    once it has, stores the box's flexible range on whatever the dimension is set to."""
    err = _write_dimension_value(design, job)
    if not err and job.get('flex') is not None and section is not None:
        try:
            dim = _resolve(design, job['token'])
            p = dim.parameter if dim is not None else None
            if p is not None:
                target = design.userParameters.itemByName((p.expression or '').strip()) or p
                f = job['flex']
                section.write_flex(target, f.get('flexible', True), f.get('min', ''), f.get('max', ''))
        except Exception:
            pass
    return err


def _store_flex(design, name, extra):
    """A parameter session's flexible range, onto the parameter it ended up as."""
    f = (extra or {}).get('flex')
    if f is None or section is None or not name:
        return
    try:
        p = design.userParameters.itemByName(name)
        if p is not None:
            section.write_flex(p, f.get('flexible', True), f.get('min', ''), f.get('max', ''))
    except Exception:
        pass


def _doppelganger(design, name, axis):
    """(name to make, existing name to reuse, error). The same measurement named again
    with no axis: the existing one is renamed to carry ITS axis and the new one is made
    with this dimension's — "obviously one is for one axis and one is for the other".
    On the SAME axis it is the same parameter, so the dimension simply uses it."""
    p = design.userParameters.itemByName(name)
    if p is None:
        return name, '', ''
    old_axis = _axis_of_param(design, p)
    axis = (axis or '').upper()
    if not axis or not old_axis or old_axis == axis:
        return '', name, ''
    try:
        renamed = '%s_%s' % (name, old_axis)
        if design.allParameters.itemByName(renamed) is None:
            p.name = renamed        # Fusion rewrites every expression that used it
    except Exception as e:
        return '', '', 'Could not give "%s" its axis: %s' % (name, e)
    new = '%s_%s' % (name, axis)
    if design.allParameters.itemByName(new) is not None:
        return '', new, ''
    return new, '', ''


def _commit_gap(design, dim, flex):
    """Flexible, a limit and no value: the dimension was drawn to MEASURE a gap between
    two things (section references, usually), and the limit is the point. The limit is
    kept as a gap monitor — two parametric Forms, so it outlives the sketch — and the
    dimension goes. Section: Move then refuses to close the gap past it."""
    if section is None:
        return 'The Section Sketch engine is not loaded, so there is nothing to keep the limit.'
    sketch = dim.parentSketch
    try:
        if isinstance(dim, adsk.fusion.SketchOffsetDimension):
            ents = [dim.line, dim.entityTwo]
        else:
            ents = [dim.entityOne, dim.entityTwo]
    except Exception:
        return 'That dimension does not measure between two things.'
    axis = _situation_of(dim)[0]
    if not axis:
        return 'A gap limit needs a dimension square to X, Y or Z.'
    forms = []
    for ent in ents:
        hit = section.on_reference(ent)
        f = None
        if hit is not None:
            curve, rec = hit
            uv = section._geom_of(ent)
            pf = (section._forms_near(rec, uv) if adsk.fusion.SketchPoint.cast(ent)
                  else section._rec_point_forms(rec, 0))
            amap, off = section._target_frame(design, sketch)
            key = section.sg.sketch_axis_of(amap, axis) if amap else None
            if key in ('u', 'v') and pf.get(key) is not None:
                f = section.sg.world_form(amap, key, pf[key], section.Form) + \
                    section.Form.const(off[section.sg.AXES.index(axis)])
        if f is None:
            return ('Both ends of a gap limit must be section references (the model, '
                    'drawn in the sketch).')
        forms.append(f)
    label = 'gap %s–%s' % tuple(section.source_of(section.on_reference(e)[1]) or '?' for e in ents)
    if not section.add_monitor(design, axis, forms[0], forms[1], flex.get('min', ''),
                               flex.get('max', ''), label):
        return 'Could not store the gap limit.'
    try:
        dim.deleteMe()
    except Exception:
        pass
    return ''


def _write_dimension_value(design, job):
    """Apply a validated edit. Returns '' or the reason it could not be applied."""
    dim = _resolve(design, job['token'])
    if dim is None or not isinstance(dim, adsk.fusion.SketchDimension):
        return 'That dimension is no longer available.'
    param = dim.parameter
    if param is None:
        return 'That dimension has no editable parameter.'

    name, expr, unit = job['name'], job['expr'], job['unit']
    if job.get('link'):
        # A driven dimension: name it and alias it (Driven Link.py), then file the alias
        # beside its sketch's component exactly as a parameter made from a driving
        # dimension is — its parsed name's owner pin, in the Browser's structured tree.
        if dim.isDriving:
            return 'That dimension is driving now — its value can be set instead.'
        if design.allParameters.itemByName(name) is not None:
            return 'A parameter named "%s" already exists.' % name
        err = drivenlink.create_link(design, dim, name)
        if err:
            return err
        _file_param_in_browser(name, job.get('occToken', ''), job.get('compId', ''))
        return ''
    if not name and _ZERO_RE.match(expr or ''):
        err = _zero_to_constraint(dim)
        if err is not None:
            return err
    if name:
        if design.allParameters.itemByName(name) is not None:
            return 'A parameter named "%s" already exists.' % name
        old = param.expression
        made = []
        try:
            expr, _left, made = numbers.convert(design, expr, unit, keep_whole=True)
            design.userParameters.add(
                name, adsk.core.ValueInput.createByString(expr), unit, '')
        except Exception as e:
            numbers.drop(design, made)
            return 'Could not create "%s": %s' % (name, e)
        try:
            param.expression = name
        except Exception as e:
            # The parameter was created but the dimension refused it (driven dimension,
            # locked sketch). Say so rather than reporting success — the parameter is
            # left in place, which is recoverable; a silent half-commit is not.
            return ('Parameter "%s" created, but the dimension would not take it: %s'
                    % (name, e))
        # The number parameters the dimension held before, if nothing else uses them.
        numbers.drop(design, numbers.np.names_in(old))
        _file_param_in_browser(name, job.get('occToken', ''), job.get('compId', ''))
        return ''
    # A typed number is a parameter (Number Params.py); one that was a number chip
    # before keeps its parameter, changed.
    why = numbers.set_expression(design, param, expr, unit)
    if why:
        return 'The dimension would not take that value: %s' % why
    return ''


def _apply_pending_write(design):
    """Land a write that was waiting for the running command to end."""
    global _pending_write, _session
    job = _pending_write
    if job is None:
        return
    if time.time() < job['due']:
        return
    # Still inside a command? Give it another tick rather than writing into a
    # transaction that something could still roll back.
    try:
        running = ui.activeCommand
    except Exception:
        running = ''
    if running and running != 'SelectCommand':
        job['due'] = time.time() + _WRITE_DELAY
        return
    _pending_write = None
    mod = _server_mod
    err = _write_dimension(design, job)
    if mod is None:
        return
    if err:
        mod.set_error(err)
        return
    _session = None
    mod.close_session()


def _arm_delete(token):
    """Queue the just-placed dimension for deletion and get the tool out of the way.

    Terminating COMMITS the placement, which is the point: the delete that follows a tick
    or two later then has something real to act on, and lands as one ordinary undo step
    instead of racing the tool's own transaction. See _pending_delete.
    """
    global _pending_delete
    if not token:
        return
    _pending_delete = {'token': token, 'due': time.time() + _WRITE_DELAY}
    try:
        running = ui.activeCommand
    except Exception:
        running = ''
    if running and running != 'SelectCommand':
        try:
            ui.terminateActiveCommand()
        except Exception:
            pass


def _apply_pending_delete(design):
    """Delete a cancelled dimension once the tool that placed it has finished."""
    global _pending_delete
    job = _pending_delete
    if job is None:
        return
    if time.time() < job['due']:
        return
    try:
        running = ui.activeCommand
    except Exception:
        running = ''
    if running and running != 'SelectCommand':
        job['due'] = time.time() + _WRITE_DELAY
        return
    _pending_delete = None
    # Silent on failure by design. The dimension being gone already — undone, or taken
    # with a cancelled sketch — is the ordinary case, not something to interrupt with.
    try:
        dim = _resolve(design, job['token'])
        if isinstance(dim, adsk.fusion.SketchDimension):
            dim.deleteMe()
    except Exception:
        if _server_mod:
            _server_mod.set_debug(last_error=traceback.format_exc())


def _apply_commands(design):
    mod = _server_mod
    if mod is None:
        return
    global _session
    for c in mod.pop_commands():
        action = c.get('action')
        if action == 'cancel':
            # Backing out of a dimension you are in the middle of placing backs out of
            # the placement too — see _pending_delete.
            if (_session or {}).get('mode') == 'dim' and _session.get('created'):
                _arm_delete(_session['token'])
            was_plane_box = (_session or {}).get('mode') == 'sketch'
            _session = None
            globals()['_pending_write'] = None
            _drop_plane_preview()
            _restore_planes(design)
            mod.close_session()
            if was_plane_box:
                _sweep_planes(design)
        elif action == 'preview':
            _do_preview(design, str(c.get('expression', '')), str(c.get('unit', '')))
        elif action == 'openparam':
            # Another window (the Browser) asking for the parameter editor. Opening a
            # session touches the Fusion API, so it has to happen here on the tick
            # rather than in the HTTP thread that received the request.
            # `filed`: where the Browser has it hand-filed, for a rename's chips.
            _open_edit_param_dialog(design, str(c.get('name', '')).strip(),
                                    c.get('filed') if isinstance(c.get('filed'), dict) else None)
        elif action == 'commit':
            _do_commit(design, str(c.get('expression', '')).strip(),
                       str(c.get('name', '')).strip(), str(c.get('mode', '')),
                       extra={'flex': c.get('flex') if isinstance(c.get('flex'), dict) else None,
                              'doppel': bool(c.get('doppel')),
                              'axis': str(c.get('axis', '') or '')})
        elif action == 'makeparam':
            # The value box's "make a parameter from this" panel. Creates it and
            # answers; the session itself carries on — see _do_make_param.
            _do_make_param(design, str(c.get('expression', '')).strip(),
                           str(c.get('name', '')).strip())
        elif action == 'editmade':
            # A parameter chip's "Edit parameter": expression and/or name. Written here
            # for the dimension / parameter sessions; behind Extrude and the feature
            # panels the server routed it to that command instead (Made Params.py).
            _do_edit_made(design, str(c.get('name', '')).strip(),
                          str(c.get('expression', '')).strip(),
                          str(c.get('newName', '')).strip())
        elif action == 'makeinfo':
            # "Make a parameter" in a session that never said which component it is
            # in (Extrude, the plane box, the panels): its name stepper needs a parent.
            _do_make_info(design)
        elif action == 'retarget':
            # The Name field's own autosuggest picking an existing parameter mid-
            # session. Fired the moment the suggestion is accepted, well ahead of
            # the eventual commit — see _do_retarget_param.
            _do_retarget_param(design, str(c.get('name', '')).strip())
        elif action == 'opencomponent':
            # The New Component add-in asking for its panel. It lives in this window
            # because a component's bounds are expressions, and expressions want the
            # autocomplete that is already here.
            _open_component_dialog(design)
        elif action == 'createcomponent':
            _do_create_component(design, c)
        elif action == 'openaddbounds':
            # The New Component add-in's sibling command, asking for the same panel
            # aimed at the active component instead of a new one — or, from the
            # Browser's component menu, at the component whose row was held. `edit`
            # (the Browser's Edit Bounds) is what may open on existing bounds.
            _open_add_bounds_dialog(design, str(c.get('comp', '')), bool(c.get('edit')))
        elif action == 'addboundstocomponent':
            _do_add_bounds_to_component(design, c)
        elif action == 'editbounds':
            _do_edit_bounds(design, c)
        elif action == 'opensketch':
            # The Smart Sketch add-in asking for the plane box. Same arrangement as the
            # New Component panel: it owns the key, this owns the window.
            _open_sketch_dialog(design)
        elif action == 'opensketchplane':
            # The Browser's menu on a sketch that was placed this way, asking to edit
            # the line that placed it.
            _open_sketch_dialog(design, str(c.get('sketch', '')))
        elif action == 'openprojectbounds':
            # The Project Bounds add-in's key, asking for the same box restricted to
            # the one axis the edited sketch's plane already runs along.
            _open_project_bounds_dialog(design, str(c.get('sketch', '')))
        elif action == 'createsketch':
            _do_create_sketch(design, c)
        elif action == 'previewplane':
            # The plane box showing its angled plane in the viewport as it is typed.
            _do_plane_preview(design, c)
        elif action == 'showplanes':
            # The "Planes" chip: show every construction plane the component has while
            # the box is open, put back exactly as found the moment it closes.
            _reveal_planes(design, bool(c.get('on')))
        elif action == 'ensurewindow':
            # The Extrude add-in, about to arm a session on a window it has no way to
            # check itself. Every opener in THIS file calls _ensure_window directly; that
            # one is a separate add-in and can only ask. Without it, pressing the extrude
            # key with the window down or wedged starts a command with no dialog at all —
            # nothing on screen and no way to tell why.
            _ensure_window()


def _open_component_dialog(design):
    """Arm the New Component panel, prefilled from whatever the new component's parent
    would be. Main thread."""
    mod = _server_mod
    if mod is None or bounds is None:
        return
    global _session
    try:
        parent = design.activeComponent or design.rootComponent
        _session = {'mode': 'component', 'parentToken': parent.entityToken}
        _ensure_window()
        rules = bounds.list_sheet_metal_rules(design)
        default_rule = next((n for n, is_def in rules if is_def), rules[0][0] if rules else '')
        mod.open_component_session(
            parent=parent.name,
            values=bounds.inherited_values(design, parent),
            symmetry={a: bounds.is_symmetric(design, parent, a) for a in bounds.AXES},
            make_sketches=True,
            params=_params_snapshot(design),
            sheet_rules=[{'name': n, 'isDefault': is_def} for n, is_def in rules],
            default_rule=default_rule)
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())


def _open_add_bounds_dialog(design, comp_token='', edit=False):
    """Arm the same panel for the ACTIVE component, so it gets bounds of its own
    without a new occurrence. Main thread.

    `comp_token` (the Browser's "Edit Bounds" on a component row) aims it at that
    occurrence's component instead, active or not.

    At the top level this is the ROOT: its bounds are the model's own, owned by
    TopLevel (TopLevel_Bounds_Top_Offset_Z, … — see Bounds.py's Names), and every
    component's faces fall back to them.

    A component that already has bounds is REFUSED with a HUD cross — Add Bounds adding
    to bounds made the second set that drew nested inside the first. Only `edit` (the
    Browser's Edit Bounds) opens the same panel in EDIT mode instead, prefilled with its
    current expressions (blank for a face left out).
    """
    mod = _server_mod
    if mod is None or bounds is None:
        return
    global _session
    try:
        comp = design.activeComponent or design.rootComponent
        if comp_token:
            ent = _resolve(design, comp_token)
            comp = getattr(ent, 'component', ent) if ent is not None else None
            if comp is None:
                _hud('fail', 'Add Bounds: that component is no longer available.')
                return
        if not edit and bounds.has_own_bounds(design, comp):
            _hud('fail', 'Add Bounds: %s already has bounds.\n\nUse Edit Bounds on its '
                         'row in the Browser to change them.' % (
                             'the top level' if comp == design.rootComponent
                             else comp.name))
            return
        if bounds.bounds_parameters(design, comp):
            _session = {'mode': 'editbounds', 'compToken': comp.entityToken,
                        'root': comp == design.rootComponent}
            _ensure_window()
            mod.open_add_bounds_session(
                component_name=comp.name,
                values=bounds.current_values(design, comp),
                symmetry={a: bounds.is_symmetric(design, comp, a) for a in bounds.AXES},
                params=_params_snapshot(design),
                edit=True)
            return
        _session = {'mode': 'addbounds', 'compToken': comp.entityToken,
                    'root': comp == design.rootComponent}
        _ensure_window()
        mod.open_add_bounds_session(
            component_name=comp.name,
            values=bounds.inherited_values(design, comp),
            symmetry={a: bounds.is_symmetric(design, comp, a) for a in bounds.AXES},
            params=_params_snapshot(design))
    except Exception:
        _session = None
        tb = traceback.format_exc()
        mod.set_debug(last_error=tb)
        _hud('fail', 'Add Bounds – error:\n' + tb)


def _open_sketch_dialog(design, sketch_token=''):
    """Arm the plane box. Main thread.

    With no token this is a NEW sketch in the active component; with one it is the line
    that already placed that sketch, opened again to be changed. The two differ only in
    what is prefilled and what the commit does, which is why they share an opener.
    """
    mod = _server_mod
    if mod is None:
        return
    global _session
    try:
        if bounds is None:
            ui.messageBox('The bounds module could not be loaded, so the plane box '
                          'has no vocabulary to offer.', SKETCH_CMD_NAME)
            return
        sketch, text, name = None, '', ''
        if sketch_token:
            sketch = _resolve(design, sketch_token)
            if sketch is None:
                ui.messageBox('That sketch is no longer available.', SKETCH_CMD_NAME)
                return
            text = bounds.sketch_datum_text(sketch)
            if not text:
                # Not placed by the plane box (the Browser's Set Plane on any sketch):
                # the same slide-only box Project Bounds opens, minus the projection.
                _open_slide_only_dialog(design, sketch_token, SKETCH_CMD_NAME)
                return
            native = sketch.nativeObject or sketch
            comp = native.parentComponent
            # The name it already has, so the box opens showing it and leaving the field
            # alone renames nothing. The NATIVE object's — a proxy's name is the same
            # string, but the native one is what the commit will write to.
            name = native.name
        else:
            comp = design.activeComponent or design.rootComponent

        datums = bounds.datum_words(design, comp)
        if not datums:
            ui.messageBox('%s has no bounds, so there is nothing to place a sketch '
                          'against.\n\nGive it bounds first (Component: New makes '
                          'them; the root can be given them by hand).' % comp.name,
                          SKETCH_CMD_NAME)
            return

        # Other plane-box sketches in the same component, offered as "use the same
        # plane as...". Same enumeration pattern as _plane_box_helper_tokens: walk
        # comp.sketches fresh rather than caching, so one made since the box last
        # opened is on the list.
        others = []
        for i in range(comp.sketches.count):
            s = comp.sketches.item(i)
            if sketch_token and s.entityToken == sketch_token:
                continue                # not itself
            t = bounds.sketch_datum_text(s)
            if not t:
                continue                # '' = not placed by the plane box
            others.append({'name': s.name, 'token': s.entityToken,
                           'text': t, 'angle': bounds.sketch_angle(s)})

        _drop_plane_preview()           # a box reopened mid-preview starts clean
        _restore_planes(design)         # ditto for a reveal left armed by a force-close
        # A sketch being redefined may only slide, so a pick is held to its axis — the
        # datum word's, which an angled sketch still has though its normal is tilted.
        _session = {'mode': 'sketch', 'compToken': comp.entityToken,
                    'sketchToken': sketch_token,
                    'axis': bounds._datum_axis(text) if sketch_token else ''}
        if not sketch_token and fvs is not None:
            # Where the visibility cycle is NOW, when the command ran — the new sketch
            # opens there rather than at the entry defaults.
            try:
                _session['cyclePos'] = fvs.cycle_position()
            except Exception:
                pass
        _ensure_window()
        mod.open_sketch_session(datums=datums, text=text,
                                redefine=bool(sketch_token), sketch=sketch_token,
                                angle=(bounds.sketch_angle(sketch) if sketch else None),
                                sketches=others, name=name,
                                params=_params_snapshot(design))
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())


def _open_project_bounds_dialog(design, sketch_token):
    """Arm the plane box for Project Bounds: the same box _open_sketch_dialog opens to
    redefine a sketch, restricted to the ONE axis this sketch's plane already runs
    along. That is the only difference — a plane can move but never turn (see
    bounds.redefine_plane), so the other two axes' words would only ever be refused.

    No angle is offered: a sketch square to an axis is by definition flat on it, and
    an angled move belongs to Smart Sketch on a sketch the plane box made, not to a
    key whose whole point is "wherever this sketch already is, plus bounds".
    """
    _open_slide_only_dialog(design, sketch_token, PROJECT_BOUNDS_CMD_NAME,
                            project_bounds=True)


def _open_slide_only_dialog(design, sketch_token, title, project_bounds=False):
    """The plane box on ONE axis — the one `sketch_token`'s plane already runs along —
    prefilled with where it sits now. Shared by Project Bounds and the Browser's Set
    Plane on a sketch the plane box did not place; `project_bounds` is the only
    difference (the commit then projects the bounds in as well)."""
    mod = _server_mod
    if mod is None:
        return
    global _session
    try:
        if bounds is None:
            ui.messageBox('The bounds module could not be loaded, so the plane box '
                          'has no vocabulary to offer.', title)
            return
        sketch = _resolve(design, sketch_token)
        if sketch is None:
            ui.messageBox('That sketch is no longer available.', title)
            return
        native = sketch.nativeObject or sketch
        comp = native.parentComponent
        axis = bounds.sketch_axis(native)
        if not axis:
            ui.messageBox('%s is not square to X, Y or Z, so there is no axis to slide '
                          'it along — a plane can move, but it cannot turn.'
                          % native.name, title)
            return
        datums = [d for d in bounds.datum_words(design, comp) if d['axis'] == axis]
        if not datums:
            ui.messageBox('%s has no bounds on the axis this sketch is on, so there '
                          'is nothing to place it against.' % comp.name, title)
            return
        text = (bounds.sketch_datum_text(native)
                or bounds.describe_position(design, comp, native, axis))

        _drop_plane_preview()           # a box reopened mid-preview starts clean
        _restore_planes(design)         # ditto for a reveal left armed by a force-close
        _session = {'mode': 'sketch', 'compToken': comp.entityToken,
                    'sketchToken': sketch_token, 'projectBounds': project_bounds,
                    'axis': axis}
        _ensure_window()
        mod.open_sketch_session(datums=datums, text=text, redefine=True,
                                sketch=sketch_token, angle=None, flat=True,
                                name=native.name,
                                params=_params_snapshot(design))
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())


# ── Picking where the plane goes ─────────────────────────────────────────────
# While the plane box is open, a click on a face in the viewport answers "put it
# THERE": the face's position is read back into ranked datum lines (plane_guess) and
# the window shows the first, ⌥↑/⌥↓ cycling the rest. An edge, vertex or sketch
# curve/point works too, but only when an existing sketch is being moved — its axis is
# known already; a new sketch needs a face to say which axis it is on at all.
#
# Everything is compared in the COMPONENT's own frame, which is the frame its bounds
# parameters are in: the pick is world space and is carried back through the
# occurrence's transform first.

_LENGTH_UNITS = {'mm', 'cm', 'm', 'um', 'in', 'ft'}
_AXIS_INDEX = {'X': 0, 'Y': 1, 'Z': 2}


def _selection_sig(token=True):
    """(token, rounded pick point) of the single selection, or None. token=False keys it
    on the entity's TYPE instead — every entityToken read leaves records in the undo
    transaction, and the dimension box polls this for as long as it is open."""
    try:
        sel = ui.activeSelections
        if sel.count != 1:
            return None
        s = sel.item(0)
        try:
            tok = s.entity.entityToken if token else s.entity.objectType
        except Exception:
            tok = str(id(s.entity))
        p = s.point
        return (tok, round(p.x, 5), round(p.y, 5), round(p.z, 5))
    except Exception:
        return None


def _occurrence_for(design, comp, ent):
    """The occurrence to take `comp`'s frame from: the one the pick was made in if that
    is an instance of `comp`, else the active one, else the first. None = the root."""
    if comp is None or comp == design.rootComponent:
        return None
    occ = getattr(ent, 'assemblyContext', None)
    while occ is not None:
        try:
            if occ.component == comp:
                return occ
            occ = occ.assemblyContext
        except Exception:
            break
    try:
        occ = design.activeOccurrence
        if occ is not None and occ.component == comp:
            return occ
    except Exception:
        pass
    try:
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        return occs.item(0) if occs and occs.count else None
    except Exception:
        return None


def _to_comp(occ):
    """world → component frame, as a function on Point3D/Vector3D copies."""
    m = None
    if occ is not None:
        try:
            m = occ.transform2.copy()
            m.invert()
        except Exception:
            m = None

    def conv(g):
        g = g.copy()
        if m is not None:
            g.transformBy(m)
        return g
    return conv


_PLANE_TURN = ('That face is square to %s, but this sketch is on %s. A plane can move, '
               'but it cannot turn.')


def _pick_target(ent, pick_pt, axis, conv, mismatch=_PLANE_TURN):
    """(axis, coordinate in the component frame, native face or None, problem).

    `axis` is the axis the session is held to ('' for a new sketch); `mismatch` says why
    a face square to another axis is refused (% the face's axis, the session's)."""
    face = adsk.fusion.BRepFace.cast(ent)
    cplane = adsk.fusion.ConstructionPlane.cast(ent)
    if face is not None or cplane is not None:
        # A construction plane places like a flat face, minus the face's provenance.
        plane = adsk.core.Plane.cast((face if face is not None else cplane).geometry)
        if plane is None:
            return '', None, None, 'That face is not flat, so it has no one position.'
        normal = conv(plane.normal)
        ax, _sign = bounds._principal_axis(normal)
        # _principal_axis allows 5° for its own purposes; a pick means SQUARE.
        if ax and abs((normal.x, normal.y, normal.z)[_AXIS_INDEX[ax]]) \
                < 0.99999 * normal.length:
            ax = None
        if not ax:
            return '', None, None, "That face isn't square to X, Y or Z."
        if axis and ax != axis:
            return '', None, None, mismatch % (ax, axis)
        where = conv(plane.origin)
        return ax, (where.x, where.y, where.z)[_AXIS_INDEX[ax]], \
            ((face.nativeObject or face) if face is not None else None), ''

    if not axis:
        return '', None, None, 'Pick a face — it is what says which axis the sketch is on.'
    idx = _AXIS_INDEX[axis]
    pts = []
    try:
        if adsk.fusion.BRepVertex.cast(ent):
            pts = [ent.geometry]
        elif adsk.fusion.BRepEdge.cast(ent):
            pts = [ent.startVertex.geometry, ent.endVertex.geometry]
        elif adsk.fusion.SketchPoint.cast(ent):
            pts = [ent.worldGeometry]
        elif adsk.fusion.SketchCurve.cast(ent):
            c = ent
            for a in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
                sp = getattr(c, a, None)
                if sp is not None:
                    pts.append(sp.worldGeometry)
                    if a == 'endSketchPoint':
                        break
    except Exception:
        pts = []
    if not pts:
        return '', None, None, 'Pick a face, an edge, or a sketch line or point.'
    local = [conv(p) for p in pts]
    vals = [(p.x, p.y, p.z)[idx] for p in local]
    if max(vals) - min(vals) <= bounds.TOL:
        return axis, vals[0], None, ''
    # Runs along the axis: the end nearer where it was clicked.
    here = conv(pick_pt)
    hv = (here.x, here.y, here.z)
    best = min(range(len(local)), key=lambda i: sum(
        (a - b) ** 2 for a, b in zip((local[i].x, local[i].y, local[i].z), hv)))
    return axis, vals[best], None, ''


def _guess_datums(design, comp, axis):
    """The component's datums on `axis` as plane_guess wants them, with coordinates."""
    high, _low = bounds.high_low(axis)
    out = []
    for d in bounds.datum_words(design, comp):
        if d['axis'] != axis:
            continue
        if d['kind'] == 'midline':
            out.append(dict(d, coord=0.0))
            continue
        direction = d['datum'] if d['datum'] in bounds.DIRECTIONS else d.get('side', high)
        c = bounds.coord(design, comp, direction)
        if c is not None:
            out.append(dict(d, coord=c))
    return out


def _guess_params(design, comp, occ):
    """Length user parameters, ranked by how local they are to `comp`."""
    own = set()
    try:
        own = {p.name for p, _k in bounds.bounds_parameters(design, comp)}
    except Exception:
        pass
    here = bounds.owner_level(design, comp) + '_'
    above = [bounds.owner_level(design, design.rootComponent) + '_']
    o = getattr(occ, 'assemblyContext', None) if occ is not None else None
    while o is not None:
        try:
            above.append(bounds.owner_level(design, o.component) + '_')
            o = o.assemblyContext
        except Exception:
            break
    out = []
    ups = design.userParameters
    for i in range(ups.count):
        p = ups.item(i)
        try:
            if p.unit not in _LENGTH_UNITS:
                continue
            name = p.name
            rank = (0 if name in own or name.startswith(here)
                    else 1 if any(name.startswith(a) for a in above) else 2)
            out.append({'name': name, 'value': p.value, 'rank': rank})
        except Exception:
            pass
    return out


def _face_provenance(design, comp, face, axis, target, datums):
    """([{text, note}], [hint terms]) from the extrude that made `face`, if any.

    A start face sits on its profile's sketch; an end face is that sketch moved on by
    a one-side distance. With a plane-box sketch in this component, that composes into
    the sketch's own line (checked to land on `target` before it is offered). Any other
    sketch still gives the distance as a hint term, ranked above every parameter."""
    lines, hints = [], []
    idx = _AXIS_INDEX[axis]
    try:
        body_comp = face.body.parentComponent
        exts = body_comp.features.extrudeFeatures
    except Exception:
        return lines, hints
    for i in range(exts.count):
        ext = exts.item(i)
        try:
            is_start = any(f == face for f in ext.startFaces)
            is_end = not is_start and any(f == face for f in ext.endFaces)
            if not (is_start or is_end):
                continue
            prof = ext.profile
            if adsk.core.ObjectCollection.cast(prof):
                prof = prof.item(0)
            sk = prof.parentSketch
        except Exception:
            continue
        dist = None
        if is_end:
            try:
                if not ext.hasTwoExtents:
                    dist = adsk.fusion.DistanceExtentDefinition.cast(ext.extentOne)
            except Exception:
                dist = None
            if dist is None:
                return lines, hints     # found it, but not a shape we can read
            # axes [] — this distance runs along the pick's own axis by construction,
            # whatever its parameter's name says (plane_guess.axis_friendly).
            hints.append({'name': dist.distance.expression,
                          'value': abs(dist.distance.value), 'rank': -1, 'axes': []})

        text = bounds.sketch_datum_text(sk)
        if not text or sk.parentComponent != comp or bounds.sketch_axis(sk) != axis:
            return lines, hints
        word, rest = bounds.planes.split_input(text)
        d = next((x for x in datums if x['word'].lower() == word.lower()), None)
        if d is None:
            return lines, hints
        try:
            o = sk.transform.getAsCoordinateSystem()[0]
            pos = (o.x, o.y, o.z)[idx]
        except Exception:
            return lines, hints
        note = '%s of %s' % ('start' if is_start else 'end', ext.name)
        if is_start:
            if abs(pos - target) <= bounds.TOL:
                lines.append({'text': text, 'note': note})
        else:
            got = plane_guess.extend(d['word'], rest, plane_guess.rest_for(d, pos),
                                     dist.distance.expression, abs(dist.distance.value),
                                     plane_guess.rest_for(d, target))
            if got:
                lines.append({'text': got, 'note': note})
        return lines, hints
    return lines, hints


_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_COPY = re.compile(r'^(.*) \(\d+\)$')


def _body_twins(comp, body):
    """Section body keys whose history is `body`'s: itself, and for a copy Fusion named
    "Ribs15 (1) (1)" (a mirror's or a split's output, which no extrude lists as its own —
    MirrorFeature.inputEntities raises without a timeline roll) every body up the name
    chain that still exists. None for a copy none of whose sources is left (combined
    away): its history is then the whole component's."""
    out = [section.body_key(body)]
    name = body.name or ''
    m = _COPY.match(name)
    copy = m is not None
    while m:
        name = m.group(1)
        try:
            src = comp.bRepBodies.itemByName(name)
            if src is not None:
                out.append(section.body_key(src))
        except Exception:
            pass
        m = _COPY.match(name)
    return None if copy and len(out) == 1 else out


def _face_history(design, comp, face, axis, target, datums, ent=None):
    """(derived [{text, note}], only) — where `face` is, written from its own body's
    history (Section's pool: the extrudes that made or cut the body, their sketches
    derived dimension by dimension, sketch and feature patterns, mirrors), never from a
    value that merely coincides.

    The body may be in ANOTHER component than the sketch's (a rib of Ribs picked while
    placing a sketch in Main): its history is read in its own component's frame and
    carried over by the occurrences' offset — nothing when they share an origin, else a
    bound of `comp` the offset lands on; a rotated occurrence, or an offset no bound
    names, gives no derivation. `ent` is the picked proxy, which says which occurrence.

    `only` is the parameters that history uses on this axis, for plane_guess's
    narrowed search when the derivation did not land; None when there is no readable
    history and the search has to be open."""
    if section is None:
        return [], None
    try:
        body = face.body
        bcomp = body.parentComponent
        P = section.params_table(design)
        idx = _AXIS_INDEX[axis]
        shift = section.Form.const(0.0)
        if bcomp != comp:
            here = section.occurrence_offset(_occurrence_for(design, comp, None))
            there = section.occurrence_offset(_occurrence_for(design, bcomp, ent))
            if here is None or there is None:
                return [], None
            d = there[idx] - here[idx]
            if abs(d) > bounds.TOL:
                shift = section.face_form(design, comp, axis, d, P)
                if shift is None:
                    return [], None
        target_b = target - section.sd.form_value(shift, P)
        comp_b = comp
        comp = bcomp
        cr = section.component_rows(design, comp, P)
        twins = _body_twins(comp, body)
        key = frozenset(twins) if twins is not None else None
        pool = section.sg.Pool()
        for ax, v, f, prio, label, owners in cr['rows']:
            if ax == axis:
                pool.add(ax, v, f, prio, label, owners, comp.id)
        for ax, m, mform in cr['mirrors']:
            pool.add_mirror(comp.id, ax, m, mform)
        for ax, sv, sform, q in cr['patterns']:
            pool.add_pattern(comp.id, ax, sv, sform, q)
        form, label = pool.match(axis, target_b, prefer=key, group=comp.id, owned=key is not None)
        if form is not None and not form.t:
            form = None             # a bare number: nothing the history can be written in
        if form is not None:
            form = form + shift
        comp = comp_b               # the datums are the sketch's component's

        only = set()
        forms = [f for ax, _v, f, _p, _l, owners in cr['rows']
                 if ax == axis and (key is None or not key.isdisjoint(owners))]
        forms += [f for ax, _m, f in cr['mirrors'] if ax == axis]
        forms += [f for ax, _s, f, _q in cr['patterns'] if ax == axis]
        for f in forms:
            for term in f.t:
                only.update(n for n in _NAME.findall(term) if n in P.user)
        if not forms:
            only = None

        derived = []
        if form is not None:
            ds = []
            for d in datums:
                d = dict(d)
                if d.get('kind') == 'face':
                    name = d.get('face') or ''
                    if name in P.user and abs(d['sign'] * P.user[name][0] - d['coord']) <= bounds.TOL:
                        d['form'] = section.Form.term(name, float(d['sign']))
                    else:
                        d['form'] = section.face_form(design, comp, axis, d['coord'], P)
                ds.append(d)
            unit = P.default_length
            derived = plane_guess.from_form(
                form, ds, target, lambda f: section.sd.render(f, unit),
                lambda f: section.sd.form_value(f, P), note=label)
        section.log('plane pick', comp.name, axis, '%.5f' % target, key,
                    'form', form, label, 'derived', [d['text'] for d in derived],
                    'only', sorted(only) if only is not None else None)
        return derived, only
    except Exception:
        _server_mod and _server_mod.set_debug(last_error=traceback.format_exc())
        return [], None


_DIM_HOOK = 'dim_outside_active_hook'   # registered by Dim Outside Active's run()


def _set_picking(design, on):
    """While the plane box is open, a plane or face in ANY component is a valid pick, so
    Dim Outside Active's lock on everything outside the active component is lifted.
    Called every tick; the hook does nothing unless the state changes."""
    fn = getattr(sys.modules.get(_DIM_HOOK), 'set_picking', None)
    if fn is not None:
        try:
            fn(on)
        except Exception:
            pass


def _watch_plane_pick(design):
    """One tick of the plane box's viewport pick. Cheap when nothing changed."""
    mod = _server_mod
    s = _session
    if mod is None or s is None or s.get('mode') != 'sketch' \
            or bounds is None or plane_guess is None:
        return
    sig = _selection_sig()
    if 'pickSig' not in s:
        s['pickSig'] = sig          # whatever was selected before the box opened
        return
    if sig == s['pickSig']:
        return
    s['pickSig'] = sig
    if sig is None:
        return
    try:
        found = design.findEntityByToken(s.get('compToken', ''))
        comp = adsk.fusion.Component.cast(found[0]) if found else None
        if comp is None:
            return
        sel = ui.activeSelections.item(0)
        ent, pick_pt = sel.entity, sel.point
        occ = _occurrence_for(design, comp, ent)
        conv = _to_comp(occ)
        axis, target, face, problem = _pick_target(ent, pick_pt, s.get('axis', ''), conv)
        if problem:
            mod.set_plane_suggestions([], error=problem)
            return
        datums = _guess_datums(design, comp, axis)
        if not datums:
            mod.set_plane_suggestions([], error='%s has no bounds on %s to place it '
                                                'against.' % (comp.name, axis))
            return
        prov, hints, derived, only = [], [], [], None
        if face is not None:
            prov, hints = _face_provenance(design, comp, face, axis, target, datums)
            derived, only = _face_history(design, comp, face, axis, target, datums, ent)
        um = design.unitsManager

        def fmt(v):
            try:
                return um.formatInternalValue(v, um.defaultLengthUnits, False)
            except Exception:
                return '%g' % v
        items = plane_guess.suggest(target, datums,
                                    hints + _guess_params(design, comp, occ),
                                    provenance=prov, fmt=fmt, derived=derived, only=only)
        mod.set_plane_suggestions(items)
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())


# ── Measuring a dimension off a pick ─────────────────────────────────────────
# The plane box's pick, asked of the dimension box: with it open, a click on a face,
# edge, vertex or sketch point says "this dimension reaches THERE". The pick is read
# along the axis the dimension measures, its distance from the end the dimension is
# measured FROM is taken, and plane_guess.suggest_distance finds the parameters that add
# up to it. Only the best answer is sent — it replaces the value; there is no cycling.
#
# Which end is FROM: the one sitting on the origin or a bound face (_datum_of's rule);
# with both or neither, the one further from the pick — the nearer end is the one being
# sent there. It is the dimension's own end, not the nearest bound, because that is the
# number that puts the other end on the pick.
#
# Ahead of every parameter (rank -1): what MADE the picked geometry — the extrude
# distance when the face is an extrude's end, and the driving dimensions along the same
# axis in the sketch behind it (or the picked sketch entity's own sketch). Parameters
# count as local to the picked part's component as well as the dimension's.
#
# A dimension placed just now (_session['created']) opens with the DIMENSION TOOL still
# running, and a click in the viewport then starts another dimension instead of selecting
# anything. So the tool is terminated on the first tick — the same terminate a commit or
# cancel does anyway (_do_commit, _arm_delete: it commits the placement), only sooner;
# the write and the cancel-delete then find nothing running and go straight through.
# Whatever the tool leaves selected as it ends is not a pick: the baseline follows the
# selection for _PICK_SETTLE_S after.

_DIM_TURN ='That face is square to %s, but this dimension measures along %s.'
_HINT_BUDGET_S = 0.25
_PICK_SETTLE_S = 0.4
_IDENT = re.compile(r'[A-Za-z_]\w*')


def _uses_params(expr, names):
    """Does `expr` name a user parameter? A bare "18 mm" is no better than the number."""
    return any(w in names for w in _IDENT.findall(expr or ''))


def _pick_faces(ent):
    """The native faces a picked face, edge or vertex belongs to."""
    out = []
    try:
        face = adsk.fusion.BRepFace.cast(ent)
        if face is not None:
            out = [face]
        else:
            e = adsk.fusion.BRepEdge.cast(ent) or adsk.fusion.BRepVertex.cast(ent)
            if e is not None:
                out = [e.faces.item(i) for i in range(min(e.faces.count, 4))]
    except Exception:
        pass
    return [f.nativeObject or f for f in out]


def _made_by(faces, deadline):
    """[(extrude, 'start'|'end'|'side')] that made any of `faces`, most recent first.
    Stops at the deadline — on a big document this is a walk over every extrude."""
    comps, out = [], []
    for f in faces:
        try:
            c = f.body.parentComponent
            if c not in comps:
                comps.append(c)
        except Exception:
            pass
    for comp in comps:
        exts = comp.features.extrudeFeatures
        for i in range(exts.count - 1, -1, -1):
            if time.time() > deadline:
                return out
            ext = exts.item(i)
            try:
                for role, coll in (('end', ext.endFaces), ('start', ext.startFaces),
                                   ('side', ext.sideFaces)):
                    if any(g == f for g in coll for f in faces):
                        out.append((ext, role))
                        break
            except Exception:
                continue
    return out


def _sketch_hints(sketch, axis, names, skip, deadline):
    """The driving dimensions of `sketch` along `axis` whose expressions use parameters."""
    out = []
    try:
        dims = sketch.sketchDimensions
        n = min(dims.count, 80)
    except Exception:
        return out
    for i in range(n):
        if time.time() > deadline:
            break
        try:
            d = dims.item(i)
            if d == skip or not d.isDriving:
                continue
            p = d.parameter
            if p is None or p.unit not in _LENGTH_UNITS \
                    or not _uses_params(p.expression, names):
                continue
            if _situation_of(d)[0] != axis:
                continue
            out.append({'name': p.expression, 'value': abs(p.value), 'rank': -1,
                        'axes': []})
        except Exception:
            continue
    return out


def _pick_hints(ent, axis, names, skip):
    """([hint terms], picked component) — see the section comment."""
    deadline = time.time() + _HINT_BUDGET_S
    hints, sketches, comp = [], [], None
    try:
        se = adsk.fusion.SketchEntity.cast(ent)
        if se is not None:
            sketches.append(se.parentSketch)
            comp = se.parentSketch.parentComponent
    except Exception:
        pass
    faces = _pick_faces(ent)
    if faces:
        try:
            comp = faces[0].body.parentComponent
        except Exception:
            pass
    for ext, role in _made_by(faces, deadline):
        if role == 'end':
            try:
                dist = (None if ext.hasTwoExtents else
                        adsk.fusion.DistanceExtentDefinition.cast(ext.extentOne))
                if dist is not None and _uses_params(dist.distance.expression, names):
                    hints.append({'name': dist.distance.expression,
                                  'value': abs(dist.distance.value), 'rank': -1,
                                  'axes': []})
            except Exception:
                pass
        try:
            prof = ext.profile
            if adsk.core.ObjectCollection.cast(prof):
                prof = prof.item(0)
            sk = prof.parentSketch
            if sk is not None and sk not in sketches:
                sketches.append(sk)
        except Exception:
            pass
    for sk in sketches:
        hints += _sketch_hints(sk, axis, names, skip, deadline)
    return hints, comp


def _dim_pick_problem(mod, text):
    mod.set_plane_suggestions([], error=text)


def _watch_dim_pick(design):
    """One tick of the dimension box's viewport pick. Cheap when nothing changed."""
    mod = _server_mod
    s = _session
    if mod is None or s is None or s.get('mode') != 'dim' or plane_guess is None:
        return
    if s.get('driven'):
        return                      # a driven dimension's value is not ours to suggest
    if s.get('created') and not s.get('toolDown'):
        s['toolDown'] = True        # the dimension tool out of the way — see above
        try:
            if ui.activeCommand == DIMENSION_CMD:
                ui.terminateActiveCommand()
        except Exception:
            pass
        s['pickSettle'] = time.time() + _PICK_SETTLE_S
    sig = _selection_sig(token=False)
    if 'pickSig' not in s or time.time() < s.get('pickSettle', 0):
        s['pickSig'] = sig          # the dimension itself, selected as the box opened
        return
    if sig == s['pickSig']:
        return
    s['pickSig'] = sig
    if sig is None:
        return
    try:
        sel = ui.activeSelections.item(0)
        ent, pick_pt = sel.entity, sel.point
        if isinstance(ent, adsk.fusion.SketchDimension):
            return                  # a dimension re-selected is not a place
        dim = adsk.fusion.SketchDimension.cast(_resolve(design, s.get('token', '')))
        if dim is None:
            return
        param = dim.parameter
        if param is None or param.unit not in _LENGTH_UNITS:
            return _dim_pick_problem(mod, 'Only a length can be measured off a pick.')
        axis = _situation_of(dim)[0]
        if not axis:
            return _dim_pick_problem(mod, "This dimension doesn't run along X, Y or Z, "
                                          "so a pick can't say how long it is.")
        comp = dim.parentSketch.parentComponent
        occ = _occurrence_for(design, comp, ent)
        _ax, target, _face, problem = _pick_target(ent, pick_pt, axis, _to_comp(occ),
                                                   mismatch=_DIM_TURN)
        if problem:
            return _dim_pick_problem(mod, problem)
        coords = _axis_coords(dim, axis)
        if not coords:
            return _dim_pick_problem(mod, "Can't tell where this dimension starts.")

        def anchored(v):
            return abs(v) < _END_TOL or bool(bounds and bounds.face_at(design, comp, axis, v))
        start = max(coords, key=lambda v: (anchored(v), abs(v - target)))
        distance = abs(target - start)
        if distance <= plane_guess.TOL:
            return _dim_pick_problem(mod, 'That is where the dimension starts.')

        ups = design.userParameters
        names = {ups.item(i).name for i in range(ups.count)}
        hints, picked_comp = _pick_hints(ent, axis, names, dim)
        params = {p['name']: p for p in _guess_params(design, comp, occ)}
        if picked_comp is not None and picked_comp != comp:
            for p in _guess_params(design, picked_comp,
                                   getattr(ent, 'assemblyContext', None)):
                if p['rank'] < params[p['name']]['rank']:
                    params[p['name']] = p
        um = design.unitsManager

        def fmt(v):
            try:
                return um.formatInternalValue(v, param.unit, False)
            except Exception:
                return '%g' % (v * 10.0)
        items = plane_guess.suggest_distance(distance, hints + list(params.values()),
                                             fmt=fmt, axis=axis)
        mod.set_plane_suggestions(items[:1])
    except Exception:
        mod.set_debug(last_error=traceback.format_exc())


# ── The angled-plane preview ─────────────────────────────────────────────────
# Only an ANGLED plane is previewed: a flat offset plane is a sentence you can read
# ("Top 18") and needs no picture, while whether 30° leans the way you meant is
# exactly the kind of thing you want to see before there is a sketch on it. The
# preview is the real construction geometry, built lit, and updated in place as the
# box changes; commit finishes it (light off, sketch on top), cancel deletes it.
_plane_preview = None      # a build_plane parts dict, or None


def _drop_plane_preview():
    global _plane_preview
    parts, _plane_preview = _plane_preview, None
    if parts and bounds is not None:
        try:
            bounds.delete_parts(parts)
        except Exception:
            pass


def _take_plane_preview():
    """Hand the preview over to whoever is finishing it, and forget it here."""
    global _plane_preview
    parts, _plane_preview = _plane_preview, None
    return parts


def _sweep_planes(design):
    """Once the plane box closes: delete every plane it ever made that nothing stands
    on any more (bounds.sweep_unused_planes — anything with a dependent stays). A
    preview still up is left alone. Never an error: a plane left over is clutter."""
    if bounds is None:
        return
    try:
        keep = [v for v in (_plane_preview or {}).values()
                if isinstance(v, (adsk.fusion.ConstructionPlane, adsk.fusion.ConstructionAxis))]
        bounds.sweep_unused_planes(design, keep=keep)
    except Exception:
        if _server_mod is not None:
            _server_mod.set_debug(last_error=traceback.format_exc())


# The "Planes" chip: show every construction plane the session's component has while
# the box is open, so there's something to see the line against, and put the folder
# and every bulb back exactly as found the moment the box closes. Snapshot rather than
# "turn them all off again", because an OFF plane the user wanted lit for their own
# reasons has to stay lit.
_planes_reveal = None      # {'compToken', 'folder': bool, 'bulbs': {token: bool}}, or None


def _plane_box_helper_tokens(design, comp):
    """Tokens of the offset-plane / pivot-axis SCAFFOLDING an angled plane-box sketch
    stands on (see Bounds.py, build_plane) — never worth lighting, so the reveal skips
    them. Read fresh each call: sketch_parts is a couple of attribute reads, and this
    stays correct even if a sketch was added since the reveal turned on."""
    out = set()
    if bounds is None:
        return out
    try:
        sketches = comp.sketches
    except Exception:
        return out
    for i in range(sketches.count):
        try:
            parts = bounds.sketch_parts(design, sketches.item(i))
        except Exception:
            continue
        if not parts:
            continue
        for key in ('offset', 'axis'):
            ent = parts.get(key)
            if ent is None or ent is parts.get('plane'):
                continue
            try:
                out.add(ent.entityToken)
            except Exception:
                pass
    return out


def _reveal_planes(design, on):
    """Arm or drop the "Planes" chip's reveal. Main thread — it writes bulbs."""
    global _planes_reveal
    if not on:
        _restore_planes(design)
        return
    if _planes_reveal is not None:
        return                          # already on
    comp = _planes_reveal_component(design)
    if comp is None:
        return
    helpers = _plane_box_helper_tokens(design, comp)
    bulbs = {}
    try:
        planes = comp.constructionPlanes
        for i in range(planes.count):
            pl = planes.item(i)
            try:
                tok = pl.entityToken
            except Exception:
                continue
            if tok in helpers:
                continue
            bulbs[tok] = bool(pl.isLightBulbOn)
    except Exception:
        pass
    try:
        folder_was = bool(comp.isConstructionFolderLightBulbOn)
    except Exception:
        folder_was = False
    try:
        comp_token = comp.entityToken
    except Exception:
        comp_token = ''
    _planes_reveal = {'compToken': comp_token, 'folder': folder_was, 'bulbs': bulbs}
    try:
        comp.isConstructionFolderLightBulbOn = True
    except Exception:
        pass
    for tok in bulbs:
        ent = _resolve(design, tok)
        if ent is not None:
            try:
                ent.isLightBulbOn = True
            except Exception:
                pass


def _restore_planes(design):
    """Put the folder and every plane bulb back as _reveal_planes found them. Safe to
    call with nothing armed — every exit path calls this unconditionally."""
    global _planes_reveal
    snap, _planes_reveal = _planes_reveal, None
    if not snap:
        return
    comp = _resolve(design, snap.get('compToken', '')) if snap.get('compToken') else None
    if comp is not None:
        try:
            comp.isConstructionFolderLightBulbOn = snap.get('folder', False)
        except Exception:
            pass
    for tok, was_on in (snap.get('bulbs') or {}).items():
        ent = _resolve(design, tok)
        if ent is not None:
            try:
                ent.isLightBulbOn = was_on
            except Exception:
                pass


def _planes_reveal_component(design):
    """The component the reveal should act on — the session's, falling back to the
    active component, same resolution _do_create_sketch uses."""
    comp = None
    comp_token = (_session or {}).get('compToken')
    if comp_token:
        found = _resolve(design, comp_token)
        if found is not None:
            comp = adsk.fusion.Component.cast(found)
    if comp is None:
        comp = design.activeComponent or design.rootComponent
    return comp


def _do_plane_preview(design, cmd):
    """Build or update the angled plane the box describes; drop it when the angle is
    cleared or the line stops making sense. Main thread. Never an error in the box —
    a preview that cannot be built simply is not there."""
    global _plane_preview
    mod = _server_mod
    if mod is None or bounds is None:
        return
    try:
        if (_session or {}).get('mode') != 'sketch' or (_session or {}).get('sketchToken'):
            _drop_plane_preview()           # not a new sketch: nothing to preview
            return
        text = str(cmd.get('text', '')).strip()
        angle = cmd.get('angle') or {}
        if not text or not str(angle.get('text', '')).strip():
            _drop_plane_preview()
            return
        comp = None
        comp_token = (_session or {}).get('compToken')
        if comp_token:
            found = design.findEntityByToken(comp_token)
            if found:
                comp = adsk.fusion.Component.cast(found[0])
        if comp is None:
            comp = design.activeComponent or design.rootComponent
        parts, _problems = bounds.build_plane(design, comp, text, angle=angle,
                                             existing=_plane_preview, shown=True)
        _plane_preview = parts          # None when it could not be built
    except Exception:
        _plane_preview = None
        mod.set_debug(last_error=traceback.format_exc())


def _rename_sketch(sketch, name):
    """Give `sketch` the name the box asked for. Returns [problems].

    Nothing at all for an empty name or one it already has — the field arrives prefilled
    with the current name, so "leave it alone" is the common case and must not be a
    write. The NATIVE object is renamed: a proxy's name is read-only in an assembly
    context, and the name belongs to the sketch, not to the occurrence looking at it.

    A name Fusion refuses (one already used in that component) is a problem reported
    afterwards rather than a refusal: the plane is already built and the sketch already
    moved by the time this runs, and throwing that away over a label would be the wrong
    trade.
    """
    if not name:
        return []
    try:
        native = sketch.nativeObject or sketch
        if native.name == name:
            return []
        native.name = name
        return []
    except Exception as e:
        return ['could not name it "%s": %s' % (name, e)]


def _do_create_sketch(design, cmd):
    """Build the plane the line describes and a sketch on it — or move the plane an
    existing sketch already sits on. Main thread.

    The component is re-resolved from the token rather than trusted from the session:
    the box is open across many ticks and the active component can move underneath it.
    """
    mod = _server_mod
    global _session
    try:
        if bounds is None:
            mod.set_error('The bounds module could not be loaded.')
            return
        text = str(cmd.get('text', '')).strip()
        # What to call the sketch. Empty means "leave it alone": Fusion's own name on a
        # new one, and the name it already has on one being redefined.
        name = str(cmd.get('name', '')).strip()
        token = (_session or {}).get('sketchToken') or str(cmd.get('sketch', ''))

        if token:
            sketch = _resolve(design, token)
            if sketch is None:
                mod.set_error('That sketch is no longer available.')
                return
            moved, problems = bounds.redefine_plane(design, sketch, text,
                                                    angle=cmd.get('angle'))
            if not moved:
                # Nothing happened, so the box stays open with the reason in it rather
                # than closing on a change that was never made.
                mod.set_error(problems[0] if problems else 'that line could not be used.')
                return
            problems += _rename_sketch(sketch, name)
            is_project_bounds = bool((_session or {}).get('projectBounds'))
            if is_project_bounds:
                # The whole point of the key: whatever the box just did to the plane,
                # the sketch still wants its origin and bounds rectangle projected in.
                native = sketch.nativeObject or sketch
                problems += bounds.project_new_sketch(design, native)
            _session = None
            _restore_planes(design)
            mod.close_session()
            _sweep_planes(design)
            if problems:
                ui.messageBox('The plane moved, but:\n\n  ' + '\n  '.join(problems),
                              PROJECT_BOUNDS_CMD_NAME if is_project_bounds else SKETCH_CMD_NAME)
            return

        comp = None
        comp_token = (_session or {}).get('compToken')
        if comp_token:
            found = design.findEntityByToken(comp_token)
            if found:
                comp = adsk.fusion.Component.cast(found[0])
        if comp is None:
            comp = design.activeComponent or design.rootComponent

        # A preview that is up is finished rather than rebuilt: same entities, light
        # off, sketch on top. It is claimed here so a failure below does not leave it
        # standing as a second copy — build_plane deletes the parts it was handed when
        # it cannot finish them.
        parts = _take_plane_preview()
        try:
            _tl0 = design.timeline.count
        except Exception:
            _tl0 = None
        sketch, _plane, problems = bounds.create_plane_sketch(
            design, comp, text, angle=cmd.get('angle'), parts=parts, name=name)
        if sketch is None:
            mod.set_error(problems[0] if problems else 'that line could not be used.')
            return

        # The new plane and sketch are at the tail: file them in THIS component's
        # section now, before the sketch is entered (the Timeline add-in's own
        # placement is skipped while a sketch is open, and Phases.place_one only knows
        # the end of the whole Sketches phase).
        try:
            _ph = bounds._phases()
            if _ph is not None and _tl0 is not None:
                _ph.place_in_component_section(design, _tl0)
        except Exception:
            mod.set_debug(last_error=traceback.format_exc())

        cycle_pos = (_session or {}).get('cyclePos')
        _session = None
        if fvs is not None:
            fvs.arm_new_sketch_carry(cycle_pos)
        # Restored BEFORE _pending_enter, so the sketch is entered with the component's
        # original plane visibility, not with everything the chip lit still lit.
        _restore_planes(design)
        mod.close_session()
        _sweep_planes(design)
        # Land in the sketch: the point of the box is to say where you are drawing and
        # then be drawing there. Armed rather than done, so the window is out of the way
        # first — see _pending_enter.
        global _pending_enter
        try:
            _pending_enter = (sketch.entityToken, time.time() + _ENTER_DELAY)
        except Exception:
            _pending_enter = None
        if problems:
            ui.messageBox('Sketch created, but:\n\n  ' + '\n  '.join(problems),
                          SKETCH_CMD_NAME)
    except Exception:
        _session = None
        _restore_planes(design)
        mod.set_debug(last_error=traceback.format_exc())
        mod.close_session()


def _apply_pending_enter(design):
    """Open a sketch armed a few ticks ago, once the window has had time to go."""
    global _pending_enter
    if _pending_enter is None:
        return
    token, due = _pending_enter
    if time.time() < due:
        return
    _pending_enter = None
    sketch = _resolve(design, token)
    if sketch is not None:
        _enter_sketch(design, sketch)


def _enter_sketch(design, sketch):
    """Open a sketch for edit, the way the Browser does: select it (in its occurrence's
    context, or it cannot be selected at all) and run SketchActivate."""
    try:
        occ = _owning_occurrence(design, sketch)
        sk = sketch
        if occ is not None and hasattr(sketch, 'createForAssemblyContext'):
            try:
                sk = sketch.createForAssemblyContext(occ)
            except Exception:
                sk = sketch
        sel = ui.activeSelections
        sel.clear()
        try:
            sel.add(sk)
        except Exception:
            pass
        cd = ui.commandDefinitions.itemById('SketchActivate')
        if cd:
            cd.execute()
        _snap_on()
    except Exception:
        pass


def _snap_on():
    """Turn the sketch palette's Snap on, for a sketch this add-in just made.

    Snap is the one palette option with NO API: it is not on Sketch (which has
    areDimensionsShown, isProjectedGeometryShown and the rest), not on GridPreferences,
    and not anywhere else in adsk — checked, not assumed. What it does have is a pair of
    command definitions, SketchSnapOn / SketchSnapOff, which is how the palette's own
    checkbox drives it. Neither reports the current state, so this only ever sets: it
    turns snap ON and never off, which is what a default is.

    Queued, not run — cd.execute() only enqueues — so it lands after the SketchActivate
    above, which is the order it needs.
    """
    try:
        cd = ui.commandDefinitions.itemById('SketchSnapOn')
        if cd:
            cd.execute()
    except Exception:
        pass


def _file_bounds_in_browser(design, comp, params_before, collapse=False):
    """Ask the Browser to gather this component's bounds into a "Bounds" folder.
    `collapse` asks it to draw the Bounds groups shut (Add Bounds to Component).

    Best effort, and deliberately so: with the browser closed the parameters and sketches
    are simply unfiled, which is where anything made another way starts out. The
    parameters are identified by diffing against the names that existed a moment ago,
    which is exact — matching on the component's prefix would also sweep up anything the
    user happened to name the same way.
    """
    if comp is None:
        return
    try:
        made = []
        for i in range(design.userParameters.count):
            name = design.userParameters.item(i).name
            if name not in params_before:
                made.append(name)
        # The sketches go by their DURABLE KEY (component id, kind, name — the same
        # identity the Browser files group membership under), not by token: the rows
        # do not exist in the window yet when this arrives, so a token could not be
        # looked up there, while a key can be written down and matched when they do.
        sketch_keys = []
        for sk in comp.sketches:
            if bounds.is_bounds_sketch(sk):
                sketch_keys.append('%s|sk|%s' % (comp.id, sk.name))
        if not made and not sketch_keys:
            return
        # The COMPONENT ID, not an entity token. The occurrence handed back by
        # addNewComponent is the native one; the window's row for a nested component
        # is built from the root-context proxy, whose token is different — filing by
        # the native token hung the folder off a row that was never drawn. Every row
        # carries its compId regardless of how many occurrences the component has.
        # The root has no row at all: the window spells it ROOT_TOKEN ("__root__").
        cid = '__root__' if comp == design.rootComponent else comp.id
        _post_ui_action(FILE_BOUNDS_ID, {'compId': cid,
                                         'params': made, 'sketchKeys': sketch_keys,
                                         'collapse': collapse})
    except Exception:
        pass


def _do_create_component(design, cmd):
    """Create the component the panel described. Main thread.

    The parent is re-resolved from the token rather than trusted from the session: the
    panel is open across many ticks and the active component can move underneath it.
    """
    mod = _server_mod
    global _session
    try:
        if bounds is None:
            mod.set_error('The bounds module could not be loaded.')
            return
        parent = None
        token = (_session or {}).get('parentToken')
        if token:
            try:
                found = design.findEntityByToken(token)
                if found:
                    parent = found[0]
            except Exception:
                parent = None
        if parent is None:
            parent = design.rootComponent

        before = set()
        try:
            for i in range(design.userParameters.count):
                before.add(design.userParameters.item(i).name)
        except Exception:
            pass

        occ, problems = bounds.create_component(
            design, parent,
            str(cmd.get('name', '')).strip(),
            {k: str(v) for k, v in (cmd.get('values') or {}).items()},
            {k: bool(v) for k, v in (cmd.get('symmetry') or {}).items()},
            True,   # the bounds sketches are the point; there is nothing to opt out of
            sheet_metal=bool(cmd.get('sheetMetal')),
            sheet_rule=str(cmd.get('sheetRule', '')).strip())

        _file_bounds_in_browser(design, occ.component if occ else None, before)

        _session = None
        mod.close_session()
        if problems:
            ui.messageBox('Component created, but:\n\n  ' + '\n  '.join(problems),
                          'New Component')
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())
        mod.close_session()


def _do_add_bounds_to_component(design, cmd):
    """Give the panel's target component the bounds it described. Main thread.

    Re-resolved from the token rather than trusted from the session, same reasoning as
    _do_create_component: the panel is open across many ticks.
    """
    mod = _server_mod
    global _session
    try:
        if bounds is None:
            _session = None
            mod.close_session()
            _hud('fail', 'Add Bounds: the bounds module could not be loaded.')
            return
        comp = None
        token = (_session or {}).get('compToken')
        if token:
            try:
                found = design.findEntityByToken(token)
                if found:
                    comp = found[0]
            except Exception:
                comp = None
        # The root's entity token is not something findEntityByToken promises to
        # answer, and the root cannot be gone.
        if comp is None and (_session or {}).get('root'):
            comp = design.rootComponent
        if comp is None:
            _session = None
            mod.close_session()
            _hud('fail', 'Add Bounds: that component is gone.')
            return

        before = set()
        try:
            for i in range(design.userParameters.count):
                before.add(design.userParameters.item(i).name)
        except Exception:
            pass

        _slots, problems = bounds.add_bounds_to_component(
            design, comp,
            {k: str(v) for k, v in (cmd.get('values') or {}).items()},
            {k: bool(v) for k, v in (cmd.get('symmetry') or {}).items()},
            True)   # the bounds sketches are the point; there is nothing to opt out of

        _file_bounds_in_browser(design, comp, before, collapse=True)

        _session = None
        mod.close_session()
        if problems:
            _hud('fail', ('Bounds added, but:' if _slots else 'No bounds were added:')
                         + '\n\n  ' + '\n  '.join(problems))
    except Exception:
        _session = None
        tb = traceback.format_exc()
        mod.set_debug(last_error=tb)
        mod.close_session()
        _hud('fail', 'Add Bounds – error:\n' + tb)


def _do_edit_bounds(design, cmd):
    """Apply the edit panel to the session's component. Main thread.

    Re-resolved from the token, same as _do_add_bounds_to_component. What was rebased
    onto the other face — or could not be — is shown afterwards, because a dimension
    that became a plain number is something to go and look at.
    """
    mod = _server_mod
    global _session
    title = 'Edit Component Bounds'
    try:
        comp = None
        token = (_session or {}).get('compToken')
        if token and bounds is not None:
            try:
                found = design.findEntityByToken(token)
                if found:
                    comp = found[0]
            except Exception:
                comp = None
        if comp is None and (_session or {}).get('root'):
            comp = design.rootComponent
        _session = None
        mod.close_session()
        if comp is None:
            ui.messageBox('That component is gone.', title)
            return

        before = set()
        try:
            for i in range(design.userParameters.count):
                before.add(design.userParameters.item(i).name)
        except Exception:
            pass

        report, problems = bounds.edit_bounds(
            design, comp, {k: str(v) for k, v in (cmd.get('values') or {}).items()})

        _file_bounds_in_browser(design, comp, before)

        lines = []
        if report:
            lines += ['Rebased onto the other face:', ''] + ['  ' + r for r in report]
        if problems:
            lines += ([''] if lines else []) + ['Problems:', ''] + ['  ' + p for p in problems]
        if lines:
            ui.messageBox('\n'.join(lines), title)
    except Exception:
        _session = None
        mod.set_debug(last_error=traceback.format_exc())
        mod.close_session()


# ── Event handlers ───────────────────────────────────────────────────────────
class SyncHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _tick_pending, _params_pushed_at
        _tick_pending = 0.0
        if not _active:
            return
        mod = _server_mod
        if mod is None:
            return
        # Stand down if anything else is already in the model — and keep everyone
        # else out while this tick writes a dimension or builds a plane preview.
        # See Tick Guard.py. Missing one 70 ms frame costs nothing.
        with tickguard.tick('dimension value') as go:
            if not go:
                return
            mod.bump_notify()
            try:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if design:
                    _apply_commands(design)
                    _apply_pending_enter(design)
                    _apply_pending_write(design)
                    _apply_pending_delete(design)
                    # Before the pick watcher's first look: unlocking clears the
                    # selection, which must land before it records what was selected.
                    _set_picking(design, (_session or {}).get('mode') == 'sketch'
                                 and mod.session_open())
                    # Only while the dialog is up: pushing the parameter list on every
                    # tick with nothing open would be pure waste on a 70 ms timer.
                    # Even cached, a list that only changes on an edit has no business
                    # being re-read 14 times a second.
                    now = time.monotonic()
                    if _session is not None and now - _params_pushed_at >= _PARAMS_PERIOD:
                        _params_pushed_at = now
                        mod.update_params(_params_snapshot(design), doc=_doc_key())
                    if _session is not None:
                        _watch_plane_pick(design)
                        _watch_dim_pick(design)
                    # A preview belongs to a sketch session. If that session ended some
                    # other way (another box opened over it, the window closed), the
                    # geometry must not be left standing lit in the viewport.
                    if _plane_preview is not None and (
                            (_session or {}).get('mode') != 'sketch'
                            or not mod.session_open()):
                        _drop_plane_preview()
            except Exception:
                mod.set_debug(last_error=traceback.format_exc())


class HotkeyStartingHandler(adsk.core.ApplicationCommandEventHandler):
    """Veto our own command at commandStarting and open the dialog here instead.

    Fusion is single-command-at-a-time: letting this become the active command would
    terminate whatever is running, and the thing running is often the sketch the
    dimension lives in. Refusing the command before it starts sidesteps that entirely —
    the same trick the Browser's dock toggle uses.
    """
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cid = args.commandId
            if cid == CMD_ID:
                opener = _open_dialog
            elif cid == NEWPARAM_CMD_ID:
                opener = _open_new_param_dialog
            elif cid == LINK_CMD_ID:
                opener = _open_link_dialog
            elif cid == INLINE_EDIT_CMD_ID and REPLACE_INLINE_BOX:
                # Only take it over when the dimension can actually be identified. If it
                # cannot, fall through untouched and let Fusion show its own box: a
                # vetoed command with no dialog behind it would mean a dimension you
                # could place but never type a value into.
                design = adsk.fusion.Design.cast(app.activeProduct)
                dim = _selected_dimension(design) if design else None
                if dim is None:
                    return
                # The dimension tool still running means this box is the one that opens
                # ON placement, so the dimension is ours to take back if you cancel. A
                # double-click on an existing one reaches here at idle — see
                # DIMENSION_CMD.
                try:
                    placing = (ui.activeCommand == DIMENSION_CMD)
                except Exception:
                    placing = False
                opener = (lambda: _open_dialog(created=True)) if placing else _open_dialog
            elif cid in GOIN_CMD_IDS:
                # Only take the key when there is actually a dimension to edit; otherwise
                # fall through untouched and let "go in" mean what it always meant.
                design = adsk.fusion.Design.cast(app.activeProduct)
                if not design or _selected_dimension(design) is None:
                    return
                opener = _open_dialog
            else:
                return
            args.isCanceled = True
            if _server_mod:
                # Counted so "did my key even reach the add-in?" is answerable from
                # GET /debug without a debugger — the veto is silent by design, which
                # otherwise makes a dead binding indistinguishable from a dead add-in.
                _server_mod.set_debug(veto_count=_server_mod._debug.get('veto_count', 0) + 1)
            opener()
        except Exception:
            if _server_mod:
                _server_mod.set_debug(last_error=traceback.format_exc())


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, opener):
        super().__init__()
        self._opener = opener

    def notify(self, args):
        try:
            if self not in handlers:
                return
            h = CommandExecuteHandler(self._opener)
            args.command.execute.add(h)
            handlers.append(h)
        except Exception:
            pass


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    """Fallback only — the veto above normally stops the command long before here."""
    def __init__(self, opener):
        super().__init__()
        self._opener = opener

    def notify(self, args):
        self._opener()


# ── Lifecycle ────────────────────────────────────────────────────────────────
def _mirror_into_render(cmd_def):
    """Make `cmd_def` reachable in the Render workspace, where Josh models."""
    try:
        ws = ui.workspaces.itemById(RENDER_WS_ID)
        if not ws:
            return
        tab = (ws.toolbarTabs.itemById(RENDER_TAB_ID)
               or ws.toolbarTabs.add(RENDER_TAB_ID, 'DesignCmds'))
        rp = (tab.toolbarPanels.itemById(RENDER_PANEL_ID)
              or tab.toolbarPanels.add(RENDER_PANEL_ID, 'DCLink'))
        if not rp.controls.itemById(cmd_def.id):
            rp.controls.addCommand(cmd_def)
    except Exception:
        pass


def run(context):
    global _custom_evt, _active, _starting_handler
    try:
        _start_server()
        _active = True
        _stop_evt.clear()
        if section is not None:
            section.STATE['value_box'] = _holds_dimension
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = SyncHandler()
        _custom_evt.add(h)
        handlers.append(h)
        threading.Thread(target=_poller, daemon=True).start()

        for cid, cname, cdesc, opener in (
                (CMD_ID, CMD_NAME, CMD_DESC, _open_dialog),
                (NEWPARAM_CMD_ID, NEWPARAM_CMD_NAME, NEWPARAM_CMD_DESC,
                 _open_new_param_dialog),
                (LINK_CMD_ID, LINK_CMD_NAME, LINK_CMD_DESC, _open_link_dialog)):
            cmd_def = ui.commandDefinitions.itemById(cid)
            if not cmd_def:
                cmd_def = ui.commandDefinitions.addButtonDefinition(cid, cname, cdesc)
            elif cmd_def.name != cname:
                # Link Driven Dimension's definition may still carry Parameters.py's old
                # name from earlier in this Fusion session.
                try:
                    cmd_def.name = cname
                    cmd_def.tooltip = cdesc
                except Exception:
                    pass
            if cid == LINK_CMD_ID:
                _mirror_into_render(cmd_def)
            hc = CommandCreatedHandler(opener)
            cmd_def.commandCreated.add(hc)
            handlers.append(hc)

            # A control is what makes the command appear in Fusion's own keyboard-shortcut
            # list — which is the only keymap in this setup.
            panel = ui.allToolbarPanels.itemById(PANEL_ID)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(cmd_def)

        _starting_handler = HotkeyStartingHandler()
        ui.commandStarting.add(_starting_handler)
        handlers.append(_starting_handler)

        # Started here rather than on first use so the first press is as instant as the
        # rest: `open -a` costs a few hundred visible milliseconds. The process sits
        # hidden until a session is armed.
        _launch_app()

        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Dimension Value failed to start:\n{}'.format(traceback.format_exc()))


def _holds_dimension():
    """Section Sketch asks this before turning a dimension to a reference into one to
    the bounds — which deletes the original, the one this box would then commit to.
    True from the box opening on a dimension until its write lands or it is cancelled."""
    return bool(_session and _session.get('mode') == 'dim')


def stop(context):
    global _custom_evt, _active, _starting_handler, _session, _pending_enter
    global _pending_write, _pending_delete
    try:
        if section is not None and section.STATE.get('value_box') is _holds_dimension:
            section.STATE.pop('value_box', None)
        _active = False
        _stop_evt.set()
        _session = None
        _pending_enter = None
        _pending_write = None
        _pending_delete = None
        if _starting_handler is not None:
            try:
                ui.commandStarting.remove(_starting_handler)
            except Exception:
                pass
            _starting_handler = None
        handlers.clear()
        _set_picking(None, False)
        _stop_server()
        if _custom_evt:
            try:
                app.unregisterCustomEvent(EVENT_ID)
            except Exception:
                pass
            _custom_evt = None
        _kill_app()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cid in (CMD_ID, NEWPARAM_CMD_ID, LINK_CMD_ID):
                ctrl = panel.controls.itemById(cid)
                if ctrl:
                    ctrl.deleteMe()
    except Exception:
        pass
