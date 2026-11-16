"""
Made Params — the value window's "Make parameter" while one of OUR commands is live.

The make panel (fusion_value/index.js, "Make a parameter") turns part of an expression
into a user parameter of its own. In a dimension or parameter session the Dimension Value
add-in simply creates it. Behind the Extrude and feature panels it CANNOT: a command of
ours is running there, and every executePreview is rolled back (isValidResult = False —
see Extrude's _ExecutePreview), taking with it whatever was written to the model since
the last one. A parameter the Dimension Value tick made between two previews showed up
in the Browser, went into the distance field, and vanished with the very next preview —
the field then named nothing ("invalid expression") and the document never had it
(2026-09-30).

So the value server routes makeparam to the live command's own queue, and the command
owns the parameter the way it owns its feature:

    take(design, c)   check it and remember it (no write) — the refusal, or None
    ensure(design)    write every remembered one that is missing. At the top of each
                      preview (rolled back with it, written again the next time) and
                      of execute, where it stands.
    params(design, snapshot)   the panel's parameter list with the remembered ones in
                      it, whether or not the model has them at this instant.

Execute also runs on a CANCEL (doExecute(True) with the builder off), and the commands
call ensure before that early return: a parameter was explicitly made, and backing out
of the extrude is not backing out of that.

Loaded with _load() like Bounds.py, so each add-in runs its own copy. Dimension Value's
own _do_make_param checks through `check` here too, so the two refuse alike.
"""

import json
import re
import urllib.request

import adsk.core
import adsk.fusion

NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
BROWSER_URL = 'http://127.0.0.1:8732'
PLACE_PARAM_ID = 'BrowserPlaceNewParamCmd'
EDITED_PARAM_ID = 'BrowserParamEditedCmd'


def infer_unit(design, expr):
    """The unit Fusion takes the expression in — length, then angle, then none (see
    Dimension Value's _infer_unit for why that order) — or None if it takes it in none."""
    um = design.unitsManager
    for unit in (um.defaultLengthUnits, 'deg', ''):
        try:
            if um.isValidExpression(expr, unit):
                return unit
        except Exception:
            pass
    return None


def check(design, name, expr):
    """(unit, None) for a parameter that can be made; (None, why) for one that can't."""
    if not name:
        return None, 'Give the parameter a name.'
    if not NAME_RE.match(name):
        return None, '"%s" is not a valid parameter name.' % name
    if design.allParameters.itemByName(name) is not None:
        return None, 'A parameter named "%s" already exists.' % name
    unit = infer_unit(design, expr)
    if unit is None:
        return None, '"%s" is not a valid expression on its own.' % expr
    return unit, None


def check_edit(design, name, expr, new_name):
    """None for an edit that can be made; else why not. `new_name` '' = keep the name."""
    p = design.userParameters.itemByName(name)
    if p is None:
        return 'The parameter "%s" no longer exists.' % name
    if not expr:
        return 'Give the parameter a value.'
    if new_name and new_name != name:
        if not NAME_RE.match(new_name):
            return '"%s" is not a valid parameter name.' % new_name
        if design.allParameters.itemByName(new_name) is not None:
            return 'A parameter named "%s" already exists.' % new_name
    unit = p.unit or design.unitsManager.defaultLengthUnits
    try:
        if not design.unitsManager.isValidExpression(expr, unit):
            return '"%s" is not a valid expression.' % expr
    except Exception:
        return '"%s" is not a valid expression.' % expr
    return None


def apply_edit(design, name, expr, new_name):
    """Write one edit — expression, then name (Fusion rewrites every expression that
    uses the old name). Looks the parameter up under either name, so an edit that was
    already applied is not an error. True if anything was written."""
    p = design.userParameters.itemByName(name)
    if p is None and new_name:
        p = design.userParameters.itemByName(new_name)
    if p is None:
        return False
    wrote = False
    if (p.expression or '').strip() != expr:
        p.expression = expr
        wrote = True
    if new_name and p.name != new_name:
        p.name = new_name
        wrote = True
    return wrote


def notify_browser_edit(name, new_name, expr):
    """Tell the Browser a parameter changed (it files and sorts them by name). Best-effort."""
    try:
        body = json.dumps({'id': EDITED_PARAM_ID,
                           'arg': {'name': name, 'newName': new_name or '', 'expression': expr}})
        req = urllib.request.Request(BROWSER_URL + '/uiaction', data=body.encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=0.4).read()
    except Exception:
        pass


def place_in_browser(name, expr, unit):
    """File the new row where the Browser window says it belongs — Dimension Value's
    _place_param_in_browser. Best-effort: with the dock closed it lands unfiled."""
    try:
        body = json.dumps({'id': PLACE_PARAM_ID,
                           'arg': {'name': name, 'expression': expr, 'unit': unit}})
        req = urllib.request.Request(BROWSER_URL + '/uiaction', data=body.encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=0.4).read()
    except Exception:
        pass


class Made:
    def __init__(self):
        self.items = []              # [{name, expression, unit}], in the order made
        self.edits = []              # [{name, expression, newName}] — "Edit parameter"

    def reset(self):
        del self.items[:]
        del self.edits[:]

    def take_edit(self, design, c):
        """Check an "Edit parameter" and remember it (no write — see ensure). The
        refusal, or None."""
        name = str(c.get('name', '')).strip()
        expr = str(c.get('expression', '')).strip()
        new_name = str(c.get('newName', '')).strip()
        why = check_edit(design, name, expr, new_name)
        if why is not None:
            return why
        self.edits.append({'name': name, 'expression': expr, 'newName': new_name})
        return None

    def take(self, design, c):
        name = str(c.get('name', '')).strip()
        expr = str(c.get('expression', '')).strip()
        unit, why = check(design, name, expr)
        if why is None and any(i['name'] == name for i in self.items):
            why = 'A parameter named "%s" already exists.' % name
        if why is not None:
            return why
        self.items.append({'name': name, 'expression': expr, 'unit': unit})
        place_in_browser(name, expr, unit)
        return None

    def ensure(self, design):
        if self.items:
            params = design.userParameters
            for i in self.items:
                try:
                    if design.allParameters.itemByName(i['name']) is None:
                        params.add(i['name'], adsk.core.ValueInput.createByString(i['expression']),
                                   i['unit'], '')
                except Exception:
                    pass
        for e in self.edits:
            try:
                apply_edit(design, e['name'], e['expression'], e['newName'])
            except Exception:
                pass

    def finish_edits(self):
        """Execute is done: tell the Browser about each edit once, and forget them."""
        for e in self.edits:
            notify_browser_edit(e['name'], e['newName'], e['expression'])
        del self.edits[:]

    def params(self, design, snapshot):
        have = set(p.get('name') for p in snapshot)
        out = [dict(p) for p in snapshot]
        # Edits not yet written (a preview rolls them back): show them as made.
        for e in self.edits:
            for p in out:
                if p.get('name') in (e['name'], e['newName']):
                    p['expression'] = e['expression']
                    if e['newName']:
                        p['name'] = e['newName']
        have = set(p.get('name') for p in out)
        for i in self.items:
            if i['name'] in have:
                continue
            try:
                value = design.unitsManager.evaluateExpression(i['expression'], i['unit'])
            except Exception:
                value = 0
            out.append({'name': i['name'], 'expression': i['expression'],
                        'unit': i['unit'], 'value': value, 'comment': ''})
        return out
