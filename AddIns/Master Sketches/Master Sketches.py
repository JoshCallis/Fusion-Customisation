"""
Master Sketches
────────────────
Convert (MasterConvertCmd), Edit Original (MasterEditOriginalCmd) and its way
back (MasterReturnCmd — run by Go Up / Go to Top / the Browser in place of their
own sketch exit while the master is open; the session itself, and how every nav
handler honours it, is Visibility Snapshot.py's master-session section), Delete
Master (MasterDeleteCmd) and Convert to Sketch (MasterConvertBackCmd) — the way
back out of a master, baking or deleting its projections — grouped selection of a
projection while its sketch is open, and whole-projection delete — the UI on top
of the mechanics in Masters.py. See the design note in chat ("Master sketches —
shared contours projected down the component tree") for the full spec; this file
follows its Architecture section point for point.

RESULTS ARE A HUD, NOT A DIALOG — same convention as Sketch Clipboard: success is
silent, failure is a clickable cross. See _hud below. Delete Master and Convert to
Sketch are the one exception: each opens an are-you-sure messageBox first (bake /
delete / cancel), since which projections to keep is a real decision, not a result.
"""

import adsk.core, adsk.fusion, traceback, os, subprocess, sys, time
import importlib.util
import types as _types

LOG = '/tmp/master_sketches_addin.log'
HUD_FLASH = '/Users/josh/bin/hud_flash'

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


masters = _load('masters', 'Masters.py')
sc = _load('sc', os.path.join('Sketch Clipboard', 'Sketch Clipboard.py'))
fvs = _load('fvs', 'Visibility Snapshot.py')
fss = _load('fss', 'Shared State.py')

try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    bounds = None

CONVERT_ID   = 'MasterConvertCmd'
CONVERT_NAME = 'Convert to Master'
CONVERT_DESC = ('Convert the selected sketch geometry into a master, projected '
                'back into this sketch with a live link.')

EDIT_ORIG_ID   = 'MasterEditOriginalCmd'
EDIT_ORIG_NAME = 'Edit Original'
EDIT_ORIG_DESC = 'Open the master this projection comes from; return here when you finish.'

# Run by the nav commands in place of their own sketch exit while a master opened by
# Edit Original is open (fvs.request_master_return) — the id is fvs's, so the two
# can't drift apart. Declared like any other action, so it can also take a key.
RETURN_ID   = fvs.MASTER_RETURN_CMD_ID
RETURN_NAME = 'Return to Original Sketch'
RETURN_DESC = 'Leave the master opened by Edit Original, back into the sketch you came from.'

DELETE_MASTER_ID   = 'MasterDeleteCmd'
DELETE_MASTER_NAME = 'Delete Master'
DELETE_MASTER_DESC = ('Delete the master, baking or deleting its projections in every '
                       'sketch that holds one.')

CONVERT_BACK_ID   = 'MasterConvertBackCmd'
CONVERT_BACK_NAME = 'Convert to Sketch'
CONVERT_BACK_DESC = ('Replace this projection with real geometry, then delete the '
                      'master (the reverse of Convert to Master).')

app = None
ui = None
handlers = []


def _hud(kind, message=None):
    try:
        args = [HUD_FLASH, kind]
        if message:
            args.append(message)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _hotkeys():
    """Same sys.modules-cached load as every other add-in here — see Sketch
    Clipboard.py's _hotkeys() for the full rationale."""
    HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
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


# ─────────────────────────────────────────────────────────────────────────────
# Convert
# ─────────────────────────────────────────────────────────────────────────────

def _selected_entities(design):
    out = []
    for i in range(ui.activeSelections.count):
        e = adsk.fusion.SketchEntity.cast(ui.activeSelections.item(i).entity)
        if e is not None:
            out.append(e)
    return out


def _anchor_kind(e):
    """('origin' | 'axis' | 'bounds' | None, referenced native entity) for a
    projected entity in the source sketch.

    Every component's origin and axes sit where the root's do (Convert refuses a
    moved occurrence), so an origin or axis projected from ANY component is the
    root's origin/axis as far as a root master is concerned.
    """
    try:
        ref = e.referencedEntity
    except Exception:
        return None, None
    if ref is None:
        return None, None
    native = getattr(ref, 'nativeObject', None) or ref
    cp = adsk.fusion.ConstructionPoint.cast(native)
    if cp:
        try:
            if cp == cp.component.originConstructionPoint:
                return 'origin', native
        except Exception:
            pass
    if adsk.fusion.ConstructionAxis.cast(native):
        return 'axis', native
    if _is_bounds_point_projection(e):
        return 'bounds', native
    return None, native


def _is_bounds_point_projection(e):
    if bounds is None:
        return False
    try:
        ref = e.referencedEntity
        parent = ref.parentSketch if ref is not None else None
    except Exception:
        return False
    return parent is not None and bounds.is_bounds_sketch(parent)


def _validate_convert(design, sketch, ents):
    """None on success, else a refusal message."""
    if masters.is_master(sketch.nativeObject or sketch):
        return 'this sketch is itself a master — convert from the sketch that USES it'

    for e in ents:
        if getattr(e, 'isReference', False):
            if _anchor_kind(e)[0] is None:
                return ('the selection includes projected/reference geometry that '
                        'is not an origin, an axis or a bounds point (a master '
                        'projection included — nesting is not supported yet)')
        if adsk.fusion.SketchText.cast(e):
            return 'text is not supported in a master'

    err = sc._unconstrained_error(ents)
    if err:
        return err

    occ = sketch.assemblyContext
    if occ is not None and not sc._is_identity(occ.transform2):
        return ('"%s" sits in a moved occurrence — Convert only works when every '
                'occurrence on the path is at the identity transform' % occ.name)
    return None


