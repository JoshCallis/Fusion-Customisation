"""Offline check: a companion window's server restarts ABOVE the count it left behind.

The bug this pins down (2026-09-30): Command Search and Extrude "stopped working" —
every press armed a session on the fusion_value server, the FusionValue window was
up and polling, and nothing ever appeared. When Fusion quits or crashes, the
Dimension Value add-in's stop() never kills FusionValue.app, so the window outlives
the server. The next Fusion's server counted `seq` from 0 again, and the page only
opens a session on `seq > lastSeq` — so it ignored every session until the new count
overtook the old one. windowAlive stayed true throughout, so no heal fired either.

The servers now seed every edge-detect counter from the clock (webwin.seq_base), and
the page re-adopts a counter that goes backwards. This checks the server half: a
fresh module (what a new Fusion loads) starts above everything the last one reached.

Run: python3 fusion_tests/window_seq_restart.py
"""
import importlib.util, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fails = []


def check(label, ok):
    print(('  PASS  ' if ok else '  FAIL  ') + label)
    if not ok:
        fails.append(label)


# ── fusion_value: seq and every other counter the page edge-detects ───────────
old = fresh('fusion_value/server.py', 'fv_old')
for _ in range(50):                      # a busy session: 50 dialogs opened
    old.open_search_session('viewport')
old.close_session()
# A restart takes seconds (Fusion relaunching); 0.1 s is already far less, and the
# seed only needs fewer sessions than milliseconds elapsed.
time.sleep(0.1)
new = fresh('fusion_value/server.py', 'fv_new')   # Fusion restarted

for key in ('seq', 'errSeq', 'madeSeq', 'insertSeq', 'pickSeq'):
    check('fusion_value %s restarts above the old server (%d > %d)'
          % (key, new._state[key], old._state[key]),
          new._state[key] > old._state[key])
check('fusion_value seq stays exact in a JS number',
      new._state['seq'] < 2 ** 53)

# ── Browser / Timeline uiSeq ───────────────────────────────────────────────────
for rel in ('fusion_browser/server.py', 'fusion_timeline/server.py'):
    a = fresh(rel, rel.replace('/', '_') + '_a')
    a.push_ui_action('X')
    time.sleep(0.01)
    b = fresh(rel, rel.replace('/', '_') + '_b')
    check('%s uiSeq restarts above the old server' % rel.split('/')[0],
          b._state['uiSeq'] > a._state['uiSeq'] and b._ui_seq == b._state['uiSeq'])

print('\n' + ('all passed' if not fails else 'FAILURES: %d' % len(fails)))
sys.exit(1 if fails else 0)
