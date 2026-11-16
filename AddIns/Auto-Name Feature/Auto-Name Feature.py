"""
Auto-Name Feature — add-in.

A feature built from a sketch keeps Fusion's default name ("Extrude1", "Revolve2",
…), which says nothing about what it was made from. This renames it after the
SKETCH it was built from, the moment it is created — the same thing that was being
done by hand. Fusion appends its own " (1)" when the name collides with the
sketch's own name, so an extrude of the sketch "Profile - X" ends up as
"Profile - X (1)", exactly matching the existing hand-named features.

Only NEW features are touched. The timeline length is remembered per document and
only the items a command actually added are examined, so opening an old document
never rewrites its history, and a feature that has already been renamed by hand is
left alone (it no longer carries a default name).

Not every feature has a sketch behind it — a fillet, mirror, pattern, sheet-metal
flange, or an extrude taken from a face has nothing to be named after. Those are
skipped rather than given a made-up name.

One exception comes first: the extrude or flange that gives a component its FIRST
body is called "Base", sketch or no sketch.

Fusion has no "feature created" event, so the scan runs after every command
finishes (ui.commandTerminated) — the same hook Auto-Name Body uses. Renaming via
the API fires no command event, so this can't recurse.
"""

import adsk.core, adsk.fusion
import re, time, traceback

app = adsk.core.Application.get()
ui  = app.userInterface
handlers = []
_running = False

LOG = '/tmp/auto_name_feature.log'

# Where a feature keeps what it was built from, tried in this order — the first one
# that leads to a sketch wins. (Checked against a real 176-item timeline: every
# sketch-driven feature in it resolved through `profile`; the rest are here so the
# less common feature types aren't silently missed.)
SOURCE_ATTRS = ('profile', 'profiles', 'loftSections', 'path',
                'holePositionDefinition', 'boundaryCurve', 'curve', 'sketch')

# Hops that lead from one of those inputs towards the sketch itself: a Profile or a
# sketch curve knows its parentSketch, a LoftSection / Path entry wraps its entity,
# a hole's position definition holds sketch points.
CHILD_ATTRS = ('parentSketch', 'entity', 'sketch',
               'sketchPoint', 'sketchPoints', 'sketchCurve')

MAX_DEPTH = 4      # hops to follow before giving up
MAX_ITEMS = 25     # entries to inspect in a collection (a profile set, a path)

# A sketch that hasn't been named yet ("Sketch7") gives the feature a name that is
# no more meaningful than "Extrude7" — but it does still pair the two, so it is
# used. Set this False to leave features from unnamed sketches at their default.
RENAME_FROM_DEFAULT_SKETCH = True
DEFAULT_SKETCH_RE = re.compile(r'^Sketch\s*\d+$')

# The extrude or flange that gives a component its first body is its Base, whatever it
# was drawn from — ahead of the sketch rule. See _makes_first_body.
BASE_NAME = 'Base'
BASE_TYPES = ('adsk::fusion::ExtrudeFeature', 'adsk::fusion::FlangeFeature')