# ── Anchors: origin, axes, bounds ────────────────────────────────────────────
# A master lives in the root, so everything it is tied to has to be the ROOT's:
# origins map to the master's origin and axes to the root's axes (every
# component's sit in the same place — Convert refuses a moved occurrence).
#
# A bounds point becomes an ANCHOR POINT in the master: a construction point
# pinned to the origin by the source component's own bound parameters
# (`PanelLeft`, `PanelBack`…), and every original dimension/constraint relinks to
# it unchanged. So "50 from the left bound" stays exactly that, the anchor
# dimensions are bare parameters that are positive by Bounds' own convention (no
# sign to go wrong when a bound changes), and every dimension and constraint type
# carries across without a special case. Anchors are tagged and never projected
# out of the master — see Masters.project_master.

def _root_axis_line(design, master_sk, axis_native, cache, problems):
    """The master's projection of the root axis parallel to *axis_native*."""
    try:
        d = axis_native.geometry.direction
        comps = (abs(d.x), abs(d.y), abs(d.z))
        letter = 'XYZ'[comps.index(max(comps))]
    except Exception as e:
        problems.append('could not read the direction of a projected axis: %s' % e)
        return None
    if letter in cache:
        return cache[letter]
    line = None
    try:
        root_axis = getattr(design.rootComponent, letter.lower() + 'ConstructionAxis')
        res = master_sk.project(root_axis)
        line = res.item(0) if res and res.count else None
        if line is None:
            problems.append('projecting the root %s axis into the master gave nothing' % letter)
    except Exception as e:
        problems.append('could not project the root %s axis into the master: %s' % (letter, e))
    cache[letter] = line
    return line


def _param_value(design, name):
    try:
        p = design.userParameters.itemByName(name) or design.allParameters.itemByName(name)
        return p.value if p is not None else None
    except Exception:
        return None


def _bounds_exprs(design, master_sk, src_point, ref_native, problems):
    """(h expression, v expression) pinning a projected bounds point's sketch x and
    y from the origin — '' where it sits on the origin line. Each is the bound's
    parameter wherever it can be found, else a literal length (reported: it will no
    longer follow the bounds).

    Sketch coordinates are the master's own too (it is built on the source sketch's
    frame and the copy uses the identity), and an in-plane sketch coordinate is a
    world coordinate up to sign — so |coordinate| is the bound's value.
    """
    g = src_point.geometry
    try:
        h_axis, v_axis = bounds._sketch_axis_map(master_sk)
        bcomp = ref_native.parentSketch.parentComponent
    except Exception:
        h_axis = v_axis = bcomp = None
    world = master_sk.sketchToModelSpace(adsk.core.Point3D.create(g.x, g.y, 0))
    out = []
    for local, axis in ((g.x, h_axis), (g.y, v_axis)):
        if abs(local) <= masters.TOL:
            out.append('')
            continue
        name = ''
        if bcomp is not None and axis:
            try:
                # The face sitting at this world coordinate, and the parameter naming it.
                # (This called a bounds._corner_pin that never existed; the except hid
                # it, so every converted bound fell through to a plain length.)
                face = bounds.face_at(design, bcomp, axis, getattr(world, axis.lower()))
                name = bounds.slot(design, bcomp, face) if face else ''
            except Exception:
                name = ''
        value = _param_value(design, name) if name else None
        if value is not None and abs(abs(value) - abs(local)) <= masters.TOL:
            out.append(name)
        else:
            out.append('%r cm' % round(abs(local), 9))
            problems.append('a bound at %.4g mm could not be tied to its parameter%s — '
                            'the master holds it as a plain length'
                            % (abs(local) * 10, ' (%s)' % name if name else ''))
    return tuple(out)


def _bounds_anchors(design, master_sk, points, problems):
    """Anchor points in *master_sk* for projected bounds points *points*
    [(token, source projection, referenced native point)]. Returns {token: point}.

    Pinned all at once through Bounds' own column/row arrangement: dimensioning
    each point from the origin twice is the lattice Fusion rejects as
    over-constrained (what this used to do, and why a converted master floated).
    """
    placed, out = [], {}
    try:
        h_axis, v_axis = bounds._sketch_axis_map(master_sk)
    except Exception as e:
        problems.append('could not read the master orientation: %s' % e)
        return out
    for tok, src, ref in points:
        g = src.geometry
        hx, vx = _bounds_exprs(design, master_sk, src, ref, problems)
        try:
            pt = master_sk.sketchPoints.add(adsk.core.Point3D.create(g.x, g.y, 0))
            pt.attributes.add(masters.ATTR_GROUP, masters.ANCHOR_KEY, '1')
        except Exception as e:
            problems.append('could not place a bounds anchor: %s' % e)
            continue
        placed.append(('bound %d' % (len(placed) + 1), pt, g.x, g.y, hx, vx))
        out[tok] = pt
    if placed:
        bounds._pin_points(master_sk, (h_axis, v_axis), placed, problems)
    return out

