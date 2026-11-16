"""
Reload Recent AddIns
────────────────────
One button (bind it to a hotkey) that reloads every add-in edited since it was
last loaded, plus the add-in whose source was edited most recently (even if that
one is already current).  Automates the Shift+S → Stop → Run round-trip for
exactly the add-ins you touched, however long ago.

How it works
  • Scan every *.py under the AddIns tree for its mtime.
  • Each reload stamps the live module with when it was loaded (LOADED_AT); a
    module with no stamp counts as loaded when Fusion launched.  A file newer
    than its add-in's stamp makes that add-in stale.  (It used to be "edited
    within 5 min of the newest edit" — an add-in edited earlier than the last
    burst of edits elsewhere was silently never reloaded.)
  • Map each edited file to the add-in that owns it — the nearest ancestor folder
    holding a .manifest.  A shared file that lives at the AddIns ROOT and is not
    itself an add-in (e.g. "Visibility Snapshot.py", "Shared State.py") maps to
    EVERY add-in whose entry script references it, so editing the engine reloads
    all of its consumers in one go.
  • Reload each target: find its live module (matched by __file__), call stop(),
    re-exec the entry script from disk, call run().  This mirrors a manual
    Stop/Run, so each add-in's reload-safe run()/stop() does the real work and
    its own re-exec of any shared module picks up the latest engine too.

Safeguards
  • Every file to be reloaded is compile-checked first; if any fails to compile
    nothing is reloaded and the syntax error is reported — a half-saved edit can
    never tear a working add-in down.
  • This add-in never reloads itself (you can't safely re-exec the module you're
    currently running inside).  If you edit THIS file, reload it once via Shift+S.
"""

import adsk.core, adsk.fusion, traceback, os, sys, time, ctypes

CMD_ID   = 'ReloadRecentAddInsCmd'
CMD_NAME = 'Reload Recent AddIns'
CMD_DESC = ('Reload every add-in edited since it was last loaded, and the most '
            'recently edited one.')

LOADED_AT = '_reload_recent_loaded_at'   # stamped on each module this reloads

_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))   # this add-in's folder
ADDINS_ROOT  = os.path.dirname(_THIS_DIR)                    # the AddIns tree root
SELF_DIR     = os.path.realpath(_THIS_DIR)

app = None
ui = None
handlers = []


# ── filesystem helpers ────────────────────────────────────────────────────────

def _all_py_files():
    out = []
    for dirpath, _dirnames, filenames in os.walk(ADDINS_ROOT):
        if '__pycache__' in dirpath:
            continue
        for fn in filenames:
            if fn.endswith('.py'):
                out.append(os.path.join(dirpath, fn))
    return out


def _has_manifest(dirpath):
    try:
        return any(f.endswith('.manifest') for f in os.listdir(dirpath))
    except Exception:
        return False