# Last document we looked at, and the timeline length when we did.
_doc_key_seen  = None
_timeline_count = 0


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('{:.3f}  '.format(time.time()) + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _doc_key():
    """Stable per-document key: the dataFile id, or a name-based one when unsaved."""
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


def _sketch_of(obj, depth=0):
    """The sketch `obj` belongs to (or is), following the hops above; None if the
    trail doesn't reach one — which is the normal answer for a face, an edge or a
    body, i.e. a feature that isn't sketch-driven."""
    if obj is None or depth > MAX_DEPTH:
        return None
    sk = adsk.fusion.Sketch.cast(obj)
    if sk:
        return sk
    for attr in CHILD_ATTRS:
        try:
            child = getattr(obj, attr, None)
        except Exception:
            child = None
        if child is not None:
            found = _sketch_of(child, depth + 1)
            if found:
                return found
    try:
        count = obj.count
    except Exception:
        count = None
    if isinstance(count, int) and count > 0:
        for i in range(min(count, MAX_ITEMS)):
            try:
                item = obj.item(i)
            except Exception:
                continue
            found = _sketch_of(item, depth + 1)
            if found:
                return found
    return None


def _source_sketch(feature):
    for attr in SOURCE_ATTRS:
        try:
            value = getattr(feature, attr, None)
        except Exception:
            value = None          # an input the feature type doesn't support
        if value is not None:
            sk = _sketch_of(value)
            if sk:
                return sk
    return None


def _is_default_name(entity, name):
    """True while the feature still carries Fusion's own auto-name for its type —
    "Extrude7" for an ExtrudeFeature, "Hole2" for a HoleFeature. Derived from the
    type rather than a catch-all "word + number" regex, so a hand-typed name like
    "Rib Slot 2" is never mistaken for a default and overwritten.

    (A few types default to something other than their class name — MirrorFeature
    to "MirrorComponent1", RectangularPatternFeature to "R-Pattern1" — so this
    under-matches for those. None of them are sketch-driven, so they are skipped
    either way.)"""
    type_name = entity.objectType.rsplit('::', 1)[-1]
    prefix = type_name[:-len('Feature')] if type_name.endswith('Feature') else type_name
    return bool(re.match(r'^' + re.escape(prefix) + r'\s*\d+$', name))


def _makes_first_body(feature):
    """True for an extrude or flange that made the FIRST body of its component: every
    body the component has now is one this feature made. Checked straight after the
    command, with the marker just past the feature, so a later body that exists further
    down a rolled-back timeline is not there to count."""
    if feature.objectType not in BASE_TYPES:
        return False
    try:
        made = feature.bodies
        mine = {made.item(i).entityToken for i in range(made.count)}
        have = feature.parentComponent.bRepBodies
        return bool(mine) and all(have.item(i).entityToken in mine
                                  for i in range(have.count))
    except Exception:
        return False


def _name_after_sketch(timeline_item):
    """Rename one just-created timeline entry after its sketch, if both apply — or call
    it Base, when it is the extrude or flange that gave its component its first body."""
    try:
        entity = timeline_item.entity
    except Exception:
        return                     # some entries (e.g. a rolled-back group) have none
    feature = adsk.fusion.Feature.cast(entity)
    if not feature:
        return                     # a sketch / occurrence / construction geom — not ours
    if not _is_default_name(entity, feature.name):
        return                     # already named by hand
    if _makes_first_body(feature):
        old = feature.name
        feature.name = BASE_NAME   # Fusion appends " (1)" etc. if the name is taken
        _log('renamed', repr(old), '->', repr(feature.name), '| first body')
        return
    sketch = _source_sketch(feature)
    if sketch is None:
        return                     # nothing to be named after
    if not RENAME_FROM_DEFAULT_SKETCH and DEFAULT_SKETCH_RE.match(sketch.name):
        return
    old = feature.name
    feature.name = sketch.name     # Fusion appends " (1)" etc. if the name is taken
    _log('renamed', repr(old), '->', repr(feature.name), '| sketch', repr(sketch.name))


def _scan(design):
    global _doc_key_seen, _timeline_count
    timeline = design.timeline
    key = _doc_key()
    count = timeline.count
    if key != _doc_key_seen:
        # First look at this document: adopt its history as it stands. Renaming what
        # is already there would rewrite the whole timeline of every file opened.
        _doc_key_seen, _timeline_count = key, count
        return
    grown = count - _timeline_count
    _timeline_count = count
    if grown <= 0:
        return                     # nothing added (an edit, an undo, a delete)
    # New entries are inserted AT the marker, which is not the end of the timeline
    # when the history is rolled back — so the items just added are the `grown`
    # entries ending at the marker.
    marker = timeline.markerPosition
    for i in range(max(0, marker - grown), min(marker, count)):
        try:
            _name_after_sketch(timeline.item(i))
        except Exception:
            _log('ERR', traceback.format_exc())


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if not _running:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design:
                _scan(design)
        except Exception:
            _log('ERR', traceback.format_exc())


def run(context):
    global _running
    try:
        _running = True
        h = CommandTerminatedHandler()
        ui.commandTerminated.add(h)
        handlers.append(h)
        _log('=== Auto-Name Feature run() ===')
        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Auto-Name Feature failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _running
    try:
        _running = False
        for h in handlers:
            try:
                ui.commandTerminated.remove(h)
            except Exception:
                pass
        handlers.clear()
    except Exception:
        pass
