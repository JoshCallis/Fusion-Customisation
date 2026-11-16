"""
Dim Outside Active
──────────────────
Everything outside the active component draws as if a 50% black sheet lay over it —
not transparent, just darker (on a black background, darker reads as faded). While a
sketch is being edited, EVERY body dims: the sketch is the thing in focus.
The Browser's suspend-mode PEEK (fvs § Suspend mode: peek) is ignored on purpose: it
moves only what is shown, never the active component, so whatever you peek at draws
dimmed — it is not where you are.

How
  Each dimmed body gets a darkened COPY of its own appearance ("<name> · dim"), so wood
  stays wood and black stays black, just halfway to black. Leaving the context puts the
  original back. Copies are made once per original and reused.

Measured on a live design (2026-09-19) — why it's built this way:
  • Occurrence overrides LOSE to body-level appearances, so dimming is per body.
  • Setting a body's appearance through a proxy changes the native body — every
    instance. A component with instances both inside and outside the active one is
    left undimmed rather than dimming the one you're working in.
  • Every appearance write is its own undo transaction (see Flush Undo's notes), so the
    add-in writes only the difference: visible outside bodies are dimmed, a dimmed body
    is only restored once it's both visible and back in context. Hidden bodies cost 0.
  • Calibration (viewport pixels, dimmed/original): colour ×0.5 → 0.47–0.53; texture
    Brightness ×0.30 → 0.50 (Fusion tone-maps textures, so it isn't the same factor).
  • Face-level appearance overrides are ignored — scanning 4.4k faces took 4 s.
  • NEVER edit an appearance a body is using: writing one property of a used appearance
    froze Fusion for 15+ minutes on the big doc (2026-09-30, force-quit). A fresh copy
    is unused while it's written (colour ≈3 ms, texture Brightness ≈19 ms), which is
    why everything here goes copy → write → swap.

Mode tone
  The same copies also lighten or darken the WHOLE model per appearance mode — Light /
  Dark / Extra Dark / Extra Light, the four Control Room cycles through (macOS's own
  AppleInterfaceStyle + fusion_ui/extra.json, read the way remap/server.py's
  contrast_mode_now does). Amounts live in fusion_ui/model_tone.json, -50 (darker) ..
  50 (lighter), set by Control Room's sliders. At a non-zero amount every visible body
  wears "<name> · tone -20" (a dimmed one "<name> · tone -20 dim": toned, then darkened
  as above), so each copy is one (tone, dim) variant of its original. A poller thread
  watches the mode and the file; a change applies once it has held ~0.4 s (a slider
  drag writes the file every 80 ms), and copies of a tone no longer in use are deleted
  after that pass and on save. Darker: colour ×(1+t/100), texture Brightness by that
  factor ^1.74 (the ×0.5 ↔ ×0.30 calibration above). Lighter: colour moves t% toward
  white, texture Brightness ×(1+t/100)^1.74 (Brightness >1.0 is accepted).
  Dim off still tones; both off (or the amount 0) restores everything.

Safety
  Saving restores every body first and re-dims after, so a saved file never holds dim
  appearances. The dim-copy → original map is kept in a design attribute, so a crash
  while dimmed is repaired on the next open (the sweep finds any body still wearing a
  dim copy). Each pass goes by what the visible bodies actually wear, not by memory:
  Undo/Redo replays appearance writes behind the add-in's back (2026-09-29: an undone
  restore left the whole model dim until a save), so a stray dim copy is restored and
  an undone dim is re-applied on the next pass.

When
  Synchronously at the end of fvs.apply_policy (every navigation path ends there, and
  the Browser's never produce a commandTerminated), so dimming and the visibility
  change render in one frame; plus debounced after any command ends, as a backstop.

Lock
  The same things can't be clicked either: they look exactly as dimmed, but hover,
  click and picking inside commands (Joint, Extrude To-object, Project…) ignore them.
  Via the API's own `isSelectable`:
  • outside the active occurrence — the topmost outside occurrences (Occurrence
    .isSelectable, per instance, so shared components are no problem), plus the own
    bodies of the root and of the active occurrence's ancestors (they can't be locked
    as occurrences without locking the active subtree too);
  • while a sketch is edited — every body as well (native BRepBody.isSelectable), but
    no occurrence on the active path, so the sketch itself stays pickable.
  Nothing is ever made unselectable by hand, so anything unselectable is this add-in's:
  a lock target that's already unselectable is adopted, and every apply makes the whole
  active path (active occurrence, its descendants and their bodies, its ancestor
  occurrences) selectable unless it's a current target — stale locks can't stick there.
  The locked tokens are kept in a design attribute for crash repair; saving unlocks
  first, like the dimming.
  At the top (root, or the 'Top Level' occurrence) nothing is locked, and arriving
  there makes EVERY occurrence and body selectable — including locks whose tokens
  went stale and would otherwise pass for hand-set (nothing is ever set by hand).
  The custom Browser's suspend mode unlocks everything (HOOK.set_suspended).

Toolbar: "Dim Outside Active" / "Lock Outside Active" toggle each (state.json beside
this file).
"""

