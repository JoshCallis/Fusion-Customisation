"""
Versions — Save and the version list, without Fusion's own dialogs.

  QUICK SAVE  (VersionsSaveCmd — assign it a key in the Shortcuts window)
    On the latest version: saves at once, the version described "Saved <time>" — no
    description dialog. Nothing modified: a "Nothing to save" HUD, no version.
    On an OLDER version: the value window asks "Save as latest?" (Enter / Y = yes,
    Esc / N = no). Yes saves the edits as a new version on top; an unmodified old
    version is promoted (DataFile.promote) and the latest reopened in its place.
    Never saved at all: Fusion's own Save (it has to ask where).

  VERSIONS  (VersionsPanelCmd, and "Versions…" on every Browser menu's tail)
    The value window's versions panel (fusion_value/index.js, "The versions panel"):
      Open      every open document — click to switch to it, × to close it (modified:
                Save / Don't Save / Cancel; never saved: Don't Save / Cancel).
      Versions  the active document's newest 20 — click (or ↑/↓ + Enter) to open one:
                the open document is saved if modified, closed, and that version
                opened in its place.

API FACTS (measured on 260612, 602 versions, 2026-10-09)
  DataFile.versions costs ~1.7 s (a cloud round trip) — on the main thread, so it is
  cached per (lineage, latestVersionNumber) and fetched on the tick AFTER the panel is
  up (`loading`). Its order is the version numbers sorted as STRINGS, descending (99,
  98 … 552, 551 … 1) — sort by versionNumber. item()/versionNumber/description/
  dateCreated are free once fetched; lastUpdatedBy and isMilestone are a cloud call
  per item (~0.3 s each — isMilestone on 20 rows made the list take 10 s) — not read.
  Document.save / close are refused inside command events: everything runs on this
  add-in's custom-event tick, never from the router callback. save() can take seconds
  and pumps the event loop — documents are re-resolved after it (Flush Undo's lesson),
  and `_busy` keeps a re-entered tick out.

Server: fusion_value/server.py (open_versions_session / update_versions, GET
/versions = this add-in's queue). Log: /tmp/fusion_versions.log.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

import adsk.core

app = adsk.core.Application.get()
ui = app.userInterface

VALUE_URL = 'http://127.0.0.1:8734'
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
HUD_FLASH = '/Users/josh/bin/hud_flash'
LOG_PATH = '/tmp/fusion_versions.log'
OWNER = 'Versions'
SAVE_CMD = 'VersionsSaveCmd'
PANEL_CMD = 'VersionsPanelCmd'
EVENT_ID = 'VersionsTick'
LIST_N = 20
_POLL_SECS = 0.15

# One per process, shared by every copy of this module (Flush Undo's reason: a live
# bootstrap can load it twice, and two ticks inside one save is the crash).
_S = sys.__dict__.setdefault('__josh_versions__', {'busy': False, 'stop': None})

_lock = threading.Lock()
_inbox = []               # work for the tick: dicts with 'action'
_handlers = []
_custom_evt = None
_cache = {}               # (lineage id, latest n) -> {'rows': [...], 'files': {n: DataFile}}
_panel = {'open': False, 'compact': False, 'since': 0.0}


def _log(*parts):
    try:
        with open(LOG_PATH, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _hud(kind, message=None):
    try:
        args = [HUD_FLASH, kind] + ([message] if message else [])
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn, not a fork of Fusion
    except Exception:
        pass


def _hotkeys():
    """The shared action registry — Feature Panel.py's hotkeys(), same reasons."""
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


def _post(path, payload=None):
    try:
        req = urllib.request.Request(
            VALUE_URL + path, data=json.dumps(payload or {}).encode(),
            headers={'Content-Type': 'application/json'})
        body = urllib.request.urlopen(req, timeout=2).read()
        return json.loads(body.decode() or '{}')
    except Exception:
        return None


