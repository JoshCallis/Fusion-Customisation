"""
Default Appearance — the Browser palette's "Default for New Bodies", readable by any
add-in that makes bodies.

The default is set in the Browser window (right-click a swatch) and stored in
Browser/default_appearance.json: name + id + swatch look. Two add-ins wear it:

  Browser.py `_paint_new_bodies` — after the fact, on whatever New Bodies just saw
    appear (the native commands, Combine, Paste…), inside the Browser's one_step.
  Extrude — inside its own build, so the PREVIEW of a new body already wears it rather
    than Fusion's stock white, and the committed body is born in it (nothing left for
    the Browser to paint).

NOT Fusion's Default Appearance Override preference: that takes Library items only (a
design-only copy was in the script that crashed Fusion 2026-09-30), the palette's design
swatches are mostly recoloured/renamed copies, and natively it only reaches designs made
AFTER the change anyway. The preference is only ever READ here.

Loaded fresh from disk by each add-in (the _load / _load_local exec pattern).
"""

import json
import os

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()

LIB_ID = 'BA5EE55E-9982-449B-9D66-9F036540E140'   # Fusion Appearance Library
SWATCH_PIDS = ('opaque_albedo', 'metal_f0', 'transparent_color', 'layered_bottom_f0',
               'glazing_transmission_color', 'surface_albedo')
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    'Browser', 'default_appearance.json')


def swatch_of(a):
    """{'rgb': [r,g,b]} or {'tex': path} — the surface colour a swatch draws."""
    for pid in SWATCH_PIDS:
        try:
            cp = adsk.core.ColorProperty.cast(a.appearanceProperties.itemById(pid))
        except Exception:
            cp = None
        if not cp:
            continue
        try:
            if cp.hasConnectedTexture:
                fp = cp.connectedTexture.properties.itemById('unifiedbitmap_Bitmap')
                fp = adsk.core.FilenameProperty.cast(fp)
                if fp and fp.value:
                    return {'tex': fp.value}
                continue
            v = cp.value
            if v:
                return {'rgb': [v.red, v.green, v.blue]}
        except Exception:
            continue
    return {}


def spec():
    """{name, id, rgb | tex} or None."""
    try:
        with open(FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if d.get('name') else None
    except Exception:
        return None


def look(d):
    return {k: d[k] for k in ('rgb', 'tex') if k in d}


def is_default(a, d):
    return a.name == d['name'] and a.id == d['id'] and swatch_of(a) == look(d)


def find_in(design, d):
    """The design appearance that IS the default: one already here with its name, id and
    look, else a fresh copy of its Library original (by id), renamed and recoloured —
    a new, unused copy, so the colour write is cheap (unlike one bodies wear)."""
    apps = design.appearances
    for i in range(apps.count):
        a = apps.item(i)
        if is_default(a, d):
            return a
    lib = app.materialLibraries.itemById(LIB_ID)
    src = lib.appearances.itemById(d['id']) if lib else None
    if src is None:
        return None
    a = apps.addByCopy(src, d['name'])
    rgb = d.get('rgb')
    if rgb and swatch_of(a) != look(d):
        for pid in SWATCH_PIDS:
            cp = adsk.core.ColorProperty.cast(a.appearanceProperties.itemById(pid))
            if not cp or cp.hasConnectedTexture or not cp.value:
                continue
            cp.value = adsk.core.Color.create(rgb[0], rgb[1], rgb[2], cp.value.opacity)
            break
    return a


def paints_own(body):
    """True when the body's look is its own choice: its own/override appearance, or one
    inherited from a painted occurrence on any path down to it. Fusion stamps every new
    occurrence with its Default Appearance Override preference (measured 2026-10-01:
    a new component's occurrence reads ABS (White)), so wearing THAT isn't a paint."""
    T = adsk.core.AppearanceSourceTypes
    src = body.appearanceSourceType
    if src == T.MaterialAppearanceSource:
        return False
    if src != T.OccurrenceAppearanceSource:
        return True
    comp = body.parentComponent
    root = comp.parentDesign.rootComponent
    if comp == root:
        return False           # the design's own default (a root body reads Occurrence)
    try:
        stock = app.preferences.materialPreferences.appearanceOverride   # read only
    except Exception:
        stock = None
    for o in root.allOccurrencesByComponent(comp):
        while o:
            a = o.appearance
            if a and not (stock and a.name == stock.name and a.id == stock.id):
                return True
            o = o.assemblyContext
    return False


def wanting(bodies, d):
    """The bodies that inherit their look and don't already wear default `d`."""
    out = []
    for b in bodies:
        a = b.appearance
        if (a and is_default(a, d)) or paints_own(b):
            continue
        out.append(b)
    return out


def paint_new(design, bodies):
    """Put the default on `bodies` a command has just made (those that want it), with a
    plain write — for a caller mid-command, where Dim Outside Active's paint bookkeeping
    would track a preview that is about to be rolled back. Returns the count painted."""
    d = spec()
    if d is None:
        return 0
    todo = wanting(bodies, d)
    if not todo:
        return 0
    a = find_in(design, d)
    if a is None:
        return 0
    for b in todo:
        b.appearance = a
    return len(todo)
