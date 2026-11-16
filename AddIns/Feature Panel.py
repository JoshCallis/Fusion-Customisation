"""
Feature Panel — the Mirror arrangement, written once for every add-in that wants it.

Mirror.py (and Extrude.py before it) is a native feature wearing the value window's face:
a command of OUR own runs with its palette hidden, every change rebuilds a real feature
in executePreview, and a panel in the value window steers it. Read Extrude.py's
docstring for why it is built that way, and Mirror.py's for the version with no text
field. Those two each carry their own copy of the plumbing; the later ones — Rectangular
Pattern, Combine, Scale — share this one, and each says only what is its own:

    class Pattern(fpanel.Feature):
        kind, cmd_id, name, desc     who it is ('pattern' is also its queue on 8734)
        rows                         the panel, as data (ROWS below)
        defaults                     the spec every run starts from
        object_filters               what a click picks as a thing to work on
        slots                        other things a click can be: {slot: filters}
        accept(ent)                  a selected entity → the native object, a list of
                                     them (one pick standing for several), or None
        narrow(objs)                 the objects as a set the feature can take
        accept_pick(slot, ent)       the same as accept, for a slot
        begin() / start()            before / after the pre-selection is read
        build(design)                the feature, from self.spec / objects / picks
        extra()                      what the panel shows besides the spec
        spec_changed(before)         the panel changed the spec; before the preview
        cycles                       {shared action: seg row it steps} — the one
                                     Cycle Direction / Cycle Operation key (hotkeys.py)

Loaded with _load() like Bounds.py, so each add-in runs its own copy of this module and
nothing here is shared between two of them at run time.

ROWS
────
  {'type': 'seg',  'key', 'label', 'options': [[value, text], …]}   a choice
  {'type': 'text', 'key', 'placeholder'}                            an expression field
  {'type': 'sel',  'key', 'label', 'button'}                        a read-out
  {'type': 'chips', 'key', 'label', 'single'?}                      choices the add-in lists
Any row may also carry 'adv': True (under Advanced) and 'when': {key: [values]} (shown
only while spec[key] is one of those).

  A 'sel' row reads spec[key + 'Text'] — the add-in's words ("3 bodies", "Body1"; empty
  reads "No") — and its button posts clearfeature {what: key}. 'selection' is the
  objects; any other key names a SLOT, whose button lets go of the picked thing and
  waits for another.

  A 'seg' row shows only the options listed in spec[key + 'Only'] when the add-in
  sends one (a slot's 'picked' always shows) — choices that depend on what is selected.

  A 'chips' row is a 'seg' row whose options come from spec[key + 'Chips'] ([[value,
  text], …], the add-in's, sent with each push — Combine's groups in the component).
  spec[key] is the one lit value with 'single', else a list of them, toggled per click
  (with a Clear).

  A 'seg' row with a 'slot' treats its 'picked' option as "the thing clicked": choosing
  it with nothing picked arms the slot (the next click of the slot's kind is taken),
  and it reads the picked thing's name.

WHAT A CLICK DOES
─────────────────
The command's selection input holds everything clicked while it runs, and the selection
when the command started is put back into it — so the set grows with each click and
shrinks with a click on something already in it, as Fusion's own commands do. While a
slot is armed the filter is the slot's and the NEWEST click it accepts is the picked
thing; that entity is never also an object. Objects are replaced only by a non-empty
set, as in Mirror: Fusion empties the selection as the command starts.

Once picked, a slot's thing is not even OFFERED while objects are being clicked (the
command's preSelect, selectable()): a click on a face it shares with an object — a
Combine tool sitting flush on its target — finds the object instead of taking the pick
again, and a click can't let go of it either. Its row's button does that.

The panel is fusion_value's "The feature panel" (index.js). The server keeps one queue
per kind (open_feature_session / update_feature / GET /feature/<kind>).
"""

import ctypes
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.request

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface

VALUE_URL = 'http://127.0.0.1:8734'
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
PANEL_ID = 'SolidScriptsAddinsPanel'
_TICK_SECS = 0.06


def hotkeys():
    """The shared action registry — Extrude.py's _hotkeys, for the same reasons (ONE
    registry via sys.modules, and hot-reload on the file's mtime)."""
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod


def native(entity):
    try:
        return entity.nativeObject or entity
    except Exception:
        return entity


def token(entity):
    try:
        return entity.entityToken
    except Exception:
        return repr(entity)


