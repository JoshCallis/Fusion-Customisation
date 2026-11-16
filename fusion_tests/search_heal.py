"""Offline exercise of the command-search palette's open path.

The bug this pins down: Shortcuts.py arms the palette by POSTing /search/open to the
fusion_value server — but that server lives in the DIMENSION VALUE add-in, so it
answers 200 whether or not the FusionValue.app window process is healthy. A 200
therefore says "session armed", never "palette rendered", and the heal that used to
hang off the POST failing was unreachable. A window that was up but no longer polling
swallowed every press of the search key in silence, with the router, the keymap, the
server and the page all testing fine.

So the liveness answer rides back on the reply (`windowAlive`, as it already does on
/extrude), and the add-in asks Dimension Value's tick for a heal when it comes back
false. This checks that branch, and that a reply which cannot carry the field is read
as healthy rather than provoking a pkill on every keypress.

Run: python3 fusion_tests/search_heal.py
"""
import json, os, sys, threading, types
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer

ADDIN = ('/Users/josh/Library/Application Support/Autodesk/Autodesk Fusion 360'
         '/API/AddIns/Shortcuts/Shortcuts.py')

# ── Stub adsk, exactly as far as Shortcuts.py's import needs ───────────────────
adsk = types.ModuleType('adsk')
core = types.ModuleType('adsk.core')
fus  = types.ModuleType('adsk.fusion')
class _H: pass
core.ApplicationCommandEventHandler = _H
core.CommandCreatedEventHandler = _H
core.CommandEventHandler = _H
core.CustomEventHandler = _H
class _App:
    @staticmethod
    def get():
        return types.SimpleNamespace(userInterface=types.SimpleNamespace())
core.Application = _App
adsk.core = core
adsk.fusion = fus
sys.modules['adsk'] = adsk
sys.modules['adsk.core'] = core
sys.modules['adsk.fusion'] = fus

spec = importlib.util.spec_from_file_location('fusion_shortcuts_addin', ADDIN)
sc = importlib.util.module_from_spec(spec)
sys.modules['fusion_shortcuts_addin'] = sc
spec.loader.exec_module(sc)

# ── A stand-in for the fusion_value server ────────────────────────────────────
HITS  = []
REPLY = [b'{"ok": true, "windowAlive": true}']    # None -> answer 500

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        HITS.append(self.path)
        body = REPLY[0]
        if body is None:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

srv = HTTPServer(('127.0.0.1', 0), _Handler)
sc.VALUE_URL = 'http://127.0.0.1:%d' % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

FAILED = []

def case(label, reply, expect):
    HITS.clear()
    REPLY[0] = reply
    sc._open_search()
    got = list(HITS)
    ok = got == expect
    if not ok:
        FAILED.append(label)
    print('%s  %-34s %s' % ('PASS' if ok else 'FAIL', label, got))

ARM  = ['/search/open']
HEAL = ['/search/open', '/cmd']

# The happy path is ONE round trip: arming is all a healthy window needs.
case('window polling',        b'{"ok": true, "windowAlive": true}',  ARM)
# The bug. Arm first and heal second, on purpose: the relaunched page adopts an open
# session on its first poll, so the keypress that found the window wedged still lands.
case('window up, not polling', b'{"ok": true, "windowAlive": false}', HEAL)
# A reply that cannot answer the question is read as healthy — the pre-fix behaviour.
# Reading it as dead would pkill and relaunch the window on every single keypress.
case('reply without the field', b'{"ok": true}',                      ARM)
case('empty reply body',        b'',                                  ARM)
# Not an answer at all: the server is confused, so treat the window as suspect.
case('unparseable reply',       b'<html>',                            HEAL)
case('server error',            None,                                 HEAL)

srv.shutdown()
if FAILED:
    print('\nFAILED: %s' % ', '.join(FAILED))
    sys.exit(1)
print('\nall ok')