def _addin_dir_for(path):
    """The add-in folder that owns `path` — the nearest ancestor with a
    .manifest.  Returns None when the file is a shared file at the AddIns root
    (not inside any add-in)."""
    root = os.path.realpath(ADDINS_ROOT)
    d = os.path.dirname(os.path.realpath(path))
    while True:
        if d == root:
            return None
        if _has_manifest(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _entry_script(addin_dir):
    """Entry .py of an add-in folder = "<foldername>.py" (Fusion convention)."""
    cand = os.path.join(addin_dir, os.path.basename(addin_dir) + '.py')
    return cand if os.path.exists(cand) else None


def _addins_referencing(filename):
    """Every add-in folder whose entry script references `filename` (basename
    match).  Maps a shared root module to the add-ins that exec/import it."""
    base = os.path.basename(filename)
    hits = []
    for dirpath, _dirnames, filenames in os.walk(ADDINS_ROOT):
        if '__pycache__' in dirpath:
            continue
        if not any(f.endswith('.manifest') for f in filenames):
            continue
        entry = _entry_script(dirpath)
        if not entry:
            continue
        try:
            with open(entry, 'r', encoding='utf-8') as f:
                if base in f.read():
                    hits.append(dirpath)
        except Exception:
            pass
    return hits


def _fusion_started():
    """When this Fusion process launched — what an add-in this never reloaded counts
    as loaded at.  libproc's proc_bsdinfo (pbi_start_tvsec at byte 120 of 136), so
    no subprocess: forking inside Fusion stalls the GIL ~50 ms."""
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        buf = ctypes.create_string_buffer(136)
        if lib.proc_pidinfo(os.getpid(), 3, ctypes.c_uint64(0), buf, 136) == 136:
            return int.from_bytes(buf.raw[120:128], 'little')
    except Exception:
        pass
    return 0.0   # unknown → every edited add-in counts as stale


# ── reload mechanics ──────────────────────────────────────────────────────────

def _find_module(entry_path):
    """The live module whose __file__ is `entry_path`, or None if not loaded."""
    target = os.path.realpath(entry_path)
    for _name, mod in list(sys.modules.items()):
        f = getattr(mod, '__file__', None)
        if not f:
            continue
        try:
            if os.path.realpath(f) == target:
                return mod
        except Exception:
            pass
    return None


def _reload_addin(addin_dir, ctx):
    """Reload one add-in.  Returns (status, detail) with status in
    {'reloaded', 'skipped', 'error'}."""
    name  = os.path.basename(addin_dir)
    entry = _entry_script(addin_dir)
    if not entry:
        return 'skipped', name + ' (no entry script)'
    mod = _find_module(entry)
    if mod is None:
        return 'skipped', name + ' (not currently loaded)'
    try:
        try:
            mod.stop(ctx)
        except Exception:
            pass  # tear-down is best-effort; the fresh run() re-establishes state
        # Stamped before the source is read, so an edit saved mid-reload still
        # reads as newer next time.
        mod.__dict__[LOADED_AT] = time.time()
        with open(entry, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(compile(code, entry, 'exec'), mod.__dict__)
        mod.run(ctx)
        return 'reloaded', name
    except Exception:
        mod.__dict__.pop(LOADED_AT, None)   # still stale: the next press retries it
        return 'error', name + ':\n' + traceback.format_exc()


# ── command ───────────────────────────────────────────────────────────────────

class _OnExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            files = _all_py_files()
            if not files:
                return  # nothing to reload → stay silent

            mtimes = {f: os.path.getmtime(f) for f in files}
            newest = max(mtimes, key=mtimes.get)

            # 1. targets = each add-in with a file newer than its last load (a shared
            # file at the root counts for every add-in that references it), plus the
            # owners of the newest edit even when they are current.  A file older
            # than Fusion's launch can't be newer than any load, so only newer ones
            # are mapped (_addins_referencing reads every entry script).
            started = _fusion_started()
            loads = {}
            def loaded_at(d):
                if d not in loads:
                    e = _entry_script(d)
                    mod = _find_module(e) if e else None
                    loads[d] = (getattr(mod, LOADED_AT, started)
                                if mod is not None else None)
                return loads[d]

            targets, window, shared = [], [], []
            for f in sorted(files, key=lambda x: mtimes[x], reverse=True):
                if f != newest and mtimes[f] <= started:
                    break
                own = _addin_dir_for(f)
                hit = False
                for d in [own] if own is not None else _addins_referencing(f):
                    rd = os.path.realpath(d)
                    t = loaded_at(rd)
                    if rd == SELF_DIR or t is None:
                        continue   # never itself; never one that isn't running
                    if f == newest or mtimes[f] > t:
                        hit = True
                        if rd not in targets:
                            targets.append(rd)
                if hit:
                    window.append(f)
                    if own is None:
                        shared.append(f)

            if not targets:
                return  # nothing edited since it was loaded → stay silent

            # 2. compile-check every edited file before touching anything
            errors = []
            for f in window:
                try:
                    with open(f, 'r', encoding='utf-8') as fh:
                        compile(fh.read(), f, 'exec')
                except SyntaxError as e:
                    errors.append('{}: {}'.format(os.path.relpath(f, ADDINS_ROOT), e))
            if errors:
                ui.messageBox(
                    'Reload aborted — these edited files don\'t compile:\n\n'
                    + '\n'.join(errors), CMD_NAME)
                return

            # 3. reload
            ctx = {'IsApplicationStartup': False}
            reloaded, skipped, failed = [], [], []
            for d in targets:
                status, detail = _reload_addin(d, ctx)
                {'reloaded': reloaded, 'skipped': skipped,
                 'error': failed}[status].append(detail)

            # 4. report — success is silent; only a genuine failure gets a
            # dialog, so a broken reload is never swallowed.
            if not failed:
                return
            lines = ['Newest edit: ' + os.path.relpath(newest, ADDINS_ROOT)]
            if shared:
                lines.append('Edited shared module(s) → reloaded consumers: '
                             + ', '.join(os.path.basename(s) for s in shared))
            lines.append('')
            lines.append('Reloaded ({}): {}'.format(
                len(reloaded), ', '.join(reloaded) if reloaded else '—'))
            if skipped:
                lines.append('Skipped: ' + ', '.join(skipped))
            lines.append('\nFailed:\n' + '\n\n'.join(failed))
            ui.messageBox('\n'.join(lines), CMD_NAME)
        except Exception:
            if ui:
                ui.messageBox('Reload Recent AddIns – Execute error:\n'
                              + traceback.format_exc(), CMD_NAME)


class _OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            on_exec = _OnExecute()
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
        except Exception:
            if ui:
                ui.messageBox('Reload Recent AddIns – CommandCreated error:\n'
                              + traceback.format_exc(), CMD_NAME)


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        existing = ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()

        cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)

        on_created = _OnCommandCreated()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        if ui:
            ui.messageBox('Reload Recent AddIns – run error:\n'
                          + traceback.format_exc(), CMD_NAME)


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        handlers.clear()
    except Exception:
        if ui:
            ui.messageBox('Reload Recent AddIns – stop error:\n'
                          + traceback.format_exc(), CMD_NAME)