def body_of(entity):
    """A body, or a face / edge / vertex's body, as the native body; else None."""
    for e in (entity, getattr(entity, 'body', None)):
        b = adsk.fusion.BRepBody.cast(e) if e is not None else None
        if b is not None:
            return native(b)
    return None


def value(expr, fallback, unit=None):
    """A ValueInput from typed text. In execute (`_numbers` set) a typed length or angle
    becomes a number parameter first (Number Params.py) — the unit is the fallback's
    when not given ('0 mm' is a length; a count's '2' converts nothing)."""
    text = (expr or '').strip() or fallback
    if _numbers is not None:
        if unit is None:
            m = re.search(r'([A-Za-z]+)\s*$', fallback or '')
            unit = m.group(1) if m else ''
        if numbers.np.kind_of_unit(unit):
            design = adsk.fusion.Design.cast(app.activeProduct)
            try:
                text, _left, made = numbers.convert(design, text, unit, _numbers['old'])
                _numbers['made'] += made
            except Exception:
                pass
    return adsk.core.ValueInput.createByString(text)


# {'old': old_numbers() pool, 'made': [names]} while execute builds, else None.
_numbers = None


def params_snapshot(design):
    """The parameter list the panel's expression fields complete against — Extrude's."""
    out = []
    try:
        for p in design.allParameters:
            try:
                out.append({'name': p.name, 'expression': p.expression,
                            'unit': p.unit, 'value': p.value,
                            'comment': p.comment or ''})
            except Exception:
                continue
    except Exception:
        pass
    return out


def count_text(n, words):
    return '%d %s' % (n, words[0] if n == 1 else words[1]) if n else ''


def _tb():
    return traceback.format_exc().replace('\n', ' | ')


def _load_made_params(fname='Made Params.py', alias='madeparams'):
    """AddIns/Made Params.py (or another root module), loaded the way the add-ins load
    this file."""
    import types
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


madeparams = _load_made_params()
numbers = _load_made_params('Number Params.py', 'numbers')


# ── Getting the native palette off the screen ────────────────────────────────
# Extrude.py's section of the same name — read it. GUI THREAD ONLY.

class _Qt:
    def __init__(self):
        libc = ctypes.CDLL(None)
        libc._dyld_image_count.restype = ctypes.c_uint32
        libc._dyld_get_image_name.restype = ctypes.c_char_p
        libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]

        def image(substr):
            b = substr.encode()
            for i in range(libc._dyld_image_count()):
                n = libc._dyld_get_image_name(i)
                if n and b in n:
                    return n.decode()
            return None

        qtw = ctypes.CDLL(image('QtWidgets.framework/QtWidgets'))
        qtc = ctypes.CDLL(image('QtCore.framework/QtCore'))

        class QList(ctypes.Structure):
            _fields_ = [('d', ctypes.c_void_p), ('ptr', ctypes.c_void_p),
                        ('size', ctypes.c_longlong)]

        self._all = qtw['_ZN12QApplication10allWidgetsEv']
        self._all.restype = QList
        self._all.argtypes = []
        self._name = qtc['_ZNK7QObject10objectNameEv']
        self._name.restype = QList
        self._name.argtypes = [ctypes.c_void_p]
        self._setVisible = qtw['_ZN7QWidget10setVisibleEb']
        self._setVisible.restype = None
        self._setVisible.argtypes = [ctypes.c_void_p, ctypes.c_bool]

    def widgets(self):
        lst = self._all()
        if not lst.ptr or not (0 < lst.size < 200000):
            return []
        arr = (ctypes.c_void_p * lst.size).from_address(lst.ptr)
        return [arr[i] for i in range(lst.size) if arr[i]]

    def name(self, w):
        qs = self._name(w)
        if qs.ptr and 0 < qs.size < 4096:
            return bytes((ctypes.c_char * (qs.size * 2)).from_address(
                qs.ptr)).decode('utf-16-le', 'replace')
        return ''

    def hide(self, w):
        self._setVisible(w, False)


_qt = None


def _qt_bind():
    global _qt
    if _qt is None:
        try:
            _qt = _Qt()
        except Exception:
            _qt = False
    return _qt or None


def _handler(base, fn, log):
    """An event handler of `base` that calls fn(args) — one class per wiring, so the
    command's events don't each need a class written out."""
    class _H(base):
        def __init__(self):
            super().__init__()

        def notify(self, args):
            try:
                fn(args)
            except Exception:
                log(fn.__name__, 'failed:', _tb())
    return _H()


