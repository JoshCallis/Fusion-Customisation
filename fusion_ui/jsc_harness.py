#!/usr/bin/env python3
"""
fusion_ui/jsc_harness.py — run a window's REAL page script headlessly, under jsc.

The companion windows cannot be checked by looking at them: screencapture returns
solid black for these WKWebView windows, and they expose themselves to accessibility
inconsistently. So each window's test/run.py concatenates one program for jsc —

    dom_shim.js       just enough document/window for the page script to load
    <engines>         the shared fusion_ui/ scripts, IN THE ORDER index.html loads them
    index.js          the page's own script, verbatim
    <prelude>         data the cases need (vocabularies, a parameter snapshot…)
    cases.js          the assertions

— so the page's top-level `let`s share one scope with the cases, which drive its
functions directly. Each run.py used to carry its own copy of this, with its own
hand-kept list of engines; a page that picked up a new shared script (keys.js) then
failed its tests for no reason of its own. The engine list is now read off the
page's own <script> tags, so the harness loads exactly what the window does.

LOADED BY PATH like the other fusion_ui modules (see webwin.py's header):

    _spec = importlib.util.spec_from_file_location("jsc_harness", <this file>)
    harness = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(harness)
    sys.exit(harness.run(WINDOW, prelude=[...], cases=...))

The one thing this canNOT tell you is whether the window is running the file on
disk: the WKWebView loads its sources once at launch. Restart the app after editing.
"""

import io
import os
import re
import subprocess
import sys
import tempfile

UI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(UI)
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/'
       'Helpers/jsc')
# The generic DOM shim is window-agnostic (see its own header); every window uses it.
DOM_SHIM = os.path.join(REPO, 'fusion_params', 'test', 'dom_shim.js')

# Shared scripts a page loads that the harness leaves out: they bind to the live
# document or poll a server at load, and nothing any case asserts depends on them.
DOM_BOUND = {'appearance.js', 'browser_sel.js', 'capitalise.js', 'fingers.js', 'fit_popups.js'}


def read(path):
    with io.open(path, encoding='utf-8') as fh:
        return fh.read()


def page_engines(window):
    """The shared fusion_ui/ scripts `window`'s index.html loads, in its order."""
    html = read(os.path.join(window, 'index.html'))
    out = []
    for src in re.findall(r'<script\s+src="/([^"]+)"', html):
        if src == 'index.js' or src in DOM_BOUND:
            continue
        path = os.path.join(UI, src)
        if not os.path.exists(path):
            raise SystemExit('%s loads /%s, which is not in fusion_ui/' % (window, src))
        out.append(path)
    return out


def vocab(var, name):
    """`var VAR = <fusion_ui/name>;` — a JSON registry handed to the cases."""
    return 'var %s = %s;' % (var, read(os.path.join(UI, name)))


def run(window, prelude=(), cases=None, shim=DOM_SHIM, ok_note=''):
    """Build and run the program; print jsc's output and a verdict. Returns the exit code."""
    here = os.path.join(window, 'test')
    program = '\n'.join(
        [read(shim)]
        + [read(p) for p in page_engines(window)]
        + [read(os.path.join(window, 'index.js'))]
        + list(prelude)
        + [read(cases or os.path.join(here, 'cases.js'))])

    tmp = tempfile.mkdtemp(prefix='%s_test_' % os.path.basename(window))
    path = os.path.join(tmp, 'harness.js')
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write(program)

    if not os.path.exists(JSC):
        raise SystemExit('jsc not found at %s' % JSC)
    res = subprocess.run([JSC, path], capture_output=True, text=True, timeout=60)
    sys.stdout.write(res.stdout)
    if res.stderr.strip():
        sys.stderr.write(res.stderr)

    failed = 'FAIL' in res.stdout or res.returncode != 0
    print('')
    print('FAILURES PRESENT' if failed else 'all cases passed' + ok_note)
    return 1 if failed else 0