def _find_dependents(sketch, closed_toks):
    """[(kind, other_entity, closed_geo_key, expression)] for constraints and
    dimensions on UNSELECTED geometry that reference something in the closed set
    — step 3 of Convert. Recorded before the closed set is deleted, so they can
    be relinked to the projection afterward, matched by geo key.

    A tie to an ANCHOR (the sketch origin, projected origin/axis/bounds point) is
    not a dependent: the master already carries it, so re-adding it here between
    the anchor and the projection would only over-constrain the sketch.
    """
    def anchor(e):
        return sc._is_external(e, sketch)

    out = []
    for d in sketch.sketchDimensions:
        one, two = sc._dim_entities(d)
        if one is None or two is None:
            continue
        a_in, b_in = sc._tok(one) in closed_toks, sc._tok(two) in closed_toks
        if a_in == b_in:
            continue                      # both or neither in the closed set
        closed, other = (one, two) if a_in else (two, one)
        if anchor(other):
            continue
        try:
            expr = d.parameter.expression
        except Exception:
            expr = None
        out.append(('dimension', d.objectType.split('::')[-1], other,
                     sc._geo_key(closed), expr, d.orientation
                     if hasattr(d, 'orientation') else None))
    for c in sketch.geometricConstraints:
        refs = sc._referenced(c)
        in_closed = [r for r in refs if sc._tok(r) in closed_toks]
        out_closed = [r for r in refs if sc._tok(r) not in closed_toks and not anchor(r)]
        if in_closed and out_closed:
            out.append(('constraint', c.objectType.split('::')[-1], out_closed[0],
                         sc._geo_key(in_closed[0]), None, None))
    return out


def _relink_dependents(sketch, dependents, projected_by_key):
    """Reattach what _find_dependents recorded to its projected counterpart,
    matched by geo key. Best-effort: a dependent whose closed-set entity has no
    projected match, or whose constraint/dimension kind isn't recreated here, is
    reported and left alone rather than guessed at.

    *sketch* is the edit-context (possibly proxied) sketch project_master
    projected into; *other* came off the NATIVE sketch in _find_dependents, so
    it is re-proxied here to match — addCoincident/addDistanceDimension refuse
    to mix a native entity with a proxy one from a different occurrence context.
    """
    problems = []
    for kind, subkind, other, key, expr, orientation in dependents:
        target = projected_by_key.get(key)
        if target is None:
            problems.append('could not relink a %s to the projection (no matching '
                             'geometry found)' % subkind)
            continue
        other = sc._proxy_for_entity(other, sketch)
        try:
            if kind == 'dimension' and subkind == 'SketchLinearDimension':
                text = adsk.core.Point3D.create(0, 0, 0)
                dim = sketch.sketchDimensions.addDistanceDimension(
                    other, target, orientation or
                    adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, text)
                if expr and dim and dim.parameter:
                    try:
                        dim.parameter.expression = expr
                    except Exception:
                        pass
            elif kind == 'constraint' and subkind == 'CoincidentConstraint':
                sketch.geometricConstraints.addCoincident(other, target)
            else:
                problems.append('did not know how to relink a %s' % subkind)
        except Exception as e:
            problems.append('could not relink a %s: %s' % (subkind, e))
    return problems