# ── The feature ──────────────────────────────────────────────────────────────

class Feature:
    kind = ''
    cmd_id = ''
    name = ''
    desc = ''
    owner = ''
    rows = []
    defaults = {}
    object_filters = ('Bodies',)
    slots = {}                       # slot -> selection filters
    words = ('body', 'bodies')       # what one / several objects are called
    # The shared actions (hotkeys.py's SHARED) this feature provides, and the 'seg' row
    # each one steps: {'operation': 'operation'} is Cycle Operation walking that row.
    cycles = {}

    def __init__(self):
        self.log_path = '/tmp/fusion_%s.log' % self.kind
        self.event_id = 'FeaturePanelTick_' + self.kind
        self.keep = []               # every live handler — Fusion holds them weakly
        self.cmd_handlers = []
        self.created = None
        self.custom_evt = None
        self.poller_stop = None
        self.lock = threading.Lock()
        # Held around every take-from-outbox-and-post, so a post made straight from the
        # main thread (open_panel) can never overtake one the poller has in flight.
        self.post_lock = threading.Lock()
        self.inbox, self.outbox = [], []
        self._reset()

    def _reset(self):
        self.cmd = None
        self.sel_input = None
        self.running = False
        # Set once this command's open has actually reached the window. Until then there
        # is no panel to have lost — see _poller.
        self.opened = False
        # Set by the first activate. Fusion activates the command AGAIN after every view
        # op (orbit, zoom, pan) it ran nested inside it — see _on_activate.
        self.activated = False
        # A cancel the watch queued as the last command ended must not end this one.
        with self.lock:
            del self.inbox[:]
        self.spec = dict(self.defaults)
        self.objects, self.objects_id = [], ()
        self.picks = {}              # slot -> native entity
        self.pick_raw = {}           # slot -> the entity as clicked (for putting back)
        self.pick_toks = {}          # slot -> the tokens its pick was taken from
        self.armed = ''
        self.sel_id = ()
        self.pre = []                # the selection the command started with, as clicked
        self.filter = None
        self.previewed = False
        self.cancelling = False
        self.resolved = {}           # what the last build found out, for the panel
        # The newest panel `rev` acted on, echoed on every push — Extrude's _rev.
        self.rev = 0
        # Half-typed text rows — Extrude's "Half-typed expressions": each row's last
        # whole text, the rows the standing preview stood in for, and what it was built
        # from (so another keystroke into a half-typed row rebuilds nothing).
        self.last_good = {}
        self.half_typed = []
        self.built_key = None
        # A cycle key Fusion had: preview once its push has had a beat to be shown.
        self.preview_at = 0.0
        # "Make parameter" from the panel: written in every preview and in execute, or
        # a preview's rollback takes it — see Made Params.py.
        self.made = madeparams.Made()

    # ── What a subclass says ─────────────────────────────────────────────────

    def accept(self, entity):
        return body_of(entity)

    def narrow(self, objs):
        """All in ONE component — the first one's — since that is where the feature
        goes."""
        if not objs:
            return objs
        comp = objs[0].parentComponent
        return [o for o in objs if o.parentComponent == comp]

    def accept_pick(self, slot, entity):
        return native(entity)

    def pick_label(self, slot, entity):
        try:
            return entity.name
        except Exception:
            return 'Picked'

    def begin(self):
        """Called as the command is created, BEFORE the pre-selection is read — for a
        subclass whose accept() depends on where the command was started (a sketch)."""

    def start(self):
        """Called once the pre-selection has been read, before the panel opens."""

    def build(self, design):
        raise NotImplementedError

    def after_execute(self, result):
        """Called right after execute actually commits build()'s result — still inside
        the command's own execute, so anything done here is part of the same undo step."""

    def extra(self):
        return {}

    def spec_changed(self, before):
        """The panel changed the spec (`before` is how it was) — ahead of the preview."""

    def component(self):
        return self.objects[0].parentComponent if self.objects else None

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def log(self, *parts):
        try:
            with open(self.log_path, 'a') as f:
                f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
        except Exception:
            pass

    def _post(self, path, payload):
        try:
            req = urllib.request.Request(
                VALUE_URL + path, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=2).read()
            return True
        except Exception:
            return False

    def _get(self, path, timeout=2):
        try:
            with urllib.request.urlopen(VALUE_URL + path, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def send(self, path, payload):
        with self.lock:
            self.outbox.append((path, payload))

    def flush(self):
        """Post everything queued, in order. Either thread."""
        with self.post_lock:
            with self.lock:
                pending = self.outbox[:]
                del self.outbox[:]
            for path, payload in pending:
                if self._post(path, payload) and path == '/feature/open':
                    self.opened = True

    def error(self, text):
        self.send('/feature/error', {'error': text})

    def has_text(self):
        return any(r.get('type') == 'text' for r in self.rows)

    # ── What a click picks ───────────────────────────────────────────────────

    def arm(self, slot):
        self.armed = slot
        self.arm_filter()

    def arm_filter(self):
        """What a click in the viewport picks: the armed slot's kind, else the objects'.
        A SolidBodies-style filter takes a click on a face as its body — Extrude found
        that — which is what makes "click the thing" work."""
        want = self.armed or ''
        if self.sel_input is None or want == self.filter:
            return
        self.filter = want
        try:
            self.sel_input.clearSelectionFilter()
            for f in (self.slots.get(want, ()) if want else self.object_filters):
                try:
                    self.sel_input.addSelectionFilter(f)
                except Exception:
                    pass
        except Exception:
            self.log('filter failed:', _tb())

    def _selection(self):
        try:
            sel = ui.activeSelections
            return [sel.item(i).entity for i in range(sel.count)]
        except Exception:
            return []

    def picked_tokens(self):
        return set().union(*self.pick_toks.values()) if self.pick_toks else set()

    def selectable(self, entity):
        """Whether a click may take `entity` now — False for a slot's pick (or a face of
        a picked body) while no slot is armed. See WHAT A CLICK DOES."""
        if self.armed:
            return True
        taken = self.picked_tokens()
        if not taken:
            return True
        b = body_of(entity)
        mine = {token(entity), token(native(entity))}
        if b is not None:
            mine.add(token(b))
        return not (mine & taken)

    def take(self, ents, fresh):
        """Read `ents` (the whole selection, click order) into picks and objects.
        `fresh` is the tokens that were not there last time. True if anything changed."""
        changed = False
        if self.armed:
            taken = self.picked_tokens()
            for e in reversed(ents):
                t = token(e)
                if t not in fresh or t in taken:
                    continue
                got = self.accept_pick(self.armed, e)
                if got is not None:
                    self.set_pick(self.armed, got, e)
                    changed = True
                    break
        taken = self.picked_tokens()
        objs, seen = [], set()
        for e in ents:
            if token(e) in taken:
                continue
            got = self.accept(e)
            if got is None:
                continue
            for g in (got if isinstance(got, (list, tuple)) else (got,)):
                k = token(g)
                if k in seen or k in taken:
                    continue
                seen.add(k)
                objs.append(g)
        objs = self.narrow(objs)
        ident = tuple(token(o) for o in objs)
        if objs and ident != self.objects_id:
            self.objects, self.objects_id = objs, ident
            changed = True
        return changed

    def set_pick(self, slot, entity, raw=None):
        self.picks[slot] = entity
        self.pick_raw[slot] = raw if raw is not None else entity
        self.pick_toks[slot] = {token(entity), token(self.pick_raw[slot])}
        for r in self.rows:
            if r.get('slot') == slot and r.get('type') == 'seg':
                self.spec[r['key']] = 'picked'
        if self.armed == slot:
            self.arm('')

    def adopt(self):
        """Notice a change in the viewport selection. Main thread."""
        ents = self._selection()
        now = tuple(token(e) for e in ents)
        if now == self.sel_id:
            return False
        fresh = set(now) - set(self.sel_id)
        self.sel_id = now
        return self.take(ents, fresh)

    def clear(self, what):
        """The panel's button on a 'sel' row."""
        if what == 'selection':
            self.objects, self.objects_id = [], ()
            self.preview()                 # while every reference is still valid
            try:
                ui.activeSelections.clear()
            except Exception:
                pass
            try:
                if self.sel_input is not None:
                    self.sel_input.clearSelection()
            except Exception:
                pass
            # The picks are still what they were; put them back so they stay lit and are
            # never mistaken for objects the next time round.
            for raw in self.pick_raw.values():
                try:
                    self.sel_input.addSelection(raw)
                except Exception:
                    pass
            self.sel_id = tuple(token(e) for e in self._selection())
        elif what in self.slots:
            self.drop_pick(what)
            self.arm(what)
        self.push_state()

    def drop_pick(self, slot):
        """Let go of a slot's pick: it can be picked again, or be an object."""
        self.picks.pop(slot, None)
        self.pick_raw.pop(slot, None)
        self.pick_toks.pop(slot, None)

    # ── The panel ────────────────────────────────────────────────────────────

    def state(self):
        comp = self.component()
        spec = dict(self.spec)
        spec.update({
            'hasSelection': bool(self.objects),
            'selectionCount': len(self.objects),
            'selectionText': count_text(len(self.objects), self.words),
            'armed': self.armed,
            'picked': {s: self.pick_label(s, e) for s, e in self.picks.items()},
            'component': comp.name if comp is not None else '',
            'resolved': dict(self.resolved),
            # Every chord that means something right now, or only the Active While
            # Typing ones when there is a field to type in — see hotkeys.py.
            'keys': (hotkeys().typing_keys() if self.has_text()
                     else hotkeys().panel_keys()),
            # …the cycles among them, and the row each steps: the panel steps the row
            # itself on one (hotkeys.cycle_keys), rather than wait for the round trip.
            'cycleKeys': hotkeys().cycle_keys(),
            'cycles': dict(self.cycles),
            'rev': self.rev,
        })
        for s, e in self.picks.items():
            spec[s + 'Text'] = self.pick_label(s, e)
        spec.update(self.extra())
        return spec

    def open_panel(self):
        """Posted HERE, on the main thread, not left for the poller: with a lot selected
        what follows (putting the selection back, Fusion's first executePreview) can hold
        the main thread for seconds, and the panel should be up through that rather than
        after it."""
        design = adsk.fusion.Design.cast(app.activeProduct)
        self.send('/feature/open', {
            'kind': self.kind, 'rows': self.rows, 'spec': self.state(),
            'title': self.name,
            'unit': design.unitsManager.defaultLengthUnits if design else '',
            'params': params_snapshot(design) if design and self.has_text() else None})
        self.flush()

    def push_state(self):
        self.send('/feature/update', {'kind': self.kind, 'spec': self.state()})

    def apply_commands(self):
        with self.lock:
            commands = self.inbox[:]
            del self.inbox[:]
        changed = False
        for c in commands:
            action = c.get('action')
            if isinstance(c.get('rev'), (int, float)):
                self.rev = max(self.rev, int(c['rev']))
            if action == 'setfeature':
                before = dict(self.spec)
                for key in self.defaults:
                    if key in c:
                        self.spec[key] = c[key]
                # A slot's 'picked' chosen with nothing picked: wait for the click.
                for r in self.rows:
                    slot = r.get('slot')
                    if not slot or r.get('type') != 'seg':
                        continue
                    if self.spec.get(r['key']) == 'picked':
                        if slot not in self.picks or c.get('repick') == slot:
                            self.drop_pick(slot)
                            self.arm(slot)
                    elif self.armed == slot:
                        self.arm('')
                self.spec_changed(before)
                changed = True
            elif action == 'clearfeature':
                self.clear(c.get('what') or 'selection')
            elif action == 'makeparam':
                self.make_param(c)
            elif action == 'editmade':
                self.edit_made(c)
            elif action == 'commitfeature':
                self.commit()
                return
            elif action == 'cancelfeature':
                self.cancel()
                return
        if changed:
            if self.same_as_built():
                self.push_state()        # the rev still has to go back
            else:
                self.preview()

    # ── Half-typed expressions — Extrude's section of the same name ──────────

    def text_rows(self):
        return [r['key'] for r in self.rows if r.get('type') == 'text']

    def viable_spec(self, design):
        """self.spec as a preview builds it: a half-typed text row as its last whole
        text. Keeps last_good current."""
        out = dict(self.spec)
        for key in self.text_rows():
            text = str(self.spec.get(key) or '').strip()
            if not text:
                self.last_good.pop(key, None)
                continue
            try:
                whole = madeparams.infer_unit(design, text) is not None
            except Exception:
                whole = True
            if whole:
                self.last_good[key] = text
            elif key in self.last_good:
                out[key] = self.last_good[key]
        return out

    def spec_key(self, spec):
        return (json.dumps(spec, sort_keys=True, default=str)
                + repr((self.objects_id, sorted(self.pick_toks.items()), self.armed)))

    def same_as_built(self):
        """Extrude's _same_as_built — never with a made parameter (it exists only
        inside a preview, so out here a row naming one reads as half typed)."""
        if not self.previewed or self.built_key is None or self.made.items or self.made.edits:
            return False
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return False
        return self.spec_key(self.viable_spec(design)) == self.built_key

    def make_param(self, c):
        """Extrude's _make_param: remembered only — the preview the panel's name swap
        sets off writes it, and so does execute."""
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return
        why = self.made.take(design, c)
        if why:
            self.error(why)
            return
        self.send('/made', {'name': self.made.items[-1]['name'],
                            'params': self.made.params(design, params_snapshot(design))})

    def edit_made(self, c):
        """Extrude's _edit_made: an "Edit parameter", remembered and written by the
        next preview and by execute."""
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return
        why = self.made.take_edit(design, c)
        if why:
            self.error(why)
            return
        e = self.made.edits[-1]
        self.send('/made', {'name': e['newName'] or e['name'],
                            'edited': {'old': e['name'], 'new': e['newName']},
                            'params': self.made.params(design, params_snapshot(design))})

    def preview(self):
        if self.cmd is None:
            return
        try:
            self.cmd.doExecutePreview()
            try:
                app.activeViewport.refresh()
            except Exception:
                pass
            self.push_state()
        except Exception:
            self.log('preview failed:', _tb())

    def cycle_row(self, key, d=1):
        """A shared Cycle action: step the 'seg' row `key` on one (d=-1: back one — the
        '_back' twin every entry in `cycles` gets), in the panel's own
        order (only the options the panel is showing, when the add-in narrows them), and
        redraw. Runs from the router inside the live command — the key's own command was
        vetoed before Fusion could start it, so this one is still running."""
        if not self.running or self.cmd is None:
            return
        row = next((r for r in self.rows if r.get('type') == 'seg' and r['key'] == key),
                   None)
        if row is None:
            return
        ring = [v for v, _text in row['options']]
        only = self.state().get(key + 'Only')
        if only:
            ring = [v for v in ring if v in only or v == 'picked']
        if row.get('slot') and row['slot'] not in self.picks:
            ring = [v for v in ring if v != 'picked']
        if not ring:
            return
        now = self.spec.get(key)
        nxt = ring[(ring.index(now) + d) % len(ring)] if now in ring else ring[0]
        self.spec[key] = nxt
        self.log('cycle', key, now, '->', nxt)
        # The row first, the viewport after — Extrude's _defer_choose. Posted here (as
        # open_panel is) so it has gone before the preview holds the main thread; the
        # tick previews after a beat, for the server to answer the panel's long-poll.
        self.push_state()
        self.flush()
        self.preview_at = time.time() + 0.04

    def run_deferred_preview(self):
        if not self.preview_at or time.time() < self.preview_at:
            return
        self.preview_at = 0.0
        self.preview()
        self.push_state()          # preview pushes too, but not when it failed

    def why_not(self):
        """What to say when the feature will not build."""
        return 'That %s will not build.' % self.name.lower()

    def commit(self):
        """Mirror's _commit: preview once more, and only commit what built."""
        if self.cmd is None:
            return
        try:
            self.cmd.doExecutePreview()
        except Exception:
            self.log('commit preview failed:', _tb())
        if not self.previewed:
            self.error(self.why_not())
            return
        if self.half_typed:
            # Previewed as a row's last whole text; execute builds what is typed.
            row = next((r for r in self.rows if r.get('key') == self.half_typed[0]), {})
            self.error('%s is not finished.' % (row.get('placeholder')
                                                 or self.half_typed[0]))
            return
        try:
            self.cmd.doExecute(True)
        except Exception:
            self.log('commit failed:', _tb())

    def cancel(self):
        """doExecute(terminate=True) with the builder off — the only cancel that reaches
        a command whose palette is hidden. See Extrude's _cancel."""
        if self.cmd is None:
            return
        self.cancelling = True
        try:
            self.cmd.doExecute(True)
        except Exception:
            self.log('cancel failed:', _tb())
        finally:
            self.cancelling = False

    def hide_palette(self):
        q = _qt_bind()
        if q is None:
            return
        mark = 'QTCommandDialogContentPanel' + self.cmd_id
        hidden = 0
        try:
            for w in q.widgets():
                if mark in q.name(w):
                    q.hide(w)
                    hidden += 1
        except Exception:
            self.log('hide failed:', _tb())
        if not hidden:
            self.log('palette not found — looked for', mark)

    # ── The command ──────────────────────────────────────────────────────────

    def _on_created(self, args):
        cmd = args.command
        self._reset()
        self.cmd = cmd
        cmd.isAutoExecute = False
        cmd.isExecutedWhenPreEmpted = False
        try:
            cmd.isOKButtonVisible = False
        except Exception:
            pass
        try:
            cmd.setDialogSize(1, 1)
            cmd.setDialogMinimumSize(1, 1)
        except Exception:
            pass
        sel = cmd.commandInputs.addSelectionInput(
            self.kind + 'Selection', 'Objects', self.desc)
        sel.setSelectionLimits(0, 0)
        self.sel_input = sel
        self.begin()
        # The pre-selection, read NOW — Fusion clears it as the command takes over.
        self.pre = self._selection()
        self.take(self.pre, set(token(e) for e in self.pre))
        self.start()
        self.arm_filter()
        del self.cmd_handlers[:]
        for evt, fn, base in (
                (cmd.activate, self._on_activate, adsk.core.CommandEventHandler),
                (cmd.executePreview, self._on_preview, adsk.core.CommandEventHandler),
                (cmd.execute, self._on_execute, adsk.core.CommandEventHandler),
                (cmd.inputChanged, self._on_input, adsk.core.InputChangedEventHandler),
                (cmd.preSelect, self._on_preselect, adsk.core.SelectionEventHandler),
                (cmd.destroy, self._on_destroy, adsk.core.CommandEventHandler)):
            h = _handler(base, fn, self.log)
            evt.add(h)
            self.cmd_handlers.append(h)

    def _on_activate(self, args):
        self.running = True
        if self.activated:
            # Back from a view op: the panel is up, the preview built and the selection
            # is the user's now. Re-opening re-armed the panel (its field re-selected)
            # and putting the pre-selection back undid clicks made since.
            self.hide_palette()
            return
        self.activated = True
        # The panel FIRST: everything after this can be slow with a lot selected.
        self.open_panel()
        self.hide_palette()
        # Put the pre-selection back, so a click adds to it rather than replacing it.
        for e in self.pre:
            try:
                self.sel_input.addSelection(e)
            except Exception:
                pass
        self.sel_id = tuple(token(e) for e in self._selection())

    def _on_preview(self, args):
        self.previewed = False
        self.built_key = None
        self.half_typed = []
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return
        self.made.ensure(design)
        # Half-typed rows build as their last whole text, for this build only.
        viable = self.viable_spec(design)
        swapped = {k: self.spec.get(k) for k in self.text_rows()
                   if viable.get(k) != self.spec.get(k)}
        self.spec.update({k: viable[k] for k in swapped})
        try:
            built = self.build(design)
        finally:
            for k, typed in swapped.items():
                if self.spec.get(k) == viable[k]:
                    self.spec[k] = typed
        if built is None:
            return
        self.half_typed = list(swapped)
        self.built_key = self.spec_key(viable)
        # False: shown, then rolled back — see Extrude's _ExecutePreview.
        args.isValidResult = False
        self.previewed = True

    def _on_execute(self, args):
        design = adsk.fusion.Design.cast(app.activeProduct)
        # Before the cancel's return: a parameter made from the panel stays made.
        if design is not None:
            self.made.ensure(design)
            self.made.finish_edits()
        if self.cancelling:
            return
        if design is None:
            return
        global _numbers
        # Typed lengths and angles become number parameters in the build that is kept
        # (see value()); any the kept feature does not use (a failed build, a first try
        # the build retried negated) go again — drop() spares a used one.
        _numbers = {'old': {}, 'made': []}
        result = None
        try:
            result = self.build(design)
        finally:
            numbers.drop(design, _numbers['made'])
            _numbers = None
        if result is not None:
            try:
                self.after_execute(result)
            except Exception:
                self.log('after_execute failed:', _tb())

    def _on_input(self, args):
        if self.adopt():
            self.preview()

    def _on_preselect(self, args):
        if not self.selectable(args.selection.entity):
            args.isSelectable = False

    def _on_destroy(self, args):
        self._reset()
        self.send('/cmd', {'action': 'cancel'})

    # ── The tick ─────────────────────────────────────────────────────────────

    def _on_tick(self, args):
        if not self.running:
            return
        if self.adopt():
            self.preview()
        self.apply_commands()
        self.run_deferred_preview()

    def _poller(self, stop):
        """All of the network I/O, and the only thing that fires the tick — Mirror's."""
        healing_until = 0.0
        lost = 0.0          # see Extrude's _watch_panel
        while not stop.is_set():
            try:
                self.flush()
                if self.running:
                    got = self._get('/feature/' + self.kind, timeout=1)
                    commands = (got or {}).get('commands') or []
                    if commands:
                        with self.lock:
                            self.inbox.extend(commands)
                    app.fireCustomEvent(self.event_id, '')
                    if (got is not None and not got.get('windowAlive', True)
                            and time.time() >= healing_until):
                        healing_until = time.time() + 6.0
                        self._post('/cmd', {'action': 'ensurewindow'})
                    # A replaced panel must not leave the hidden command running —
                    # Fusion reads as busy and idle-only shortcuts go dead. Only once the
                    # open has gone out: a beat counted from `running` through a slow
                    # start (a big selection) queued a cancel that landed the moment the
                    # panel appeared — it opened and shut at once.
                    if (not self.opened or not got or 'showing' not in got
                            or got.get('showing')
                            or not got.get('windowAlive', True)):
                        lost = 0.0
                    elif not lost:
                        lost = time.time()
                    elif time.time() - lost > 1.5:
                        lost = 0.0
                        self.log('panel gone (window shows %r) — ending the hidden command'
                                 % (got.get('mode') or 'nothing'))
                        with self.lock:
                            self.inbox.append({'action': 'cancelfeature'})
                else:
                    lost = 0.0
            except Exception:
                pass
            stop.wait(_TICK_SECS)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def run(self):
        cmd_def = ui.commandDefinitions.itemById(self.cmd_id)
        if cmd_def is None:
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                self.cmd_id, self.name, self.desc)
        else:
            try:
                if cmd_def.name != self.name:
                    cmd_def.name = self.name
                cmd_def.tooltip = self.desc
            except Exception:
                pass

        def created(args):
            if self.created is None:
                return                    # a stale load's handler
            self._on_created(args)
        self.created = _handler(adsk.core.CommandCreatedEventHandler, created, self.log)
        cmd_def.commandCreated.add(self.created)
        self.keep.append(self.created)

        try:
            app.unregisterCustomEvent(self.event_id)
        except Exception:
            pass
        self.custom_evt = app.registerCustomEvent(self.event_id)
        tick = _handler(adsk.core.CustomEventHandler, self._on_tick, self.log)
        self.custom_evt.add(tick)
        self.keep.append(tick)

        self.poller_stop = threading.Event()
        threading.Thread(target=self._poller, args=(self.poller_stop,), daemon=True).start()

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(self.cmd_id):
            panel.controls.addCommand(cmd_def)
        try:
            hk = hotkeys()
            # The command itself, as a native claimant — see Extrude's.
            hk.declare(self.cmd_id, self.name, self.desc, owner=self.owner,
                       when={'command': 'none'}, native=True)
            for kind, key in self.cycles.items():
                hk.provide(kind, self.owner, {'command': self.cmd_id},
                           lambda key=key: self.cycle_row(key))
                hk.provide(kind + '_back', self.owner, {'command': self.cmd_id},
                           lambda key=key: self.cycle_row(key, -1))
            hk.attach()
        except Exception:
            self.log('could not declare actions:', _tb())

    def stop(self):
        try:
            if self.running and self.cmd is not None:
                self.cancel()
            self._post('/cmd', {'action': 'cancel'})
        except Exception:
            pass
        if self.poller_stop is not None:
            self.poller_stop.set()
        self.poller_stop = None
        cmd_def = ui.commandDefinitions.itemById(self.cmd_id)
        if cmd_def is not None and self.created is not None:
            try:
                cmd_def.commandCreated.remove(self.created)
            except Exception:
                pass
        self.created = None
        if self.custom_evt is not None:
            for h in self.keep:
                try:
                    self.custom_evt.remove(h)
                except Exception:
                    pass
        self.custom_evt = None
        try:
            app.unregisterCustomEvent(self.event_id)
        except Exception:
            pass
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(self.cmd_id)
            if ctrl:
                ctrl.deleteMe()
        try:
            hotkeys().withdraw(self.owner)
        except Exception:
            pass
        del self.keep[:]
        del self.cmd_handlers[:]
        self._reset()
