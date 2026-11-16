# Fusion tooling — system map

The repo's second system (the first is the slots/window layer — `ARCHITECTURE.md`).
A family of local web-app windows that ride alongside Autodesk Fusion, plus the
shared library they all sit on and the ~45 in-process add-ins that feed them.

```
  Fusion (one Python process)                     WKWebView windows (native apps)
    AddIns/*  ── import ──►  <tool>/server.py  ◄──── poll /state ──── <tool>/index.{html,css,js}
    main-thread sync tick        (daemon HTTP thread)        POST /cmd
      update_*() / pop_commands()   in-memory snapshot + queue, under a lock
                                    NEVER touches the Fusion API off the main thread
                     │
                     ▼
              fusion_ui/  — theme, JS behaviours, JSON registries (shared by all)
```

## The windows

| dir | app bundle | what it is |
|---|---|---|
| `fusion_params/`    | `FusionParamsApp/`    | parameter editor |
| `fusion_browser/`   | `FusionBrowserApp/`   | custom component browser |
| `fusion_shortcuts/` | `FusionShortcutsApp/` | keyboard-shortcut browser/editor; also serves `/data` `/bind` `/assign` for `remap/fusion.py` |
| `fusion_value/`     | `FusionValueApp/`     | dimension-value / new-parameter dialog; also hosts the command-search palette (assign it a key in the Shortcuts window — S is the natural pick) |
| `fusion_dock/`      | `FusionDockApp/`      | the left-edge dock — the Browser, or (swapped by `DockTimelineToggleCmd`) the vertical timeline |
| `fusion_timeline/`  | (in `FusionDockApp/`) | vertical timeline: operation icons, errors, edit/rename/suppress, nested window-only groups; locks the active component while shown (`AddIns/Timeline`) |
| `remap/` (Control Room) | `RemapApp/`      | not Fusion-specific, but same window shape and shares `fusion_ui/theme.css` — see `remap/README.md` |

Ports: `OPS.md`. Each `server.py` opens with a docstring describing that window specifically; read it
before touching the window. The architecture note above is verbatim from those
docstrings and is identical across all four.

Each window's page is three files: `index.html` (markup), `index.css`, `index.js`
(behaviour — the big one, grep it). Grep `index.js`, don't read it whole.

**The plumbing is shared, not copied.** Every `server.py` subclasses
`fusion_ui/webwin.py`'s `Handler` and starts `do_GET` with `serve_static()`, which
answers `index.*`, **every** shared `fusion_ui/` asset (`/theme.css`, `/general`,
`/capitalise.js`, `/naming.js`, `/planes.js`, `/chips.js`, `/keys.js`, `/list.js`, `/list.css`,
`/param_naming.*`, `/variables.json`), `/settings` and `/health`; CORS preflight, JSON send helpers,
`atomic_write`, `drain` (every `pop_commands()`), `page_stamp` (the value window's
reload-when-stale stamp, read off its own `<script>`/`<link>` tags) and
`make_server`/`main` live there too. A server.py holds only its
own state, queue and routes — so a page adopting a shared engine needs just the
`<script>` tag, and a fix to the plumbing reaches all four windows. Fusion's keymap
is read through `fusion_ui/keymap.py` (`read`, `seq_of`, `forward_map`,
`design_command` — which picks the Design-side id on a chord Fusion shares across
environments). Both are loaded by path, fresh per server load (see webwin's header
for why not `import`). The Control Room's `remap/server.py` keeps its own Handler
but serves its shared `.css`/`.js` from the same `SHARED_ASSETS` table. Same for the Swift side: `bin/build_app.sh` builds every
`*App` and compiles `fusion_ui/WindowShell.swift` in beside `main.swift` — the Edit
menu (`installEditMenu`) and the page's alert/confirm/prompt sheets (`jsAlert` /
`jsConfirm` / `jsPrompt`) live there, not in each shell.

**`fusion_value` is the odd one:** modal in spirit. Hidden until the add-in opens a
session (hotkey pressed with a sketch dimension selected, or the new-parameter key),
hides again on commit/cancel. The window edge-detects `seq` to know a new session
started, so re-pressing the hotkey re-arms rather than stacking.

## `fusion_ui/` — the shared layer

Every window loads these **by absolute path** (`/Users/josh/Code/Scripts/fusion_ui/…`),
so the path is a contract — don't move the directory. What's in it and the rules
that go with each piece: `fusion_ui/CLAUDE.md`.

## Tests

