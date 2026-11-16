#!/usr/bin/env python3
"""
Cases for keymap.py's apply-on-restart half (apply_ops / overlay / write).

    python3 fusion_ui/keymap_test.py

Needs neither Fusion nor the options file, except the last case, which round-trips
the real file through write() into a temp copy (skipped if there is none) — the
promise there is that an unedited doc comes back byte-identical, so the only thing
a restart ever changes in Fusion's file is the keymap edit it was asked for.
"""

import copy
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keymap  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(name)
        print("FAIL %s\n  got  %r\n  want %r" % (name, got, want))
    else:
        print("ok   %s" % name)


def cmd(cid, default=False, arg=""):
    return {"command_id": cid, "command_argument": arg, "isDefault": default}


BASE = {"hotkeys": [
    {"file_version": "2"},
    {"hotkey_sequence": "e", "commands": [cmd("SmartSketchCmd")]},
    {"hotkey_sequence": "shift+control+d",
     "commands": [cmd("SimDof", True), cmd("JoshRoute_CtrlShiftD")]},
    {"hotkey_sequence": "command+alt+j", "commands": [cmd("SmartDimensionCmd")]},
]}


def seqs(doc):
    return {e["hotkey_sequence"]: [c["command_id"] for c in e["commands"]]
            for e in doc["hotkeys"] if "commands" in e}


# spelling
check("fusion_seq reorders", keymap.fusion_seq("ctrl+alt+cmd+f"), "command+alt+control+f")
check("fusion_seq option", keymap.fusion_seq("opt+shift+E"), "shift+alt+e")
check("fusion_seq bare", keymap.fusion_seq("["), "[")

# anchor onto a new chord: leaves its old one, which empties and goes
d = keymap.apply_ops(copy.deepcopy(BASE), [
    {"op": "anchor", "id": "SmartDimensionCmd", "seq": "alt+cmd+k"}])
check("anchor moves", seqs(d).get("command+alt+k"), ["SmartDimensionCmd"])
check("old chord emptied and dropped", "command+alt+j" in seqs(d), False)
check("header kept", d["hotkeys"][0], {"file_version": "2"})

# anchor onto a shared chord, spelled the other way: joins the entry, not a new one
d = keymap.apply_ops(copy.deepcopy(BASE), [
    {"op": "anchor", "id": "SmartSketchCmd", "seq": "ctrl+shift+d"}])
check("anchor joins existing entry", seqs(d)["shift+control+d"],
      ["SimDof", "JoshRoute_CtrlShiftD", "SmartSketchCmd"])
check("anchor left e", "e" in seqs(d), False)

# unanchor one of two holders
d = keymap.apply_ops(copy.deepcopy(BASE), [
    {"op": "unanchor", "id": "JoshRoute_CtrlShiftD", "seq": "shift+control+d"}])
check("unanchor keeps the default", seqs(d)["shift+control+d"], ["SimDof"])

# unanchor on the wrong chord does nothing
d = keymap.apply_ops(copy.deepcopy(BASE), [
    {"op": "unanchor", "id": "SmartSketchCmd", "seq": "f"}])
check("unanchor elsewhere is a no-op", seqs(d), seqs(BASE))

# order: later op wins
ops = [{"op": "anchor", "id": "X", "seq": "q"},
       {"op": "anchor", "id": "X", "seq": "w"},
       {"op": "unanchor", "id": "X", "seq": "w"}]
d = keymap.apply_ops(copy.deepcopy(BASE), ops)
check("replayed in order", [k for k, v in seqs(d).items() if "X" in v], [])

# idempotent
ops = [{"op": "anchor", "id": "SmartDimensionCmd", "seq": "alt+cmd+k"},
       {"op": "unanchor", "id": "JoshRoute_CtrlShiftD", "seq": "shift+control+d"},
       {"op": "anchor", "id": "New", "seq": "shift+control+d"}]
once = keymap.apply_ops(copy.deepcopy(BASE), ops)
twice = keymap.apply_ops(copy.deepcopy(once), ops)
check("idempotent", twice, once)

# overlay on the entries() shape agrees with apply_ops
ents = keymap.entries(copy.deepcopy(BASE))
check("overlay == apply_ops", keymap.overlay(ents, ops),
      keymap.entries(keymap.apply_ops(copy.deepcopy(BASE), ops)))
check("overlay with no ops is identity", keymap.overlay(ents, []), ents)

# write() round-trip on the real file, into a temp copy
real = keymap.hotkey_file()
if real:
    tmpd = tempfile.mkdtemp()
    try:
        p = os.path.join(tmpd, "NGlobalOptions.xml")
        shutil.copy(real, p)
        before = open(p, "rb").read()
        path, xml, doc = keymap.load(p)
        keymap.write(path, xml, doc)
        check("unedited write is byte-identical", open(p, "rb").read() == before, True)
        keymap.write(path, xml, keymap.apply_ops(doc, [
            {"op": "anchor", "id": "KeymapTestCmd", "seq": "command+alt+control+f12"}]))
        got = keymap.seq_of(keymap.entries(keymap.load(p)[2]), "KeymapTestCmd")
        check("edited write reads back", got, "command+alt+control+f12")
        check(".bak kept", open(p + ".bak", "rb").read() == before, True)
    finally:
        shutil.rmtree(tmpd)
else:
    print("skip write round-trip (no options file)")

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
