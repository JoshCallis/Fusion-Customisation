"""
Driven Link — turning a driven (reference) dimension into a user parameter.

Shared by two add-ins, loaded by path the way every sibling module here is:
  - Dimension Value makes the link. Its value box, opened on a driven dimension (its
    own key, or Link Driven Dimension's), names the parameter with the structured
    convention's stepper (fusion_ui/param_naming.js) and commits through create_link.
  - Parameters keeps the pair in step afterwards: renaming an alias renames the
    dimension's own parameter with flip_case, and re-pointing one at another dimension
    (_relink_to) uses both helpers.

Nothing polls or copies a value. A driven dimension already owns a ModelParameter
that Fusion keeps in step with the geometry, and other expressions may legally
reference it — measured: moving the geometry took the dimension 5→8, and a user
parameter reading it followed, on through a feature. So a link is just that parameter
given a readable name plus a user parameter aliasing it.
"""

import adsk.core, adsk.fusion
import traceback


def unique_name(design, base):
    """`base`, or base2/base3/… — the first name no parameter in the design holds."""
    name, n = base, 2
    while design.allParameters.itemByName(name):
        name = '%s%d' % (base, n)
        n += 1
    return name


def flip_case(name):
    """The dimension's own parameter name: the alias's with the first letter's case
    flipped. Parameter names ARE case-sensitive (measured: 'bootGap' and 'BootGap'
    coexist and both resolve), so this tells the pair apart without adding a single
    character — and the flipped name is what Fusion shows when you hover the dimension,
    which is the whole reason it has to stay quiet. Brackets are not an option there:
    Fusion silently ignores '(name)' exactly as it ignores a space. A structured name
    flips its owner word only — Drawer_Shelf_Span_X → drawer_Shelf_Span_X."""
    if not name:
        return name
    head, rest = name[0], name[1:]
    flipped = head.lower() if head.isupper() else head.upper()
    return (flipped + rest) if flipped != head else (name + '2')   # non-letter: no case


def linked_dimension(design, up):
    """The driven SketchDimension the user parameter `up` is a link to, or None.
    A link's expression is exactly its twin's name — which, being the alias's own name
    case-flipped, reads as the parameter pointing at ITSELF wherever it is shown raw. So
    every window that shows a value shows a link's measurement instead (measured_text)."""
    try:
        expr = (up.expression or '').strip()
        # Only the twin's own shape is worth a lookup — flip_case(name), or unique_name's
        # flip_case(name)2/3/… — since allParameters.itemByName costs ~5 ms on 260612 and
        # most of its ~200 user parameters are a bare reference to something.
        twin = flip_case(up.name)
        if not expr.startswith(twin) or not (expr == twin or expr[len(twin):].isdigit()):
            return None
        mp = adsk.fusion.ModelParameter.cast(design.allParameters.itemByName(expr))
        if mp is None:
            return None
        # A dimension's parameter is createdBy its SKETCH (measured), not the dimension.
        sk = adsk.fusion.Sketch.cast(mp.createdBy)
        if sk is None:
            return None
        dims = sk.sketchDimensions
        for i in range(dims.count):
            d = dims.item(i)
            p = d.parameter
            if p is not None and p.name == expr:
                return d if not d.isDriving else None
        return None
    except Exception:
        return None


def measured_text(design, p, precision=None):
    """`p`'s value as Fusion would display it — '266.15 mm' — rounded to the Units
    preference (formatInternalValue ignores it and gives full precision)."""
    try:
        if precision is None:
            precision = int(adsk.core.Application.get().preferences
                            .unitAndValuePreferences.generalPrecision)
    except Exception:
        precision = 2
    try:
        um = design.unitsManager
        unit = p.unit or ''
        if not unit:
            return '%.*f' % (precision, p.value)
        base = 'rad' if unit in ('deg', 'rad') else um.internalUnits   # angles: radians
        return '%.*f %s' % (precision, um.convert(p.value, base, unit), unit)
    except Exception:
        return ''


def driven_dimension(entities):
    """The first driven (reference) dimension among `entities`, or None."""
    for e in entities:
        d = adsk.fusion.SketchDimension.cast(e)
        if d and not d.isDriving:
            return d
    return None


def create_link(design, dim, name):
    """Name the driven dimension and create the user parameter `name` that mirrors it.
    Returns '' on success, else a message. The caller has already checked `name` is a
    valid, unused parameter name.

    Two objects are unavoidable. Fusion's own dimension-input dropdown lists USER
    parameters only — a bare ModelParameter still resolves if you type it out in full
    (measured), but it never offers itself while you are typing a dimension, which is
    the whole point of having the name. So the user parameter is what goes in that
    list, and the dimension's own parameter takes the case-flipped name.
    """
    dim = getattr(dim, 'nativeObject', None) or dim   # a proxy's parameter is the native's
    mp = dim.parameter
    if mp is None:
        return 'That dimension has no parameter.'
    was = mp.name
    ref = unique_name(design, flip_case(name))
    try:
        mp.name = ref
    except Exception:
        return traceback.format_exc()
    # Fusion IGNORES a name it doesn't like SILENTLY — no exception, the old name simply
    # stays (measured: 'Boot Gap', 'Boot-Gap' and '(BootGap)' all left it as 'd1'). So
    # never trust the assignment; read it back.
    if mp.name != ref:
        return ('Fusion would not take the name "%s". Parameter names allow only '
                'letters, digits and underscores.' % ref)
    try:
        design.userParameters.add(
            name, adsk.core.ValueInput.createByString(ref), mp.unit or '',
            'driven dimension in ' + dim.parentSketch.name)
    except Exception as e:
        mp.name = was                    # put the dimension back the way we found it
        return 'Could not create "%s": %s' % (name, e)
    return ''
