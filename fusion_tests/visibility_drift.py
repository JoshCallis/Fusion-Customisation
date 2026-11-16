"""Visibility round-trip test for the Fusion nav/visibility engine.

INVARIANT: navigating must not change what is visible. Descending into a component and
returning to Top Level has to leave every light bulb exactly as it was.

RUN IT the way this file does: call the add-in functions DIRECTLY.

    THE TRAP THAT INVALIDATED AN ENTIRE DAY OF MEASUREMENTS
    ------------------------------------------------------
    `cmd_def.execute()` does NOT run a command. It QUEUES one; it runs when Fusion's
    event loop next turns. A script that calls execute() and then sleeps measures a
    round trip in which the return leg never happened — every cycle looks like massive
    "drift" because you descended and never came back. And the obvious fix,
    `adsk.doEvents()`, re-enters the event loop from a bridge script and WEDGES Fusion
    (its Python stops responding entirely; only a force-restart recovers it).

    So: never drive this through the command. `Go to Top Level.go_to_top(design)` and
    `Browser._activate_target(design, occ)` are plain functions for exactly this reason.

RESULT 2026-09-04, doc 260612 v278, after the stale-active-occurrence fix:
22 targets to depth 4, two consecutive runs, ZERO drift in occurrences AND bodies.
"""
import adsk.core, adsk.fusion, sys

MAX_TARGETS = 22
MAX_DEPTH   = 4

app = adsk.core.Application.get()
design = adsk.fusion.Design.cast(app.activeProduct)

_browser = _gototop = None
for _n, _m in list(sys.modules.items()):
    _f = getattr(_m, '__file__', '') or ''
    if _f.endswith('/Browser/Browser.py'):
        _browser = _m
    if _f.endswith('/Go to Top Level/Go to Top Level.py'):
        _gototop = _m
if _browser is None or _gototop is None:
    raise SystemExit('Browser and Go to Top Level add-ins must both be loaded')
fvs = _browser.fvs


def snapshot():
    """Occurrence bulbs and body visibility, kept SEPARATE — they are governed by
    different code and a combined number hides which one is misbehaving."""
    occ, bod = {}, {}
    def walk(occs):
        for i in range(occs.count):
            o = occs.item(i)
            c = o.component
            occ[fvs._occ_path_str(o)] = bool(o.isLightBulbOn)
            for j in range(c.bRepBodies.count):
                bod['%s:%d' % (c.id, j)] = bool(c.bRepBodies.item(j).isVisible)
            walk(o.childOccurrences)
    walk(design.rootComponent.occurrences)
    return occ, bod


def targets():
    out = []
    def walk(occs, path, depth):
        for i in range(occs.count):
            o = occs.item(i)
            p = path + '/' + o.component.name
            if depth >= 2:
                out.append((p, o))
            if depth < MAX_DEPTH:
                walk(o.childOccurrences, p, depth + 1)
    walk(design.rootComponent.occurrences, '', 1)
    return out[:MAX_TARGETS]


def main():
    print('go_to_top ran      :', _gototop.go_to_top(design))
    prev_occ, prev_bod = snapshot()
    occ_bad = occ_tot = bod_bad = bod_tot = 0
    tg = targets()
    for path, occ in tg:
        _browser._activate_target(design, occ)      # descend
        _gototop.go_to_top(design)                  # and back
        now_occ, now_bod = snapshot()
        oc = [k for k in prev_occ if k in now_occ and prev_occ[k] != now_occ[k]]
        bc = [k for k in prev_bod if k in now_bod and prev_bod[k] != now_bod[k]]
        if oc: occ_bad += 1
        if bc: bod_bad += 1
        occ_tot += len(oc); bod_tot += len(bc)
        print('  %-42s occ=%-3d body=%d' % (path[:42], len(oc), len(bc)))
        prev_occ, prev_bod = now_occ, now_bod       # judge each cycle against its start
    print('\nOCCURRENCE bulbs : %d/%d cycles unstable, %d changes' % (occ_bad, len(tg), occ_tot))
    print('BODY visibility  : %d/%d cycles unstable, %d changes' % (bod_bad, len(tg), bod_tot))


def snapshot_while_descended():
    """Regression 2026-09-25: every nav add-in's run() (and documentOpened) snapshots
    ROOT, so reloading the add-ins while descended recorded isolation's hiding as
    intent and the next Go Up / Go to Top hid the Legs for good. Snapshot root while
    isolated, then climb out both ways — the top level must come back unchanged."""
    tg = targets()
    if not tg:
        return
    _, deep = tg[-1]
    base, _ = snapshot()
    _browser._activate_target(design, deep)
    fvs.take_snapshot(design, None)                 # what an add-in reload does
    _gototop.go_to_top(design)
    via_top = [k for k, v in snapshot()[0].items() if base.get(k) != v]
    _browser._activate_target(design, deep)
    fvs.take_snapshot(design, None)
    c = deep                                        # Go Up a Level, repeatedly
    while c is not None:
        fvs.take_snapshot(design, c)
        fvs.restore_snapshot(design, c.assemblyContext)
        c = c.assemblyContext
    _gototop.go_to_top(design)
    via_up = [k for k, v in snapshot()[0].items() if base.get(k) != v]
    print('\nSNAPSHOT WHILE DESCENDED: go_to_top %s, go-up %s'
          % (via_top or 'OK', via_up or 'OK'))


main()
snapshot_while_descended()