import adsk.core, adsk.fusion
import json, os, subprocess, sys, threading, time, traceback, types

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []


class _NullStep:
    def __enter__(self): return True
    def __exit__(self, *exc): return False


# Tick Guard.py § One undo step: each apply / save-restore below is ONE undo step
# (a read-only one is aborted, dropping its entityToken tracking records). Fails open.
try:
    _TG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'Tick Guard.py')
    tickguard = types.ModuleType('tickguard')
    tickguard.__file__ = _TG_PATH
    with open(_TG_PATH, 'r', encoding='utf-8') as _f:
        exec(compile(_f.read(), _TG_PATH, 'exec'), tickguard.__dict__)
    _one_step = tickguard.one_step
except Exception:
    _one_step = lambda name: _NullStep()

CMD_ID = 'DimOutsideActiveCmd'
PANEL_ID = 'SolidScriptsAddinsPanel'
EVENT_ID = 'DimOutsideActiveApply'
HOOK = 'dim_outside_active_hook'   # sys.modules key fvs.apply_policy calls through
ATTR_GROUP = 'DimOutsideActive'
LOCK_CMD_ID = 'LockOutsideActiveCmd'
LOCK_ATTR = ('DimOutsideActiveLock', 'tokens')   # separate group: _map reads all of ATTR_GROUP
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')

COLOR_K = 0.5        # multiply for plain colour values (sRGB bytes)
TEXTURE_K = 0.30     # multiply for a texture's Brightness — tone-mapped, see header
EMISSION_K = 0.30
DEBOUNCE = 0.15      # s after a command ends before re-evaluating (lets it settle)

# ColorProperty ids that carry a surface's colour. Anything else (subsurface weight,
# luminance scale, cutouts) is left alone.
_COLOR_SUFFIXES = ('_albedo', '_f0', '_f90', '_diffuse', '_color')
_COLOR_SKIP = ('modifier', 'tint', 'mfp')

# Mode tone (see header). The Scripts repo's fusion_ui files, like Appearance Toggle's.
_FUSION_UI = '/Users/josh/Code/Scripts/fusion_ui'
TONE_FILE = os.path.join(_FUSION_UI, 'model_tone.json')
EXTRA_FILE = os.path.join(_FUSION_UI, 'extra.json')
TONE_MAX = 50
TEX_EXP = 1.74       # texture Brightness factor = colour factor ^ this (0.5 → 0.30)
TONE_POLL = 0.5      # s between looks at the mode + file
TONE_SETTLE = 0.4    # s a new amount must hold before it's applied

_enabled = True
_lock_enabled = True
_suspended = False   # the Browser's suspend mode — unlocks while on
_picking = False     # Dimension Value's sketch plane box is open — unlocks while on
_locked = {}         # entityToken → occurrence / native body this add-in made unselectable
_evt = None
_timer = None
_lock = threading.Lock()
_dimmed = {}         # entityToken → (native body wearing one of our copies, what it had)
_doc_key = None      # the document _dimmed describes
_was_top = False     # the last apply was at the top (see _release_everything)
_tone = 0            # the mode-tone amount passes apply (set by the poller, once settled)
_tone_stop = threading.Event()
_pruned = None       # (doc, tone) the last _prune ran for


# ── mode tone ───────────────────────────────────────────────────────────────────

