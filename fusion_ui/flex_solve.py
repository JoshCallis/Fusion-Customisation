"""
flex_solve.py — which parameters a Move changes, and how far it can go.

Moving a thing means moving the coordinate its Form describes (section_derive) by Δ.
Its Form is a sum of parameters, so the move is a change to SOME of them — and which
ones is the whole question. The answers, as rules:

  * only BASE parameters change: a user parameter whose expression is a number. A
    derived one (Gap = Wall + 2 mm) is expanded into the base ones it is made of, so
    the relation it states is kept.
  * a parameter marked rigid (flexible off) never changes; bounds parameters do not
    either, unless the move is allowed to move a bound.
  * TIES hold: two Forms tied together (a constraint the user made) stay equal, so
    moving one drags the other's parameters along.
  * the move is kept LOCAL: among all the ways to make it, the one that changes
    rarely-used parameters most. Weighted least squares — sum(w_i * d_i^2), w_i the
    parameter's weight (how widely it is used) — subject to the target moving by Δ
    and every tie holding.
  * RANGES hold: a flexible parameter's min/max, and each gap monitor's (a Form
    measured between two things, with a min and/or max). A solution that breaks one
    freezes the offender at its limit and solves again with what is left (active
    set); when nothing is left to give, the move is refused with the largest Δ that
    does fit and the names of what stops it.
  * the OTHER SIDE — parameters the target does not use at all, only reachable
    through a monitor or tie (the things tethered to the other bound) — is only
    touched when allowed. The add-in asks first.

Pure, no adsk. Test: flex_solve_test.py.
"""

TOL = 1e-9     # cm. Loose enough to forgive float noise, tight enough that the bisection
               # for "how far can it go" does not land a visible hair past a limit.


# ── Linear algebra (no numpy in every Fusion) ───────────────────────────────
def _solve(A, b):
    """Solve A x = b, A square (list of rows). None if singular."""
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def _least_norm(rows, rhs, weights):
    """min sum(w_i d_i^2)  s.t.  rows · d = rhs.   d = W^-1 R^T (R W^-1 R^T)^-1 rhs.
    Dependent rows (a tie restating the target) are dropped first. None if the rows
    contradict each other."""
    n = len(weights)
    keep_rows, keep_rhs = [], []
    for r, v in zip(rows, rhs):
        cand = keep_rows + [r]
        G = [[sum(a[k] * c[k] / weights[k] for k in range(n)) for c in cand] for a in cand]
        if _solve(G, [0.0] * len(cand)) is None:
            # Singular: this row repeats earlier ones. Fine if its rhs agrees —
            # checked against the solution below.
            continue
        keep_rows.append(r)
        keep_rhs.append(v)
    if not keep_rows:
        return [0.0] * n
    G = [[sum(a[k] * c[k] / weights[k] for k in range(n)) for c in keep_rows] for a in keep_rows]
    lam = _solve(G, keep_rhs)
    if lam is None:
        return None
    d = [sum(keep_rows[j][k] * lam[j] for j in range(len(keep_rows))) / weights[k] for k in range(n)]
    for r, v in zip(rows, rhs):
        if abs(sum(r[k] * d[k] for k in range(n)) - v) > 1e-7:
            return None
    return d


# ── Expansion to base parameters ─────────────────────────────────────────────
def expand(coefs, derived, depth=0):
    """{term: coef} with every derived parameter replaced by its own expansion.
    `derived` = {name: {term: coef}} (its expression as a linear Form's terms; the
    constant does not matter for a CHANGE)."""
    if depth > 40:
        raise ValueError('derived parameters nest too deep')
    out = {}
    for n, c in coefs.items():
        if n in derived:
            for m, cm in expand(derived[n], derived, depth + 1).items():
                out[m] = out.get(m, 0.0) + c * cm
        else:
            out[n] = out.get(n, 0.0) + c
    return {n: c for n, c in out.items() if abs(c) > 1e-12}