def _get(path, timeout=1):
    try:
        with urllib.request.urlopen(VALUE_URL + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _queue(item):
    with _lock:
        _inbox.append(item)
    try:
        app.fireCustomEvent(EVENT_ID, '')
    except Exception:
        pass


# ── Documents ────────────────────────────────────────────────────────────────

def _doc_key(d):
    """A handle the page can send back. An open version's versionId is unique; an
    unsaved document has no data file, so its name stands in."""
    try:
        if d.dataFile:
            return d.dataFile.versionId
    except Exception:
        pass
    try:
        return 'unsaved:' + d.name
    except Exception:
        return ''


def _find_doc(key):
    for d in app.documents:
        if _doc_key(d) == key:
            return d
    return None


def _find_lineage(lineage):
    """The open document of a lineage, the active one first — re-resolved after every
    save, since save() can leave an earlier Document reference dead."""
    act = app.activeDocument
    try:
        if act and act.dataFile and act.dataFile.id == lineage:
            return act
    except Exception:
        pass
    for d in app.documents:
        try:
            if d.dataFile and d.dataFile.id == lineage:
                return d
        except Exception:
            pass
    return None


def _description():
    return 'Saved ' + time.strftime('%-d %b %H:%M')


def _version_info(d):
    """(this version, the latest) — (0, 0) for a document never saved."""
    try:
        df = d.dataFile
        if df:
            return df.versionNumber, df.latestVersionNumber
    except Exception:
        pass
    return 0, 0


def _save(doc):
    """Save `doc` as a new version. True on success; the document may be a different
    object afterwards (_find_lineage)."""
    try:
        ok = doc.save(_description())
    except Exception as e:
        _log('save raised', e)
        return False
    _log('save', ok)
    return bool(ok)


def _reopen_latest(lineage):
    """Close the open version of `lineage` (unmodified by now) and open its latest."""
    d = _find_lineage(lineage)
    if d is None:
        return False
    try:
        latest = d.dataFile.latestVersion
    except Exception:
        latest = None
    if latest is None:
        return False
    d.close(False)
    app.documents.open(latest, True)
    return True


def _save_as_latest(doc):
    """Make what is open the latest version: save the edits on top, or promote an
    unmodified old version and reopen the latest (which is now a copy of it)."""
    lineage = doc.dataFile.id
    if doc.isModified:
        if not _save(doc):
            _hud('fail', 'Save as Latest: Fusion refused the save.')
            return
        d = _find_lineage(lineage)
        n, latest = _version_info(d) if d else (0, 0)
        if n and n >= latest:
            _hud('text', 'Saved as v%d' % n)
            return
        _log('saved but still not latest', n, latest)
        doc = d
    try:
        if not doc.dataFile.promote():
            _hud('fail', 'Save as Latest: Fusion refused to promote this version.')
            return
        _reopen_latest(lineage)
        _hud('text', 'Saved as latest')
    except Exception as e:
        _log('promote', traceback.format_exc())
        _hud('fail', 'Save as Latest failed: %s' % e)


def _quick_save():
    doc = app.activeDocument
    if doc is None:
        return
    if not doc.isSaved:
        # The first save has to ask where — that dialog is Fusion's.
        cd = ui.commandDefinitions.itemById('SaveDocumentCommand')
        if cd:
            cd.execute()
        return
    n, latest = _version_info(doc)
    if n and latest and n < latest:
        _panel.update(open=True, compact=True, since=time.time())
        _open_panel({'compact': True, 'title': 'Save',
                     'file': {'id': doc.dataFile.id, 'name': doc.dataFile.name,
                              'version': n},
                     'confirm': {'kind': 'saveas', 'text': 'Save as latest?',
                                 'sub': 'This is v%d — the latest is v%d.' % (n, latest)
                                        + ('' if doc.isModified else
                                           ' Nothing is modified: v%d is copied to the top.' % n)}})
        return
    if not doc.isModified:
        _hud('text', 'Nothing to save')
        return
    lineage = doc.dataFile.id
    t0 = time.perf_counter()
    if _save(doc):
        t1 = time.perf_counter()
        d = _find_lineage(lineage)
        t2 = time.perf_counter()
        v = _version_info(d)[0] if d else 0
        t3 = time.perf_counter()
        _log('TEMP timing save %.3fs  find %.3fs  version %.3fs'
             % (t1 - t0, t2 - t1, t3 - t2))
        _hud('text', ('Saved v%d' % v) if d else 'Saved')
    else:
        _hud('fail', 'Save: Fusion refused the save.')


# ── The panel ────────────────────────────────────────────────────────────────

def _open_panel(data):
    got = _post('/versions/open', data)
    if got is not None and not got.get('windowAlive', True):
        _post('/cmd', {'action': 'ensurewindow'})      # Dimension Value heals it


def _docs():
    out = []
    for d in app.documents:
        try:
            if not d.isVisible:
                continue
            n, latest = _version_info(d)
            df = d.dataFile
            out.append({'id': _doc_key(d), 'name': df.name if df else d.name,
                        'version': n, 'latest': latest, 'modified': bool(d.isModified),
                        'active': bool(d.isActive), 'saved': bool(d.isSaved)})
        except Exception:
            pass
    return out


def _file():
    doc = app.activeDocument
    if doc is None:
        return None
    try:
        df = doc.dataFile
        if df is None:
            return {'id': '', 'name': doc.name, 'version': 0, 'saved': False}
        return {'id': df.id, 'name': df.name, 'version': df.versionNumber, 'saved': True}
    except Exception:
        return None


def _versions(df):
    """The newest LIST_N versions of df's lineage, cached — see API FACTS."""
    key = (df.id, df.latestVersionNumber)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    t = time.time()
    vs = df.versions
    files = {}
    for i in range(vs.count):
        v = vs.item(i)
        files[v.versionNumber] = v
    top = sorted(files, reverse=True)[:LIST_N]
    rows = []
    for n in top:
        v = files[n]
        try:
            rows.append({'n': n, 'desc': v.description or '', 'date': v.dateCreated})
        except Exception:
            rows.append({'n': n, 'desc': '', 'date': 0})
    hit = {'rows': rows, 'files': {n: files[n] for n in top}}
    _cache.clear()                  # one lineage at a time is all the panel shows
    _cache[key] = hit
    _log('versions', df.name, vs.count, '%.2fs' % (time.time() - t))
    return hit


def _show_panel():
    """Up at once with the documents; the versions follow on the next tick."""
    _panel.update(open=True, compact=False, since=time.time())
    _open_panel({'file': _file(), 'docs': _docs(), 'list': [], 'loading': True})
    _queue({'action': '_fillversions'})


def _refresh(confirm=None, error=''):
    """Push the lists again — after a close or a switch, the active document is
    another one."""
    if not _panel['open'] or _panel['compact']:
        return
    f = _file()
    data = {'file': f, 'docs': _docs(), 'confirm': confirm, 'error': error,
            'list': [], 'loading': bool(f and f.get('saved'))}
    _post('/versions/update', data)
    if data['loading']:
        _queue({'action': '_fillversions'})


def _fill_versions():
    if not _panel['open'] or _panel['compact']:
        return
    doc = app.activeDocument
    try:
        df = doc.dataFile if doc else None
    except Exception:
        df = None
    if df is None:
        _post('/versions/update', {'loading': False, 'list': []})
        return
    try:
        rows = _versions(df)['rows']
        _post('/versions/update', {'loading': False, 'list': rows})
    except Exception as e:
        _log('versions', traceback.format_exc())
        _post('/versions/update', {'loading': False, 'list': [],
                                   'error': 'Could not read the versions: %s' % e})


def _open_version(cmd):
    """Save (if modified) and close what is open, then open the picked version."""
    doc = app.activeDocument
    lineage = cmd.get('file') or ''
    try:
        if doc is None or doc.dataFile is None or doc.dataFile.id != lineage:
            _hud('fail', 'Versions: the active document changed — nothing opened.')
            return
    except Exception:
        return
    n = cmd.get('version')
    hit = next(iter(_cache.values()), None)
    target = hit['files'].get(n) if hit else None
    if target is None:
        try:
            target = _versions(doc.dataFile)['files'].get(n)
        except Exception:
            target = None
    if target is None:
        _hud('fail', 'Versions: v%s is not in the list any more.' % n)
        return
    if doc.isModified and not _save(doc):
        _hud('fail', 'Versions: could not save the open document, so it was not closed.')
        return
    doc = _find_lineage(lineage)
    try:
        if doc is not None:
            doc.close(False)
    except Exception as e:
        if _find_lineage(lineage) is not None:
            _hud('fail', 'Versions: could not close the open document.\n\n%s' % e)
            return
    try:
        app.documents.open(target, True)
        _log('opened v%s' % n)
    except Exception as e:
        _log('open', traceback.format_exc())
        _hud('fail', 'Versions: could not open v%s.\n\n%s' % (n, e))


def _close_doc(cmd):
    d = _find_doc(cmd.get('doc') or '')
    if d is None:
        _refresh()
        return
    if d.isModified:
        name = d.dataFile.name if d.dataFile else d.name
        _refresh(confirm={'kind': 'close', 'doc': cmd.get('doc'),
                          'text': 'Save changes to %s?' % name,
                          'sub': '' if d.isSaved else 'It has never been saved — '
                                 'use Save to give it a place first.',
                          'unsaved': not d.isSaved})
        return
    d.close(False)
    _refresh()


def _answer(cmd):
    kind, answer = cmd.get('kind'), cmd.get('answer')
    if kind == 'saveas':
        _panel.update(open=False)
        if answer != 'yes':
            return
        doc = app.activeDocument
        try:
            if doc is None or doc.dataFile is None or doc.dataFile.id != cmd.get('file'):
                return
        except Exception:
            return
        _save_as_latest(doc)
        return
    if kind == 'close':
        d = _find_doc(cmd.get('doc') or '')
        if d is None or answer == 'cancel':
            _refresh()
            return
        if answer == 'save':
            lineage = d.dataFile.id if d.dataFile else ''
            if not _save(d):
                _refresh(error='Could not save it, so it was not closed.')
                return
            d = _find_lineage(lineage)
        if d is not None:
            d.close(False)
        _refresh()


def _activate(cmd):
    d = _find_doc(cmd.get('doc') or '')
    if d is not None:
        d.activate()
    _refresh()


def _run(cmd):
    a = cmd.get('action')
    if a == '_save':
        _quick_save()
    elif a in ('_panel', 'versionsshow'):
        _show_panel()
    elif a == '_fillversions':
        _fill_versions()
    elif a == 'versionsopen':
        _panel.update(open=False)
        _open_version(cmd)
    elif a == 'versionsclose':
        _close_doc(cmd)
    elif a == 'versionsactivate':
        _activate(cmd)
    elif a == 'versionsanswer':
        _answer(cmd)
    elif a == 'versionscancel':
        _panel.update(open=False)


# ── The tick ─────────────────────────────────────────────────────────────────

class _TickHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        if _S['busy']:
            return                  # re-entered from inside a save; the poller re-fires
        _S['busy'] = True
        try:
            while True:
                with _lock:
                    if not _inbox:
                        break
                    cmd = _inbox.pop(0)
                try:
                    _run(cmd)
                except Exception:
                    _log('ERROR', cmd.get('action'), traceback.format_exc())
        finally:
            _S['busy'] = False


def _poller(stop):
    while not stop.is_set():
        got = _get('/versions')
        cmds = (got or {}).get('commands') or []
        # Not showing (cancelled by the page, or replaced) — after a grace for the
        # open itself to land.
        if (got is not None and _panel['open'] and not got.get('showing')
                and time.time() - _panel['since'] > 1.0):
            _panel.update(open=False)          # replaced, or cancelled by the page
        with _lock:
            if cmds:
                _inbox.extend(cmds)
            pending = bool(_inbox)
        if pending:
            try:
                app.fireCustomEvent(EVENT_ID, '')
            except Exception:
                pass
        stop.wait(_POLL_SECS)


# ── Lifecycle ────────────────────────────────────────────────────────────────

def run(context):
    global _custom_evt
    try:
        if _S.get('stop') is not None:
            _S['stop'].set()                   # a previous copy's poller
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = _TickHandler()
        _custom_evt.add(h)
        _handlers.append(h)

        stop = threading.Event()
        _S['stop'] = stop
        threading.Thread(target=_poller, args=(stop,), daemon=True).start()

        hk = _hotkeys()
        hk.declare(SAVE_CMD, 'Quick Save',
                   'Save without the description dialog. On an older version, asks '
                   '"save as latest?" in the value window instead.',
                   owner=OWNER, run=lambda: _queue({'action': '_save'}))
        hk.declare(PANEL_CMD, 'Versions',
                   'The open documents (close any, with a save prompt) and the active '
                   'document\'s last 20 versions (open one: saves and closes the current).',
                   owner=OWNER, run=lambda: _queue({'action': '_panel'}))
        hk.attach()
        _log('run')
    except Exception:
        _log('run failed', traceback.format_exc())


def stop(context):
    global _custom_evt
    try:
        if _S.get('stop') is not None:
            _S['stop'].set()
            _S['stop'] = None
        if _custom_evt is not None:
            for h in _handlers:
                try:
                    _custom_evt.remove(h)
                except Exception:
                    pass
        _custom_evt = None
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        del _handlers[:]
        try:
            _hotkeys().withdraw(OWNER)
        except Exception:
            pass
    except Exception:
        _log('stop failed', traceback.format_exc())
