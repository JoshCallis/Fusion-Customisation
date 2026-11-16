#!/usr/bin/env python3
"""
Reads Fusion's keymap off disk — the ONE reader every window server and the
Shortcuts add-in use.

It used to be copied into three server.py files, and the copies drifted: two
globbed every options file and took the first hit (Fusion keeps a stale copy at
the non-user-scoped path, so that could be last month's keymap), and one still
forwarded a Simulation command id from a window that only sits beside Design.
`hotkey_file()` and `design_command()` below are the fixed versions of both.

The Shortcuts server uses this as the fallback when the add-in isn't connected
(window opened before Fusion, or the server run standalone). While the add-in IS
connected the live table from Fusion's HotKey.List text command is used instead,
since it reflects Fusion's in-memory state rather than whatever was last flushed
to disk. Both have the entries() shape, so the helpers take either.

Fusion stores the whole keymap as an HTML-escaped JSON blob inside one attribute
of a UTF-16 XML options file:

    <HotKeyJSONString ToolTip="…" UserName="HotKeyUser" Value="{&quot;hotkeys&quot;:[…]}"/>

WRITTEN ONLY WHILE FUSION IS CLOSED. The running Fusion never re-reads this file and
rewrites it itself (on a preference change and on exit), so an edit made under it is
both ignored and then discarded. While Fusion runs, bindings are set by handing the
command to Fusion's own "Change Keyboard Shortcut" prompt — see the Shortcuts add-in
docstring. The other way is "apply on restart" (Control Room ▸ Fusion ▸ Shortcuts):
each anchor/unanchor is queued in PENDING_FILE instead, `overlay()` makes every
reader of the live keymap see the queue as if it had already happened, and
~/bin/fusion-restart runs `apply_pending()` in the gap between Fusion quitting and
relaunching. See remap/README.md § Apply on restart.
"""

import glob, html, json, os, re, tempfile, time

HOTKEY_GLOB = os.path.expanduser(
    "~/Library/Application Support/Autodesk/Neutron Platform/Options/*/NGlobalOptions.xml")

_ATTR_RE = re.compile(r'<HotKeyJSONString[^>]*\sValue="([^"]*)"')


def _decode(raw):
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    return raw.decode(enc, errors="replace")


def hotkey_file():
    """The live options file: the most recently written one that has a keymap in
    it (Fusion also keeps a stale copy at the non-user-scoped path)."""
    best, best_mtime = "", -1.0
    for p in glob.glob(HOTKEY_GLOB):
        if not os.path.isfile(p):
            continue
        try:
            raw = open(p, "rb").read()
        except OSError:
            continue
        if "HotKeyJSONString" not in _decode(raw):
            continue
        m = os.path.getmtime(p)
        if m > best_mtime:
            best, best_mtime = p, m
    return best


def load(path=None):
    """Returns (path, xml_text, doc) with doc the parsed keymap JSON, or
    (path, '', None) if it can't be read."""
    path = path or hotkey_file()
    if not path:
        return "", "", None
    try:
        xml = _decode(open(path, "rb").read())
    except OSError:
        return path, "", None
    m = _ATTR_RE.search(xml)
    if not m:
        return path, xml, None
    try:
        return path, xml, json.loads(html.unescape(m.group(1)))
    except ValueError:
        return path, xml, None


def entries(doc):
    """Keymap as [{seq, commands:[{id, arg, isDefault}]}].

    Several commands can share one sequence — Fusion disambiguates by
    workspace/context at press time, which is why 'a' ships bound to four
    different commands at once."""
    out = []
    for entry in (doc or {}).get("hotkeys", []):
        if not isinstance(entry, dict):
            continue
        seq = (entry.get("hotkey_sequence") or "").strip()
        if not seq:
            continue                      # the {"file_version": "2"} header
        cmds = []
        for c in entry.get("commands", []):
            if isinstance(c, dict) and c.get("command_id"):
                cmds.append({
                    "id": c["command_id"],
                    "arg": c.get("command_argument", "") or "",
                    "isDefault": bool(c.get("isDefault")),
                })
        if cmds:
            out.append({"seq": seq, "commands": cmds})
    return out


def read():
    """The on-disk keymap as entries(), or [] if it can't be read."""
    _path, _xml, doc = load()
    return entries(doc) if doc else []