`fusion_tests/` — offline. `hotkey_domains.py` stubs `adsk` and exercises the router's
pure logic (domains, exclusivity, specificity, resolution). `visibility_drift.py` is
the nav/visibility round-trip invariant. Both call add-in functions directly; note
the `cmd_def.execute()` trap documented at the top of `visibility_drift.py`
(execute() queues, it doesn't run). Per-window JS/py tests live under the window dir
(`fusion_value/test/`, `fusion_params/test/`, `fusion_browser/test/`, `fusion_ui/*_test.*`)
— all the same jsc-headless technique: concatenate the shared engines, the page's own
`index.js` verbatim, and a `cases.js` of assertions, then run it under
`/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc`. The three
window runners share `fusion_ui/jsc_harness.py`, which reads the engine list off the
page's own `index.html`, so a test loads exactly what the window does. Each
only covers what has no live-Fusion dependency; anything that needs the real add-in
(activation, selection, folder filing) is verified by hand in Fusion.

## Add-ins

`~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/` (a working
directory of this session) — ~45 add-ins. They import the `server.py` modules and
push state in / pull commands out on their main-thread sync tick.

**Our own versions of Fusion's feature commands.** Extrude and Mirror each carry their
own plumbing (a command of ours with its palette hidden, a real feature rebuilt in
executePreview, the value window as the dialog). Rectangular Pattern, Combine and Scale
share it instead: `AddIns/Feature Panel.py` is the engine (subclass `Feature`, say the
rows, defaults and `build`), and the value window's generic "feature panel" draws the
rows it sends. Combine can also take a whole component as its target and/or its tools,
and keeps such a run live — re-synced after each command when a body is added to or
deleted from either component (`AddIns/Combine/Combine.py` docstring). Flange can't be one — the API has no way to build a flange — so
`AddIns/Flange` runs Sketch Directions' Unify on the picked curves and then starts
Fusion's own Flange on them. New features are named by `Auto-Name Feature` (the first
extrude/flange that gives a component a body is "Base").

**What a viewport click does is data — `AddIns/Click Actions`.** A click, two-finger
tap or three-finger tap runs the Fusion command `fusion_ui/clicks.json` binds to that
gesture + held modifiers (Space = Karabiner's F20, read off the HID state) + what was
clicked (body / sketch / component / construction / empty / any). Set on Control Room ▸
Fusion ▸ Clicks. Clicks keep Click To Descend's exact path — Fusion's selection event,
80 ms defer, direct activate + isolate for Go Down / Go to Bottom — because reading the
pick off the tap's mouse-up report raced Fusion's own pick (it read an empty selection).
The taps come from the tap's `/tmp/fusion_click_event`. It replaced Click To Descend (now
inert) and the Browser's hard-wired jump-down, right-click → Go Up and double-click → Go
to Top; the Browser only mirrors the selection now.

**Suspend mode peeks — `fvs` § Suspend mode: peek.** With the Browser's suspend mode on
(`BrowserSuspendNavCmd`), every navigation — the window's and the viewport's Go Up /
Go Down / Go to Top / Go to Bottom / Click Actions go-downs — is a PEEK: the viewport
isolates onto the target with the active path kept lit, and the active component, the
open sketch and the running command never change (the nav commands are vetoed at
`commandStarting` and peek from the veto). The navigation LOGIC is exactly the normal one —
only the target is looked at instead of activated; two/three-finger taps navigate even
with a command open (Click Actions), clicks on bodies stay the command's. Off jumps back. A blue glow fades in round
Fusion's window meanwhile (`bin/fusion_glow.m`, started by the Browser add-in; tuned
from the `Glow Settings` add-in's palette → `config/fusion_glow.json`). See
`fusion_browser/CLAUDE.md` § Suspend mode.

**One tick in the model at a time — `AddIns/Tick Guard.py`.** Four add-ins poll
(Browser 70 ms, Dimension Value 70 ms, Shortcuts 400 ms, Parameters 500 ms) and
several of them mutate. A mutation recomputes, Fusion pumps its Qt task queue
while it recomputes, and the task queue is what delivers `fireCustomEvent` — so
one add-in's tick could start inside another's recompute and walk a half-rebuilt
model. Every `.hang` spindump between 2026-09-16 and 2026-09-22 caught exactly
that: a signal delivered inside our Python, in `Timeline::getMarkerPosition` /
`UserParameters::item` / `BRepBody::entityToken`. So:

- a poller wraps its whole tick in `tickguard.tick(name)` and returns when it
  gets `False` — a skipped frame costs nothing, all four re-fire within 500 ms;
- anything that CHANGES the model wraps itself in `tickguard.hold(name)`, which
  always proceeds and nests. The shared visibility engine does this for you: its
  mutating entry points carry `@_holds_model`;
- the poller thread also coalesces — it never fires again while an event is
  still unhandled, so ticks cannot stack up behind a busy main thread and drain
  as one long beachball.

The latch lives on `builtins`, not in a module global: `_load_local()` exec's each
shared module afresh per add-in, so a module global would give every add-in its own
private latch and guard nothing. Test: `fusion_tests/tick_guard.py` (offline).

**Busy shows at the pointer — `bin/fusion_busy.m`.** Everything above runs on Fusion's
main thread, and so does Fusion's own recompute; macOS shows no beachball for ~2 s. A
launchd helper (repo root, Accessibility grant) pings Fusion's main thread over AX every
100 ms while Fusion or a companion window is in front, and shows a small spinner beside
the pointer whenever a ping goes unanswered for 150 ms. `/tmp/fusion_busy.log` records
every busy spell with its length — the first place to look for the next hitch. Off
switch: the **Busy Spinner** tick on the Browser's right-click menu tail
(`config/fusion_busy_off` present = off; not logged while off).