def _load_ui(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(_FUSION_UI, name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


try:
    _macos_style = _load_ui('macos_style')
except Exception:
    _macos_style = None


def _mode_now():
    """light / dark / xdark / xlight — the pref `defaults read` gives, not NSAppearance
    (see remap/server.py contrast_mode_now for why), plus the extras' flag file. Read
    in-process (fusion_ui/macos_style.py): running `defaults` forked Fusion every
    TONE_POLL, ~50 ms each — the view stuttered twice a second."""
    try:
        style = _macos_style.interface_style()
    except Exception:
        style = ''
    try:
        with open(EXTRA_FILE) as f:
            extra = bool(json.load(f).get('on'))
    except Exception:
        extra = False
    if style == 'Dark':
        return 'xdark' if extra else 'dark'
    return 'xlight' if extra else 'light'


def _tone_for_now():
    try:
        with open(TONE_FILE) as f:
            v = json.load(f).get(_mode_now(), 0)
        return max(-TONE_MAX, min(TONE_MAX, int(round(float(v)))))
    except Exception:
        return 0


def _tone_poller(stop):
    """Background thread: a changed amount (mode flipped, slider moved) is handed to
    the main thread once it has held for TONE_SETTLE."""
    global _tone
    seen, since = _tone, 0.0
    while not stop.wait(TONE_POLL):
        t = _tone_for_now()
        now = time.monotonic()
        if t != seen:
            seen, since = t, now
        elif t != _tone and now - since >= TONE_SETTLE:
            _tone = t
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                pass


# ── state file ──────────────────────────────────────────────────────────────────

def _load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return bool(s.get('enabled', True)), bool(s.get('lock', True))
    except Exception:
        return True, True


def _save_state():
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'enabled': _enabled, 'lock': _lock_enabled}, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


# ── dim copies ──────────────────────────────────────────────────────────────────
# Appearance ids are NOT unique — a copy keeps its source's asset id, and a design can
# hold several same-name, same-id appearances. So a dim copy is known by its (unique)
# NAME, and its original by the object itself while this session lasts. The design
# attribute (name = dim copy's name, value = {"name", "id"} of the original) is only
# the fallback after a reload/crash, where same name+id is as close as it gets.

#
# NAMES aren't unique either (2026-09-30: seven "Powder Coat (Black)" in different
# colours, four "Cherry"), so the attribute also records the original's colour
# signature — name+id alone handed one Powder Coat's copy to another after a reload.
# Every copy is one VARIANT of its original: (tone, dim) — see Mode tone. A record
# without "tone"/"dim" predates mode tone and is (0, dim).

_copies = []         # [(original Appearance, its copy, (tone, dim))] — compared with ==
_last = {}           # token → (copy name, what the body had) — outlives the restore,
                     # so a copy that Undo/Redo puts back still restores to the right thing


def _map(design):
    out = {}
    for a in design.attributes.itemsByGroup(ATTR_GROUP):
        try:
            out[a.name] = json.loads(a.value)
        except Exception:
            pass
    return out


def _variant(rec):
    return int(rec.get('tone', 0)), bool(rec.get('dim', True))


def _colour_props(app_):
    """(ColorProperty, its texture or None) for each property carrying surface colour,
    plus the emission FloatProperty (or None)."""
    cols, lum = [], None
    for p in app_.appearanceProperties:
        pid = p.id
        if pid == 'opaque_luminance':
            lum = adsk.core.FloatProperty.cast(p)
            continue
        if not pid.endswith(_COLOR_SUFFIXES) or any(s in pid for s in _COLOR_SKIP):
            continue
        cp = adsk.core.ColorProperty.cast(p)
        if not cp:
            continue
        try:
            tex = cp.connectedTexture if cp.hasConnectedTexture else None
        except Exception:
            tex = None
        cols.append((cp, tex))
    return cols, lum


def _scale(app_, col, tex_k, emis_k):
    """Rewrite `app_`'s colours — only ever on a fresh, unused copy (see header)."""
    C = adsk.core.Color
    cols, lum = _colour_props(app_)
    if lum and emis_k != 1:
        try:
            lum.value *= emis_k
        except Exception:
            pass
    for cp, tex in cols:
        if tex:
            br = tex.properties.itemById('unifiedbitmap_RGBAmount')
            if br and tex_k != 1:
                br.value = br.value * tex_k
            continue
        v = cp.value
        if v:
            cp.value = C.create(col(v.red), col(v.green), col(v.blue), v.opacity)


def _darken(app_):
    _scale(app_, lambda c: round(c * COLOR_K), TEXTURE_K, EMISSION_K)


def _tone_colours(app_, t):
    f = 1 + t / 100.0
    tex_k = f ** TEX_EXP
    if t < 0:
        _scale(app_, lambda c: round(c * f), tex_k, tex_k)
    else:
        _scale(app_, lambda c: round(c + (255 - c) * t / 100.0), tex_k, 1)


def _sig(app_):
    """The original's colours as a string — tells same-name, same-id originals apart."""
    parts = []
    try:
        for cp, tex in _colour_props(app_)[0]:
            if tex:
                br = tex.properties.itemById('unifiedbitmap_RGBAmount')
                parts.append('t%.3f' % br.value if br else 't')
            else:
                v = cp.value
                parts.append('%d.%d.%d' % (v.red, v.green, v.blue) if v else '-')
    except Exception:
        pass
    return ' '.join(parts)


def _matches(rec, orig, sig):
    return rec.get('name') == orig.name and rec.get('id') == orig.id and rec.get('sig') == sig


def _copy_for(design, orig, var, amap):
    """The `var` = (tone, dim) copy of `orig` (made on first use)."""
    for o, c, v in _copies:
        if v == var and o == orig and c.isValid:
            return c
    sig = _sig(orig)
    for cname, rec in amap.items():
        if _variant(rec) == var and _matches(rec, orig, sig):
            c = design.appearances.itemByName(cname)
            if c:
                _copies.append((orig, c, var))
                return c
    tone, dim = var
    suffix = ('tone %+d' % tone if tone else '') + (' dim' if dim else '')
    base = '%s · %s' % (orig.name, suffix.strip())
    name, n = base, 2
    while design.appearances.itemByName(name):
        name = '%s %d' % (base, n); n += 1
    c = design.appearances.addByCopy(orig, name)
    if tone:
        _tone_colours(c, tone)
    if dim:
        _darken(c)
    rec = {'name': orig.name, 'id': orig.id, 'sig': sig, 'tone': tone, 'dim': dim}
    design.attributes.add(ATTR_GROUP, name, json.dumps(rec))
    amap[name] = rec
    _copies.append((orig, c, var))
    return c


def _original_of(design, copy_name, amap):
    """Fallback (no session memory): the appearance matching the recorded original's
    name, id and colours — or, for a record from before signatures, name and id."""
    rec = amap.get(copy_name) or {}
    first = None
    for a in design.appearances:
        if a.name == rec.get('name') and a.id == rec.get('id') and a.name not in amap:
            if 'sig' not in rec or _sig(a) == rec['sig']:
                return a
            first = first or a
    return first if 'sig' not in rec else None


def _source_of(design, cur, amap):
    """The original the copy `cur` was made from."""
    for o, c, _ in _copies:
        if c == cur and o.isValid:
            return o
    return _original_of(design, cur.name, amap)


def _orig_for(design, tok, cur, amap):
    """What a body found wearing the copy `cur` should go back to, when _dimmed
    doesn't know (Undo/Redo re-applied a copy write, a crash, a reopen): what it had
    when this session last gave it a copy (None = inherited), else the copy's original."""
    rec = _last.get(tok)
    if rec and rec[0] == cur.name:
        return rec[1]
    return _source_of(design, cur, amap)


def _prune(design, amap):
    """Delete copies no body wears that the current tone won't ask for again (a tone
    left behind, or a record from before signatures). Copies worn by hidden bodies stay
    until a later prune finds them free."""
    for cname, rec in list(amap.items()):
        if _variant(rec)[0] == _tone and 'sig' in rec:
            continue
        a = design.appearances.itemByName(cname)
        if a is not None:
            if a.isUsed:
                continue
            try:
                if not a.deleteMe():
                    continue
            except Exception:
                continue
        attr = design.attributes.itemByName(ATTR_GROUP, cname)
        if attr:
            attr.deleteMe()
        amap.pop(cname, None)
    _copies[:] = [e for e in _copies if e[1].isValid]


def _native_bodies(design):
    root = design.rootComponent
    comps, seen = [root], {root.entityToken}
    for o in root.allOccurrences:
        c = o.component
        if c.entityToken not in seen:
            seen.add(c.entityToken)
            comps.append(c)
    for c in comps:
        for b in c.bRepBodies:
            yield b


# ── what should be dim ──────────────────────────────────────────────────────────

def _context(design, editing=None):
    """('sketch', None) | ('occ', occurrence) | ('root', None). `editing` = the
    caller already knows whether a sketch is open (see apply)."""
    if editing is None:
        # raises for a sketch nested in an occurrence — the pass is then skipped, and
        # apply_policy's call (which passes `editing`) is the one that counts
        e = app.activeEditObject
        editing = bool(e and e.objectType == adsk.fusion.Sketch.classType())
    if editing:
        return 'sketch', None
    ao = design.activeOccurrence
    return ('occ', ao) if ao else ('root', None)


def _wanted(design, editing=None):
    """({token: native body} for every VISIBLE body that should be dim,
     {token: native body} of every visible body — restore skips hidden ones)."""
    kind, ao = _context(design, editing)
    root = design.rootComponent
    visible, want = {}, {}

    inside_path = ao.fullPathName if ao else None
    inside_comps = set()
    if kind == 'occ':
        inside_comps.add(ao.component.entityToken)
        for o in ao.component.allOccurrences:
            inside_comps.add(o.component.entityToken)

    def consider(native, outside):
        tok = native.entityToken
        visible[tok] = native
        if outside:
            want[tok] = native

    for b in root.bRepBodies:
        if b.isVisible:
            consider(b, kind != 'root')
    for o in root.allOccurrences:
        if not o.isVisible:
            continue
        comp = o.component
        if kind == 'sketch':
            outside = True
        elif kind == 'root':
            outside = False
        else:
            path = o.fullPathName
            outside = not (path == inside_path or path.startswith(inside_path + '+'))
            # a shared component can't be dimmed here without dimming it in there
            if outside and comp.entityToken in inside_comps:
                outside = False
        for pb in o.bRepBodies:
            if pb.isVisible:
                consider(pb.nativeObject, outside)
    return want, visible


# ── what should be locked ───────────────────────────────────────────────────────

def _at_top(design, editing=None):
    """The root, or the root's 'Top Level' occurrence (where Go to Top Level lands),
    is active and no sketch is open."""
    kind, ao = _context(design, editing)
    if kind == 'root':
        return True
    return (kind == 'occ' and ao.assemblyContext is None
            and ao.component.name == 'Top Level')


def _lock_targets(design, editing=None):
    """{token: occurrence or native body} that should be unselectable (see Lock)."""
    if _at_top(design, editing):
        return {}
    kind, _ = _context(design, editing)
    ao = design.activeOccurrence
    root = design.rootComponent
    out = {}

    def add_bodies(bodies, proxy):
        for b in bodies:
            if b.isVisible:
                n = b.nativeObject if proxy else b
                out[n.entityToken] = n

    add_bodies(root.bRepBodies, False)
    if not ao:
        # a sketch edited at the root: every body, no occurrence
        for o in root.allOccurrences:
            add_bodies(o.bRepBodies, True)
        return out

    inside = ao.fullPathName
    parts = inside.split('+')
    ancestors = {'+'.join(parts[:i]) for i in range(1, len(parts))}
    for o in root.allOccurrences:
        path = o.fullPathName
        if path == inside or path.startswith(inside + '+'):
            if kind == 'sketch':
                add_bodies(o.bRepBodies, True)
        elif path in ancestors:
            add_bodies(o.bRepBodies, True)
        elif ('+' not in path) or path.rsplit('+', 1)[0] in ancestors:
            out[o.entityToken] = o      # topmost outside: locks its whole subtree
    return out


def _save_locked(design):
    attr = design.attributes.itemByName(*LOCK_ATTR)
    if not _locked:
        if attr:
            attr.deleteMe()
        return
    v = json.dumps(sorted(_locked))
    if attr:
        attr.value = v
    else:
        design.attributes.add(LOCK_ATTR[0], LOCK_ATTR[1], v)


def _lock_pass(design, want):
    """Bring selectability in line with `want`, touching only the difference."""
    changed = False
    for tok, e in list(_locked.items()):
        if tok in want:
            continue
        try:
            if e.isValid and not e.isSelectable:
                e.isSelectable = True
        except Exception:
            pass
        _locked.pop(tok, None)
        changed = True
    for tok, e in want.items():
        if tok in _locked:
            continue
        if e.isSelectable:
            e.isSelectable = False
        _locked[tok] = e     # already unselectable = a stale lock of ours: adopt it
        changed = True
    if changed:
        _save_locked(design)


def _free_active(design, want):
    """Everything on the active path — the active occurrence, all its descendants and
    their bodies, and the ancestor occurrences — is selectable unless `want` locks it
    (a sketch edit locks bodies). Nothing is ever made unselectable by hand, so an
    unselectable one here is a stale lock that _lock_pass lost track of."""
    ao = design.activeOccurrence
    if not ao or _at_top(design):
        return
    inside = ao.fullPathName
    parts = inside.split('+')
    ancestors = {'+'.join(parts[:i]) for i in range(1, len(parts))}
    for o in design.rootComponent.allOccurrences:
        path = o.fullPathName
        if path in ancestors:
            ents = [o]
        elif path == inside or path.startswith(inside + '+'):
            ents = [o] + [b.nativeObject for b in o.bRepBodies]
        else:
            continue
        for e in ents:
            tok = e.entityToken
            if tok not in want and not e.isSelectable:
                e.isSelectable = True
                _locked.pop(tok, None)


def unlock_all(design=None):
    design = design or _design()
    if design:
        _lock_pass(design, {})


def _release_everything(design):
    """Make every occurrence and body selectable, remembered or not. Nothing is ever
    made unselectable by hand, so an unselectable one is a lock whose token went stale
    (timeline edit, reload, crash) and _lock_pass can no longer see as ours."""
    root = design.rootComponent
    comps, seen = [root], {root.entityToken}
    for o in root.allOccurrences:
        if not o.isSelectable:
            o.isSelectable = True
        c = o.component
        if c.entityToken not in seen:
            seen.add(c.entityToken)
            comps.append(c)
    for c in comps:
        for b in c.bRepBodies:
            if not b.isSelectable:
                b.isSelectable = True
    _locked.clear()
    _save_locked(design)


# ── apply / restore ─────────────────────────────────────────────────────────────

def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


def _sweep(design, amap):
    """Rebuild _dimmed from the document itself (after open / reload / crash)."""
    global _dimmed, _doc_key, _was_top
    _dimmed = {}
    _worn.clear()
    _was_top = False     # a newly opened/switched-to doc gets its release too
    _doc_key = design.parentDocument.creationId if design.parentDocument else None
    _locked.clear()
    attr = design.attributes.itemByName(*LOCK_ATTR)
    try:
        for tok in (json.loads(attr.value) if attr else []):
            ents = design.findEntityByToken(tok)
            if ents:
                _locked[tok] = ents[0]
    except Exception:
        pass
    if not amap:
        return
    for b in _native_bodies(design):
        a = b.appearance
        if a is not None and a.name in amap:
            tok = b.entityToken
            _dimmed[tok] = (b, _orig_for(design, tok, a, amap))


def apply(editing=None):
    """Bring every body's dimming in line. Also called synchronously from the end of
    fvs.apply_policy (via HOOK) so it lands in the same frame as the visibility change;
    `editing` is then apply_policy's own sketch-edit verdict."""
    design = _design()
    if not design:
        return
    amap = _map(design)
    doc = design.parentDocument
    if doc is None or doc.creationId != _doc_key:
        _sweep(design, amap)
    global _pruned
    if _enabled or _tone:
        _dim_pass(design, amap, editing)
        if _pruned != (_doc_key, _tone):
            _prune(design, amap)
            _pruned = (_doc_key, _tone)
    else:
        restore_all(design, amap)
    global _was_top
    top = _at_top(design, editing)
    if top and not _was_top:
        _release_everything(design)   # arriving at the top: a clean slate
    _was_top = top
    want = (_lock_targets(design, editing)
            if _lock_enabled and not _suspended and not _picking else {})
    _lock_pass(design, want)
    _free_active(design, want)


def _dim_pass(design, amap, editing):
    """Judged by what each visible body actually WEARS, not by _dimmed alone: Undo/Redo
    replays appearance writes behind this add-in's back (a restore undone left every
    body dim until the next save), and Fusion's own Appearance dialog repaints."""
    want, visible = _wanted(design, editing)
    if not _enabled:
        want = {}        # tone only

    for tok, (b, _) in list(_dimmed.items()):
        if not b.isValid:
            _dimmed.pop(tok, None)

    for tok, b in visible.items():
        cur = b.appearance
        if cur is None:
            continue
        goal = (_tone, tok in want)    # (0, False) = the original
        rec = amap.get(cur.name)
        if rec is not None:
            # wearing one of our copies
            if tok not in _dimmed:      # (Redo, reopen): adopt it
                _dimmed[tok] = (b, _orig_for(design, tok, cur, amap))
            if _variant(rec) == goal:
                continue
            had = _dimmed[tok][1]
            src = _source_of(design, cur, amap) if goal != (0, False) else None
            if src is None:
                # back to the original (or its original is gone: restore, and the next
                # pass copies whatever it then wears)
                b.appearance = had
                _dimmed.pop(tok, None)
                continue
            c = _copy_for(design, src, goal, amap)
            b.appearance = c
            _last[tok] = (c.name, had)
            continue
        # wearing an original (never copied, a copy undone, or repainted)
        _noticed(tok, cur)
        _dimmed.pop(tok, None)
        if goal == (0, False):
            continue
        own = b.appearanceSourceType in (
            adsk.core.AppearanceSourceTypes.BodyAppearanceSource,
            adsk.core.AppearanceSourceTypes.OverrideAppearanceSource)
        had = cur if own else None
        c = _copy_for(design, cur, goal, amap)
        b.appearance = c
        _dimmed[tok] = (b, had)
        _last[tok] = (c.name, had)


_worn = {}           # token → the original a visible body last wore (this document)


def _noticed(tok, cur):
    """A visible body found wearing original `cur`. One it wore something else before
    was repainted — Fusion's Appearance dialog, the Browser, Undo — so `cur` becomes the
    Material add-in's last material (its Apply Material). Our own restores put back
    what the body wore before, so they never count."""
    was = _worn.get(tok)
    _worn[tok] = cur
    if was is None or was == cur:
        return
    hook = sys.modules.get('material_hook')
    if hook is not None:
        try:
            hook.set_last(cur)
        except Exception:
            pass


def restore_all(design=None, amap=None):
    design = design or _design()
    if not design:
        return
    amap = amap if amap is not None else _map(design)
    if not amap:
        _dimmed.clear()
        return
    # every body, hidden ones too, by what it wears — a save must never keep a dim copy,
    # even one _dimmed lost track of (~7 ms / 120 bodies)
    for b in _native_bodies(design):
        try:
            cur = b.appearance
            if cur is None or cur.name not in amap:
                continue
            tok = b.entityToken
            rec = _dimmed.get(tok)
            b.appearance = rec[1] if rec else _orig_for(design, tok, cur, amap)
        except Exception:
            pass
    _dimmed.clear()


def _safe(fn, *a):
    try:
        with _one_step('Dim'):   # nested inside apply_policy's step when called from it
            fn(*a)
    except Exception:
        app.log('Dim Outside Active: ' + traceback.format_exc())


# ── triggers ────────────────────────────────────────────────────────────────────

def _schedule():
    """Debounced: fire the main-thread apply shortly after the last trigger."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE, lambda: app.fireCustomEvent(EVENT_ID, ''))
        _timer.daemon = True
        _timer.start()


class _ApplyHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        if self in handlers:
            t0 = time.perf_counter()
            _safe(apply)
            try:   # TEMP timing (save lag hunt 2026-10-13)
                with open('/tmp/fusion_versions.log', 'a') as f:
                    f.write('%.3f  dim apply %.3fs\n' % (time.time(), time.perf_counter() - t0))
            except Exception:
                pass


class _TermHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if self in handlers and args.commandId not in (CMD_ID, LOCK_CMD_ID):
            _schedule()


class _DocHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        if self in handlers:
            _schedule()


class _SavingHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        if self not in handlers:
            return
        t0 = time.perf_counter()
        design = _design()
        if design and design.parentDocument == args.document:
            with _one_step('Dim'):   # was ~80 steps per save, and as many again after
                _safe(restore_all, design)
                _safe(unlock_all, design)
                # nothing wears a copy now: drop the ones the current tone won't reuse
                _safe(lambda: _prune(design, _map(design)))
        try:   # TEMP timing (save lag hunt 2026-10-13)
            with open('/tmp/fusion_versions.log', 'a') as f:
                f.write('%.3f  dim saving handler %.3fs\n' % (time.time(), time.perf_counter() - t0))
        except Exception:
            pass


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, flag):
        super().__init__()
        self.flag = flag

    def notify(self, args):
        h = _ExecHandler(self.flag)
        args.command.execute.add(h)
        handlers.append(h)


class _ExecHandler(adsk.core.CommandEventHandler):
    def __init__(self, flag):
        super().__init__()
        self.flag = flag       # '_enabled' (dim) or '_lock_enabled'

    def notify(self, args):
        globals()[self.flag] = not globals()[self.flag]
        _save_state()
        _safe(apply)


def sketch_look(design, app_):
    """What a body wearing `app_` would wear while a sketch is open — every body dims
    then. For Section Sketch's slice, which draws the bodies itself (their folders held
    off, so the dim pass never sees them). Makes the copy on first use."""
    if app_ is None:
        return None
    amap = _map(design)
    src = _source_of(design, app_, amap) if app_.name in amap else app_
    if src is None:
        return app_
    goal = (_tone, bool(_enabled))
    return src if goal == (0, False) else _copy_for(design, src, goal, amap)


def _undimmed(design, cur):
    amap = _map(design)
    return _source_of(design, cur, amap) if cur.name in amap else cur


def _paint(bodies, appearance):
    """The Browser's appearance palette: give native `bodies` `appearance`. A body that
    was wearing a dim copy is forgotten first (else the dim pass skips it as done and it
    shows undimmed), then apply re-dims whatever is outside — in its new colour."""
    for b in bodies:
        _dimmed.pop(b.entityToken, None)
        _worn[b.entityToken] = appearance   # recorded, not noticed: the caller says
        b.appearance = appearance           # whether it was a choice (set_last)
    apply()


def _set_picking(on):
    """Dimension Value's sketch plane box: while it is open a click anywhere in the model
    is a plane pick, so nothing outside the active component can stay locked. Its own
    flag, not _suspended, so the Browser's suspend toggle can't cancel it (or vice
    versa). Turned on before the box takes its first look at the selection."""
    global _picking
    if bool(on) != _picking:
        _picking = bool(on)
        _safe(apply)


def _set_suspended(on):
    """Suspend mode is a way of LOOKING, so toggling it must leave the selection alone.
    But any model write — an isSelectable flip, the lock attribute — clears Fusion's
    activeSelections (measured 2026-10-01), so what was selected is put back after the
    unlock / relock. A pick that is now locked (made while suspended, outside the
    active component) can't be re-added and drops, as it should."""
    global _suspended
    if bool(on) != _suspended:
        _suspended = bool(on)
        try:
            kept = [s.entity for s in ui.activeSelections]
        except Exception:
            kept = []
        _safe(apply)
        try:
            if kept and ui.activeSelections.count == 0:
                for e in kept:
                    try:
                        if e.isValid:
                            ui.activeSelections.add(e)
                    except Exception:
                        pass     # locked now, or a running command owns the selection
        except Exception:
            pass