# One sequence can carry several command ids — Fusion keeps one per environment and
# dispatches by context (e.g. command+d is Simulation's DOF view, Manufacturing's
# Duplicate, Electronics' DRC and Design's Delete Selection). Forwarding takes a single
# id, and the windows only ever sit beside Design, so pick the id that isn't one of the
# other environments'.
OTHER_ENV_PREFIXES = ("Sim", "Gen", "Iron", "Electron", "Publisher", "Nest", "SDK.",
                      "FusionDrawing", "FDWG", "Cam", "CAM")


def design_command(commands, prefer=()):
    """The one id out of an entry's per-environment set that a window should use.

    `prefer` wins outright: a key bound to one of our own actions shares its sequence
    with whatever Sim / Drawing / Electronics already claimed it, and those ids come
    first in Fusion's list, so prefix-elimination alone would hand back the wrong one.

    Every id on the sequence belonging to another environment returns None. Returning
    the first id instead meant a window cheerfully forwarded a Simulation command
    while sitting next to Design, which looks exactly like the key "doing nothing"
    while actually firing something meaningless. A chord with no Design meaning is
    better left inert."""
    ids = [c["id"] for c in commands if c.get("id")]
    for cid in ids:
        if cid in prefer:
            return cid
    for cid in ids:
        if not cid.startswith(OTHER_ENV_PREFIXES):
            return cid
    return None


def forward_map(keymap_entries, prefer=(), modifiers_only=False):
    """{sequence (lowercased): command_id} — what a window forwards to Fusion while
    it is focused, so Fusion's shortcuts keep working inside it. `modifiers_only`
    drops bare keys (the windows that still keep every bare key for themselves)."""
    out = {}
    for e in keymap_entries:
        seq = e["seq"]
        if modifiers_only and "+" not in seq:
            continue
        cid = design_command(e["commands"], prefer)
        if cid:
            out.setdefault(seq.lower(), cid)
    return out


def seq_of(keymap_entries, command_id):
    """The sequence bound to `command_id`, or ''."""
    for e in keymap_entries:
        if any(c["id"] == command_id for c in e["commands"]):
            return e["seq"]
    return ""


# ── Apply on restart ────────────────────────────────────────────────────────────
#
# {"deferred": bool, "ops": [{"op": "anchor"|"unanchor", "id", "seq", "ts"}]}.
# Machine-local and gitignored. `deferred` is the mode (Control Room's Apply toggle);
# `ops` is what the next restart writes. Turning the mode off leaves `ops` alone —
# they are still owed a restart.

PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "keymap_pending.json")

_MOD_ALIAS = {"cmd": "command", "command": "command", "ctrl": "control",
              "control": "control", "alt": "alt", "opt": "alt", "option": "alt",
              "shift": "shift", "fn": "fn"}
_FUSION_ORDER = ("shift", "command", "alt", "control", "fn")


def fusion_seq(seq):
    """Any spelling of a chord -> Fusion's own ('ctrl+alt+cmd+f' ->
    'command+alt+control+f'). Fusion compares the string, so what goes into its
    file has to be this spelling."""
    parts = [p.strip().lower() for p in str(seq or "").split("+") if p.strip()]
    if not parts:
        return ""
    mods = {_MOD_ALIAS[m] for m in parts[:-1] if m in _MOD_ALIAS}
    return "+".join([m for m in _FUSION_ORDER if m in mods] + [parts[-1]])


def pending():
    try:
        with open(PENDING_FILE) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    ops = [o for o in (doc.get("ops") or [])
           if isinstance(o, dict) and o.get("op") in ("anchor", "unanchor") and o.get("id")]
    return {"deferred": bool(doc.get("deferred")), "ops": ops}


def _save_pending(doc):
    d = os.path.dirname(PENDING_FILE)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, PENDING_FILE)


def set_deferred(on):
    doc = pending()
    doc["deferred"] = bool(on)
    _save_pending(doc)


def add_op(op, cid, seq):
    """Queue one keymap change. A later op on the same id supersedes an earlier one
    only in effect (apply_ops replays in order), so the list is kept whole — it is
    also the record of what the restart will do."""
    doc = pending()
    doc["ops"].append({"op": op, "id": cid, "seq": fusion_seq(seq), "ts": int(time.time())})
    _save_pending(doc)


def drop_ops(ids):
    """Forget every queued op on these command ids — Assign Now set them in the
    running Fusion instead (remap/fusion.py::apply_now)."""
    ids = set(ids or ())
    if not ids:
        return
    doc = pending()
    doc["ops"] = [o for o in doc["ops"] if o["id"] not in ids]
    _save_pending(doc)


def clear_ops():
    doc = pending()
    doc["ops"] = []
    _save_pending(doc)