# ── The solve ────────────────────────────────────────────────────────────────
def solve(target, delta, values, derived=None, rigid=(), bounds=(), ranges=None,
          ties=(), monitors=(), weights=None, allow_other=False, allow_bounds=False,
          prefer=None):
    """
    target    {term: coef}   the moved coordinate's Form terms
    delta     cm             how far it should move
    values    {base param: current value}
    derived   {param: {term: coef}}
    rigid     names that never change; bounds: the bounds parameters
    ranges    {param: (min|None, max|None)}   absolute values
    ties      [({term: coef}, {term: coef})]  must stay equal
    monitors  [({term: coef}, current value, min|None, max|None, label)]
    weights   {param: w > 0}  (default 1)
    prefer    a candidate's name, or 'spread'; None = the most local

    Returns {'ok', 'changes': {param: d}, 'achieved': cm, 'limit': cm|None,
             'blocked': [labels], 'reason': text, 'candidates': [names], 'chosen'}
    """
    derived = derived or {}
    ranges = ranges or {}
    weights = weights or {}
    tgt = expand(target, derived)
    tie_rows = [expand(_sub(a, b), derived) for a, b in ties]
    mons = [(expand(g, derived), v, lo, hi, label) for g, v, lo, hi, label in monitors]

    mine = set(tgt)
    for row in tie_rows:
        if set(row) & mine:
            mine |= set(row)
    pool = set(mine)
    if allow_other:
        for row in tie_rows:
            pool |= set(row)
        for g, _v, _lo, _hi, _l in mons:
            pool |= set(g)
    free = sorted(n for n in pool if n in values and n not in rigid
                  and (allow_bounds or n not in bounds))
    if not free:
        others = [n for n in tgt if n not in bounds]
        why = ('every parameter that places it is rigid' if others
               else 'it is placed by the bounds alone')
        return {'ok': False, 'changes': {}, 'achieved': 0.0, 'limit': 0.0,
                'blocked': [], 'reason': 'Nothing can move it: %s.' % why}

    def attempt(d_total, allowed=None):
        allowed = free if allowed is None else allowed
        frozen = {}
        active = []           # (row, rhs) for monitors pinned at a limit
        for _it in range(len(allowed) + len(mons) + 2):
            var = [n for n in allowed if n not in frozen]
            if not var and not frozen:
                return None, ['nothing free']
            w = [float(weights.get(n, 1.0)) for n in var]
            rows, rhs = [], []
            fixed_contrib = lambda row: sum(row.get(n, 0.0) * v for n, v in frozen.items())
            rows.append([tgt.get(n, 0.0) for n in var])
            rhs.append(d_total - fixed_contrib(tgt))
            for row in tie_rows:
                rows.append([row.get(n, 0.0) for n in var])
                rhs.append(-fixed_contrib(row))
            for row, target_change in active:
                rows.append([row.get(n, 0.0) for n in var])
                rhs.append(target_change - fixed_contrib(row))
            if not var:
                ok = all(abs(r) < 1e-9 for r in rhs)
                if not ok:
                    return None, ['nothing free']
                d = []
            else:
                d = _least_norm(rows, rhs, w)
                if d is None:
                    return None, ['the ties and limits contradict each other']
            change = dict(frozen)
            change.update({n: d[i] for i, n in enumerate(var)})
            # Parameter ranges.
            worst = None
            for n, dv in change.items():
                lo, hi = ranges.get(n, (None, None))
                new = values[n] + dv
                if lo is not None and new < lo - TOL and n not in frozen:
                    worst = (n, lo - values[n])
                    break
                if hi is not None and new > hi + TOL and n not in frozen:
                    worst = (n, hi - values[n])
                    break
            if worst:
                frozen[worst[0]] = worst[1]
                continue
            # Monitors.
            hit = None
            for row, v, lo, hi, label in mons:
                new = v + sum(row.get(n, 0.0) * dv for n, dv in change.items())
                if lo is not None and new < lo - TOL:
                    hit = (row, lo - v, label)
                    break
                if hi is not None and new > hi + TOL:
                    hit = (row, hi - v, label)
                    break
            if hit:
                if any(r is hit[0] for r, _c in active):
                    return None, [hit[2]]
                active.append((hit[0], hit[1]))
                continue
            return change, []
        return None, ['no solution settled']

    # WHICH parameter takes the move. One, by default the most local (the one fewest
    # things use) — moving a shelf's bottom face changes its thickness, not a little of
    # the thickness and a little of where the shelf sits — with whatever its ties drag
    # along. `prefer` names another candidate, or 'spread' for least squares across
    # all of them. When the chosen one is stopped by a limit the move is REFUSED with
    # how far it can go: it never quietly switches to another way of making it.
    candidates = [n for n in sorted(free, key=lambda n: (float(weights.get(n, 1.0)), n))
                  if abs(tgt.get(n, 0.0)) > 1e-12]
    other_side = set(free) - mine
    if prefer == 'spread' or not candidates:
        allowed = None
        chosen = 'spread'
    else:
        chosen = prefer if prefer in candidates else candidates[0]
        group = {chosen}
        for row in tie_rows:
            if chosen in row:
                group |= {n for n in row if n in free}
        if allow_other:
            group |= other_side
        allowed = sorted(group)

    change, why = attempt(delta, allowed)
    base = {'candidates': candidates, 'chosen': chosen}
    if change is not None:
        base.update({'ok': True, 'changes': {n: d for n, d in change.items() if abs(d) > 1e-9},
                     'achieved': delta, 'limit': None, 'blocked': [], 'reason': ''})
        return base

    # Refused: find how far it CAN go, by bisection on the same solve.
    lo, hi = 0.0, delta
    best = {}
    for _ in range(40):
        mid = (lo + hi) / 2.0
        c, _w = attempt(mid, allowed)
        if c is not None:
            lo, best = mid, c
        else:
            hi = mid
    blocked = _blockers(best, values, ranges, mons, tgt, delta - lo)
    base.update({'ok': False, 'changes': {n: d for n, d in best.items() if abs(d) > 1e-9},
                 'achieved': lo, 'limit': lo, 'blocked': blocked or why,
                 'reason': 'It can move %s of the way.' % ('none' if abs(lo) < 1e-9 else 'only part')})
    return base


def _sub(a, b):
    out = dict(a)
    for n, c in b.items():
        out[n] = out.get(n, 0.0) - c
    return {n: c for n, c in out.items() if abs(c) > 1e-12}


def _blockers(change, values, ranges, mons, tgt, rest):
    """What sits AT its limit in the best partial solution — what stopped the move."""
    out = []
    for n, (lo, hi) in ranges.items():
        if n not in values:
            continue
        v = values[n] + change.get(n, 0.0)
        if (lo is not None and abs(v - lo) < 1e-5) or (hi is not None and abs(v - hi) < 1e-5):
            out.append(n)
    for row, v, lo, hi, label in mons:
        new = v + sum(row.get(n, 0.0) * dv for n, dv in change.items())
        if (lo is not None and abs(new - lo) < 1e-5) or (hi is not None and abs(new - hi) < 1e-5):
            out.append(label)
    return out


def weights_from_usage(usage):
    """{param: how many forms use it} -> weights. A parameter used everywhere (a
    material thickness) is expensive to change; one used once is cheap."""
    return {n: float(max(1, c)) ** 2 for n, c in usage.items()}
