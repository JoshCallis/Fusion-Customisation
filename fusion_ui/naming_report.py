#!/usr/bin/env python3
"""
naming_report — what the variable engine makes of a real parameter list.

    python3 naming_report.py                 # live, from the params server
    python3 naming_report.py params.json     # a saved /params snapshot
    python3 naming_report.py names.txt       # one name per line
    python3 naming_report.py ... -o out.md   # write markdown instead of printing

Run it whenever the vocabulary in variables.json changes, or the naming convention
does. It is a diagnostic, not a build step: nothing depends on its output. What it is
FOR is seeing where the convention does not reach — the families that collapse the way
you meant, the strips that read a word as situational when it was part of a feature's
identity, and the words that are already acting like variables without being in the
list yet.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import naming  # noqa: E402

PARAMS_URL = 'http://127.0.0.1:8731/params'


def load_names(source):
    """Parameter names from a /params snapshot, a plain list, or the live server."""
    if source is None:
        with urllib.request.urlopen(PARAMS_URL, timeout=3) as fh:
            return [p['name'] for p in json.load(fh)['params']]
    with open(source) as fh:
        text = fh.read()
    try:
        data = json.loads(text)
    except ValueError:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    if isinstance(data, dict):
        data = data.get('params', [])
    return [p['name'] if isinstance(p, dict) else str(p) for p in data]


def tag_str(a):
    if not a.tags:
        return '—'
    return ' '.join('%s=%s' % (k, v) for k, v in sorted(a.tags.items()))


def situation_str(a):
    if not a.axis:
        return '—'
    return a.axis + ('/' + a.end if a.end else '')


def report(names, vocab):
    L = []
    w = L.append
    fams = naming.families(names, vocab)

    multi = {s: m for s, m in fams.items() if len(m) > 1}
    singles = {s: m for s, m in fams.items() if len(m) == 1}
    tagged_singles = {s: m for s, m in singles.items() if m[0].is_tagged}
    plain = {s: m for s, m in singles.items() if not m[0].is_tagged}

    w('# Naming report')
    w('')
    w('%d parameters, %d families. %d families have more than one member (%d names), '
      '%d names are tagged but stand alone, %d carry no variables at all.'
      % (len(names), len(fams), len(multi), sum(len(m) for m in multi.values()),
         len(tagged_singles), len(plain)))
    w('')

    w('## Vocabulary')
    w('')
    for dim in vocab.dimensions:
        note = '  *(symmetric axes only)*' if dim.get('symmetricOnly') else ''
        w('- **%s** — %s%s' % (dim.get('id'), ', '.join(
            v.get('word') for v in dim.get('values', [])), note))
    w('')

    w('## Families that collapse')
    w('')
    w('These are the wins: one name to type, the situation picks the member.')
    w('')
    for stem in sorted(multi, key=lambda s: (-len(multi[s]), s)):
        members = multi[stem]
        w('### `%s`  — %d members' % (stem, len(members)))
        w('')
        w('| parameter | tags | situation |')
        w('|---|---|---|')
        for a in sorted(members, key=lambda x: x.name):
            w('| `%s` | %s | %s |' % (a.name, tag_str(a), situation_str(a)))
        w('')
        clashes = naming.ambiguous(members)
        if clashes:
            for key, group in sorted(clashes.items()):
                w('> **Ambiguous** at %s: %s — no situation can choose between these.'
                  % (situation_str(group[0]),
                     ', '.join('`%s`' % a.name for a in group)))
            w('')

    w('## Every strip, for eyeballing')
    w('')
    w('Each of these names had a word read as situational. A word that is really part '
      'of a feature\'s identity rather than its situation will look wrong here — that '
      'is the thing to scan for.')
    w('')
    w('| parameter | stem | stripped | situation | measured from |')
    w('|---|---|---|---|---|')
    for a in sorted((naming.analyse(n, vocab) for n in names), key=lambda x: x.name):
        if not a.is_tagged:
            continue
        w('| `%s` | `%s` | %s | %s | %s |'
          % (a.name, a.stem,
             ', '.join('**%s**' % v for _, v, _ in a.hits), situation_str(a),
             'the origin' if a.frame == 'origin' else 'the %s' % a.frame.lower()))
    w('')

    w('## Measured from a bound')
    w('')
    w('A parameter whose measurement type is one of %s takes the direction beside it as '
      'the thing it is measured FROM, rather than as a description of which end it '
      'concerns. Everything else is measured from the origin. This is what lets a '
      'dimension starting at one face stop offering values measured from another.'
      % ', '.join('`%s`' % t for t in sorted(vocab.relative_types)))
    w('')
    framed = [a for a in (naming.analyse(n, vocab) for n in names)
              if a.frame != 'origin']
    if framed:
        for a in sorted(framed, key=lambda x: x.name):
            w('- `%s` — from the %s' % (a.name, a.frame.lower()))
    else:
        w('None yet: every parameter here is measured from the origin.')
    w('')

    conflicts = [a for a in (naming.analyse(n, vocab) for n in names) if a.conflicts]
    w('## Conflicts')
    w('')
    if conflicts:
        for a in sorted(conflicts, key=lambda x: x.name):
            for c in a.conflicts:
                w('- `%s` — %s' % (a.name, c))
    else:
        w('None. No name contradicts itself.')
    w('')

    w('## Stems that collide with a real parameter name')
    w('')
    w('A family whose stem is ITSELF a parameter. The stem cannot be offered for these '
      'or typing that name in full would silently resolve to a different parameter, so '
      'the family is unreachable by stem until one of them is renamed.')
    w('')
    collisions = naming.stem_collisions(names, vocab)
    if collisions:
        for g in collisions:
            w('- `%s` is a parameter AND the stem of %s'
              % (g['stem'], ', '.join('`%s`' % n for n in g['members'])))
    else:
        w('None.')
    w('')

    w('## Words already acting like variables')
    w('')
    w('Found structurally: dropping one more word from these names makes them coincide, '
      'which means those words are filling the same slot. Candidates for the vocabulary.')
    w('')
    cands = naming.candidate_variables(names, vocab)
    likely = [g for g in cands if g['likely']]
    rest = [g for g in cands if not g['likely']]

    w('### Likely')
    w('')
    if likely:
        w('Every word in these groups is a qualifier rather than a feature name.')
        w('')
        for g in likely:
            w('- **%s** — around `%s`' % (' | '.join(g['words']), g['reduced']))
            for n in g['names']:
                w('  - `%s`' % n)
    else:
        w('None.')
    w('')
    w('### Raw structural pairs')
    w('')
    w('Everything else the search turned up. Mostly pairs of feature names, which is '
      'noise — scan for anything adjectival that the qualifier list has not heard of.')
    w('')
    for g in rest:
        w('- %s — around `%s` (%d names)'
          % (' | '.join(g['words']), g['reduced'], len(g['names'])))
    w('')

    w('## Carrying no variables')
    w('')
    w('Untouched by the engine, offered in every situation. Correct for genuinely '
      'general parameters; a sign of a missing convention if one of these is really '
      'situational.')
    w('')
    w(', '.join('`%s`' % m[0].name for _, m in sorted(plain.items())))
    w('')

    return '\n'.join(L)


def main():
    args = [a for a in sys.argv[1:]]
    out_path = None
    if '-o' in args:
        i = args.index('-o')
        out_path = args[i + 1]
        del args[i:i + 2]
    source = args[0] if args else None

    names = load_names(source)
    vocab = naming.load_vocabulary()
    text = report(names, vocab)

    if out_path:
        with open(out_path, 'w') as fh:
            fh.write(text)
        print('wrote %s (%d names)' % (out_path, len(names)))
    else:
        print(text)


if __name__ == '__main__':
    main()
