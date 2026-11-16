"""A sketch picked as a WHOLE sketch.

Browser selects a sketch as its curves plus the Sketch itself (Sketch last), so the
viewport lights the lines as well as the profiles — the Sketch alone draws only its
profiles. Anything that reads ui.activeSelections and acts per entity must see that as
ONE sketch, not a sketch and N lines: whole_sketches() drops sketch geometry whose own
sketch is in the same selection. Hide, Delete Selection and Sketch Clipboard use it.
"""

import adsk.fusion


def _tok(ent):
    try:
        return (ent.nativeObject or ent).entityToken
    except Exception:
        try:
            return ent.entityToken
        except Exception:
            return None


def whole_sketches(ents):
    """`ents` with every sketch curve / point dropped whose parent sketch is also in it."""
    sketches = {_tok(e) for e in ents if isinstance(e, adsk.fusion.Sketch)}
    sketches.discard(None)
    if not sketches:
        return list(ents)
    out = []
    for e in ents:
        if not isinstance(e, adsk.fusion.Sketch):
            ps = getattr(e, 'parentSketch', None)
            if ps is not None and _tok(ps) in sketches:
                continue
        out.append(e)
    return out
