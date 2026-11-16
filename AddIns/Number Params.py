"""
Number Params — every typed number becomes a parameter nobody has to name.

The Fusion half of fusion_ui/number_params.py (read its docstring for which literals
count and how a changed one keeps its parameter). A number parameter is a user
parameter named Num_<8 hex>, its expression the literal ("10", "2.5 cm"), its comment
number_params.COMMENT. The Parameters add-in leaves them out of the Browser and the
Params window; the value window shows each as its value, in its own chip colour.

    convert(design, text, unit, old)      text with its literals swapped for number
                                          parameters (made, or reused from `old`)
    set_expression(design, param, text)   the same, written to a parameter (a sketch
                                          dimension's, a feature's, a user one), and the
                                          old expression's leftovers dropped
    sweep(design)                         every literal already in the document, once

Loaded with _load() like Made Params.py, so each add-in runs its own copy.
"""

import os
import types

import adsk.core
import adsk.fusion

_PATH = '/Users/josh/Code/Scripts/fusion_ui/number_params.py'


def _load_np():
    m = types.ModuleType('number_params')
    m.__file__ = _PATH
    with open(_PATH, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), _PATH, 'exec'), m.__dict__)
    return m


np = _load_np()
is_number_name = np.is_number_name
shown = np.shown


def is_number(p):
    try:
        return np.is_number_name(p.name)
    except Exception:
        return False


def old_numbers(design, text):
    """{name: (expression, unit)} — the number parameters `text` names, in order. Hand
    the same dict to several convert() calls (a feature's distance, taper, offset): each
    takes the ones it reuses out of it, and what is left at the end is to drop()."""
    return _old_numbers(design, text)


def _old_numbers(design, text):
    """{name: (expression, unit)} — the number parameters `text` names, in order."""
    out = {}
    ups = design.userParameters
    for n in np.names_in(text):
        try:
            p = ups.itemByName(n)
            if p is not None:
                out[n] = (p.expression, p.unit or '')
        except Exception:
            continue
    return out


def convert(design, text, unit, old='', keep_whole=False):
    """(new text, leftover names, made names). Raises on a parameter Fusion refuses;
    anything made before that is deleted again first. `old` is the expression this
    replaces, or an old_numbers() dict (shared across calls: reused names are taken
    out of it)."""
    text = text or ''
    if keep_whole and np.is_whole_literal(text):
        return text, [], []
    pool = old if isinstance(old, dict) else (_old_numbers(design, old) if old else {})
    spans, left = np.plan(text, unit, pool, kinds=_kinds(design))
    if isinstance(old, dict):
        for s in spans:
            old.pop(s['name'], None)
    if not spans:
        return text, left, []
    ups = design.userParameters
    made = []
    try:
        for s in spans:
            if s['name']:
                p = ups.itemByName(s['name'])
                if p is not None and np.literal_value(p.expression, p.unit) != \
                        np.literal_value(s['text'], s['unit']):
                    p.expression = s['text']
                continue
            name = np.new_name()
            ups.add(name, adsk.core.ValueInput.createByString(s['text']), s['unit'], np.COMMENT)
            s['name'] = name
            made.append(name)
    except Exception:
        drop(design, made)
        raise
    return np.rewrite(text, spans), left, made


def _kinds(design):
    """name -> 'L' / 'A' / 'S' (unitless) / None, read only for the names asked about
    (np.literals: a bracket that multiplies — "Shelf * (N + 1)" — is a factor when N
    is unitless). Looked up once each."""
    memo = {}

    def kind(name):
        if name not in memo:
            k = None
            try:
                p = design.allParameters.itemByName(name)
                if p is not None:      # 'mm^2' and the like: neither, so unknown
                    u = (p.unit or '').strip()
                    k = np.kind_of_unit(u) or ('S' if not u else None)
            except Exception:
                pass
            memo[name] = k
        return memo[name]
    return kind


_FEATURE_VALUES = ('extentOne.distance', 'extentTwo.distance', 'taperAngleOne',
                   'taperAngleTwo', 'startExtent.offset', 'distanceOne', 'distanceTwo',
                   'scaleFactor')


def feature_text(feat):
    """Every value expression a feature of ours writes, joined — the `old` for
    old_numbers() when that feature is being edited."""
    out = []
    for path in _FEATURE_VALUES:
        try:
            o = feat
            for a in path.split('.'):
                o = getattr(o, a)
            out.append(o.expression)
        except Exception:
            continue
    return ' + '.join(out)


def set_expression(design, param, text, unit=None, keep_whole=False):
    """Write `text` to `param` with its literals as number parameters. Returns '' or
    why not (and then nothing made is left behind)."""
    try:
        old = param.expression
        unit = unit if unit is not None else (param.unit or '')
    except Exception as e:
        return str(e)
    try:
        new, left, made = convert(design, text, unit, old, keep_whole)
    except Exception as e:
        return 'Could not make a parameter of a typed number: %s' % e
    try:
        if new != old:
            param.expression = new
    except Exception as e:
        drop(design, made)
        return str(e)
    drop(design, left)
    return ''


def drop(design, names):
    """Delete the number parameters named, where nothing uses them any more."""
    ups = design.userParameters
    for n in names or ():
        try:
            p = ups.itemByName(n)
            if p is not None and is_number(p) and p.dependentParameters.count == 0:
                p.deleteMe()
        except Exception:
            continue


def sweep(design, progress=None):
    """Every literal in the document, as number parameters: each model parameter (a
    sketch dimension's, a feature's), each user parameter's value unless it is one
    literal (that IS a parameter already). Driven dimensions and anything Fusion will
    not let us write are skipped. Returns (converted, [(name, why)])."""
    todo = []
    allp = design.allParameters
    n = allp.count
    for i in range(n):
        try:
            p = allp.item(i)
            if is_number(p):
                continue
            expr, unit = p.expression, p.unit or ''
            whole = adsk.fusion.UserParameter.cast(p) is not None
            if whole and np.is_whole_literal(expr):
                continue
            if not np.literals(expr, unit):
                continue
            todo.append((p, whole))
        except Exception:
            continue
    done, failed = 0, []
    for k, (p, whole) in enumerate(todo):
        if progress is not None:
            try:
                if not progress(k, len(todo)):
                    break
            except Exception:
                pass
        try:
            dim = p.createdBy
            sd = adsk.fusion.SketchDimension.cast(dim)
            if sd is not None and not sd.isDriving:
                continue
        except Exception:
            pass
        err = set_expression(design, p, p.expression, keep_whole=whole)
        if err:
            try:
                failed.append((p.name, err))
            except Exception:
                failed.append(('?', err))
        else:
            done += 1
    return done, failed