_EVENTS = []   # (event, handler) pairs to detach in stop()


def run(context):
    global _enabled, _lock_enabled, _evt, _tone
    try:
        _enabled, _lock_enabled = _load_state()
        _tone = _tone_for_now()
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _evt = app.registerCustomEvent(EVENT_ID)
        for event, h in ((_evt, _ApplyHandler()),
                         (ui.commandTerminated, _TermHandler()),
                         (app.documentActivated, _DocHandler()),
                         (app.documentOpened, _DocHandler()),
                         (app.documentSaved, _DocHandler()),
                         (app.documentSaving, _SavingHandler())):
            event.add(h)
            handlers.append(h)
            _EVENTS.append((event, h))

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid, name, tip, flag in (
                (CMD_ID, 'Dim Outside Active',
                 'Toggle: bodies outside the active component (or all bodies while editing '
                 'a sketch) draw halfway to black.', '_enabled'),
                (LOCK_CMD_ID, 'Lock Outside Active',
                 'Toggle: whatever is outside the active component (or every body while '
                 'editing a sketch) can\'t be selected. The Browser\'s suspend mode unlocks.',
                 '_lock_enabled')):
            cd = ui.commandDefinitions.itemById(cid) or \
                ui.commandDefinitions.addButtonDefinition(cid, name, tip)
            h = _CreatedHandler(flag)
            cd.commandCreated.add(h)
            handlers.append(h)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(cd)

        sys.modules[HOOK] = types.SimpleNamespace(
            apply=lambda editing=None: _safe(apply, editing),
            set_suspended=_set_suspended,
            set_picking=_set_picking,
            paint=lambda bodies, appearance: _safe(_paint, bodies, appearance),
            sketch_look=sketch_look,
            # what a body wearing the dim copy `cur` really wears (None = inherited) —
            # the Browser's palette lists that, not the copy
            original=lambda design, tok, cur: _orig_for(design, tok, cur, _map(design)),
            # the appearance a copy was made from (anything else as it is) — what a
            # body wearing `cur` shows undimmed; the Material add-in's Pick
            undimmed=_undimmed)
        _tone_stop.clear()
        threading.Thread(target=_tone_poller, args=(_tone_stop,), daemon=True).start()
        _schedule()
        adsk.autoTerminate(False)
    except Exception:
        ui.messageBox('Dim Outside Active failed to start:\n' + traceback.format_exc())


def stop(context):
    global _evt
    _tone_stop.set()
    sys.modules.pop(HOOK, None)
    _safe(restore_all)
    _safe(unlock_all)
    try:
        if _timer:
            _timer.cancel()
        for event, h in _EVENTS:
            try:
                event.remove(h)
            except Exception:
                pass
        _EVENTS.clear()
        handlers.clear()
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _evt = None
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid in (CMD_ID, LOCK_CMD_ID):
            ctrl = panel.controls.itemById(cid) if panel else None
            if ctrl:
                ctrl.deleteMe()
    except Exception:
        pass
