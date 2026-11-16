#!/usr/bin/env python3
"""
Fusion Value — local web server for the dimension value / parameter dialog.

Fourth window in the family (Params 8731, Browser 8732, Shortcuts 8733, this 8734).
Same architecture as the others: the Fusion add-in imports this module and,
from its main-thread sync handler, calls open_session() / update_params() and
pop_commands(). The window (a WKWebView app) polls /state and POSTs to /cmd.

The HTTP thread only ever touches the in-memory snapshot and the command queue under
a lock — never the Fusion API.

Unlike the other two, this window is MODAL in spirit: it is hidden until the add-in
opens a session (the user pressed a hotkey — with a sketch dimension selected to edit
its value, or the new-parameter key), and hides again the moment the session is
committed or cancelled. `seq` is what the window
edge-detects to know a NEW session started, so pressing the hotkey again while the
window is already up re-arms it rather than stacking.

Run standalone to serve the UI for styling work (it'll sit in its closed state).
"""

import importlib.util, json, os, queue, subprocess, threading, time

PORT = 8734
HERE = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = os.path.join(os.path.dirname(HERE), "fusion_ui")
# The shared server plumbing — static files, CORS, /settings, and every shared engine
# the dialog loads (naming.js, planes.js, chips.js, capitalise.js, param_naming.*,
# variables.json: served rather than copied, so what the dialog shows and what the
# add-ins' .py twins write are one set of rules). See fusion_ui/webwin.py's header.
_spec = importlib.util.spec_from_file_location(
    "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)

# Explicit parameter tags, per document. What a name does not SAY, this file records:
# the naming engine reads "PowerboardWidth" and knows its axis, but "Steel" carries
# nothing, and tagging it is how the suggestion list is told what it is for.
TAGS_FILE = webwin.shared("tags.json")

# A Condition rather than a bare Lock: `with _lock:` works exactly the same, and every
# `seq` bump also notify_all()s, which is what lets GET /wait return the instant a
# session is armed instead of on the page's next /state tick.
_lock = threading.Condition()
_cmd_queue: queue.Queue = queue.Queue()
# The extrude panel's commands go in a queue of their OWN, and this is why: every other
# command here is drained in-process by the Dimension Value add-in, which imported this
# module and owns the tick. The extrude command is hosted by a different add-in — it has
# to be, because it needs a live Fusion command of its own to hang the viewport preview
# off — and a different add-in is a different module object over HTTP, not a shared one.
# Sharing the single queue would mean whichever tick ran first swallowed the other's
# commands and dropped them on the floor.
_extrude_queue: queue.Queue = queue.Queue()
_EXTRUDE_ACTIONS = ("openextrude", "setextrude", "clearextrude",
                    "commitextrude", "cancelextrude")
# The Mirror add-in is a third process-level party for exactly the same reason, so its
# panel's commands get a third queue.
_mirror_queue: queue.Queue = queue.Queue()
_MIRROR_ACTIONS = ("setmirror", "clearmirror", "commitmirror", "cancelmirror")
# The add-ins on AddIns/Feature Panel.py (Rectangular Pattern, Combine, Scale…) are more
# parties of the same kind, one queue each, keyed by the `feature` kind every one of
# these actions carries — made on first use, so a new one needs nothing here.
_feature_queues: dict = {}
_FEATURE_ACTIONS = ("setfeature", "clearfeature", "commitfeature", "cancelfeature")
# The Versions add-in (AddIns/Versions) is one more party: the panel's picks, and the
# Browser's menu asking for the panel ("versionsshow"), go on its own queue.
_versions_queue: queue.Queue = queue.Queue()
_VERSIONS_ACTIONS = ("versionsshow", "versionsopen", "versionsclose", "versionsactivate",
                     "versionsanswer", "versionscancel")


def _feature_queue(kind):
    with _lock:
        return _feature_queues.setdefault(str(kind or ""), queue.Queue())


def _make_queue():
    """The live command's queue a makeparam belongs on — the Extrude's or the open
    feature panel's — or None when the session is the Dimension Value add-in's own."""
    with _lock:
        mode = _state.get("mode") if _state.get("open") else ""
        kind = (_state.get("feature") or {}).get("kind")
    if mode == "extrude":
        return _extrude_queue
    if mode == "feature" and kind:
        return _feature_queue(kind)
    return None

# The whole of what the window renders. Deliberately one flat dict: the window is a
# single transient dialog, so there is no tree to diff and no reason to split the
# snapshot across endpoints the way the Browser does.
# Every counter below starts from the clock, not 0 — see webwin.seq_base: the window
# outlives this server, and a restart counting from 0 again left it ignoring sessions.
_SEQ0 = webwin.seq_base()

_state = {
    "seq": _SEQ0,      # bumped once per session; the window edge-detects this to open
    "open": False,     # a session is live (window should be showing)
    "ts": 0,

    # ── session payload (valid while open) ──
    # Two kinds of session share this one dialog. "dim" is the original: a sketch
    # dimension is selected and the value box edits it, with the name field optional.
    # "param" has no dimension behind it at all — it just makes a user parameter, so the
    # name is the point rather than an extra, and the panel opens in the middle of the
    # screen because there is no dimension on the canvas for it to sit beside.
    "mode": "dim",     # 'dim' | 'param' | 'editparam' | 'component' | 'addbounds' |
                       # 'editbounds' | 'sketch' | 'extrude' | 'mirror' | 'feature' |
                       # 'search'
    "place": "cursor", # 'cursor' | 'center' | 'corner' — where the host app puts the window
    "searchEnv": "",   # 'sketch' | 'viewport' | 'extrude' | '' — sampled by Shortcuts.py
                       # at the moment the search key is pressed (only Fusion's own
                       # process can read it); '' means unknown, not "no filter" — the
                       # ranker treats that as "show everything" too, see searchRank.
    "nameRequired": False,   # commit is refused without a valid, free name
    "expression": "",  # what the dimension currently reads, prefilled into the value box
    "name": "",        # prefill for the NAME box (an existing parameter being edited);
                       # the window treats it as the name that is allowed to stay taken
    "unit": "",        # the dimension's unit ('mm', 'deg', …) — the unit a new parameter gets
    "dimName": "",     # the dimension's own parameter name (d12), shown as a subtitle
    "axis": "",        # 'X' | 'Y' | 'Z' | '' — the world axis this dimension runs along
    "end": "",         # 'high' | 'low' | '' — which side of the origin along that axis.
                       # axis + end is the SITUATION: (Y, high) resolves the family
                       # EdgeOffset to EdgeBackOffsetY. See naming.py.
    "datum": "",       # 'zero' | '' — what the dimension is measured FROM. A dimension
                       # starting at the origin wants values measured from the origin,
                       # not values measured in from a bound. See naming.py's `frame`.
    "datumFaces": [],  # bound faces at the origin datum too — see open_session
    "symmetric": False,  # is the measured axis symmetric on this component?
    "component": "",   # the active component's name — the name stepper's default
                       # owner, its first feature level (fusion_ui/param_naming.js)
    "componentPath": [],  # root first, down to `component` — the stepper's owner dropdown
    "components": [],  # every component in the design — the owner's type-to-find list
    "filed": None,     # an edited old-convention name's Browser filing — see open_session
    "symmetry": {},    # {axis: bool}, that component's — same shape as axisSymmetry
                       # below, but this copy travels with 'dim'/'param' sessions

    # ── the New Component session (mode 'component') ──
    # Same window, a different panel. A component's bounds are expressions like
    # "Width" or "HalfDepth", so they want exactly the autocomplete the value box
    # already has — which is the reason this is a mode here rather than a window of
    # its own.
    "parent": "",        # the component the new one is created inside, for the label
    "bounds": {},        # {direction: expression} prefilled from the parent
    "axisSymmetry": {},  # {axis: bool}. NOT "symmetric", which means something else
                         # in a dimension session and would collide.
    "makeSketches": True,
    "sheetRules": [],    # [{name, isDefault}] — design rules first, then library
                         # (Bounds.list_sheet_metal_rules' order). Empty for
                         # 'addbounds': an existing component isn't converted here.
    "sheetRule": "",     # the rule name preselected when the checkbox is ticked —
                         # the library's default rule, same as native's own dialog.

    # ── the Smart Sketch session (mode 'sketch') ──
    # One line: a datum word, then optionally how far in from it. The words on offer
    # depend on the component's bounds — a symmetric axis has a midline and a side, an
    # asymmetric one has two named faces — so they are computed on the Fusion side and
    # sent, rather than derived from the vocabulary here.
    "datums": [],      # [{word, kind, axis, end, sign, face, datum, symmetric}]
    "angle": {},       # {text, flip, plus90, pivot} — an angled plane's box state;
                       # empty for a flat one. Prefilled only when redefining.
    "redefine": False, # editing the plane an existing sketch already sits on
    "flat": False,     # Project Bounds' key: no angle to offer at all (the angle row
                       # is hidden, not just empty) — a sketch already square to an
                       # axis stays flat, see bounds.py's _move_onto_new_plane.
    "sketch": "",      # that sketch's entityToken, sent back with the commit
                       # (the SKETCH's own name rides on "name", shared with the
                       # parameter sessions — see open_sketch_session)
    "sketches": [],    # [{name, token, text, angle}] — other plane-box sketches in
                       # this component, offered by the "Same as..." picker. `angle`
                       # is {text, flip, plus90, pivot} or null (flat). Empty unless
                       # the add-in found at least one.
    "context": "",     # human label for where a new parameter would be filed
    "isDriven": False, # driven dimensions can be read but not set
    "created": False,  # a dimension placed just now: its value is only where the pointer let go

    # ── the Extrude session (mode 'extrude') ──
    # Same window again, and for the same reason as the others: the distance and the
    # taper are EXPRESSIONS, so they want the parameter autocomplete that is already
    # here. What is different is that a live Fusion command is running behind this one,
    # drawing the real preview in the viewport — so the panel is not composing something
    # to hand over at the end, it is steering something that is already happening. Every
    # edit goes straight back as a "setextrude".
    "extrude": {},     # {distance, direction, operation, isSolid, start, startOffset,
                       #  taper, hasSelection, selectionLabel, component, inward,
                       #  operationAuto, unit} — see open_extrude_session

    # ── the Mirror session (mode 'mirror') ──
    # The Extrude arrangement again — a live command behind the panel, every choice
    # posted straight back as a "setmirror" — minus the expressions: every row is a
    # choice, so the panel has no text field at all.
    "mirror": {},      # {plane, operation, compute, hasSelection, selectionCount, kind,
                       #  component, pickedLabel, keys} — see open_mirror_session

    # ── a Feature Panel session (mode 'feature') ──
    # The Mirror arrangement for any add-in on AddIns/Feature Panel.py. The panel is
    # described as data (`rows`) rather than written out per feature, so the page builds
    # it from what arrives here. See open_feature_session.
    "feature": {},     # {kind, rows, spec} — spec: the add-in's choices, fields and
                       #  read-outs (selectionText, picked, armed, keys, …)
    "versions": {},    # {file, docs, list, loading, confirm, error} — AddIns/Versions

    # ── the parameter list the value box autocompletes against ──
    "params": [],      # [{name, expression, unit, value, comment}]
    # The document those parameters belong to, as the stable key the tag store is
    # filed under. Parameters are per-document, so their tags have to be: two files
    # can both have a "Thickness" and mean different axes by it.
    "doc": "",

    # ── live feedback, written by the add-in's tick ──
    "preview": "",     # evaluated value of what is currently typed ("25.4 mm")
    "previewFor": "",  # the expression `preview` was computed for (stale-guard)
    "error": "",       # why the last commit was refused; window stays open showing it
    "errSeq": _SEQ0,     # bumped with every error so the window re-shows an identical one
    "madeEdit": {},    # {old, new} when the answer was to an "editmade", else {}
    "made": "",        # the parameter "make a parameter from this" just created
    "madeSeq": _SEQ0,    # bumped with each one, so the window acts on it exactly once
    # Who could own a parameter made from a selection — the name stepper's parent step —
    # asked for by the page (action "makeinfo") in a session that never said: Extrude,
    # the plane box, the panels. {component, componentPath, components, symmetry}.
    "makeInfo": {},
    "makeInfoSeq": _SEQ0,
    "insert": "",      # a parameter clicked in the Browser, for the value box's caret
    "insertSeq": _SEQ0,  # bumped with each one — see POST /insert
    # A face (or edge…) clicked in the viewport while the plane box is up, read back
    # into ranked datum lines by the add-in (plane_guess) — or, in a 'dim' session, into
    # the one expression the dimension should be (_watch_dim_pick). `error` says why a
    # pick gave nothing (a slanted face, the wrong axis). Cleared whenever a session opens.
    "pick": {"items": [], "error": ""},   # items: [{text, note}], best first
    "pickSeq": _SEQ0,    # bumped with each pick, so the window acts on it once
    # Fusion's Super Escape (the Browser add-in's SuperEscapeCmd) asking the page to
    # Close Whole Dialog, whatever is open inside it — bumped by escape_all().
    "escSeq": _SEQ0,
}

_debug = {
    "notify_count": 0,
    "last_sync_ts": 0,
    "last_open_ts": 0,
    "last_commit": "",
    "last_error": "",
}


def set_debug(**kw):
    with _lock:
        _debug.update(kw)


def bump_notify():
    with _lock:
        _debug["notify_count"] += 1


# ── Called from the Fusion main thread ───────────────────────────────────────

def open_session(expression="", unit="", dim_name="", axis="", end="", datum="",
                 symmetric=False, context="",
                 is_driven=False, params=None, mode="dim", place="cursor",
                 name_required=False, name="", component="", symmetry=None,
                 component_path=None, components=None, filed=None,
                 axes=None, ends=None, dir_known=False, datum_faces=None,
                 created=False, proposal=None, flex=None):
    """Arm a new dialog session. Bumping `seq` is what makes the window appear."""
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        # The add-in's guess at a NEW name's type and direction ({type, anchors, axes}),
        # applied once by the stepper so naming asks only for the feature word — and the
        # parameter's flexible range ({flexible, min, max}, Section.read_flex). See
        # index.js, stepProposal / the Flexible row.
        _state["proposal"] = dict(proposal) if isinstance(proposal, dict) else None
        _state["flex"] = dict(flex) if isinstance(flex, dict) else None
        _state["mode"] = mode or "dim"
        _state["place"] = place or "cursor"
        _state["nameRequired"] = bool(name_required)
        _state["name"] = name or ""
        # The active component's name — the name stepper's auto-prefixed, un-typed
        # first feature level (fusion_ui/param_naming.js) — and its per-axis symmetry,
        # which decides whether an Inset anchor step offers a low-end word (Front, Left,
        # Bottom) at all. Same shape as open_component_session's `symmetry` below.
        _state["component"] = component or ""
        # Root first, down to `component`: every component that could own the new
        # parameter, offered in the stepper's owner dropdown. An add-in that sends no
        # path still gets the one component it named.
        _state["componentPath"] = (list(component_path) if component_path
                                   else ([component] if component else []))
        # Every component in the design, for typing an owner by name (the path above
        # only reaches the active one's ancestors).
        _state["components"] = list(components or [])
        # Where an old-convention parameter being edited is hand-filed in the Browser —
        # {owner: raw component name, groups: [raw group names, outermost first]} — so a
        # rename's stepper opens with those as its owner and group chips. None otherwise.
        _state["filed"] = dict(filed) if isinstance(filed, dict) else None
        _state["symmetry"] = dict(symmetry or {})
        _state["expression"] = expression or ""
        _state["unit"] = unit or ""
        _state["dimName"] = dim_name or ""
        _state["axis"] = axis or ""
        # 'high' | 'low' | '' — which side of the origin along `axis` this dimension is
        # happening on. With `axis` it makes the situation the autocomplete resolves a
        # parameter family against: (Y, high) picks EdgeBackOffsetY out of EdgeOffset.
        _state["end"] = end or ""
        # What the dimension is measured FROM: 'zero' when one side sits on the origin
        # of `axis`, else ''. Decides which parameters share its frame of reference.
        _state["datum"] = datum or ""
        # Bound faces that sit AT the origin datum too (['top'] for a component whose
        # top face is at world zero) — the dimension is measured from both at once.
        _state["datumFaces"] = list(datum_faces or [])
        # The fuller 45°-pool answer the structured convention's ParamNaming.fits()
        # reads (fusion_ui/param_naming.py, window-side namingSituation()) — every axis
        # the dimension is within 45° of (45° itself included) and, per axis, which
        # end. Falls back to [axis]/{axis: end} window-side when an add-in sends
        # neither, so this is optional and additive.
        _state["axes"] = list(axes) if axes else []
        _state["ends"] = dict(ends) if isinstance(ends, dict) else {}
        _state["dirKnown"] = bool(dir_known)
        # Is the measured axis symmetric on this component? Decides whether the span
        # words are in the vocabulary at all, and whether "from the right" and "from the
        # width" name the same bound.
        _state["symmetric"] = bool(symmetric)
        _state["context"] = context or ""
        _state["isDriven"] = bool(is_driven)
        _state["created"] = bool(created)
        if params is not None:
            _state["params"] = params
        _state["preview"] = ""
        _state["previewFor"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def open_component_session(parent="", values=None, symmetry=None,
                           make_sketches=True, params=None,
                           sheet_rules=None, default_rule=""):
    """Arm the New Component panel. Runs on the Fusion main thread, like every opener.

    Top-middle, because there is no dimension on the canvas for it to sit beside.
    """
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "component"
        _state["place"] = "top"
        _state["parent"] = parent or ""
        _state["bounds"] = dict(values or {})
        _state["axisSymmetry"] = dict(symmetry or {})
        _state["makeSketches"] = bool(make_sketches)
        _state["sheetRules"] = list(sheet_rules or [])
        _state["sheetRule"] = default_rule or ""
        # The bounds fields autocomplete against the same parameter list as everything
        # else here, so they need it in the snapshot too.
        if params is not None:
            _state["params"] = params
        _state["unit"] = ""
        _state["axis"] = ""
        _state["end"] = ""
        _state["datum"] = ""
        _state["datumFaces"] = []
        _state["symmetric"] = False
        _state["expression"] = ""
        _state["name"] = ""
        _state["nameRequired"] = True
        _state["dimName"] = ""
        _state["context"] = ""
        _state["isDriven"] = False
        _state["preview"] = ""
        _state["previewFor"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def open_add_bounds_session(component_name="", values=None, symmetry=None, params=None,
                            edit=False):
    """Arm the New Component panel's rows, aimed at a component that already exists.

    Same fields as open_component_session — the bounds themselves are asked for the
    same way either way — but there is no name to type (the component keeps the one it
    has) and no parent (the target IS the component). Top-middle, same reason.

    `edit` is the same panel for a component that already HAS bounds (mode
    'editbounds'): the fields hold its current expressions, a blank one is a face left
    out, and blanking one removes that face.
    """
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "editbounds" if edit else "addbounds"
        _state["place"] = "top"
        _state["parent"] = ""
        _state["name"] = component_name or ""
        _state["bounds"] = dict(values or {})
        _state["axisSymmetry"] = dict(symmetry or {})
        _state["makeSketches"] = True
        _state["sheetRules"] = []   # an existing component isn't converted here
        _state["sheetRule"] = ""
        if params is not None:
            _state["params"] = params
        _state["unit"] = ""
        _state["axis"] = ""
        _state["end"] = ""
        _state["datum"] = ""
        _state["datumFaces"] = []
        _state["symmetric"] = False
        _state["expression"] = ""
        _state["nameRequired"] = False
        _state["dimName"] = ""
        _state["context"] = ""
        _state["isDriven"] = False
        _state["preview"] = ""
        _state["previewFor"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def open_sketch_session(datums=None, text="", redefine=False, sketch="",
                        angle=None, flat=False, sketches=None, params=None,
                        name=""):
    """Arm the Smart Sketch box: one line, a datum and how far in from it.

    Reuses the value panel rather than a panel of its own — the line IS an expression
    after the first word, so it wants the same box, the same autocomplete and the same
    live preview. The PARAMETER name field is out of place and is hidden, its row given
    over to the readout; the box shows a field of its own for the SKETCH's name.

    `name` is the name that sketch already has (redefining one, or Project Bounds), so
    leaving the field alone renames nothing. Empty for a new sketch, where empty means
    "whatever Fusion calls it".
    """
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "sketch"
        _state["place"] = "top"
        _state["datums"] = list(datums or [])
        _state["redefine"] = bool(redefine)
        _state["flat"] = bool(flat)
        _state["sketch"] = sketch or ""
        _state["sketches"] = list(sketches or [])
        _state["pick"] = {"items": [], "error": ""}
        _state["expression"] = text or ""
        # The angle box, as stored on a sketch being redefined; empty for a new one.
        _state["angle"] = dict(angle) if isinstance(angle, dict) else {}
        if params is not None:
            _state["params"] = params
        # The situation is read out of the datum word as it is typed, so the session
        # starts with none of it.
        _state["unit"] = ""
        _state["axis"] = ""
        _state["end"] = ""
        _state["datum"] = ""
        _state["datumFaces"] = []
        _state["symmetric"] = False
        # The one field this session does fill: the sketch's own name (see the docstring).
        _state["name"] = name or ""
        _state["nameRequired"] = False
        _state["dimName"] = ""
        _state["context"] = ""
        _state["isDriven"] = False
        _state["preview"] = ""
        _state["previewFor"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def open_extrude_session(spec=None, params=None):
    """Arm the Extrude panel. Called by the Extrude add-in over HTTP, not in-process.

    Unlike every other opener here this one does no Fusion work of its own, so it does
    not need to be queued onto a main-thread tick: the add-in is already ON the main
    thread when it calls this (inside its command's activate), and it has therefore
    worked out the whole situation — what is selected, which component it belongs to,
    which way is in, what operation Fusion's rules land on — before it asks for the
    panel. What arrives here is the answer, not a request to go and find it.
    """
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "extrude"
        _state["place"] = "top"
        _state["extrude"] = dict(spec or {})
        if params is not None:
            _state["params"] = params
        _state["unit"] = (spec or {}).get("unit", "")
        _state["name"] = ""
        _state["nameRequired"] = False
        _state["expression"] = ""
        _state["axis"] = ""
        _state["end"] = ""
        _state["datum"] = ""
        _state["datumFaces"] = []
        _state["symmetric"] = False
        _state["dimName"] = ""
        _state["context"] = ""
        _state["isDriven"] = False
        _state["preview"] = ""
        _state["previewFor"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def update_extrude(spec):
    """Push what the running command now says back to the panel, without re-arming it.

    The operation is the reason this exists: Fusion's own rules decide it from what you
    picked and where the extrusion goes, so it changes UNDER the panel as you type a
    distance or flip the direction. `seq` is deliberately NOT bumped — the window would
    read that as a new session and reset the field you are typing in.
    """
    with _lock:
        merged = dict(_state.get("extrude") or {})
        merged.update(spec or {})
        _state["extrude"] = merged
        _state["ts"] = int(time.time() * 1000)


def open_mirror_session(spec=None):
    """Arm the Mirror panel. Same shape as open_extrude_session, for the same reason:
    the Mirror add-in calls this over HTTP from Fusion's main thread with the
    situation already worked out."""
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "mirror"
        _state["place"] = "top"
        _state["mirror"] = dict(spec or {})
        _state["unit"] = ""
        _state["name"] = ""
        _state["nameRequired"] = False
        _state["expression"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def update_mirror(spec):
    """The Mirror twin of update_extrude — no new seq, so nothing re-arms."""
    with _lock:
        merged = dict(_state.get("mirror") or {})
        merged.update(spec or {})
        _state["mirror"] = merged
        _state["ts"] = int(time.time() * 1000)


def open_feature_session(kind, rows, spec=None, params=None, unit="", title=""):
    """Arm the feature panel for one of the Feature Panel add-ins. Same shape as
    open_mirror_session, plus the rows the page builds the panel from and — when a row
    holds an expression — the parameters it completes against."""
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "feature"
        _state["place"] = "top"
        _state["feature"] = {"kind": str(kind or ""), "rows": list(rows or []),
                             "spec": dict(spec or {}), "title": str(title or "")}
        if params is not None:
            _state["params"] = params
        _state["unit"] = unit or ""
        _state["name"] = ""
        _state["nameRequired"] = False
        _state["expression"] = ""
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def update_feature(kind, spec):
    """The feature twin of update_mirror — no new seq, so nothing re-arms. A push for a
    kind that is no longer the one open is dropped."""
    with _lock:
        cur = _state.get("feature") or {}
        if cur.get("kind") != kind:
            return
        merged = dict(cur.get("spec") or {})
        merged.update(spec or {})
        _state["feature"] = dict(cur, spec=merged)
        _state["ts"] = int(time.time() * 1000)


def open_search_session(env=None):
    """Arm the command-search palette. Called by the Shortcuts add-in — a DIFFERENT
    add-in, over HTTP — from its own router callback, which Fusion already runs on the
    main thread (see fusion_ui/hotkeys.py, Action.invoke). So, like open_extrude_session,
    this does no Fusion work of its own and needs no queueing: the caller has already
    done whatever main-thread work it needed before asking for the panel — which here
    is sampling `env`, the one thing only Fusion's own process can read.

    Opens at the pointer's corner, not centred: the palette is meant to appear right
    where you were about to type."""
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "search"
        _state["place"] = "corner"
        _state["searchEnv"] = env or ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def open_versions_session(data=None):
    """Arm the versions panel (mode 'versions') — the Versions add-in, over HTTP, from
    its main-thread tick. The same panel carries Save's "save as latest?" question on
    its own (`confirm` with no lists), so one session shape covers both."""
    with _lock:
        _state["seq"] += 1
        _lock.notify_all()          # wakes any /wait long-poll
        _state["open"] = True
        _state["mode"] = "versions"
        _state["place"] = "center"
        _state["versions"] = dict(data or {})
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)
        _debug["last_open_ts"] = _state["ts"]


def update_versions(data):
    """The versions twin of update_feature — no new seq, so the panel stays where it is.
    Dropped once the window holds something else."""
    with _lock:
        if not _state.get("open") or _state.get("mode") != "versions":
            return
        merged = dict(_state.get("versions") or {})
        merged.update(data or {})
        _state["versions"] = merged
        _state["ts"] = int(time.time() * 1000)


def session_open():
    with _lock:
        return bool(_state.get("open"))


def showing(mode, kind=None):
    """Whether the window is still holding THIS command's panel — rides back on the
    /extrude, /mirror and /feature/<kind> ticks. Those commands run hidden behind
    their panel, so a panel replaced by another session (Command Search, a value
    box, a relaunch) left the command running with nothing on screen, and Fusion
    read as busy: every idle-only shortcut stopped until some other command ran.
    The add-in ends its command when this stays False. `mode` says what took over."""
    with _lock:
        now = _state.get("mode", "")
        yes = bool(_state.get("open")) and now == mode
        if yes and kind is not None:
            yes = (_state.get("feature") or {}).get("kind") == kind
        return {"showing": yes, "mode": now if _state.get("open") else ""}


def close_session():
    """Session finished (committed or cancelled) — the window hides itself on this."""
    with _lock:
        _state["open"] = False
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)


def escape_all():
    """Super Escape: the page closes the whole dialog it has open (its own cancel, so
    each panel's add-in is told the way its last Escape tells it). True if one was open."""
    with _lock:
        _state["escSeq"] += 1
        _state["ts"] = int(time.time() * 1000)
        return bool(_state.get("open"))


def update_params(params, doc=None):
    """Refresh the autocomplete source. Cheap enough to push on every tick."""
    with _lock:
        _state["params"] = params or []
        if doc is not None:
            _state["doc"] = doc
        _state["ts"] = int(time.time() * 1000)
        _debug["last_sync_ts"] = _state["ts"]


def set_preview(expression, text):
    """Publish the evaluated value of what the user is typing (or '' if it won't parse).

    Carries the expression it was computed FOR so the window can ignore a preview that
    arrived after the text moved on — otherwise a fast typist sees the value flicker
    between the answer to what they typed two keystrokes ago and the current one.
    """
    with _lock:
        _state["previewFor"] = expression or ""
        _state["preview"] = text or ""
        _state["ts"] = int(time.time() * 1000)


def set_error(msg):
    """Refuse a commit and say why. The window stays open with the message showing."""
    with _lock:
        _state["error"] = msg or ""
        _state["errSeq"] += 1
        _state["ts"] = int(time.time() * 1000)
        _debug["last_error"] = msg or ""


def set_plane_suggestions(items, error=""):
    """What a viewport pick says the plane box's line should be (see the add-in's
    _watch_plane_pick), or the dimension box's value (_watch_dim_pick — one item). Wakes
    /wait (`pk=`) like a Browser insert does."""
    with _lock:
        _state["pick"] = {"items": list(items or []), "error": error or ""}
        _state["pickSeq"] += 1
        _state["ts"] = int(time.time() * 1000)
        _lock.notify_all()


def set_make_info(info):
    """The active component and its path, for "make a parameter"'s name stepper in a
    session that did not carry them (see `makeInfo`)."""
    with _lock:
        _state["makeInfo"] = dict(info or {})
        _state["makeInfoSeq"] += 1
        _state["ts"] = int(time.time() * 1000)


def set_made(name, params=None, edited=None):
    """A parameter made from part of the value box exists now. Unlike a commit, the
    session stays open: the window swaps the new name into the box on seeing madeSeq
    move. The parameter list comes in the same write so that name is already known
    to the window — a poll landing between two separate writes would see it made
    but not yet listed, and paint it as a stray word."""
    with _lock:
        if params is not None:
            _state["params"] = params
        _state["made"] = name or ""
        _state["madeEdit"] = dict(edited or {})
        _state["madeSeq"] += 1
        _state["error"] = ""
        _state["ts"] = int(time.time() * 1000)


def pop_commands():
    return webwin.drain(_cmd_queue)


def pop_extrude_commands():
    return webwin.drain(_extrude_queue)


def pop_mirror_commands():
    return webwin.drain(_mirror_queue)


# When the window last asked for /state. Same liveness trick the Browser uses: the page
# polls continuously while it is running, so this is an accurate "is anyone listening"
# signal without pgrep. The add-in consults it before opening a session, and falls back
# to launching the app when nothing has polled recently.
_last_poll = 0.0


def window_alive(max_age=2.0):
    return (time.time() - _last_poll) < max_age


def _read_tags():
    """The whole tag store: {"byDoc": {docKey: {paramName: {dimension: word}}}}.

    A missing or unreadable file is an EMPTY store rather than an error — tags are an
    optional layer over the naming convention, and the dialog has to work on a machine
    that has never tagged anything.
    """
    try:
        with open(TAGS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("byDoc"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"byDoc": {}}


def _tags_for(doc):
    return _read_tags().get("byDoc", {}).get(doc or "", {})


def _write_tag(doc, name, tags):
    """Set (or clear) one parameter's tags in the current document.

    `tags` is the whole tag object for that parameter — {} removes the entry, which is
    what un-tagging the last dimension does, so the file does not fill up with empty
    records.
    """
    if not doc or not name:
        return {}
    store = _read_tags()
    by_doc = store.setdefault("byDoc", {})
    doc_tags = by_doc.setdefault(doc, {})
    clean = {k: v for k, v in (tags or {}).items() if v}
    if clean:
        doc_tags[name] = clean
    else:
        doc_tags.pop(name, None)
    if not doc_tags:
        by_doc.pop(doc, None)
    _atomic_write(TAGS_FILE, json.dumps(store, indent=2))
    return doc_tags


_atomic_write = webwin.atomic_write


class Handler(webwin.Handler):
    HERE = HERE

    def do_GET(self):
        if self.serve_static():
            return
        if self.path.startswith("/wait"):
            # Long-poll: held until `seq` passes the one the page already has (or 20s),
            # so a new session reaches the page in ~1ms rather than up to a full /state
            # interval later. Only a wake-up — the page answers it with an immediate
            # poll(), which stays the one place /state is read. ThreadingTCPServer gives
            # it its own thread, and Condition.wait releases the lock while it waits.
            # `ins=` is the same arrangement for a Browser click (POST /insert) — a page
            # that doesn't send it (an older one) just never wakes for those.
            def arg(key):
                try:
                    return int(self.path.split(key + "=", 1)[1].split("&", 1)[0])
                except (IndexError, ValueError):
                    return None
            have = arg("seq")
            have = -1 if have is None else have
            have_ins = arg("ins")
            have_pk = arg("pk")         # a viewport pick for the plane box, likewise
            with _lock:
                _lock.wait_for(lambda: _state["seq"] > have or
                               (have_ins is not None and _state["insertSeq"] > have_ins) or
                               (have_pk is not None and _state["pickSeq"] > have_pk),
                               timeout=20)
                seq, ins, pk = _state["seq"], _state["insertSeq"], _state["pickSeq"]
            self._send(200, json.dumps({"seq": seq, "insertSeq": ins, "pickSeq": pk}))
        elif self.path == "/state":
            global _last_poll
            _last_poll = time.time()
            with _lock:
                payload = dict(_state)
            # The WKWebView reads its sources (index.html/.css/.js) ONCE, at launch, so
            # a running window keeps serving whatever they said when it started — and
            # reloading the ADD-IN does not help. That has silently cost hours: every
            # test result measured code other than the code on disk. So the page is told
            # how old its own sources are and reloads itself when they change (only while
            # no session is open — see poll() in index.js).
            payload["uiStamp"] = webwin.page_stamp(HERE, extra=("variables.json",))
            self._send(200, json.dumps(payload))
        elif self.path == "/extrude":
            # The Extrude add-in's tick, draining its own queue. It is a different
            # add-in in a different module, so it cannot call pop_extrude_commands()
            # in-process the way the Dimension Value tick calls pop_commands().
            # windowAlive rides along so the add-in can tell whether the window needs
            # healing WITHOUT asking for a heal every time. Healing is not free: it can
            # pkill and relaunch the app, and the wait for that runs on Fusion's main
            # thread — which, with an extrude command live, killed the command.
            self._send(200, json.dumps({"commands": pop_extrude_commands(),
                                        "windowAlive": window_alive(),
                                        **showing("extrude")}))
        elif self.path == "/mirror":
            # The Mirror add-in's tick — the /extrude arrangement exactly.
            self._send(200, json.dumps({"commands": pop_mirror_commands(),
                                        "windowAlive": window_alive(),
                                        **showing("mirror")}))
        elif self.path.startswith("/feature/"):
            # A Feature Panel add-in's tick, draining its own kind's queue.
            kind = self.path[len("/feature/"):]
            self._send(200, json.dumps({"commands": webwin.drain(_feature_queue(kind)),
                                        "windowAlive": window_alive(),
                                        **showing("feature", kind)}))
        elif self.path == "/versions":
            # The Versions add-in's tick — the /mirror arrangement.
            self._send(200, json.dumps({"commands": webwin.drain(_versions_queue),
                                        "windowAlive": window_alive(),
                                        **showing("versions")}))
        elif self.path == "/tags":
            with _lock:
                doc = _state.get("doc", "")
            self._send(200, json.dumps({"doc": doc, "tags": _tags_for(doc)}))
        elif self.path == "/debug":
            with _lock:
                self._send(200, json.dumps(dict(_debug)))
        else:
            self.not_found()

    def do_POST(self):
        data = self.read_json_body()
        if data is None:
            return

        if self.path == "/tags":
            # Tagging is a WINDOW-local edit: it changes nothing in the document and
            # needs nothing from Fusion, so it is written here and answered
            # immediately rather than queued for the add-in's tick.
            with _lock:
                doc = _state.get("doc", "")
            try:
                tags = _write_tag(doc, data.get("name", ""), data.get("tags") or {})
                self._send(200, json.dumps({"ok": True, "tags": tags}))
            except OSError as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return

        if self.path == "/clipboard":
            # Copying the expression, for pasting into another dimension. Done HERE
            # rather than in the page because a WKWebView loaded over plain http is not
            # a place the clipboard APIs can be relied on — navigator.clipboard needs a
            # permission this window can never be granted, and execCommand("copy") is a
            # deprecated path that a WebKit update is free to drop. pbcopy is neither.
            # Nothing about it touches Fusion, so it is served rather than queued.
            text = data.get("text")
            if not isinstance(text, str) or not text:
                self._send(400, json.dumps({"ok": False, "error": "nothing to copy"}))
                return
            try:
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
                self._send(200, json.dumps({"ok": True}))
            except (OSError, subprocess.SubprocessError) as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return

        if self.path == "/insert":
            # The Browser: a parameter row clicked while a value is being edited goes in
            # at the value box's caret. Served, not queued — nothing here touches Fusion.
            # `taken` tells the Browser whether to skip its own click (selecting the row
            # in the viewport), so it is only true when a value box is up to take it.
            name = data.get("name")
            with _lock:
                taken = (isinstance(name, str) and bool(name) and _state["open"]
                         and _state["mode"] in ("dim", "param", "editparam"))
                if taken:
                    _state["insert"] = name
                    _state["insertSeq"] += 1
                    _lock.notify_all()      # wakes the page's /wait long-poll
            self._send(200, json.dumps({"ok": True, "taken": taken}))
            return

        if self.path == "/cmd":
            action = data.get("action")
            # "openparam" is the one command that comes from ANOTHER window rather than
            # from this one's page: the Browser asking for its parameter editor. It is
            # queued like the rest, because opening a session has to happen on Fusion's
            # main thread — the add-in's tick is the only place that is true.
            # "createcomponent" carries the whole New Component form back in one go —
            # name, the six bounds expressions, the symmetry flags — because creating a
            # component is a single main-thread transaction rather than a field at a time.
            # "opencomponent" is the New Component add-in asking for its panel, the
            # same way the Browser asks for the parameter editor: a session has to be
            # armed on Fusion's main thread, so it is queued rather than served here.
            # "openaddbounds" is that panel's sibling — the Add Bounds to Component
            # add-in asking for it aimed at the active component instead of a new one.
            # "addboundstocomponent" is its commit, the existing-component twin of
            # "createcomponent"; "editbounds" is the commit when the component already
            # had bounds and the same panel opened to edit them.
            # "opensketch" and "opensketchplane" are the same arrangement for the plane
            # box — the first from the Smart Sketch add-in with nothing selected, the
            # second from the Browser's menu on a sketch already placed that way.
            # "openprojectbounds" is the Project Bounds add-in's key, the box restricted
            # to the one axis the edited sketch's plane already runs along.
            # "createsketch" carries the typed line back.
            # "retarget" is the Name field's own autosuggest picking an existing
            # parameter mid-session — the add-in's session state has to be told which
            # one, since commit resolves its target from that rather than from
            # anything this posts.
            # The extrude panel's edits go to the OTHER queue — see _extrude_queue.
            if action in _EXTRUDE_ACTIONS:
                _extrude_queue.put(data)
                self._send(200, json.dumps({"ok": True}))
                return
            if action in _MIRROR_ACTIONS:
                _mirror_queue.put(data)
                self._send(200, json.dumps({"ok": True}))
                return
            if action in _FEATURE_ACTIONS:
                _feature_queue(data.get("feature")).put(data)
                self._send(200, json.dumps({"ok": True}))
                return
            if action in _VERSIONS_ACTIONS:
                _versions_queue.put(data)
                self._send(200, json.dumps({"ok": True}))
                return
            # "Make parameter" behind the Extrude or a feature panel goes to THAT
            # command: it is live, every preview it runs is rolled back, and a parameter
            # the Dimension Value tick wrote between two of them went with the next one
            # (AddIns/Made Params.py). The command writes it inside its own previews and
            # execute instead, and answers on /made.
            if action in ("makeparam", "editmade"):
                q = _make_queue()
                if q is not None:
                    q.put(data)
                    self._send(200, json.dumps({"ok": True}))
                    return
            # "ensurewindow" is the Extrude add-in asking for the window to be checked
            # and, if need be, relaunched. Only the Dimension Value add-in knows how to
            # do that (it owns the process and the don't-race-the-pkill wait), and it is
            # a different add-in from the one that will arm the session — see
            # _extrude_queue.
            if action not in ("commit", "cancel", "preview", "openparam",
                              "opencomponent", "createcomponent",
                              "openaddbounds", "addboundstocomponent", "editbounds",
                              "opensketch", "createsketch", "opensketchplane",
                              "openprojectbounds",
                              "previewplane", "ensurewindow", "retarget",
                              "showplanes", "makeparam", "editmade", "makeinfo"):
                self._send(400, json.dumps({"ok": False, "error": "bad action"}))
                return
            # A cancel needs no round trip through Fusion to close the window — the page
            # hides itself immediately — but it still has to reach the add-in so the
            # session is torn down and a stale target token isn't left armed.
            _cmd_queue.put(data)
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/extrude/open":
            # From the Extrude add-in, on Fusion's main thread, with the situation
            # already worked out. Nothing here touches Fusion, so it is served rather
            # than queued.
            open_extrude_session(data.get("spec"), data.get("params"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/extrude/update":
            update_extrude(data.get("spec"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/mirror/open":
            open_mirror_session(data.get("spec"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/mirror/update":
            update_mirror(data.get("spec"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/feature/open":
            open_feature_session(data.get("kind"), data.get("rows"), data.get("spec"),
                                 data.get("params"), data.get("unit", ""),
                                 data.get("title", ""))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/feature/update":
            update_feature(data.get("kind"), data.get("spec"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/versions/open":
            open_versions_session(data)
            self._send(200, json.dumps({"ok": True, "windowAlive": window_alive()}))
        elif self.path == "/versions/update":
            update_versions(data)
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/versions/close":
            # Served, like /search/close: opening a version closes documents, and the
            # panel must be down before the add-in starts on that.
            close_session()
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/made":
            # The Extrude / a feature add-in's answer to a makeparam routed to it —
            # what the Dimension Value add-in calls set_made for in-process.
            set_made(data.get("name", ""), data.get("params"), data.get("edited"))
            self._send(200, json.dumps({"ok": True}))
        elif self.path in ("/extrude/error", "/mirror/error", "/feature/error"):
            # Its own route rather than an action on either queue: an error travels from
            # the add-in TO the page, and both queues run the other way.
            set_error(data.get("error", ""))
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/search/open":
            # From the Shortcuts add-in, over HTTP, from its own main-thread router
            # callback — same reasoning as /extrude/open.
            #
            # windowAlive rides back on the REPLY, the way it rides along on /extrude:
            # this server lives in the Dimension Value add-in, so answering 200 says
            # only that the session is armed, NOT that anything will render it. Without
            # this the Shortcuts add-in had no way to tell an armed session from a
            # rendered one, and a window process that was up but not polling swallowed
            # every press of the search key silently. One round trip on the happy path;
            # the caller asks for a heal only when this comes back false.
            open_search_session(data.get("env"))
            self._send(200, json.dumps({"ok": True,
                                        "windowAlive": window_alive()}))
        elif self.path == "/escape":
            # Super Escape, from the Browser add-in (another add-in, so over HTTP).
            self._send(200, json.dumps({"ok": True, "open": escape_all()}))
        elif self.path == "/search/close":
            # Served rather than queued, on purpose: the page calls this and AWAITS it
            # before asking Shortcuts' server to run the chosen command, so that by the
            # time that command can reopen this window in another mode, _state["open"]
            # is already False. Queuing this through the Dimension Value add-in's tick
            # (the ordinary "cancel" path) would race that reopen instead.
            close_session()
            self._send(200, json.dumps({"ok": True}))
        elif not self.serve_shared_post(data):
            self.not_found()


def make_server():
    return webwin.make_server(PORT, Handler)


if __name__ == "__main__":
    webwin.main("Fusion Value", make_server, PORT)