**Why the app freezes instead of crashing** is Autodesk's bug, not ours, and it is
not fixable from here: on any signal, `libcer.dylib`'s handler calls
`Ns::AppLogMgr::stopThread()`, which blocks forever in `std::thread::join()`. No
crash dialog, no exit — a frozen app that has to be force quit. All we can do is
not hand it signals to catch.

**The timeline is kept in phase order — `AddIns/Phases.py`.** Create components →
arrange → sketches → independent → dependent (crosses components) → post-dependent
(uses a dependent feature's result — proved by `canReorder` refusing, not guessed) →
component duplication. The Timeline add-in places every new item at the end of its
phase on its tick, whether or not its panel shows; both the panel and the Browser's
feature rows (a body's features, opened with F) reorder only within a phase. Its
header has the rules; `fusion_tests/phases.py` is the offline test.

**Features that follow a Browser group — `AddIns/Groups.py`.** Combine ("Target is /
Tools are: Group") and Extrude ("Bodies are: Group") can work on a whole Browser group
(folders.json, read-only) and keep following it as bodies are filed in or out
(`Groups.Watcher`: after each command and each folders.json write). Each reads the group
as it stands at its own place in the timeline and never moves; a body only there after
it (a later cut split one) is left alone. The groups are named
on the feature (`ScriptsCombine/link` tg/og, `josh.extrude/groups`); Browser.py's
`_group_uses` reads both and the window asks before exploding one.

**The model's section in every sketch — `AddIns/Section Sketch` + `AddIns/Section.py`.**
Opening a sketch square to X, Y or Z shows every solid body the plane cuts as FIXED
curves (locked, transparent, selectable, no dimensions, no profiles) — kept in ONE
hidden "References <axis>=… mm" sketch per component and plane, just ahead of the
sketches drawn there and shown only while one is open (Section.py § Reference sketch;
Visibility Snapshot keeps it on via the slice hook). Your sketch never holds them, so
redrawing them never makes the model recompute. Tie to one — a coincident, a dimension
"5 mm back from it" (Fusion projects just that curve in; it does not snap to another
sketch's curves while drawing) — and it becomes, when the tool ends,
DIMENSIONS TO THE BOUNDS in the same parameters that place the referenced thing, so
no sketch references another. The arithmetic is pure and tested in `fusion_ui/`:
`section_derive.py` (a sketch point's coordinates as linear Forms in user parameters,
walked from its dimensions and constraints, each checked against the geometry),
`section_geom.py` (frames, the per-axis coordinate POOL that matches a section edge
back to its Form by value, tethers, plane picks) and `flex_solve.py` (Move: which
parameter changes, within flexible ranges, ties and gap limits). Phases puts every
sketch ahead of every feature, so a sketch sees NO bodies — the section is cut from
`TemporaryBRepManager` copies captured while no sketch is open (on idle, and at
SketchActivate). Drawing runs inside one hidden command (one undo step). Flexible
ranges and doppelganger axes are set in the value window (`fusion_value/CLAUDE.md`
§ Section Sketch). Read the add-in's header for the keys; `/tmp/section_sketch.log`.
NEVER A NUMBER: a tie or Bring In that cannot be made of parameters is refused (tie
removed, curve not brought in) and flashed as an error — `Section.parametric`/`problem`.

**Typed numbers are parameters — `AddIns/Number Params.py` + `fusion_ui/number_params.py`.**
Every length/angle literal committed through our windows (value box, Parameters,
Extrude, feature panels) or typed natively into a sketch dimension (Section Sketch's
`_sketch_numbers`) becomes a hidden user parameter `Num_<8 hex>`; "Parameters: Typed
Numbers to Parameters" sweeps the existing ones once. The Browser/Params windows never
list them and show them as their literal inside other expressions; the value window
shows each as a number chip in its own colour (`.chip-numparam`), and retyping it
changes that parameter (`number_params.plan` reuses by value, then by order).

**Saving and versions without Fusion's dialogs — `AddIns/Versions`.** Quick Save
(`VersionsSaveCmd`: no description dialog; on an old version the value window asks "save
as latest?") and the Versions panel (`VersionsPanelCmd`, and "Versions…" on every Browser
menu's tail): open documents (switch / close with a save prompt) and the active one's last
20 versions (open one: saves + closes the current). The panel is the value window's
`versions` mode. Its header has the API costs (`DataFile.versions` ~1.7 s, cached;
`isMilestone`/`lastUpdatedBy` are per-item cloud calls — never read).

**Editing Fusion's own installed bundle is a different system** — `~/code/fusion-carryover`,
with its own `fusion-carryover` skill. Anything inside the Fusion.app web-deploy
bundle or a byte-patched dylib goes through that, or it's lost on the next update.