def _run_convert():
    problems = []
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        if not fvs.is_editing_sketch(design):
            _hud('fail', 'Convert to Master: open a sketch and select geometry in it first.')
            return
        sketch = fvs.active_edit_sketch(design)
        if sketch is None:
            _hud('fail', 'Convert to Master: could not tell which sketch is open.')
            return

        # Sketch Clipboard's own measured convention (see its docstring): the
        # closure, .copy() and _relink_external all want the NATIVE sketch and
        # native entities. `sketch` (possibly a proxy, from the edit context)
        # is kept only for occurrence/world-transform info and for the final
        # project_master call, which the spike confirmed working on a proxy.
        native_sketch = sketch.nativeObject or sketch
        ents = [(e.nativeObject or e) for e in _selected_entities(design)]
        if not ents:
            _hud('fail', 'Convert to Master: select some sketch geometry first.')
            return

        # _validate_convert's occurrence-identity check needs the PROXY (a
        # native sketch's own .assemblyContext is always None, which would
        # silently skip the check the moved-occurrence refusal exists for).
        err = _validate_convert(design, sketch, ents)
        if err:
            _hud('fail', 'Convert to Master: ' + err)
            return

        closed, added, floating, ref_toks = sc._close_selection(native_sketch, ents)
        if floating:
            _hud('fail', 'Convert to Master: %d selected entit%s not tied to the origin or '
                          'a bounds point — fully constrain the sketch first.'
                 % (len(floating), 'y is' if len(floating) == 1 else 'ies are'))
            return

        closed_toks = {sc._tok(e) for e in closed}
        dependents = _find_dependents(native_sketch, closed_toks)

        root = design.rootComponent
        before_index = native_sketch.timelineObject.index if native_sketch.timelineObject else 0

        n = 1
        existing = {s.name for s in root.sketches}
        while ('Master %02d – %s' % (n, sketch.name)) in existing:
            n += 1
        label = 'Master %02d – %s' % (n, sketch.name)

        local = sc._world_matrix(native_sketch, sketch.assemblyContext)
        master_sk, extras, probs = masters.create_master(design, label, before_index, local)
        problems += probs
        if master_sk is None:
            _hud('fail', 'Convert to Master:\n' + '\n'.join(problems))
            return

        # Anchors — see "Anchors: origin, axes, bounds" above. Origins map to the
        # master's origin and axes to the root's; bounds points get anchor points,
        # made after the copy so they can't shadow copied geometry in key_map.
        anchors = {}
        for coll in (native_sketch.sketchCurves, native_sketch.sketchPoints):
            for e in coll:
                t = sc._tok(e)
                if t in ref_toks:
                    anchors[t] = e
        ref_pairs, bounds_pts, axis_cache = [], [], {}
        for tok, src in anchors.items():
            kind, ref = _anchor_kind(src)
            if kind == 'origin':
                ref_pairs.append((src, master_sk.originPoint))
            elif kind == 'axis':
                line = _root_axis_line(design, master_sk, ref, axis_cache, problems)
                if line is not None:
                    ref_pairs.append((src, line))
            elif kind == 'bounds':
                bounds_pts.append((tok, src, ref))
            else:
                problems.append('the geometry is tied to projected "%s", which a master '
                                'cannot stand in for — that tie is dropped'
                                % getattr(ref, 'name', '?'))

        try:
            copy_xform = adsk.core.Matrix3D.create()
            copy_coll = adsk.core.ObjectCollection.create()
            for e in closed:
                copy_coll.add(e)
            native_sketch.copy(copy_coll, copy_xform, master_sk)
        except Exception as e:
            problems.append('could not copy the geometry into the master: %s' % e)
            _hud('fail', 'Convert to Master:\n' + '\n'.join(problems))
            return

        # Copied geometry only — the master's origin and projected axes would
        # otherwise shadow a copied point/line sitting exactly on top of them.
        key_map = {}
        for e in list(master_sk.sketchCurves) + list(master_sk.sketchPoints):
            if e == master_sk.originPoint or getattr(e, 'isReference', False):
                continue
            k = sc._geo_key(e)
            if k:
                key_map[k] = e

        for tok, pt in _bounds_anchors(design, master_sk, bounds_pts, problems).items():
            ref_pairs.append((anchors[tok], pt))

        cons, dims, skipped, expr_restored, unparam = sc._relink_external(
            design, native_sketch, master_sk, copy_xform, key_map, ref_pairs, {},
            copied=closed_toks)
        if skipped:
            problems.append('%d relink%s skipped inside the master' %
                             (skipped, ' was' if skipped == 1 else 's were'))
        if not master_sk.isFullyConstrained:
            problems.append('"%s" is not fully constrained — something it was tied to '
                            'did not come across' % label)

        # Curves first: deleting one takes its own endpoints with it, so a point
        # is deleted only if it is still there afterwards.
        for e in sorted(closed, key=lambda x: 1 if adsk.fusion.SketchPoint.cast(x) else 0):
            try:
                if e.isValid:
                    e.deleteMe()
            except Exception as ex:
                problems.append('could not remove converted geometry: %s' % ex)

        projected, proj_problems = masters.project_master(design, master_sk, sketch)
        problems += proj_problems
        if not projected:
            _hud('fail', 'Convert to Master:\n' + '\n'.join(problems))
            return

        projected_by_key = {}
        for e in projected:
            k = sc._geo_key(e)
            if k:
                projected_by_key[k] = e
        problems += _relink_dependents(sketch, dependents, projected_by_key)

        ui.activeSelections.clear()
        for e in projected:
            try:
                ui.activeSelections.add(e)
            except Exception:
                pass

        if problems:
            _hud('fail', 'Converted to "%s" with problems:\n%s' % (label, '\n'.join(problems)))
        else:
            _hud('ok')
        # The problem TEXT, not just the count: the HUD is the only other place it
        # goes and it's gone the moment it fades, which left a real failure with
        # nothing to read back but "problems=6".
        _log('convert ok, label=%s problems=%d%s'
             % (label, len(problems),
                ''.join('\n    · ' + p for p in problems)))
    except Exception:
        tb = traceback.format_exc()
        _log('convert EXCEPTION:', tb.replace('\n', ' | '))
        _hud('fail', 'Convert to Master – error:\n' + tb)


# ─────────────────────────────────────────────────────────────────────────────
# Edit Original
# ─────────────────────────────────────────────────────────────────────────────

def _selected_projection(design):
    """(master_id, projected_entity) for the first selected entity that is a
    projection of a master, or (None, None). Shared with _selected_master."""
    for i in range(ui.activeSelections.count):
        e = ui.activeSelections.item(i).entity
        proj = masters.projection_of(e)
        if proj and proj[0]:
            return proj[0], e
    return None, None


def _run_edit_original():
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        sel, _entity = _selected_projection(design)
        if sel is None:
            _hud('fail', 'Edit Original: select a projected entity first.')
            return
        master = masters.find_master(design, sel)
        if master is None:
            _hud('fail', 'Edit Original: could not find that master.')
            return

        # From inside a sketch this is a SESSION: the sketch you are in stays visible
        # while the master is open, and leaving the master (Go Up, Go to Top, a
        # Browser activation) brings you back to it. The record lives in fvs, where
        # every nav handler looks — see its master-session section. It has to be
        # written BEFORE the SketchStop is queued, so the handlers that stop fires
        # know it is a hop and not you leaving the sketch.
        cd_stop = ui.commandDefinitions.itemById('SketchStop')
        return_sketch = (fvs.active_edit_sketch(design)
                         if fvs.is_editing_sketch(design) else None)
        if return_sketch is not None and fvs._same_sketch(return_sketch, master):
            return                        # already in it
        if return_sketch is not None and cd_stop:
            origin_occ = getattr(return_sketch, 'assemblyContext', None) \
                or fvs.recall_active_occ(design)
            if fvs.begin_master_session(design, master, return_sketch, origin_occ):
                cd_stop.execute()
            else:
                return_sketch = None
        elif fvs.is_editing_sketch(design) and cd_stop:
            cd_stop.execute()             # open, but unidentified: no way back to it

        occ = master.assemblyContext
        target = master.createForAssemblyContext(occ) if occ else master
        ui.activeSelections.clear()
        try:
            ui.activeSelections.add(target)
        except Exception:
            pass
        cd = ui.commandDefinitions.itemById('SketchActivate')
        if cd:
            cd.execute()
        _log('edit original ->', master.name,
             '(session from %s)' % return_sketch.name if return_sketch is not None else '')
    except Exception:
        tb = traceback.format_exc()
        _log('edit original EXCEPTION:', tb.replace('\n', ' | '))
        fvs.end_master_session('edit original failed')
        _hud('fail', 'Edit Original – error:\n' + tb)