def apply_ops(doc, ops):
    """Replay queued ops on a keymap doc (the file's own shape), in place, and return
    it. Pure and idempotent — replaying on an already-replayed doc changes nothing,
    which is what lets two readers both overlay without counting twice.

    anchor   the id leaves every chord it was on (Fusion holds one per command), then
             joins `seq` as a user binding.
    unanchor the id leaves `seq`. An entry left with no commands goes.

    A command's `command_argument` is kept when it moves: Fusion ships a few ids
    bound several times with different arguments (ViewVisualStyleCommand), but those
    are all defaults and never ours to move."""
    rows = doc.setdefault("hotkeys", [])
    for o in ops or []:
        cid, seq = o.get("id"), fusion_seq(o.get("seq"))
        if not cid:
            continue
        if o.get("op") == "anchor":
            if not seq:
                continue
            for e in rows:
                if isinstance(e, dict) and "commands" in e and fusion_seq(e.get("hotkey_sequence")) != seq:
                    e["commands"] = [c for c in e["commands"] if c.get("command_id") != cid]
            entry = next((e for e in rows if isinstance(e, dict)
                          and fusion_seq(e.get("hotkey_sequence")) == seq), None)
            if entry is None:
                entry = {"hotkey_sequence": seq, "commands": []}
                rows.append(entry)
            if not any(c.get("command_id") == cid for c in entry["commands"]):
                entry["commands"].append({"command_id": cid, "command_argument": "",
                                          "isDefault": False})
        elif o.get("op") == "unanchor":
            for e in rows:
                if (isinstance(e, dict) and "commands" in e
                        and (not seq or fusion_seq(e.get("hotkey_sequence")) == seq)):
                    e["commands"] = [c for c in e["commands"] if c.get("command_id") != cid]
    doc["hotkeys"] = [e for e in rows
                      if not (isinstance(e, dict) and "commands" in e and not e["commands"])]
    return doc


def overlay(keymap_entries, ops=None):
    """entries() with the queued ops replayed on top — what the keymap WILL be after
    the next restart. Every reader of the live table goes through this, so the page,
    the add-in's re-adoption (hotkeys.sync_from_keymap) and the next plan all agree
    with the queue instead of undoing it. Through the doc shape so apply_ops is the
    one implementation."""
    if ops is None:
        ops = pending()["ops"]
    if not ops:
        return keymap_entries
    doc = {"hotkeys": [{"hotkey_sequence": e["seq"],
                        "commands": [{"command_id": c["id"], "command_argument": c.get("arg", ""),
                                      "isDefault": c.get("isDefault", False)}
                                     for c in e["commands"]]}
                       for e in keymap_entries]}
    return entries(apply_ops(doc, ops))


def _escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def write(path, xml, doc):
    """Put `doc` back into the options file's HotKeyJSONString, byte-for-byte in
    Fusion's own encoding (UTF-16 LE with BOM, compact JSON, &quot;-escaped — an
    unedited doc round-trips identically). The previous file is kept at .bak.
    ONLY while Fusion is closed — see the module docstring."""
    m = _ATTR_RE.search(xml)
    if not m:
        raise ValueError("no HotKeyJSONString in %s" % path)
    value = _escape(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    new = xml[:m.start(1)] + value + xml[m.end(1):]
    raw = new.encode("utf-16")                      # BOM + native (LE) order
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)    # mkstemp's 0600 otherwise
    except OSError:
        pass
    try:
        os.replace(path, path + ".bak")
    except OSError:
        pass
    os.replace(tmp, path)


def apply_pending(log=print):
    """Write the queued ops into the options file and empty the queue. Called by
    ~/bin/fusion-restart between quit and relaunch; refuses if Fusion is running,
    since it would overwrite the edit. Returns the number of ops written."""
    ops = pending()["ops"]
    if not ops:
        return 0
    import subprocess
    r = subprocess.run(["/usr/bin/pgrep", "-f", "Autodesk Fusion.app/Contents/MacOS"],
                       capture_output=True, text=True)
    if (r.stdout or "").strip():
        raise RuntimeError("Fusion is running — its keymap would overwrite this edit")
    path, xml, doc = load()
    if doc is None:
        raise RuntimeError("could not read the keymap from %r" % path)
    write(path, xml, apply_ops(doc, ops))
    clear_ops()
    log("keymap: wrote %d queued change(s) to %s" % (len(ops), path))
    return len(ops)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--apply-pending"]:
        apply_pending()
    elif sys.argv[1:] == ["--pending"]:
        print(json.dumps(pending(), indent=1))
    else:
        print("usage: keymap.py --pending | --apply-pending")
        sys.exit(2)
