"""
Dev Probe — run a file inside Fusion, from outside it.

DEVELOPMENT ONLY. Not run on startup: start it from Scripts and Add-Ins when there is
something about a live Fusion to find out, stop it when done.

    ~/.fusion_probe/in.py    written by you (or Claude); run whenever its mtime changes
    ~/.fusion_probe/out.txt  what it printed, then any traceback

The file runs on Fusion's MAIN thread, via a custom event, with `app`, `ui`, `adsk` and
`design` in scope. A background thread only watches the mtime and fires the event —
nothing on it touches Fusion (see fusion_bg_thread_qt_crash).

The directory is created 0700 in your home, not /tmp: anything that can write in.py can
run code in Fusion as you.
"""

import contextlib
import io
import os
import threading
import traceback

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface

DIR = os.path.expanduser('~/.fusion_probe')
IN = os.path.join(DIR, 'in.py')
OUT = os.path.join(DIR, 'out.txt')
EVENT_ID = 'DevProbeRun'
POLL_SECS = 0.25

_evt = None
_handler = None
_stop = None
_seen = 0.0


def _run_probe():
    buf = io.StringIO()
    try:
        with open(IN, 'r', encoding='utf-8') as f:
            src = f.read()
        scope = {'app': app, 'ui': ui, 'adsk': adsk,
                 'design': adsk.fusion.Design.cast(app.activeProduct),
                 '__name__': '__probe__'}
        with contextlib.redirect_stdout(buf):
            exec(compile(src, IN, 'exec'), scope)
    except Exception:
        buf.write('\n' + traceback.format_exc())
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(buf.getvalue())
    os.replace(tmp, OUT)


class _Run(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            _run_probe()
        except Exception:
            pass


def _watch(stop):
    global _seen
    while not stop.is_set():
        try:
            m = os.path.getmtime(IN)
            if m != _seen:
                _seen = m
                app.fireCustomEvent(EVENT_ID, '')
        except OSError:
            pass
        stop.wait(POLL_SECS)


def run(context):
    global _evt, _handler, _stop, _seen
    try:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        os.chmod(DIR, 0o700)
        try:
            _seen = os.path.getmtime(IN)     # an old probe is not re-run on start
        except OSError:
            _seen = 0.0
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _evt = app.registerCustomEvent(EVENT_ID)
        _handler = _Run()
        _evt.add(_handler)
        _stop = threading.Event()
        threading.Thread(target=_watch, args=(_stop,), daemon=True).start()
    except Exception:
        ui.messageBox('Dev Probe run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _evt, _handler, _stop
    try:
        if _stop is not None:
            _stop.set()
        if _evt is not None and _handler is not None:
            _evt.remove(_handler)
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _evt = _handler = _stop = None