def _run_return():
    """MasterReturnCmd: leave the master back into the sketch Edit Original came
    from. The nav commands that exit a sketch (Go Up, Go to Top, the Browser) run
    this in their place — fvs.request_master_return — so this is the ONE way back.
    Two steps, like the way in: SketchStop here, the rest once it has finished
    (_OnCommandTerminated), because SketchStop only queues."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design or fvs.master_session_editing(design) is None:
            return
        cd_stop = ui.commandDefinitions.itemById('SketchStop')
        if not cd_stop:
            return
        fvs.set_master_phase('returning')
        cd_stop.execute()
        _log('return: leaving master')
    except Exception:
        fvs.end_master_session('return failed')
        _log('return EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


def _finish_return(design, s):
    """The master has closed: re-enter the original sketch in its own occurrence,
    the way the Browser enters a sketch (activate the occurrence, then open the
    in-context proxy). Isolation onto that occurrence is re-asserted — a no-op
    unless you toggled it off while in the master. Everything visual after that is
    Go Down's SketchActivate handler + fvs.master_session_on_activate, which
    restores the view state from before the hop."""
    _master, origin, occ = fvs.master_session_sketches(design, s)
    if origin is None:
        fvs.end_master_session('original sketch gone')
        _hud('fail', 'Edit Original: the sketch you came from no longer exists.')
        return
    if occ is not None:
        fvs.isolate_occurrence_no_snapshot(design, occ)
        ui.activeSelections.clear()
        occ.activate()
        fvs.remember_activation(occ)
        target = origin.createForAssemblyContext(occ)
    else:
        design.activateRootComponent()
        fvs.remember_activation(None)
        target = origin
    ui.activeSelections.clear()
    ui.activeSelections.add(target)
    cd = ui.commandDefinitions.itemById('SketchActivate')
    if cd:
        cd.execute()
    _log('return: re-entering', origin.name)


class _OnCommandTerminated(adsk.core.ApplicationCommandEventHandler):
    """SketchStop finishing, while MasterReturnCmd is taking you back, continues the
    return. Any other SketchStop is none of our business: Flush Undo closes and
    re-opens the master, and a plain Finish Sketch just leaves it (the session is
    then dropped by the next thing that asks — see fvs.request_master_return)."""

    def notify(self, args):
        try:
            if args.commandId != 'SketchStop':
                return
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            s = fvs.master_session(design)
            if not s or s.get('phase') != 'returning':
                return
            _finish_return(design, s)
        except Exception:
            fvs.end_master_session('return failed')
            _log('return EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


class _OnMarkingMenuDisplaying(adsk.core.MarkingMenuEventHandler):
    """Adds Edit Original / Delete Master / Convert to Sketch to the right-click
    menu when the selection is a projected entity, and Delete Master alone when
    it's a master sketch (picked in the browser). There is no native command to
    intercept instead — the spike found no command definition with "edit" +
    "original" in its id, and the same is true of these two."""

    def notify(self, args):
        try:
            sels = ui.activeSelections
            if sels.count != 1:
                return
            entity = sels.item(0).entity
            proj = masters.projection_of(entity)
            if proj and proj[0]:
                for cid in (EDIT_ORIG_ID, DELETE_MASTER_ID, CONVERT_BACK_ID):
                    cd = ui.commandDefinitions.itemById(cid)
                    if cd:
                        args.linearMarkingMenu.controls.addCommand(cd)
                return
            sk = adsk.fusion.Sketch.cast(entity)
            if sk is not None and masters.is_master(sk.nativeObject or sk):
                cd = ui.commandDefinitions.itemById(DELETE_MASTER_ID)
                if cd:
                    args.linearMarkingMenu.controls.addCommand(cd)
        except Exception:
            _log('markingMenuDisplaying EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


# ─────────────────────────────────────────────────────────────────────────────
# Delete Master / Convert to Sketch — the way back out of a master
# ─────────────────────────────────────────────────────────────────────────────

def _selected_master(design):
    """(master, projection_entity_or_None) resolved from the current selection:
    a selected projection (Edit Original's own lookup) first, then a master
    sketch picked in the browser — the second case only makes sense for Delete
    Master, since Convert to Sketch needs a projection to replace.
    """
    mid, entity = _selected_projection(design)
    if mid is not None:
        master = masters.find_master(design, mid)
        if master is not None:
            return master, entity
    for i in range(ui.activeSelections.count):
        sk = adsk.fusion.Sketch.cast(ui.activeSelections.item(i).entity)
        if sk is None:
            continue
        native = sk.nativeObject or sk
        if masters.is_master(native):
            return native, None
    return None, None


def _master_is_open(design, master):
    """True while *master* itself is the sketch open for edit — Edit Original's
    session, which both commands refuse to touch until you leave it."""
    if not fvs.is_editing_sketch(design):
        return False
    active = fvs.active_edit_sketch(design)
    return active is not None and fvs._same_sketch(active, master)


def _occurrence_for_bake(design, sketch):
    """(occurrence, refusal) — the occurrence to bake *sketch* through when it
    isn't the one currently open for edit: its component's first occurrence, or
    None for the root. *refusal* is set instead when that occurrence isn't at
    the identity transform — the same rule Convert applies to the sketch it
    converts from.
    """
    comp = sketch.parentComponent
    if comp == design.rootComponent:
        return None, None
    occs = design.rootComponent.allOccurrencesByComponent(comp)
    occ = occs.item(0) if occs and occs.count else None
    if occ is None:
        return None, 'no occurrence of "%s" could be found' % comp.name
    if not sc._is_identity(occ.transform2):
        return None, ('"%s" sits in a moved occurrence — baking only works when every '
                       'occurrence on the path is at the identity transform' % occ.name)
    return occ, None


def _find_projection(sketch, mid):
    """Every projected entity in *sketch* belonging to master *mid* — every
    instance, via masters.instance_entities per instance id, falling back to a
    plain projection_of match when the instance attribute is missing."""
    out, seen_instances = [], set()
    for coll in (sketch.sketchCurves, sketch.sketchPoints):
        for e in coll:
            if not getattr(e, 'isReference', False):
                continue
            m, inst = masters.projection_of(e) or (None, None)
            if m != mid:
                continue
            if inst is None:
                out.append(e)
            elif inst not in seen_instances:
                seen_instances.add(inst)
                out.extend(masters.instance_entities(sketch, inst))
    return out


def _delete_projection(sketch, mid, problems):
    for e in _find_projection(sketch, mid):
        try:
            if e.isValid:
                e.deleteMe()
        except Exception as ex:
            problems.append('could not delete a projection in "%s": %s' % (sketch.name, ex))


def _match_reference_point(sketch, key):
    """The reference (projected) point in *sketch* at geo key *key*, or None —
    step 4 of _bake: an existing bounds/origin/axis projection the target
    already carries, that an anchor point can be tied to instead of copied."""
    if key is None:
        return None
    for p in sketch.sketchPoints:
        if not getattr(p, 'isReference', False):
            continue
        if sc._geo_key(p) == key:
            return p
    return None


def _bake(design, master, sketch, occ):
    """Replace *sketch*'s projection of *master* with real geometry — the
    reverse of _run_convert, using the same primitives. *sketch* is the NATIVE
    sketch; *occ* is the occurrence to read its world position through (None for
    the root). Returns (new_entities, problems); *new_entities* is empty when the
    bake was refused outright.
    """
    problems = []
    mid = masters.master_id(master)
    # 1. The projection instances this sketch holds of *master*.
    projection_toks = {sc._tok(e) for e in _find_projection(sketch, mid)}
    if not projection_toks:
        return [], problems

    # 2. What local geometry ties into the projection — recorded before anything
    # about it is deleted, so it can be relinked to the baked-back geometry.
    deps = _find_dependents(sketch, projection_toks)

    # 3. The transform from the master's world frame into this sketch's own —
    # identity for the sketch Convert came from.
    xform = sc._compose(sc._world_matrix(master, None), sc._inverse(sc._world_matrix(sketch, occ)))
    arr = xform.asArray()
    if abs(arr[11]) > sc._TOL or abs(arr[10] - 1.0) > sc._TOL:
        return [], ['"%s" is not coplanar with "%s" — the projection cannot be baked back'
                    % (sketch.name, master.name)]

    # 4. Anchor points (bounds) the master carries: mapped to an existing
    # reference point in the target when one is already there at the same
    # position (dropped from the copy, tied in as a ref_pair instead), else
    # copied as-is — a construction point still pinned by its bound parameters.
    anchor_pts = [pt for pt in master.sketchPoints
                  if pt.attributes.itemByName(masters.ATTR_GROUP, masters.ANCHOR_KEY) is not None]
    mapped_anchor_toks = set()
    anchor_ref_pairs = []
    for pt in anchor_pts:
        match = _match_reference_point(sketch, sc._geo_key(pt, xform))
        if match is not None:
            anchor_ref_pairs.append((pt, match))
            mapped_anchor_toks.add(sc._tok(pt))

    # 5. Rebuild the master's own projected root axes in the target, plus the
    # anchor pairs from step 4.
    ref_pairs, _reprojected, reproject_failed = sc._reproject_all(design, master, sketch)
    ref_pairs = ref_pairs + anchor_ref_pairs
    if reproject_failed:
        problems.append('%d reference%s inside "%s" could not be rebuilt'
                         % (reproject_failed, '' if reproject_failed == 1 else 's', master.name))

    # 6. Delete the projection — curves before points, same ordering as Convert.
    projection_ents = _find_projection(sketch, mid)
    for e in sorted(projection_ents, key=lambda x: 1 if adsk.fusion.SketchPoint.cast(x) else 0):
        try:
            if e.isValid:
                e.deleteMe()
        except Exception as ex:
            problems.append('could not remove the projection: %s' % ex)

    # 7. Copy the master's own geometry back — everything except its origin, its
    # own projected references, and any anchor that already has a match.
    copy_coll = adsk.core.ObjectCollection.create()
    for coll in (master.sketchCurves, master.sketchPoints):
        for e in coll:
            if e == master.originPoint or getattr(e, 'isReference', False):
                continue
            if sc._tok(e) in mapped_anchor_toks:
                continue
            copy_coll.add(e)
    try:
        master.copy(copy_coll, xform, sketch)
    except Exception as e:
        problems.append('could not copy the master\'s geometry back: %s' % e)
        return [], problems

    key_map = sc._build_key_map(sketch)
    cons, dims, skipped, expr_restored, unparam = sc._relink_external(
        design, master, sketch, xform, key_map, ref_pairs, {})
    if skipped:
        problems.append('%d relink%s skipped baking "%s" back' %
                         (skipped, ' was' if skipped == 1 else 's were', sketch.name))
    sc._fixup_internal_dimensions(design, master, sketch, xform, key_map, {})
    sc._refix(copy_coll, xform, key_map)

    # 8. Reattach whatever step 2 recorded to its baked-back counterpart.
    problems += _relink_dependents(sketch, deps, key_map)

    new_entities = []
    for e in copy_coll:
        target = key_map.get(sc._geo_key(e, xform))
        if target is not None:
            new_entities.append(target)
    return new_entities, problems


def _run_delete_master():
    problems = []
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        master, _entity = _selected_master(design)
        if master is None:
            _hud('fail', 'Delete Master: select a projection, or a master sketch in '
                          'the browser, first.')
            return
        if _master_is_open(design, master):
            _hud('fail', 'Delete Master: leave "%s" first — you have it open through '
                          'Edit Original.' % master.name)
            return

        mid = masters.master_id(master)
        held_by = masters.holders(design, mid)
        names = ', '.join(sorted(sk.name for sk in held_by)) if held_by else 'nothing'
        answer = ui.messageBox(
            'Delete "%s"?\n\nProjected into: %s.\n\n'
            'Yes = bake its projections into ordinary geometry first\n'
            'No = delete the projections instead\n'
            'Cancel = do nothing'
            % (master.name, names),
            'Delete Master', adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType,
            adsk.core.MessageBoxIconTypes.WarningIconType)
        if answer == adsk.core.DialogResults.DialogCancel:
            return

        if answer == adsk.core.DialogResults.DialogYes:
            for sk in held_by:
                bocc, refusal = _occurrence_for_bake(design, sk)
                if refusal:
                    problems.append(refusal)
                    continue
                _new, probs = _bake(design, master, sk, bocc)
                problems += probs
        else:
            for sk in held_by:
                _delete_projection(sk, mid, problems)

        problems += masters.delete_master(design, master)

        if problems:
            _hud('fail', 'Delete Master:\n' + '\n'.join(problems))
        else:
            _hud('ok')
        _log('delete master ok, problems=%d%s' % (len(problems),
             ''.join('\n    · ' + p for p in problems)))
    except Exception:
        tb = traceback.format_exc()
        _log('delete master EXCEPTION:', tb.replace('\n', ' | '))
        _hud('fail', 'Delete Master – error:\n' + tb)


def _run_convert_back():
    problems = []
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return
        if not fvs.is_editing_sketch(design):
            _hud('fail', 'Convert to Sketch: open the sketch that holds the projection first.')
            return
        active = fvs.active_edit_sketch(design)
        if active is None:
            _hud('fail', 'Convert to Sketch: could not tell which sketch is open.')
            return
        native_active = active.nativeObject or active

        master, _entity = _selected_master(design)
        if master is None:
            _hud('fail', 'Convert to Sketch: select a projection of the master first.')
            return
        if fvs._same_sketch(active, master):
            _hud('fail', 'Convert to Sketch: leave "%s" first — you can\'t convert the '
                          'master you have open.' % master.name)
            return

        mid = masters.master_id(master)
        if not masters._already_holds(native_active, mid):
            _hud('fail', 'Convert to Sketch: "%s" does not hold a projection of "%s".'
                 % (native_active.name, master.name))
            return

        held_by = masters.holders(design, mid)
        others = [sk for sk in held_by if not fvs._same_sketch(sk, native_active)]

        answer = adsk.core.DialogResults.DialogNo
        if others:
            names = ', '.join(sorted(sk.name for sk in others))
            answer = ui.messageBox(
                '"%s" is also projected into: %s.\n\n'
                'Yes = bake it there too\n'
                'No = delete those projections\n'
                'Cancel = do nothing'
                % (master.name, names),
                'Convert to Sketch', adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType,
                adsk.core.MessageBoxIconTypes.WarningIconType)
            if answer == adsk.core.DialogResults.DialogCancel:
                return

        occ = active.assemblyContext
        new_entities, probs = _bake(design, master, native_active, occ)
        problems += probs
        if not new_entities:
            _hud('fail', 'Convert to Sketch:\n' +
                 '\n'.join(problems or ['baking the projection back failed']))
            return

        for sk in others:
            if answer == adsk.core.DialogResults.DialogYes:
                bocc, refusal = _occurrence_for_bake(design, sk)
                if refusal:
                    problems.append(refusal)
                    continue
                _n, probs = _bake(design, master, sk, bocc)
                problems += probs
            else:
                _delete_projection(sk, mid, problems)

        problems += masters.delete_master(design, master)

        ui.activeSelections.clear()
        for e in new_entities:
            try:
                ui.activeSelections.add(sc._proxy_for_entity(e, active))
            except Exception:
                pass

        if problems:
            _hud('fail', 'Converted to sketch with problems:\n' + '\n'.join(problems))
        else:
            _hud('ok')
        _log('convert back ok, problems=%d%s' % (len(problems),
             ''.join('\n    · ' + p for p in problems)))
    except Exception:
        tb = traceback.format_exc()
        _log('convert back EXCEPTION:', tb.replace('\n', ' | '))
        _hud('fail', 'Convert to Sketch – error:\n' + tb)


# ─────────────────────────────────────────────────────────────────────────────
# Grouped selection while editing
# ─────────────────────────────────────────────────────────────────────────────

class _OnActiveSelectionChanged(adsk.core.ActiveSelectionEventHandler):
    _guard = False

    def notify(self, args):
        if _OnActiveSelectionChanged._guard:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design or not fvs.is_editing_sketch(design):
                return
            sketch = fvs.active_edit_sketch(design)
            if sketch is None:
                return
            native_sketch = sketch.nativeObject or sketch

            sels = ui.activeSelections
            to_add = []
            for i in range(sels.count):
                e = sels.item(i).entity
                native_e = getattr(e, 'nativeObject', None) or e
                try:
                    parent = native_e.parentSketch
                except Exception:
                    parent = None
                if parent != native_sketch:
                    continue
                for other in masters.reference_group(native_sketch, native_e):
                    proxied = other.createForAssemblyContext(sketch.assemblyContext) \
                        if sketch.assemblyContext else other
                    if not any(sc._tok(proxied) == sc._tok(sels.item(j).entity)
                               for j in range(sels.count)):
                        to_add.append(proxied)

            if to_add:
                _OnActiveSelectionChanged._guard = True
                try:
                    for e in to_add:
                        try:
                            sels.add(e)
                        except Exception:
                            pass
                finally:
                    _OnActiveSelectionChanged._guard = False
        except Exception:
            _OnActiveSelectionChanged._guard = False
            _log('activeSelectionChanged EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


# ─────────────────────────────────────────────────────────────────────────────
# Whole-projection delete
# ─────────────────────────────────────────────────────────────────────────────

_DELETE_IDS = ('DeleteCommand', 'FusionDeleteCommand')


class _OnCommandStarting(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if args.commandId not in _DELETE_IDS:
                return
            sels = ui.activeSelections
            tagged, untagged = [], []
            seen_instances = set()
            for i in range(sels.count):
                e = sels.item(i).entity
                native_e = getattr(e, 'nativeObject', None) or e
                proj = masters.projection_of(native_e)
                if proj and proj[1]:
                    seen_instances.add((native_e.parentSketch, proj[1]))
                    tagged.append(e)
                else:
                    untagged.append(e)
            if not tagged:
                return                    # nothing of ours in this selection

            args.isCancelled = True
            failed = 0
            for sketch, inst in seen_instances:
                for e in masters.instance_entities(sketch, inst):
                    try:
                        e.deleteMe()
                    except Exception:
                        failed += 1
            for e in untagged:
                native_e = getattr(e, 'nativeObject', None) or e
                try:
                    native_e.deleteMe()
                except Exception:
                    failed += 1
            if failed:
                _hud('fail', 'Delete: %d entit%s could not be removed.'
                     % (failed, 'y' if failed == 1 else 'ies'))
            _log('delete override: %d instance(s), %d untagged, %d failed' %
                 (len(seen_instances), len(untagged), failed))
        except Exception:
            _log('delete override EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


# ─────────────────────────────────────────────────────────────────────────────
# Command plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _declare_actions():
    hk = _hotkeys()
    hk.declare(CONVERT_ID, CONVERT_NAME, CONVERT_DESC, owner='Master Sketches',
               run=_run_convert)
    hk.declare(EDIT_ORIG_ID, EDIT_ORIG_NAME, EDIT_ORIG_DESC, owner='Master Sketches',
               run=_run_edit_original)
    hk.declare(RETURN_ID, RETURN_NAME, RETURN_DESC, owner='Master Sketches',
               run=_run_return)
    hk.declare(DELETE_MASTER_ID, DELETE_MASTER_NAME, DELETE_MASTER_DESC, owner='Master Sketches',
               run=_run_delete_master)
    hk.declare(CONVERT_BACK_ID, CONVERT_BACK_NAME, CONVERT_BACK_DESC, owner='Master Sketches',
               run=_run_convert_back)
    hk.attach()


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        _declare_actions()
        fss.delete_key('master_return')   # the pre-session return address; retired

        on_term = _OnCommandTerminated()
        ui.commandTerminated.add(on_term)
        handlers.append(on_term)

        on_marking = _OnMarkingMenuDisplaying()
        ui.markingMenuDisplaying.add(on_marking)
        handlers.append(on_marking)

        on_sel = _OnActiveSelectionChanged()
        ui.activeSelectionChanged.add(on_sel)
        handlers.append(on_sel)

        on_delete = _OnCommandStarting()
        ui.commandStarting.add(on_delete)
        handlers.append(on_delete)

        _log('=== run() — Master Sketches ready ===')
    except Exception:
        if ui:
            ui.messageBox('Master Sketches – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        for h in handlers:
            for event in (getattr(ui, 'commandTerminated', None),
                          getattr(ui, 'markingMenuDisplaying', None),
                          getattr(ui, 'activeSelectionChanged', None),
                          getattr(ui, 'commandStarting', None)):
                if event is None:
                    continue
                try:
                    event.remove(h)
                except Exception:
                    pass
        handlers.clear()
        try:
            _hotkeys().withdraw('Master Sketches')
        except Exception:
            pass
        _log('=== stop() ===')
    except Exception:
        if ui:
            ui.messageBox('Master Sketches – stop error:\n' + traceback.format_exc())
