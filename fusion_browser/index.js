const API = "http://127.0.0.1:8732";
const { seqOf, normSeq, seqLabel } = Keys;   // fusion_ui/keys.js — the chord spelling
// The Parameters window's own server. Parameters are filed into this tree, but they
// are still owned there: we read the live rows from /params and post edits to /set,
// rather than duplicating any of that in the Browser add-in.
const PARAMS_API = "http://127.0.0.1:8731";
// The value dialog (the Dimension Value add-in's window). Enter / F on a parameter row
// opens it there rather than an inline box in this tree — one editor for a value,
// wherever the value lives.
const VALUE_API = "http://127.0.0.1:8734";
// The vertical timeline (fusion_timeline/) — the dock's other panel. Its add-in owns
// which panel shows; this page only asks it to swap and reports the answer to the shell.
const TIMELINE_API = "http://127.0.0.1:8736";

// short-name -> Fusion command id, for forwarding toggle hotkeys.
const TOGGLE_CMD = {
  bodies:   "ToggleBodyVisibilityCmd",
  sketches: "ToggleSketchVisibilityCmd",
  origins:  "BrowserToggleOriginsCmd",
  isolate:  "ToggleIsolateActiveComponentCmd",
};

let state   = { tree: [], activeToken: "", selTokens: [], toggles: {}, editingSketch: false, editSketchToken: "", ts: 0 };
let prevActiveToken;              // last seen active component (to detect viewport jumps)
let prevEditing   = false;        // last seen editingSketch, to catch the exit transition
let editOwnerTok  = "";           // the component the edited sketch lives in
// Rows the spine tidy must leave expanded, with a deadline. Exiting a sketch makes
// Fusion activate the parent for a beat before landing back in the owning component,
// and the tidy that fires on the way through would collapse the owner and then
// re-expand it a moment later — a visible flicker. Protecting the path means the
// collapse never happens in the first place.
let protectOpen  = new Set();
let protectUntil = 0;
function protectPath(tok, ms){
  if (!tok) return;
  protectOpen = new Set(pathToTok(tok));
  protectUntil = Date.now() + ms;
}
function isProtected(tok){
  if (!protectOpen.size) return false;
  if (Date.now() > protectUntil){ protectOpen = new Set(); return false; }
  return protectOpen.has(tok);
}
function tidyCollapse(tok){ if (!isProtected(tok)) collapsed.add(tok); }
let prevSelKey = "";              // last seen viewport selection set (JSON, to detect change)
let prevPickKey = "";             // …and what kind of pick it was (state.selPick, JSON)
let selected  = new Set();        // real selection: tokens highlighted as selected
let selAnchor = null;             // where a ⇧-click measures its range from
// A selection built HERE (⌘-click, ⇧-range) is pushed to the viewport as one
// `selectmany`, but the viewport takes a poll or two to report it back — and the
// mirror below replaces `selected` with whatever it reports. Hold our own set until
// it catches up (or 3s), same optimistic pattern as pendingActivate/pendingVis, so
// the highlight never flickers back to a single row. Rows Fusion can't select
// (groups, parameters) live only here, which is the other reason not to take the
// mirror's word for it.
let pendingSel = null;            // {toks:[…], key:JSON of the pushed tokens, ts}
// How long an EMPTY viewport is still read as the gap in our own push rather than as a
// deselect. Pushing a selection clears Fusion's first and then adds, so a poll landing
// in between reports nothing selected; past this window nothing is left to wait for and
// an empty viewport means exactly what it says. Without the distinction, deselecting
// just after picking a group — by clicking empty space, or with the deselect key, which
// is why the highlight blinked off and came straight back — was overruled by the hold
// for the rest of its three seconds.
const PUSH_ECHO_MS = 350;
// Set while this window is deliberately holding a selection the viewport CANNOT mirror.
// Hiding a row makes Fusion drop it from its own selection, and letting that empty
// selection come back through the mirror below is what made the visibility toggle hide
// but never unhide: the second press had nothing left to act on. After a hide this
// window is the only place the row is still picked, which is exactly what makes the
// toggle a toggle. Cleared the moment the user picks something else.
let selHold = null;
let hotkeys = {};                 // short-name -> sequence (e.g. "shift+alt+c")
let fusionKeys = {};              // normalized seq -> Fusion command_id (full keymap, forwarded)
// When each GROUP was last opened. Only groups are stamped — they are the only rows
// that have to be ranked by recency (see newParamContainer), and keying on every
// component token as well would grow a map of long entity tokens for nothing.
// Persisted beside `collapsed`, because "the last group you opened" has to survive the
// dock being closed and reopened the same way the open/closed state itself does.
let expandSeq = {};
let expandTick = 0;
// …and, like the other two row overlays, persisted under the DURABLE KEY rather than
// the token — see hydrateOverlays. seqKeys is that persisted side; expandSeq is the
// live one, hydrated from it once a tree has arrived to key against. (Only groups are
// ever stamped, and a group's "fld:<id>" token is durable already, so for this one map
// the two sides currently look the same. It goes through the same translation anyway:
// one path for the three overlays is easier to keep right than two.)
let seqKeys = {};
try {
  const st = JSON.parse(localStorage.getItem('browserExpandSeq') || '{}');
  if (st && typeof st === 'object'){
    seqKeys = st;
    // The tick has to carry on from the highest stamp ever written, not from the
    // highest one that hydrates — an unresolved stamp still outranks a fresh 1.
    for (const k in st) expandTick = Math.max(expandTick, st[k] | 0);
  }
} catch(_){}
// A row is expanded simply by NOT being in `collapsed`, and it leaves that set from a
// dozen places — a click, a spine tidy, revealing a viewport pick, opening a new
// folder. Stamping the set itself catches every one of them, which a hook at each call
// site would not.
class CollapseSet extends Set {
  delete(tok){
    if (super.has(tok) && isFolderTok(tok)) expandSeq[tok] = ++expandTick;
    return super.delete(tok);
  }
}
let collapsed = new CollapseSet();        // tokens collapsed by the user
let colKeys = new Set();                  // …as persisted: durable keys
try { colKeys = new Set(JSON.parse(localStorage.getItem('browserCollapsed') || '[]')); } catch(_){}
let drag = null;                  // active drag {tok,type,toks} once past the move threshold (freezes render)
// Window-only sibling ordering overlay: parentToken -> [childToken,…] desired order.
// Fusion has no API to reorder occurrences/bodies/sketches, so reordering lives here
// (persisted locally) and is applied at render time; new items append at the end.
let siblingOrder = {};
let ordKeys = {};                 // …as persisted: durable keys on BOTH sides — the
                                  // parent and every child in its list
try { ordKeys = JSON.parse(localStorage.getItem('browserOrder') || '{}') || {}; } catch(_){}
function saveOrder(){
  // A parent the tree has never handed back is left exactly as it was persisted: it may
  // be a row the timeline is rolled back past, or another document's. Only the parents
  // this session actually holds are rewritten from memory.
  for (const p of Object.keys(siblingOrder)){
    const was = keySrc[p];                 // the key this list came in under…
    const k = saveKeyOf(p);                // …and the one it goes out under
    ordKeys[k] = mergeUnresolved(siblingOrder[p].map(saveKeyOf),
                                 ordKeys[k] || (was ? ordKeys[was] : null));
    if (was && was !== k) delete ordKeys[was];   // the token entry it replaces
  }
  try { localStorage.setItem('browserOrder', JSON.stringify(ordKeys)); } catch(_){}
}
// Put back the children that are not in the tree right now, at the place the stored
// list had them. They are not in memory either — so writing memory out on its own is
// how a rolled-back row loses its position for good.
function mergeUnresolved(list, stored){
  if (!stored || !stored.length) return list;
  const out = list.slice();
  let at = 0;                          // just past the last stored key we found.
                                       // Only ever forwards: the list has been
                                       // reordered under us, and walking backwards
                                       // would drop the row in ahead of rows the
                                       // stored list puts it after.
  for (const k of stored){
    // The stored list can be in the OLD key space — that is what a migration is — so
    // every stored key is read back through the tree before it is compared.
    const t = rowTokOf(k);
    const i = out.indexOf(t ? saveKeyOf(t) : k);
    if (i >= 0){ at = Math.max(at, i + 1); continue; }
    if (t) continue;                   // in the tree but not in the list: moved away
    out.splice(at, 0, k); at++;
  }
  return out;
}
let cursorTok;                    // highlighted row token (undefined=first load, null=deselected)
let editing  = false;
let lastJSON = "";
const PENDING_VIS_MS = 5000;
let pendingVis = {};              // token -> {visible, ts}: optimistic visibility until Fusion confirms
let pendingActivate = null;       // {token, ts}: optimistic active component until the server confirms
// Activating under isolate can take Fusion several seconds (≈5 s measured on Drawer
// Wing). The hold used to give up at 3 s: the window snapped back to the old level,
// then read Fusion's arrival as a fresh VIEWPORT jump and mirrored the viewport's
// selection — the Geometry group F had picked came back as its body, and the group
// was thrown open around it.
const ACTIVATE_HOLD_MS = 10000;
// Every browser-driven level change goes through here. Changing level is exactly when
// the tidy must run, so any open-path protection armed at the level being left (a
// revealed pick protects its whole path, the component included) is dropped — it
// kept the component you had just stepped out of expanded. The viewport-driven
// equivalent is in pollState's jumpedComp branch.
function holdActivate(tok){
  pendingActivate = { token: tok, ts: Date.now() };
  protectOpen = new Set(); protectUntil = 0;
}
let pendingEditSketch = null;     // {token, occToken, ts}: optimistic sketch-edit emphasis until the server confirms
let pendingDelete = {};           // token -> ts: rows deleted optimistically until the tree drops them
let cutToks = [];                 // parameters marked by ⌘X, moved on the next ⌘V

function saveCollapsed(){
  // Same rule as saveOrder: keys that never made it into memory are kept verbatim,
  // everything that did is rewritten from memory under its durable key.
  colKeys = new Set([...colKeys].filter(k => !hydCollapsed.has(k)));
  // A key written from memory is applied by definition: marking it so is what lets a
  // later reopen drop it. Left unmarked, the next hydrate (rebuildKeyIndex) read it
  // back as a stored collapse and shut a group reopened in between.
  for (const t of collapsed){ const k = saveKeyOf(t); colKeys.add(k); hydCollapsed.add(k); }
  try { localStorage.setItem('browserCollapsed', JSON.stringify([...colKeys])); } catch(_){}
  // Drop stamps for groups that no longer exist, so the map stays the size of the
  // document's folder list rather than of everything ever opened in it. Only once the
  // overlay is actually loaded — before that every folder looks gone, and pruning then
  // would throw the stamps away on every startup.
  if (foldersDoc) for (const k in expandSeq) if (!folderOf(k)) delete expandSeq[k];
  for (const k of Object.keys(seqKeys)) if (hydSeq.has(k)) delete seqKeys[k];
  for (const t in expandSeq) seqKeys[saveKeyOf(t)] = expandSeq[t];
  try { localStorage.setItem('browserExpandSeq', JSON.stringify(seqKeys)); } catch(_){}
}

// Closing a row closes everything inside it too, straight away, so re-opening it always
// shows a tidy single level — whatever was open in there when you left. (It used to
// wait 5 s first, so a quick re-open brought the old state back.) The caller saves.
const collapseTimers = {};
function rememberCollapse(tok){
  cancelCollapseTimer(tok);
  const n = nodeByTok(tok);
  if (n) collapseDescendants(n);
}
function cancelCollapseTimer(tok){ if (collapseTimers[tok]){ clearTimeout(collapseTimers[tok]); delete collapseTimers[tok]; } }
function collapseDescendants(node){
  (function w(ns){ for (const n of ns){ if (n.children && n.children.length){ collapsed.add(n.token); w(n.children); } } })(node.children||[]);
}
// Document arrival (see pollState): shut every row. The spine itself is reopened by the
// tidy that follows — which now also closes the Geometry group this leaves open, since
// arriving anywhere shows everything collapsed.
let spineDoc = "", docSeenDoc = "", docSeenAt = 0;   // the document the spine was tidied for
function collapseAllButGeometry(){
  protectOpen = new Set(); protectUntil = 0;
  for (const n of state.tree)
    if (n.children && n.children.length){ collapsed.add(n.token); collapseDescendants(n); }
  const act = state.activeToken ? nodeByTok(state.activeToken) : null;
  for (const c of ((act && act.children) || []))
    if (c.type === "geometry") collapsed.delete(c.token);
}

// ── Suspend mode ─────────────────────────────────────────────────────────────
// A temporary mode for moving around the model without disturbing whatever Fusion is
// doing: the running command, the selection, the open sketch and the ACTIVE component
// all stay exactly as they are. Navigation still works, as a PEEK — visibility only
// (fvs § Suspend mode: peek in AddIns/Visibility Snapshot.py): the viewport isolates
// onto where you went with the active component kept lit beside it, and this tree
// roots there (viewTok). The bold "active" mark stays on the real active component,
// so where you really are is always readable. Turning it off jumps back.
//
// The four navigating functions (activateComponentTok, goUpOneLevel, activateRootUI,
// enterSketch) peek instead when suspended — peekTo. The ones that would disturb the
// command's selection or the model (toggleVisCursor, deselectAll, the feature trace…)
// still no-op at their own top, and post() drops that family of actions regardless,
// in case anything new ever calls Fusion directly. Cursor movement, search,
// expand/collapse and the parameter editor are untouched.
let suspended = false;
const SUSPEND_BLOCKED_ACTIONS = new Set(
  ["activate", "selectbody", "selectsketch", "entersketch", "deselect", "setvis", "togglevis", "displaydetail",
   "texturemap"]);
// A peek's optimistic token, held until the add-in's /state says the same (or 3 s) —
// the same pattern as pendingActivate.
let pendingPeek = null;
let prevViewTok = "";
function setSuspended(on){
  suspended = !!on;
  document.body.classList.toggle("suspended", suspended);
  // The add-in: Lock Outside Active (Dim Outside Active) unlocks only while suspended,
  // every nav becomes a peek, the edge glow shows — and off, the view jumps back onto
  // the active component. Not in the blocked set, so it always goes through.
  post({action: "suspend", on: suspended});
  if (!suspended && state.peekToken != null){
    state.peekToken = null; pendingPeek = null;
    prevViewTok = viewTok();
    markActivePathVisible();
    revealActiveSpine(); render();
  }
}
function toggleSuspend(){ setSuspended(!suspended); }
// The component the tree is rooted on and "where you are" for navigation: the peek
// while suspended, else the active component. "" = the document root.
function viewTok(){
  if (suspended && state.peekToken != null)
    return state.peekToken === "__root__" ? "" : state.peekToken;
  return state.activeToken || "";
}
// Suspend mode's move: re-root and reveal right away, then ask the add-in to isolate
// the viewport onto it. Never activates, never touches the selection.
function peekTo(tok){
  tok = tok || "__root__";
  // A peek onto the active component itself ends the peek (the add-in drops it too,
  // so /state would never report this token for pendingPeek to match).
  const home = tok === "__root__" ? !state.activeToken : tok === state.activeToken;
  state.peekToken = home ? null : tok;
  pendingPeek = home ? null : { token: tok, ts: Date.now() };
  prevViewTok = viewTok();
  if (!prevViewTok) markAllVisible();                       // the root peek un-isolates
  else { markActivePathVisible(); markSubtreeVisible(prevViewTok); }
  revealActiveSpine(); render();
  post({action: "peek", token: tok});
}

// ── networking ───────────────────────────────────────────────────────────────
function post(body){
  if (suspended && SUSPEND_BLOCKED_ACTIONS.has(body.action)) return;
  fetch(API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)}).catch(()=>{});
}
// Actions the add-in forwarded because their behaviour lives here (see push_ui_action
// in server.py). Each carries a monotonic seq so it runs exactly once.
let lastUiSeq = null;
let forwardedDepth = 0;      // >0 while an action forwarded from the viewport is running
function runForwardedActions(s){
  // First poll adopts the current position without running anything: whatever is still
  // queued was pressed while this window was closed, and replaying it the moment the
  // dock opens would be a burst of stale navigation.
  if (lastUiSeq === null){ lastUiSeq = s.uiSeq || 0; return; }
  // The SERVER holds the seq, and reloading the add-in re-execs server.py and starts it
  // again from zero — which happens on every development cycle and every time anyone
  // presses Reload Recent AddIns. This counter only ever climbed, so after any reload
  // every genuinely new action looked older than what had already been seen and was
  // skipped for good: a component created right after a reload silently never got its
  // Bounds folder. A seq that has gone BACKWARDS can only mean the server restarted, and
  // everything queued since then is new.
  if (typeof s.uiSeq === "number" && s.uiSeq < lastUiSeq) lastUiSeq = 0;
  for (const a of (s.uiActions || [])){
    if (!a || typeof a.seq !== "number" || a.seq <= lastUiSeq) continue;
    lastUiSeq = a.seq;
    const fn = ACTION_IMPL[a.id];
    if (!fn) continue;
    // Marked while a forwarded action runs, so anything it opens knows the keystroke
    // came from the viewport and this window does not currently hold the keyboard.
    forwardedDepth++;
    try { fn(null, a.arg); } finally { forwardedDepth--; }   // null event = forwarded
  }
  if (typeof s.uiSeq === "number" && s.uiSeq > lastUiSeq) lastUiSeq = s.uiSeq;
}

// Deliberate level jumps (go to top / go to bottom), stamped by the add-in the moment
// the command starts. They must tidy the tree even when they happen on the way out of a
// sketch, whose open-path protection would otherwise refuse every collapse — see
// note_nav_jump() in server.py. Read outside the change-guard, like the forwarded
// actions: the stamp usually lands a poll BEFORE the activation it belongs to, and it
// has to be armed by the time that arrives.
let lastNavSeq   = null;
let forceTidyUntil = 0;
const FORCE_TIDY_MS = 4000;   // matches the sketch-exit protection window it overrides
function noteNavJump(s){
  const n = s.navSeq || 0;
  // First poll adopts the position without arming: whatever is banked was pressed
  // while this window was closed.
  // Below ours is a restarted server (webwin.seq_base): re-adopt, as lastUiSeq does.
  if (lastNavSeq === null || n < lastNavSeq){ lastNavSeq = n; return; }
  if (n > lastNavSeq){ lastNavSeq = n; forceTidyUntil = Date.now() + FORCE_TIDY_MS; }
}

// Which panel the dock shows (Browser / Timeline). Both pages report it to the shell
// (FusionDockApp), which swaps the web views; the visible page sees a change first.
let lastDockMode = null;
function noteDockMode(m){
  if (!m || m === lastDockMode) return;
  lastDockMode = m;
  try { window.webkit.messageHandlers.dockMode.postMessage(m); } catch(_){}
}
// bin/fusion_busy's spinner by the pointer, on or off (server.py BUSY_OFF). /state
// carries the setting; the tick flips it at once and the poll confirms.
let busySpinner = true;
function toggleBusySpinner(){
  busySpinner = !busySpinner;
  fetch(API + "/busyspinner", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({on: busySpinner})}).catch(()=>{});
}
function toggleDockMode(){
  fetch(TIMELINE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"toggle"})}).catch(()=>{});
}

// ── Extrude open: bodies are what it works on ─────────────────────────────────
// While the Extrude add-in's command runs, a body row clicked here (or a group — every
// body in it, as one) goes IN or OUT of what the join/cut/intersect works on, instead of
// being selected; a sketch still adds its profiles (selectsketch → _extrude_pick). The
// add-in reports the bodies taking part as row keys, and they are marked `xpart`.
// Outside the tree hash: it changes every keystroke of a distance, the tree does not.
let extrude = { on:false, keys:new Set() };
let extrudeSig = "";
function noteExtrude(x){
  const on = !!(x && x.on), keys = (x && x.keys) || [];
  const sig = JSON.stringify([on, keys]);
  if (sig === extrudeSig) return;
  extrudeSig = sig;
  extrude = { on, keys:new Set(keys) };
  render();
}
// Handles the click when an extrude is open and the row is a body or holds bodies.
function extrudeClick(n){
  if (!extrude.on || !n) return false;
  const isGroup = n.type === "folder" || n.type === "namegroup" || n.type === "geometry";
  if (n.type !== "body" && !isGroup) return false;
  const bodies = leavesUnder(n).filter(l => l.type === "body");
  if (!bodies.length) return false;          // an empty or sketch-only group folds as usual
  // A folder says which: with "Bodies are: Group" the extrude follows that group itself.
  post({action:"extrudebodies", group: n.type === "folder" ? folderIdOf(n.token) : "",
        items: bodies.map(b => ({ token:b.token, occToken:b.occToken || "" }))});
  return true;
}

async function pollState(){
  try {
    const r = await fetch(API + "/state", {cache:"no-store"});
    const s = await r.json();
    // Clicking in the Fusion viewport must dismiss the context menu. The window never
    // receives those clicks, so the add-in stamps them and we watch for a newer one.
    // Checked outside the change-guard below, since clickTs isn't part of that hash.
    if (typeof s.clickTs === "number" && s.clickTs){
      if (ctxOpen() && ctxOpenedAt && s.clickTs > ctxOpenedAt + CTX_ARM_GRACE_MS) hideCtx();
      if (s.clickTs > lastClickTs && lastClickTs) canvasClickDeselect(s);
      lastClickTs = s.clickTs;
    }
    // Actions pressed while the VIEWPORT had focus. Also outside the change-guard: a
    // keystroke is not part of the tree hash and must never wait for the tree to move.
    runForwardedActions(s);
    noteSketchDeps(s.sketchDeps);
    noteGroupUses(s.groupUses);
    noteNavJump(s);
    noteDockMode(s.dockMode);
    if (typeof s.busySpinner === "boolean") busySpinner = s.busySpinner;
    noteExtrude(s.extrude);
    // selPick/selQuiet/selFeats too: another face of the same body leaves selTokens
    // unchanged (it's the same body row) but traces to a different feature, and without
    // them that pick never reached the selection mirror below.
    const j = JSON.stringify([s.tree, s.activeToken, s.selTokens, s.selPick, s.selQuiet, s.selFeats,
                              s.toggles, s.editingSketch, s.editSketchToken, s.rolledBack,
                              s.comps]);
    // An optimistic visibility override that has outlived its timeout is also a change:
    // otherwise it is only ever dropped when the tree next moves, and a quiet tree kept
    // it for good (going to the top level marked a hidden component visible, and it
    // stayed un-struck until a click changed the state).
    if (j !== lastJSON || pendingVisExpired() || pendingRenameExpired()){
      lastJSON = j; state = s;
      rawTree = s.tree || [];                 // server truth, kept for folder rebuilds
      rebuildKeyIndex(rawTree);               // the durable keys this tree carries…
      migrateLeafKeys();                      // (a rename shows up here as a new key)
      migrateCompRefs();                      // (legacy owner tokens -> component ids)
      fileBoundsPending();                    // a bounds filing that was waiting on the overlay
      keepSketchesFiled();                    // every sketch on its component's Sketches side
      hydrateOverlays();                      // …and the stored overlays they unlock
      rebuildIdentIndex(); rebuildNameIndex(); // owners come and go with the timeline
      state.tree = withFolders(rawTree);      // + this window's virtual folders
      reconcilePendingDisregard();
      if ((s.doc_id || "") !== foldersDoc) loadFolders(s.doc_id || "");
      reconcilePendingVis(); reconcilePendingDelete(); reconcilePendingRename();
      // Honor an optimistic activation until the server catches up — a poll can land
      // after we've optimistically switched but before the add-in has processed our
      // activate, which would otherwise flash the UI back to the previous active
      // component and then forward again. Hold our value until confirmed (or 3 s).
      if (pendingActivate){
        if (state.activeToken === pendingActivate.token){
          pendingActivate = null;
          // A selection pushed right behind the activation (F's top-row pick) only
          // reaches Fusion once the activation has finished — the add-in runs them in
          // order — so its echo window starts now, not when it was posted.
          if (pendingSel) pendingSel.ts = pendingSel.sentTs = Date.now();
        }
        else if (Date.now() - pendingActivate.ts < ACTIVATE_HOLD_MS) state.activeToken = pendingActivate.token;
        else pendingActivate = null;
      }
      // Same for a peek (suspend mode): hold ours until the add-in reports it.
      if (pendingPeek){
        if (state.peekToken === pendingPeek.token) pendingPeek = null;
        else if (Date.now() - pendingPeek.ts < ACTIVATE_HOLD_MS) state.peekToken = pendingPeek.token;
        else pendingPeek = null;
      }
      // Same idea for an optimistic sketch-edit entry (browser click) — hold the
      // bold+underline emphasis on the clicked sketch row (and keep its owning
      // occurrence active) until the add-in actually opens the sketch, so the text
      // updates immediately instead of after the viewport catches up.
      if (pendingEditSketch){
        if (state.editingSketch && state.editSketchToken === pendingEditSketch.token)
          pendingEditSketch = null;
        else if (Date.now() - pendingEditSketch.ts < 3000){
          state.editingSketch = true;
          state.editSketchToken = pendingEditSketch.token;
          if (pendingEditSketch.occToken) state.activeToken = pendingEditSketch.occToken;
        } else pendingEditSketch = null;
      }
      // Leaving a sketch must leave the browser sitting IN the component that sketch
      // belongs to, with its contents still shown — never tidied up a level. The spine
      // tidy below collapses off-path branches, so re-open the owner's path after it
      // and for a short grace window afterwards (the activation that follows an exit
      // can land a beat later than the editingSketch flag).
      // A go-to-top / go-to-bottom pressed FROM INSIDE a sketch exits the sketch AND
      // changes level in one action, so it arrives looking exactly like a plain sketch
      // exit and used to be handled as one: the owner's path was protected, every
      // tidyCollapse below was refused, and the tree stayed open on the component just
      // left — while the same jump made outside a sketch tidied normally. The add-in's
      // stamp is what tells the two apart, and while it stands the protection below is
      // never armed in the first place. It has to be suppressed rather than cleared on
      // the way past: exiting a sketch takes a few polls, and the transient parent
      // activations in between are each a jump of their own — clearing on the first one
      // let the next poll re-protect, which is exactly what the protection is for.
      const forceTidy = Date.now() < forceTidyUntil;

      if (state.editingSketch && state.activeToken){
        editOwnerTok = state.activeToken;
        if (!forceTidy) protectPath(editOwnerTok, 4000);   // re-armed every poll of the edit
      }
      const exitedSketch = prevEditing && !state.editingSketch;
      prevEditing = !!state.editingSketch;
      // Landing at the document root is never a plain sketch exit — leaving a sketch
      // puts you in the component that OWNS it, never above it — so an exit that
      // arrives with no active component is a level jump whatever the stamp says.
      // Belt and braces for the stamp arriving late (a queued command's
      // commandStarting runs a turn after the optimistic patch that precedes it).
      const jumpedOut = exitedSketch && !state.activeToken;
      if (exitedSketch && editOwnerTok && !forceTidy && !jumpedOut)
        protectPath(editOwnerTok, 4000);

      // A document arriving — Fusion just opened it, or this window came up with one
      // already open — gets the same tidy as a jump, and a stricter one: everything
      // collapsed but the spine, rooted on whatever component Fusion restored as active.
      // It used to wait for activeToken to CHANGE, and the empty pre-document state and
      // the opened document can both report the root, so the tree came up with every
      // row open as last saved and the top level showing. Held until this document's
      // folder overlay has landed (its folders are rows the tidy has to close) and the
      // active occurrence is in the tree; lastJSON is cleared so the next poll re-checks.
      const docKey = s.doc_id || "";
      let docArrived = false;
      if (docKey && docKey !== spineDoc && state.tree.length){
        if (!docSeenAt || docSeenDoc !== docKey){ docSeenDoc = docKey; docSeenAt = Date.now(); }
        const overlayIn = foldersDoc === docKey && (foldersReady || Date.now() - docSeenAt > 2000);
        const activeIn = !state.activeToken || activePathList().length > 0;
        if (overlayIn && (activeIn || Date.now() - docSeenAt > 5000)){
          spineDoc = docKey; docArrived = true;
          collapseAllButGeometry();
        } else lastJSON = "";
      }
      // Viewport jumped to a different component → re-tidy to a clean spine so the
      // active component (and only it) is expanded with its contents on show.
      const jumpedComp = state.activeToken !== prevActiveToken || docArrived;
      if (jumpedComp){
        const oldTok = prevActiveToken;
        prevActiveToken = state.activeToken;
        // Drop any open-path protection first. It exists so the tidy that follows an
        // activation can't shut a group we just opened to show a pick — a within-one-
        // level concern. Moving to another level is exactly when the tidy is the whole
        // point: with a face picked, go-to-top ran inside the 4 s protection the pick
        // had armed, so every tidyCollapse was refused and the browser stayed open on
        // the component you had just left. Exiting a sketch re-arms its own protection
        // just above, and that one has to survive the activation that follows.
        if (!exitedSketch || forceTidy || jumpedOut){ protectOpen = new Set(); protectUntil = 0; }
        // The viewport moved you to another component, so every claim this window was
        // making about the selection belonged to the level you left. Resynchronise to
        // what the viewport actually holds, rather than trying to carry any of it over.
        //
        // A GROUP is why this has to be a reset and not an expiry. It has no Fusion
        // entity, so selecting one pushes an empty selection to the viewport, which
        // is indistinguishable from "nothing is selected". The
        // pending-selection hold then matched an empty viewport for ever, and since
        // the key never changed again after that the mirror was never consulted a
        // second time: the group stayed lit from the far side of the jump.
        //
        // Browser-driven activations set prevActiveToken themselves and manage their
        // own selection, so they never reach this.
        selHold = null; pendingSel = null;
        selected = new Set(state.selTokens || []);
        cursorTok = selected.size ? [...selected][selected.size - 1] : null;
        selAnchor = cursorTok;
        prevSelKey = JSON.stringify(state.selTokens || []);
        prevPickKey = JSON.stringify(state.selPick || {});
        revealActiveSpine();
        // Viewport-driven jump (a browser-driven one already set prevActiveToken, so this
        // only runs for viewport changes): optimistically fix visibility so greying /
        // ungreying lands in the SAME render as the spine + bold, instead of trailing until
        // the server's n.visible catches up.
        markActivePathVisible();                                   // un-grey the new active path
        if (!state.activeToken) markAllVisible();                  // → root: un-isolate everything
        else if (oldTok && isDescendantTok(state.activeToken, oldTok))
          markSubtreeVisible(state.activeToken);                   // went UP: un-grey the new subtree
      }
      // A peek made from the VIEWPORT (Go Up / Go Down keys in suspend mode) changes
      // no active component, so it re-roots here instead. A window-made one already
      // set prevViewTok in peekTo.
      const vt = viewTok();
      if (vt !== prevViewTok){
        prevViewTok = vt;
        if (!jumpedComp){
          revealActiveSpine();
          markActivePathVisible();
          if (!vt) markAllVisible(); else markSubtreeVisible(vt);
        }
      }
      // Belt and braces: if something collapsed the owner's path anyway (a tidy that
      // landed outside the protected window), put it back on the way out of the edit.
      // Not after a level jump — there the collapse is the point.
      if (exitedSketch && editOwnerTok && !forceTidy && !jumpedOut){
        for (const t of pathToTok(editOwnerTok)){ collapsed.delete(t); cancelCollapseTimer(t); }
        saveCollapsed();
      }

      // Mirror the viewport selection as a real browser selection (replacing it),
      // but only when it actually changes — so Cmd-accumulated browser-only
      // selections survive until the next viewport selection.
      const selKey = JSON.stringify(state.selTokens || [])
                   + (state.selQuiet ? "#" + JSON.stringify(state.selFeats || []) : "");
      // Another face of the same body is the same row but a new pick: it may trace to
      // a different feature (openPickedFeature).
      const pickKey = JSON.stringify(state.selPick || {});
      if (selKey !== prevSelKey || pickKey !== prevPickKey){
        prevSelKey = selKey; prevPickKey = pickKey;
        if (renameHeld()){
          // The echo of a rename this window just made — not a pick (see renameHold).
          selected = new Set(renameHold.toks);
          cursorTok = renameHold.cursor;
        } else if (state.selQuiet){
          // Timeline: Select Feature Behind Pick. The viewport holds a feature and its
          // sketch; here it is the FEATURE's rows that are picked (under bodies already
          // opened with F), never the sketch's — and nothing is opened or scrolled to
          // them: seen if they're already showing, picked either way.
          pendingSel = null; selHold = null;
          selected = new Set(quietFeatRows(state.selFeats || []));
          const picked = [...selected];
          cursorTok = picked.length ? picked[picked.length - 1] : null;
          selAnchor = cursorTok;
        } else if (selHold && (state.selTokens || []).every(t => selHold.has(t))){
          // Held across Fusion's hide-deselect: hiding a row drops it from Fusion's
          // own selection, so what comes back is a SUBSET of what we hold (usually
          // nothing at all) and this window stays the only place those rows are still
          // picked. A selection carrying anything we are not holding is a real new
          // pick — the hold has done its job and gets out of the way. Without that
          // test the hold outlived its toggle: it had no expiry and only a
          // browser-side action cleared it, so every later viewport pick anywhere in
          // the document came back as the rows hidden minutes ago.
          selected = new Set(selHold);
        } else if (pendingSel && Date.now() - pendingSel.ts < 3000
                   && !(selKey === "[]" && pendingSel.key !== "[]" && pendingSel.sentTs
                        && Date.now() - pendingSel.sentTs > PUSH_ECHO_MS)){
          const held = pendingSel.toks;                       // OUR set stands either way
          if (selKey === pendingSel.key || pendingSel.anyEcho) pendingSel = null;   // viewport caught up
          selected = new Set(held);
        } else {
          pendingSel = null;
          selHold = null;                // a fresh pick releases the post-hide hold
          selected = new Set(state.selTokens || []);
          // The cursor is drawn EXACTLY like a selected row, so one left parked on
          // whatever this window last touched reads as "that is still selected" long
          // after the viewport moved on — and it only cleared itself when its row
          // stopped being drawn, so it survived precisely when you came back to the
          // component it was in. A viewport pick moves it: onto what was picked, or
          // nowhere when the viewport has nothing picked at all.
          const mirrored = [...selected];
          cursorTok = mirrored.length ? mirrored[mirrored.length - 1] : null;
          selAnchor = cursorTok;
          // Opening the groups above a pick is for finding it where you already are.
          // Clicking a body in a component you are NOT in jumps down into that
          // component, and arriving somewhere new should show that component tidy —
          // not with the group the body happens to live in already thrown open. The
          // spine tidy has just set the level up; leave it alone for this poll.
          openPickedFeature();
          if (!jumpedComp) revealSelection(state.selTokens || []);
        }
      }
      render();
    }
  } catch(e){}
}

// Optimistic visibility: toggling a complex component can take Fusion a moment to
// actually hide it in the viewport (and flip `visible`), which used to delay the
// row's colour change. We flip it locally right away and drop the override once
// Fusion confirms (or after a timeout if the toggle didn't take).
// An entry from a hide/show GESTURE also carries `user` — the intent it states — since
// rows strike from `userHidden` (flatten's selfOff), and that only comes back from the
// add-in's record on the round trip.
function applyPendingVis(){
  for (const tok in pendingVis){
    const n = nodeByTok(tok);
    if (!n) continue;
    n.visible = pendingVis[tok].visible;
    if (typeof pendingVis[tok].user === "boolean") n.userHidden = pendingVis[tok].user;
  }
}
function pendingVisExpired(){
  const now = Date.now();
  for (const tok in pendingVis) if (now - pendingVis[tok].ts > PENDING_VIS_MS) return true;
  return false;
}
function reconcilePendingVis(){
  const now = Date.now();
  for (const tok of Object.keys(pendingVis)){
    const n = nodeByTok(tok);                       // server-truth node (state just replaced)
    const p = pendingVis[tok];
    if (!n || (n.visible === p.visible && (typeof p.user !== "boolean" || !!n.userHidden === p.user))
        || now - p.ts > PENDING_VIS_MS)
      delete pendingVis[tok];
  }
  applyPendingVis();
}
// Optimistic delete: a deleted row disappears on the keypress rather than after the
// Fusion → poll round-trip, the same standing rule the rest of this window follows.
// The node is spliced out of the tree we render from; the override is dropped once
// the server's own tree no longer carries it (or after 5s, if the delete didn't take).
function applyPendingDelete(){
  const toks = new Set(Object.keys(pendingDelete));
  if (!toks.size || !state.tree) return;
  (function w(ns){
    for (let i = ns.length - 1; i >= 0; i--){
      if (toks.has(ns[i].token)) ns.splice(i, 1);
      else if (ns[i].children) w(ns[i].children);
    }
  })(state.tree);
}
// Optimistic rename: a component/sketch/body shows its new name on Enter, not when
// Fusion gets round to it (a busy document can take seconds). Held until the server's
// name moves off the old one (it may come back normalised, so any change counts), or
// for PENDING_RENAME_MS if the rename never takes — then the row shows what Fusion has.
const PENDING_RENAME_MS = 15000;
let pendingRename = {};           // token -> {name, was, ts}
function applyPendingRename(){
  for (const tok in pendingRename){
    const n = nodeByTok(tok);
    if (n) n.name = pendingRename[tok].name;
  }
}
function pendingRenameExpired(){
  const now = Date.now();
  for (const tok in pendingRename) if (now - pendingRename[tok].ts > PENDING_RENAME_MS) return true;
  return false;
}
function reconcilePendingRename(){
  const now = Date.now();
  for (const tok of Object.keys(pendingRename)){
    const n = nodeByTok(tok), p = pendingRename[tok];   // server-truth node
    if (!n || n.name !== p.was || now - p.ts > PENDING_RENAME_MS) delete pendingRename[tok];
  }
  applyPendingRename();
}
function reconcilePendingDelete(){
  const now = Date.now();
  for (const tok of Object.keys(pendingDelete)){
    if (!nodeByTok(tok) || now - pendingDelete[tok] > 5000) delete pendingDelete[tok];
  }
  applyPendingDelete();
}
// Parameters live on their own server and change far less often than the tree, so
// they get their own slower poll. A change rebuilds the display tree (they are placed
// by the same overlay the folders use).
async function pollParams(){
  try {
    const r = await (await fetch(PARAMS_API + "/params", {cache:"no-store"})).json();
    const rows = (r && r.params) || [];
    const j = JSON.stringify(rows);
    if (j !== lastParamsJSON){ lastParamsJSON = j; paramRows = rows; refreshFolders(); }
  } catch(_){}
}
function paramByName(name){ return paramRows.find(p => p.name === name) || null; }
function isParamTok(t){ return typeof t === "string" && t.startsWith("param:"); }
function paramNameOf(t){ return isParamTok(t) ? t.slice(6) : ""; }

async function pollHotkeys(){
  try { hotkeys = await (await fetch(API + "/hotkeys", {cache:"no-store"})).json(); } catch(e){}
  // Full Fusion keymap (re-read each time → picks up new/changed shortcuts live).
  // Normalize each sequence so it matches what seqOf() produces for a real keypress.
  try {
    const raw = await (await fetch(API + "/fusionkeys", {cache:"no-store"})).json();
    const nk = {}; for (const k in raw) nk[normSeq(k)] = raw[k]; fusionKeys = nk;
  } catch(e){}
}

// ── virtual folders ──────────────────────────────────────────────────────────
// Window-only subfolders inside a component. A folder owns no Fusion entity: it is a
// display container that sketches and bodies can be dragged into, so the browser can
// be organised without touching the model. Kept per document on the server
// (folders.json), the way the Params window keeps its groups.
//   folderDefs: id -> {id, name, comp, parent, hidden}
//                     comp   = owning occurrence token
//                     parent = enclosing folder id, or ""
//                     hidden = the folder's OWN hide — ONE FLAG, never copied onto
//                              its members.  It used to be copied: hiding a group
//                              wrote every member's bulb off and kept a `snap` of
//                              what each had been, which the add-in then read back
//                              as "the user hid these".  The add-in reads this flag
//                              directly now (see hidden_group_members in the
//                              visibility module) and derives the bulbs from it, so
//                              a group hide can no longer be mistaken for a hide of
//                              the things inside it.
//   leafFolder: durable leaf key -> folder id
// Ordering inside a folder rides on the existing siblingOrder overlay, keyed by the
// folder's own row token ("fld:<id>").
const FOLDER_PREFIX = "fld:";
// The Bodies group — the add-in's reflection of a component's Bodies folder ("bg:<id>",
// see Browser.py's _geometry_group_node). It was called Geometry until that name went to
// the timeline-features group below, and the code still says "geo" / "Geometry group"
// for it throughout: `type: "geometry"`, GEO_*, geoFolderOf. The timeline group is
// `type: "features"`, FX_*, openGeos. Every body draws inside it, and a folder can be
// filed inside it too: such a folder's `parent` is the GEO_PARENT sentinel rather than
// another folder's id (or "" for the component itself). Which side a folder is on
// decides what it can hold — bodies in the Geometry group, sketches out of it — so a
// body's membership in a folder on the wrong side is kept but not drawn (it shows loose
// in the group), the same way a folder moved to another component keeps its filings.
const GEO_PREFIX = "bg:";
const GEO_PARENT = "geo";
function isGeoTok(t){ return typeof t === "string" && t.startsWith(GEO_PREFIX); }
// The Geometry group ("fx:<id>", Browser.py's _features_group_node) is the OTHER group:
// the component's timeline features, drawn right under Bodies. The add-in sends it
// empty; graftFeatures fills it from the Timeline add-in while it is open.
const FX_PREFIX = "fx:";
let rawTree    = [];      // the server's tree, before the folder overlay is applied
let folderDefs = {};
// Membership is persisted under the DURABLE KEY the add-in stamps on every leaf row
// ("<component id>|sk|<name>"), not the entityToken it used to use: 23 of 159
// memberships in one live document had already stopped resolving, and each dead one
// is an item that silently left its group and could never be given back. Tokens are
// still the row identity inside this window — they just don't outlive the session.
let leafFolder = {};      // durable leaf key -> folder id
let keyByTok   = {};      // row token -> durable key, rebuilt on every tree
let tokByKey   = {};      // and back the other way
let tokSeen    = new Set();  // every token in the current tree, keyed or not
// Set once this document's membership has been rewritten to durable keys, and saved
// alongside it. The add-in waits on this flag before it touches a single leaf bulb —
// see groups_pending: with the overlay still keyed by token the groups read as EMPTY,
// and every member of a hidden group would look like a bulb the user turned off.
// It is a flag rather than "are there any legacy keys left?" because some never
// resolve — 23 in one live document — and those would hold the wait open forever.
let foldersKeyed = false;
// Set once this document's body folders have been moved into the Geometry group (see
// migrateGeoFolders), and saved alongside.
let foldersGeo = false;
// Set once this document's folders have been split into parameter groups and the rest
// (see migrateParamFolders), and saved alongside.
let foldersSplit = false;
// Set once this document's existing bounds parameters have been given the one-word
// reading (ensureBoundsShow), over any show entry they already had; saved alongside.
let foldersBoundsShown = false;
function folderIdOfLeaf(tok){ const k = keyByTok[tok]; return k ? leafFolder[k] : undefined; }
function setLeafFolder(tok, fid){
  const k = keyByTok[tok]; if (!k) return;
  if (fid) leafFolder[k] = fid; else delete leafFolder[k];
}
// A body without an id of its own is keyed by name + '#n', its place among same-named
// twins in Fusion's body order — which shifts on recompute, swapping twins' groups. So
// filing one by hand asks the add-in to stamp ids on its component's bodies
// (fvs.stamp_bodies); the rows come back keyed '<cid>|bd|@<id>' under the same tokens
// and rebuildKeyIndex carries the filings over. Only on a gesture: a stamp is an undo
// step, and one made on every refresh would re-stamp whatever ⌘Z just took off.
let stampComps = null;
function stampBodyOf(tok){
  const k = keyByTok[tok] || "", parts = k.split("|");
  if (parts.length < 3 || parts[1] !== "bd" || parts[2].startsWith("@")) return;
  if (!stampComps){
    stampComps = new Set();
    setTimeout(() => { const comps = [...stampComps]; stampComps = null;
                       post({action:"stampbodies", comps}); }, 0);
  }
  stampComps.add(parts[0]);
}
// Memberships persisted under the old entityToken are rewritten to the durable key
// the moment the row carrying that token is drawn. This window is the only place that
// holds both, which is why the migration lives here rather than in the add-in — and
// why the add-in refuses to touch any leaf bulb until it has happened (groups_pending).
// A token that isn't in the tree is left alone rather than dropped: it may be an item
// the timeline is currently rolled back past, and only the token was ever stored, so
// deleting it would throw away a membership that could still come back.
function migrateLeafKeys(){
  if (!rawTree.length || !foldersReady) return;    // no tree, or no overlay, to key against
  let changed = false;
  for (const k of Object.keys(leafFolder)){
    if (k.split("|").length >= 3) continue;        // already a durable key
    const key = keyByTok[k];
    if (!key) continue;
    leafFolder[key] = leafFolder[k];
    delete leafFolder[k];
    changed = true;
  }
  if (!foldersKeyed){ foldersKeyed = true; changed = true; }
  if (changed) saveFolders();
}
// The one-time move of body folders into the Geometry group. Before the group existed
// a folder at the top of a component could hold bodies; now a body is only drawn in a
// folder on the Geometry side, so each top-level folder whose subtree holds bodies and
// no sketches is re-parented into the group, subfolders following. A folder that mixes
// the two stays where it is and its bodies show loose in the group — there is no right
// answer for it, and the filing is kept in case one is chosen by hand.
// Runs on the membership alone, no tree needed — a key names its kind ("<cid>|bd|…").
function migrateGeoFolders(){
  if (!foldersReady || foldersGeo) return;
  const kinds = {};                                   // folder id -> Set of "bd"/"sk"
  for (const k of Object.keys(leafFolder)){
    const parts = k.split("|");
    if (parts.length < 3) continue;
    (kinds[leafFolder[k]] = kinds[leafFolder[k]] || new Set()).add(parts[1]);
  }
  const under = fid => {                              // kinds in fid's whole subtree
    const out = new Set(kinds[fid] || []);
    for (const g of Object.values(folderDefs))
      if (g.parent === fid) for (const x of under(g.id)) out.add(x);
    return out;
  };
  for (const f of Object.values(folderDefs)){
    if (f.parent) continue;
    const ks = under(f.id);
    if (ks.has("bd") && !ks.has("sk")) f.parent = GEO_PARENT;
  }
  foldersGeo = true;
  saveFolders();
}
// The one-time split into parameter groups (see isParamFolderDef). A folder whose
// subtree holds only parameters becomes a parameter group; one holding only sketches or
// bodies stays as it is; one holding BOTH (every Bounds folder, and a few hand-made ones)
// stays a sketch folder and gets a parameter-group twin of the same name, in the same
// component, which takes its parameters. A parameter group that was nested in a sketch
// folder moves to that folder's twin, or up to the component if it has none. An empty
// folder goes by how it was ignored: ignored only while geometry was on show means it
// was being kept for parameters.
function migrateParamFolders(){
  if (!foldersReady || foldersSplit) return;
  const hasP = {}, hasL = {};
  for (const ref of Object.values(paramPlace))
    if (isFolderTok(ref)) hasP[folderIdOf(ref)] = true;
  for (const fid of Object.values(leafFolder)) hasL[fid] = true;
  const kids = {};
  for (const f of Object.values(folderDefs))
    if (f.parent && folderDefs[f.parent]) (kids[f.parent] = kids[f.parent] || []).push(f.id);
  const sub = (fid, own, seen = new Set()) => {
    if (seen.has(fid)) return false; seen.add(fid);
    return !!own[fid] || (kids[fid] || []).some(k => sub(k, own, seen));
  };
  const depth = f => { let d = 0; for (let p = f.parent; folderDefs[p] && d < 64; p = folderDefs[p].parent) d++; return d; };
  const defs = Object.values(folderDefs).sort((a, b) => depth(a) - depth(b));
  const kindOf = {};
  for (const f of defs){
    const p = sub(f.id, hasP), l = sub(f.id, hasL);
    kindOf[f.id] = (p && l) ? "mixed" : p ? "params" : l ? ""
                 : (f.ignoreGeo && !f.ignoreParams) ? "params" : "";
    // Nothing on the Geometry side can be a parameter group.
    if (kindOf[f.id] === "params" && geoChain(f)) kindOf[f.id] = "mixed";
  }
  function geoChain(f){
    for (let i = 0; f && i < 64; f = folderDefs[f.parent], i++)
      if (f.parent === GEO_PARENT) return true;
    return false;
  }
  const twin = {};
  const paramParent = pid => {
    for (let i = 0; pid && folderDefs[pid] && i < 64; pid = folderDefs[pid].parent, i++){
      if (isParamFolderDef(folderDefs[pid])) return pid;
      if (twin[pid]) return twin[pid];
    }
    return "";
  };
  for (const f of defs){
    if (kindOf[f.id] === "params"){
      f.kind = "params";
      f.parent = paramParent(f.parent);
      f.hidden = false;
    } else if (kindOf[f.id] === "mixed"){
      const id = newFolderId(), from = FOLDER_PREFIX + f.id, to = FOLDER_PREFIX + id;
      folderDefs[id] = { id, name: f.name, comp: f.comp, parent: paramParent(f.parent),
                         hidden: false, kind: "params", ignoreGeo: false, ignoreParams: false };
      twin[f.id] = id;
      for (const k of Object.keys(paramPlace)) if (paramPlace[k] === from) paramPlace[k] = to;
      if (collapsed.has(from)) collapsed.add(to);
    }
  }
  foldersSplit = true;
  saveCollapsed();
  saveFolders();
}
// Component rows, by the two handles a component row carries: its within-session
// occurrence token and its component id. The id is the one that lasts — it survives a
// rename, a reparent (which makes a NEW occurrence, with a new token) and a reopen —
// so it is what folder ownership and parameter filing are persisted under (compRef).
let compByTok = {};
let compByCid = {};
function rebuildKeyIndex(nodes){
  const prev = keyByTok;
  keyByTok = {}; tokByKey = {}; tokSeen = new Set(); compByTok = {}; compByCid = {};
  const twoKeys = new Set();       // tokens drawn under two different keys this tree
  (function w(ns){ for (const n of (ns||[])){
    tokSeen.add(n.token);
    if (n.key){
      if (keyByTok[n.token] && keyByTok[n.token] !== n.key) twoKeys.add(n.token);
      keyByTok[n.token] = n.key; tokByKey[n.key] = n.token;
    }
    if (n.type === "component"){
      compByTok[n.token] = n;
      // The first occurrence wins: a component placed several times draws its folders
      // under every one of them, and anything that wants ONE row gets the first.
      if (n.compId && !compByCid[n.compId]) compByCid[n.compId] = n;
    }
    w(n.children); } })(nodes);
  // A row's durable key carries its NAME (component id, kind, name — see fvs._leaf_key),
  // so renaming a sketch gives it a new key, and everything filed under the old one —
  // its group, its collapse state, its place in the order — silently let go of it. The
  // token, though, is the same object before and after within a session. So: the same
  // token arriving with a different key IS a rename, and the overlays follow it — unless
  // the token is on two rows at once: that is a stale token (Browser.py's _tok_cache
  // once handed a neighbour's out), and carrying on it moved filings onto the wrong row.
  let moved = false;
  const moves = [];
  for (const tok of Object.keys(keyByTok)){
    if (twoKeys.has(tok)) continue;
    const was = prev[tok], now = keyByTok[tok];
    if (was && was !== now) moves.push([was, now]);
  }
  // Fusion lets two bodies share a name, so a repeat's key ends "#2", "#3" — its place
  // among the same-named (fvs._leaf_key). Rename or delete the first of two and the
  // second shifts onto the plain key: one row's old key is another's new one. A key
  // still drawn is carried only when a mover has arrived on it; one still held by a
  // row that did NOT move isn't this row's to take.
  const arriving = new Set(moves.map(m => m[1]));
  const carry = moves.filter(([was]) => !tokByKey[was] || arriving.has(was));
  // A filing that moved key leaves the add-in's group policy reading the old one until
  // the save lands (bodies just stamped with ids — stampBodyOf — all move at once), so
  // it re-runs on the new file.
  const refile = carry.some(([was]) => was in leafFolder);
  if (carry.length){ carryKeys(carry); moved = true; }
  if (carryReparentedKeys()) moved = true;
  if (moved){ saveFolders(); saveOrder(); saveCollapsed(); }
  if (refile) post({action:"regroup"});
}
// A component's key is the chain of ids from the top ("oc|<cid>:1/<cid>:2" — see
// Browser.py's _occ_key), so REPARENTING it changes its key, and — because Fusion makes
// a new occurrence for the move — its token too. The rename carry above sees neither
// side of that. What does survive is the key's LAST segment, the component's own id and
// instance number: a stored key that no longer resolves is carried to the one live key
// that ends the same way, provided nothing is stored under that key already. That
// brings its own children's order, its collapse state and its expand stamp along;
// everything keyed by its descendants follows on the next pass, since their keys start
// with its.
function carryReparentedKeys(){
  const segOf = k => k.slice(3).split("/").pop();   // past "oc|", the own id:instance
  const live = {};                       // last segment -> [live keys ending in it]
  for (const k of Object.keys(tokByKey)){
    if (!k.startsWith("oc|")) continue;
    const seg = segOf(k);
    (live[seg] = live[seg] || []).push(k);
  }
  const stored = new Set(Object.keys(ordKeys));
  for (const p of Object.keys(ordKeys)) for (const c of ordKeys[p]) stored.add(c);
  for (const k of colKeys) stored.add(k);
  for (const k of Object.keys(seqKeys)) stored.add(k);
  let moved = false;
  for (const k of stored){
    if (!k.startsWith("oc|") || tokByKey[k]) continue;
    const cands = live[segOf(k)] || [];
    if (cands.length !== 1 || stored.has(cands[0])) continue;
    carryKey(k, cands[0]); stored.add(cands[0]); moved = true;
  }
  return moved;
}
// carryKey for several rows AT ONCE — through a parking key each, so a move onto a
// key another move is leaving never overwrites what that one still has to carry.
// A leaf's filing goes over whole: an unfiled row leaves its new key unfiled, rather
// than inheriting whatever a deleted same-named body left there.
function carryKeys(moves){
  const filed = moves.map(([was]) => was in leafFolder);
  const park = moves.map((_m, i) => "\u0001carry" + i);
  moves.forEach(([was], i) => carryKey(was, park[i]));
  moves.forEach(([, now], i) => {
    carryKey(park[i], now);
    if (!filed[i]) delete leafFolder[now];
  });
}
// Move every persisted overlay entry from one durable key to another.
function carryKey(was, now){
  if (was in leafFolder){ leafFolder[now] = leafFolder[was]; delete leafFolder[was]; }
  if (colKeys.has(was)){ colKeys.delete(was); colKeys.add(now); }
  if (hydCollapsed.has(was)){ hydCollapsed.delete(was); hydCollapsed.add(now); }
  if (was in seqKeys){ seqKeys[now] = seqKeys[was]; delete seqKeys[was]; }
  if (hydSeq.has(was)){ hydSeq.delete(was); hydSeq.add(now); }
  if (was in ordKeys){ ordKeys[now] = ordKeys[was]; delete ordKeys[was]; }
  for (const p of Object.keys(ordKeys))
    ordKeys[p] = ordKeys[p].map(c => c === was ? now : c);
  const carried = new Set();
  for (const h of hydOrder){
    const [pk, ck] = h.split("\u0000");
    carried.add((pk === was ? now : pk) + "\u0000" + (ck === was ? now : ck));
  }
  hydOrder.clear(); for (const h of carried) hydOrder.add(h);
  for (const t of Object.keys(keySrc)) if (keySrc[t] === was) keySrc[t] = now;
}

// ── the row overlays, persisted by durable key ───────────────────────────────
// siblingOrder, collapsed and expandSeq are the same story as leafFolder above: they
// were keyed by entityToken, so a fortnight later a hand-ordered component was back in
// Fusion's order and a tidied tree had sprung open again, with nothing to show why.
// They stay TOKEN-keyed in memory — they are read from forty-odd places on every render
// and a lookup that had to translate would be forty-odd places to get wrong — and are
// translated at the two edges instead: saveKeyOf on the way out, rowTokOf on the way in.
//
// Rows that already carry a durable identity of their own pass straight through: a
// folder's "fld:<id>" and a parameter's "param:<name>" are this window's own inventions
// and never rot, and the root sentinel stands for the document itself.
let keySrc = {};                      // token -> the key it came in under, so a row that
                                      // leaves the tree (another document, a rolled-back
                                      // timeline) is still written out durably
const hydOrder     = new Set();       // "<parent key>\u0000<child key>" already placed
const hydCollapsed = new Set();       // collapsed keys already applied
const hydSeq       = new Set();       // expand stamps already applied
// A name group's token is spelled out of the owner ref and the level words, so it is
// as durable as the names themselves — nothing to translate, like a folder's id.
function isOwnKey(t){
  return t === ROOT_TOKEN || isFolderTok(t) || isParamTok(t) || isNameTok(t);
}
function saveKeyOf(tok){
  if (!tok || isOwnKey(tok)) return tok;
  const k = keyByTok[tok];
  if (k){ keySrc[tok] = k; return k; }
  return keySrc[tok] || tok;          // never had a key: keep the token, upgrade later
}
function rowTokOf(key){
  if (!key) return "";
  if (isOwnKey(key)) return key;
  const t = tokByKey[key];
  if (t) return t;
  return tokSeen.has(key) ? key : "";  // a legacy token, still live in this tree
}
// Bring the persisted overlays onto the tokens of the tree that has just arrived.
// Runs on every tree rather than once, because a key that doesn't resolve today —
// the timeline is rolled back past its row — resolves the moment the row comes back.
// Each entry is applied ONCE and then remembered as applied: re-applying would drag a
// row the user has since moved, or sprung open, back to where storage last saw it.
// Whatever never resolves is left in the store untouched rather than dropped: only the
// token was ever written down, and throwing it away would end the membership for good.
// The one-time migration is the same machinery — a stored key that IS a live token
// resolves to itself, and the save below writes it back out under its durable key.
function hydrateOverlays(){
  let changed = false;
  for (const pkey of Object.keys(ordKeys)){
    const p = rowTokOf(pkey);
    if (!p) continue;
    // Re-key the stored entry the moment its parent resolves, rather than waiting for
    // the save below: a list whose children are ALL out of the tree never reaches
    // memory at all, and would sit there under a rotting token until they came back —
    // by which time the token it is filed under may have rotted too.
    const k = saveKeyOf(p);
    if (k !== pkey){
      const dst = ordKeys[k];
      ordKeys[k] = dst ? dst.concat(ordKeys[pkey].filter(c => dst.indexOf(c) < 0))
                       : ordKeys[pkey];
      delete ordKeys[pkey];
      changed = true;
    }
    const list = siblingOrder[p] || [];
    keySrc[p] = k;                               // so the save can retire this entry
    let at = 0;                                  // just past the last child we placed
    for (const ckey of (ordKeys[k] || [])){
      const t = rowTokOf(ckey);
      if (!t) continue;
      const i = list.indexOf(t);
      if (i >= 0){ at = Math.max(at, i + 1); hydOrder.add(k + "\u0000" + ckey); continue; }
      if (hydOrder.has(k + "\u0000" + ckey)) continue;
      hydOrder.add(k + "\u0000" + ckey); keySrc[t] = ckey;
      list.splice(at, 0, t); at++; changed = true;
    }
    if (list.length) siblingOrder[p] = list;
  }
  for (const k of colKeys){
    if (hydCollapsed.has(k)) continue;
    const t = rowTokOf(k); if (!t) continue;
    hydCollapsed.add(k); keySrc[t] = k; collapsed.add(t); changed = true;
  }
  for (const k of Object.keys(seqKeys)){
    if (hydSeq.has(k)) continue;
    const t = rowTokOf(k); if (!t) continue;
    hydSeq.add(k); keySrc[t] = k;
    if (!(t in expandSeq)) expandSeq[t] = seqKeys[k];
    changed = true;
  }
  if (changed){ saveOrder(); saveCollapsed(); }   // the rewrite, on the first tree
}
let foldersDoc = "";      // the document the loaded overlay belongs to
let foldersReady = false; // …and whether its overlay has actually arrived yet
let paramRows  = [];      // live rows from the Parameters server
let paramPlace = {};      // parameter name -> container ref ("" = the top level)
// parameter name -> {name?, type?, dir?}: the parts of its row the user chose to show
// (true) or hide (false) over ParamNaming's default reading — set by FusionValue's
// stepper (⌘E on a crumb chip, BrowserParamShowCmd), drawn by leafLabel, carried on
// rename with the filing. Absent = the default reading.
let paramShow = {};
let lastParamsJSON = "";
// ── compRef: how a container is named in the overlay ─────────────────────────
// A folder's owner (`comp`) and a parameter's container are persisted as a compRef:
//   ROOT_TOKEN          the document root
//   <component id>      a component — Fusion's Component.id, not the occurrence token
//   "fld:<id>"          (parameters only) a folder
// It used to be the occurrence entityToken. That is the handle every row is addressed
// by within a session, and it looked durable — it survives a save and a reopen — but a
// reparent in Fusion's browser or timeline makes a NEW occurrence, so one rearrange of
// the model left 24 of 25 owners in a live document pointing at occurrences that no
// longer existed: every folder vanished and every parameter filed in one went with it.
// Sketch and body membership never had the problem, being keyed by component id already
// (fvs._leaf_key); this puts folders and parameters on the same footing.
//   compRefOf(tok)  row token -> ref      compTokOf(ref)  ref -> the row's token
function compRefOf(tok){
  if (!tok || tok === ROOT_TOKEN) return ROOT_TOKEN;
  if (isFolderTok(tok)) return (folderOf(tok) || {}).comp || "";
  if (isGeoTok(tok)) return geoRefOf(nodeByTok(tok));
  const n = compByTok[tok];
  return n ? (n.compId || n.token) : "";
}
// A Geometry group's component, as a compRef: the root's group carries the root's own
// id, which has no row and so no ref of its own — it is spelled ROOT_TOKEN like the
// folders there (see sameOwner).
function geoRefOf(g){
  if (!g) return "";
  return g.occToken ? (g.compId || "") : ROOT_TOKEN;
}
function compTokOf(ref){
  if (!ref || ref === ROOT_TOKEN) return ROOT_TOKEN;
  const n = compByCid[ref];
  return n ? n.token : (compByTok[ref] ? ref : "");
}
// Where a parameter dropped on `containerTok` is filed: the folder itself, a
// component's ref, or "" for the top level.
function paramRefOf(containerTok){
  if (isFolderTok(containerTok)) return containerTok;
  const ref = compRefOf(containerTok);
  return (!ref || ref === ROOT_TOKEN) ? "" : ref;
}
// The one-time rewrite from occurrence tokens to component ids. It can only be done
// against a tree that still resolves the old token — which is why it runs on every tree
// rather than at load, and why folders.json was repaired by hand once for the document
// whose tokens had already died (the token is a gzipped record that happens to carry
// the component id, so the owner could still be read off it).
function migrateCompRefs(){
  if (!rawTree.length || !foldersReady) return;
  const fix = c => {
    if (!c || c === ROOT_TOKEN || isFolderTok(c) || compByCid[c]) return c;
    const n = compByTok[c];
    return (n && n.compId) ? n.compId : c;
  };
  let changed = false;
  for (const f of Object.values(folderDefs)){
    const c = fix(f.comp); if (c !== f.comp){ f.comp = c; changed = true; }
  }
  for (const k of Object.keys(paramPlace)){
    const c = fix(paramPlace[k]); if (c !== paramPlace[k]){ paramPlace[k] = c; changed = true; }
  }
  if (changed) saveFolders();
}
// Whether folders the user has ignored (see below) are drawn at all — off by default,
// since the whole point of ignoring a folder is to not see it. Deliberately NOT saved
// to localStorage: it's a temporary declutter for whatever component you're looking at
// right now, not a standing preference, so it never survives a reload either — see
// resetIgnoredOnComponentChange.
let showIgnoredFolders = false;
// Two kinds of folder, and they never mix. A PARAMETER GROUP (`kind: "params"`) holds
// parameters and other parameter groups, nothing else; every other folder holds
// sketches (or, inside Geometry, bodies) and other folders of its own kind. A parameter
// group sits in a component or another parameter group — never inside a sketch folder
// or the Geometry group — and a sketch folder never sits inside a parameter group.
// There used to be one kind of folder and a parameters/geometry MODE deciding which of
// its contents were drawn; the two lists are one now, so the folders had to pick a side.
function isParamFolderDef(f){ return !!f && f.kind === "params"; }
function isParamFolder(t){
  return isParamFolderDef(folderOf(typeof t === "string" ? t : (t && t.token)));
}
// Rows on the parameter side of a container: pinned below everything else in it.
function isParamSide(n){
  return !!n && (n.type === "param" || n.type === "namegroup"
                 || (n.type === "folder" && isParamFolder(n.token)));
}
// A folder can be marked "ignore" — the user's way of decluttering a group that is
// empty for now. The flag is stored under the key for its kind (`ignoreParams` for a
// parameter group, `ignoreGeo` for the rest — the names are from when one folder had
// one flag per mode). Cleared automatically the moment the folder has content (see
// reconcileIgnores) — a folder that holds something is never actually hidden by this,
// only ever offered the toggle while it's genuinely empty.
function ignoreKeyOf(f){ return isParamFolderDef(f) ? "ignoreParams" : "ignoreGeo"; }
function folderIgnored(n){
  const f = folderOf(n.token); if (!f) return false;
  return !!f[ignoreKeyOf(f)];
}
// "Show ignored folders" is scoped to the component you turned it on in: the moment
// the active component changes — up, down into a child, or anywhere else — it snaps
// back to its default of off, rather than lingering as a setting that quietly changes
// what a totally different component looks like. render() is the one place every
// activeToken change ends up (every call site that sets it goes on to redraw), so
// checking here catches all of them without hooking each one individually.
let _lastActiveForIgnore;
function resetIgnoredOnComponentChange(){
  if (state.activeToken === _lastActiveForIgnore) return;
  _lastActiveForIgnore = state.activeToken;
  if (showIgnoredFolders) showIgnoredFolders = false;
}

// A component's bounds — its face parameters, its derived spans and its three bounds
// sketches — live in a folder called this. They are scaffolding: correct to have, dull
// to look at, and they would otherwise sit at the top of every component you open. So
// they are hidden by default and the bar has a switch, exactly like empty folders.
const BOUNDS_FOLDER = "Bounds";
let showBoundsFolders = false;
try { showBoundsFolders = localStorage.getItem('browserShowBoundsFolders') === '1'; } catch(_){}
function isBoundsFolder(n){
  return n && n.type === "folder" && (n.name || "") === BOUNDS_FOLDER;
}
// Mirrors of Bounds.py's SKETCH_PREFIX and _BOUNDS_COMMENT — how a bounds sketch and a
// bounds/span parameter are recognised for the sweep below, independent of whichever
// path made them (New Component's own filing only fires for its own build).
const BOUNDS_SKETCH_PREFIX = "Bounds ";
const TETHERS_SKETCH_PREFIX = "Tethers ";
const REFS_SKETCH_PREFIX = "References ";   // Section.py's reference sketch, one per plane
const BOUNDS_COMMENT_RE =
  /^(Bounds|Span) of (.+?)(, derived from its faces)?(?: \[([^\]]+)\])?\s*$/;
// Master sketches (Masters.py) always live at the root, in one "Masters" folder. The
// bar switch is purely a display filter for that folder row, like showBoundsFolders —
// the sketches themselves stay hidden in the viewport except the one being edited
// (Visibility Snapshot.py's _rehide_bounds_sketches).
const MASTERS_FOLDER = "Masters";
const MASTER_SKETCH_PREFIX = "Master ";
function isMastersFolder(n){
  return n && n.type === "folder" && (n.name || "") === MASTERS_FOLDER;
}
let showMasterFolders = false;
try { showMasterFolders = localStorage.getItem('browserShowMasterFolders') === '1'; } catch(_){}
// The Origins bar switch: purely a display filter, like showIgnoredFolders/
// showBoundsFolders — it does not touch Fusion's origin folder. Forwarded to the
// add-in over /uiflags too, but only so it can skip reading seven bulbs a component
// when nobody asked to see them (see Browser.py's _leaf_nodes).
// Sketches: a display filter like the two above. Off drops every sketch — grouped or
// loose — and every sketch group (sketch-side folders — not parameter groups, not
// Bodies'), with what's inside, from the tree; the sketches' own visibility in the
// viewport is untouched. Stored as the old Hide Sketches key, so `hideSketches` is
// still the variable (its ticked state is the reverse). Reads the older Sketch Groups
// switch's key once, off = hidden.
let hideSketches = false;
try {
  const v = localStorage.getItem('browserHideSketches');
  hideSketches = v != null ? v === '1' : localStorage.getItem('browserShowSketchGroups') === '0';
} catch(_){}
// Bodies / Geometry: display filters for a component's two geometry groups — the
// bodies (bg:) and the timeline features (fx:). On by default; off drops the group
// row and everything in it from the tree, nothing more.
let showBodiesGroup = true, showGeometryGroup = true;
try {
  showBodiesGroup = localStorage.getItem('browserShowBodiesGroup') !== '0';
  showGeometryGroup = localStorage.getItem('browserShowGeometryGroup') !== '0';
} catch(_){}
function isSketchGroup(n){
  return n && n.type === "folder" && !n.params && !geoFolderOf(n.token);
}
let showOrigins = false;
try { showOrigins = localStorage.getItem('browserShowOrigins') === '1'; } catch(_){}
// Construction Planes: on (the default — Fusion's own), each component with planes of
// its own gets a Construction group, one row per plane with its own bulb; off, the
// rows go AND every construction plane leaves the viewport (the add-in turns Object
// Visibility ▸ Construction Planes off — each plane keeps its bulb for when it's back).
let showConstruction = true;
try { showConstruction = localStorage.getItem('browserShowConstruction') !== '0'; } catch(_){}
function rowVisible(n){
  if (n.type === "origin" || n.type === "originent") return showOrigins;
  if (n.type === "construction" || n.type === "conplane") return showConstruction;
  // The Bodies group only draws with something in it — Fusion's own browser shows no
  // Bodies folder for a component without bodies either. A subgroup counts as something.
  // Both groups vanish while editing a sketch — Fusion's own browser is HUD-only then,
  // and a body/feature row here is nothing you can act on mid-sketch anyway.
  if (n.type === "geometry") return showBodiesGroup && !!(n.children || []).length
                                    && !state.editingSketch;
  // The add-in only sends a Geometry group for a component with features.
  if (n.type === "features") return showGeometryGroup && !state.editingSketch;
  return true;
}

function isFolderTok(t){ return typeof t === "string" && t.startsWith(FOLDER_PREFIX); }
function folderIdOf(t){ return isFolderTok(t) ? t.slice(FOLDER_PREFIX.length) : ""; }
function folderOf(t){ return folderDefs[folderIdOf(t)] || null; }
function newFolderId(){ return "f" + Date.now().toString(36) + Math.random().toString(36).slice(2,7); }

async function loadFolders(docId){
  foldersDoc = docId || "";
  foldersReady = false;            // nothing may be written back until the fetch lands
  folderDefs = {}; leafFolder = {}; paramPlace = {}; paramShow = {}; foldersKeyed = false; foldersGeo = false;
  foldersSplit = false; foldersBoundsShown = false;
  cutToks = [];                    // a cut belongs to the document it was made in
  if (foldersDoc){
    try {
      const all = await (await fetch(API + "/folders", {cache:"no-store"})).json();
      const d = (all && all.byDoc && all.byDoc[foldersDoc]) || {};
      for (const f of (d.folders || []))
        if (f && f.id) folderDefs[f.id] = { id:f.id, name:f.name || "Folder",
                                            comp:f.comp || "", parent:f.parent || "",
                                            hidden: !!f.hidden, kind: f.kind || "",
                                            ignoreGeo: !!f.ignoreGeo, ignoreParams: !!f.ignoreParams };
      leafFolder = Object.assign({}, d.leaves || {});
      foldersKeyed = !!d.keyed;
      foldersGeo = !!d.geo;
      foldersSplit = !!d.split;
      foldersBoundsShown = !!d.boundsShown;
      paramPlace = Object.assign({}, d.params || {});
      paramShow = Object.assign({}, d.show || {});
      foldersReady = true;
    } catch(_){}
  }
  refreshFolders();
}
// A save is only ever allowed to REPLACE this document's overlay, so it must never run
// against the blank one that loadFolders puts up while its fetch is in flight — that
// wrote 57 folders, 162 memberships and 175 filed parameters away as `{}` and the file
// is the only copy. The params poll is what gets in: its first tick always calls
// refreshFolders, and on a fresh page load that lands squarely inside the load.
// A fetch that FAILS deliberately leaves this false: folder edits then don't persist
// for that session, which is the harmless half of the choice.
function saveFolders(){
  if (!foldersDoc || !foldersReady) return;
  fetch(API + "/folders", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ docId: foldersDoc,
      data: { folders: Object.values(folderDefs), leaves: leafFolder,
              params: paramPlace, show: paramShow, keyed: foldersKeyed, geo: foldersGeo,
              split: foldersSplit, boundsShown: foldersBoundsShown } })}).catch(()=>{});
}
// Re-apply the overlay to the last server tree and repaint — called after every local
// folder edit, so it shows on the gesture instead of on the next poll.
// Bounds filings waiting for the overlay to be loaded — see BrowserFileBoundsCmd.
let boundsPending = [];
// Find-or-create the top-level Bounds folder for a component ref. Shared by the
// pending-filing queue above and the manual sweep below, so a rebuilt or manually-swept
// component's bounds always refile into the one folder that's already there.
// A component has two: the sketches' Bounds folder and a Bounds PARAMETER group
// (`params`) — the two kinds of folder never share one.
function isBoundsHome(f, compRef, params){
  if (!f || f.comp !== compRef || (f.name || "") !== BOUNDS_FOLDER
      || isParamFolderDef(f) !== !!params) return false;
  if (!f.parent) return true;
  // The sketch Bounds folder may sit inside its component's top-level Sketches folder
  // (File Sketches puts it there).
  const p = folderDefs[f.parent];
  return !params && !!p && !p.parent && p.comp === compRef
      && (p.name || "") === SKETCHES_FOLDER && !isParamFolderDef(p);
}
// A component's own Bounds PARAMETER group, as its compRef ("" for any other row). It
// stands for the Bounds level of a structured name: <Owner>_Bounds_… draws inside it
// (graftFolders), and a structured parameter dropped on it is renamed to that.
function boundsParamRef(tok){
  const f = isFolderTok(tok) ? folderOf(tok) : null;
  return f && isBoundsHome(f, f.comp, true) ? f.comp : "";
}
function boundsFolderFor(compRef, params){
  let id = Object.keys(folderDefs).find(k => isBoundsHome(folderDefs[k], compRef, params));
  let created = false;
  if (!id){
    id = newFolderId();
    // hidden: the bounds sketches are built with their light bulbs off, and the
    // folder has to agree or its first un-hide would try to restore a state that was
    // never true. A parameter group has no bulb.
    // The sketch one is born inside the component's Sketches folder (fileSketches).
    folderDefs[id] = params
      ? { id, name: BOUNDS_FOLDER, comp: compRef, parent: "", hidden: false, kind: "params" }
      : { id, name: BOUNDS_FOLDER, comp: compRef, parent: sketchesFolderFor(compRef), hidden: true };
    created = true;
  }
  return { id, created };
}
// A bounds parameter reads as its one word — "Right", "Width" — on its row and when
// typed: the parts the Bounds group and the component already say are hidden, exactly
// as ⌘E on the stepper's chips would hide them. Owner and Bounds leave the typed
// reading (`lv`); the type (Offset) and a face's axis leave the row. A Span keeps its
// axis, since that is what spells it Width/Depth/Height.
function boundsShowFor(parts){
  const show = { type: false, lv: ParamNaming.groupLevels(parts) };
  if (parts.type !== "Span") show.dir = false;
  return show;
}
function isBoundsParamName(name){
  const p = paramByName(name);
  return !!p && BOUNDS_COMMENT_RE.test(p.comment || "");
}
// Every bounds parameter (by its comment — Bounds._BOUNDS_COMMENT) without a show entry
// of its own gets that default. One that has an entry, even an empty one
// (BrowserParamShowCmd keeps {} for these), was set by hand and is left alone — except
// once per document (foldersBoundsShown), when every existing one is brought onto it.
// Runs on every refresh, so new bounds and a renamed one get it too.
function ensureBoundsShow(){
  if (!foldersReady || !paramNamingReady) return;
  let changed = false, seen = false;
  for (const p of paramRows){
    if (!BOUNDS_COMMENT_RE.test(p.comment || "")) continue;
    seen = true;
    if (foldersBoundsShown && p.name in paramShow) continue;
    const parts = partsOf(p.name);
    if (!parts) continue;
    paramShow[p.name] = boundsShowFor(parts);
    changed = true;
  }
  if (seen && !foldersBoundsShown){ foldersBoundsShown = true; changed = true; }
  if (changed) saveFolders();
}
// Same idea for master sketches, but there is at most one — they always live in the
// root component (see Masters.py), never per-component.
function mastersFolderFor(){
  let id = Object.keys(folderDefs).find(k =>
    folderDefs[k].comp === ROOT_TOKEN
    && (folderDefs[k].name || "") === MASTERS_FOLDER
    && !folderDefs[k].parent && !isParamFolderDef(folderDefs[k]));
  let created = false;
  if (!id){
    id = newFolderId();
    folderDefs[id] = { id, name: MASTERS_FOLDER, comp: ROOT_TOKEN, parent: "", hidden: true };
    created = true;
  }
  return { id, created };
}
// Everything a filing names is durable — the component by id, the sketches by key —
// so it goes straight into the overlay without waiting for the rows to be drawn: the
// folder is found the moment its component's row arrives, whenever that is.
function fileBoundsPending(){
  if (!boundsPending.length || !foldersReady) return false;
  let shut = false;
  for (const p of boundsPending){
    if (p.params.length){
      const tok = FOLDER_PREFIX + boundsFolderFor(p.compId, true).id;
      for (const n of p.params) paramPlace[n] = tok;
      if (p.collapse){ collapsed.add(tok); shut = true; }
    }
    if (p.sketchKeys.length){
      const { id } = boundsFolderFor(p.compId, false);
      for (const k of p.sketchKeys) leafFolder[k] = id;
      if (p.collapse){ collapsed.add(FOLDER_PREFIX + id); shut = true; }
    }
  }
  boundsPending = [];
  saveFolders();
  if (shut) saveCollapsed();
  return true;
}
// The manual sweep — "File Sketches" on the row menu's view tail. The sketches are
// fileSketches' (which also runs on every refresh, so by now there is rarely anything
// left for it); what only this does is the bounds PARAMETERS: every one in the document
// not yet in its component's Bounds parameter group — older documents, bounds rebuilt
// with the window closed, anything "Add Bounds to Component" made — is filed there.
//
// Parameters are recognised by their comment (Bounds._BOUNDS_COMMENT), matched by the
// component id it carries — id only, no name fallback, since the display name here has
// already had its trailing digits stripped and can't be trusted to round-trip.
function fileAllBounds(){
  if (!foldersDoc || !foldersReady) return;
  const refByCid = {};                   // component id -> compRef (the root's own id
                                         // maps to ROOT_TOKEN, which has no row)
  (function walk(ns, compRef){
    for (const n of (ns || [])){
      if (n.type === "component"){
        const ref = compRefOf(n.token);
        if (n.compId) refByCid[n.compId] = ref;
        walk(n.children, ref);
        continue;
      }
      if (n.type === "sketch" && n.key){
        const cid = n.key.split("|")[0];
        if (cid && !refByCid[cid]) refByCid[cid] = compRef;
      }
      if (n.children) walk(n.children, compRef);
    }
  })(rawTree, ROOT_TOKEN);

  let changed = false;
  for (const p of paramRows){
    const m = BOUNDS_COMMENT_RE.exec(p.comment || "");
    const ref = m && m[4] && refByCid[m[4]];
    if (!ref || (isFolderTok(paramPlace[p.name] || "")
                 && isBoundsHome(folderOf(paramPlace[p.name]), ref, true))) continue;
    paramPlace[p.name] = FOLDER_PREFIX + boundsFolderFor(ref, true).id;
    changed = true;
  }
  const sk = fileSketches();
  if (sk.changed) changed = true;
  const madeFolder = sk.madeFolder;
  if (!changed) return;
  saveFolders();
  refreshFolders();
  // A newly-made folder is hidden (see boundsFolderFor); its sketches were likely still
  // showing bulbs-on when they were unfiled, so ask the add-in to run its policy pass and
  // bring them into line — the same end state New Component's own filing produces.
  if (madeFolder) post({action:"regroup"});
}
// Bodies a command just made (BrowserFileNewBodiesCmd, from the Browser add-in's New
// Bodies.py): each is filed into the group its source body is in. A mirror of more than
// five also splits that group's originals and copies into side subgroups ("Right" /
// "Left" …, named by the add-in) inside it. Waits for the source rows to be drawn, since
// the subgroups hang off wherever those sit; gives up after NEW_BODIES_WAIT_MS.
let newBodiesPending = [];
const NEW_BODIES_WAIT_MS = 5000;
const cidOfKey = k => (k || "").split("|")[0];
// Find-or-create a body folder named `name` directly inside `containerTok` (a folder or
// a Bodies group), placed `where` relative to `relTok`.
function bodySubfolder(containerTok, name, relTok, where){
  const parent = isFolderTok(containerTok) ? folderIdOf(containerTok) : GEO_PARENT;
  const comp = compRefOf(containerTok);
  let id = Object.keys(folderDefs).find(k => { const f = folderDefs[k];
    return f.comp === comp && f.parent === parent && (f.name || "") === name
        && !isParamFolderDef(f); });
  if (!id){
    id = newFolderId();
    folderDefs[id] = { id, name, comp, parent, hidden: false, kind: "" };
    orderMove(FOLDER_PREFIX + id, containerTok, relTok, relTok ? where : "end");
  }
  return id;
}
// A component's top-level "Sketches" folder, made if there is none — where fileSketches
// keeps every sketch.
const SKETCHES_FOLDER = "Sketches";
function sketchesFolderFor(compRef){
  let id = Object.keys(folderDefs).find(k => { const f = folderDefs[k];
    return f.comp === compRef && !f.parent && (f.name || "") === SKETCHES_FOLDER
        && !isParamFolderDef(f); });
  if (!id){
    id = newFolderId();
    folderDefs[id] = { id, name: SKETCHES_FOLDER, comp: compRef, parent: "", hidden: false, kind: "" };
  }
  return id;
}
// Every sketch lives on its component's Sketches side, the way every body lives in
// Bodies — kept on EVERY refresh, not only when a push names a sketch as new: a push
// misses some (made while the window was shut, a rename that moved its token too, a
// paste no idle build saw), and nothing else would ever file them. Per component with
// sketches in the tree:
//   • a bounds sketch ("Bounds …") → its Bounds folder, which lives inside Sketches;
//   • a root "Master …" sketch → Masters (its own folder, left at the top);
//   • any other sketch not in a sketch-side folder of its own component → Sketches;
//   • every other top-level sketch-side folder holding a sketch → moved inside Sketches.
// Sketches only; bounds PARAMETERS are the manual sweep's (fileAllBounds).
// Returns {changed, madeFolder}; the caller saves.
function fileSketches(){
  const out = { changed: false, madeFolder: false };
  // pollState swaps rawTree in BEFORE a new document's overlay loads: filing then would
  // write the new document's sketches into the old one's folders.
  if (!foldersDoc || !foldersReady || !rawTree.length
      || (state.doc_id || "") !== foldersDoc) return out;
  const byComp = {};                     // compRef -> {bounds, master, plain: [key, ...]}
  (function walk(ns, compRef){
    for (const n of (ns || [])){
      if (n.type === "component"){ walk(n.children, compRefOf(n.token)); continue; }
      const parts = n.type === "sketch" && n.key ? n.key.split("|") : null;
      if (parts && parts[1] === "sk"){
        const name = parts.slice(2).join("|");
        const e = byComp[compRef] = byComp[compRef] || { bounds: [], master: [], plain: [] };
        // Section.py's tether and reference sketches (hidden helpers) file with them.
        if (name.startsWith(BOUNDS_SKETCH_PREFIX) || name.startsWith(TETHERS_SKETCH_PREFIX)
            || name.startsWith(REFS_SKETCH_PREFIX)) e.bounds.push(n.key);
        else if (!name.startsWith(MASTER_SKETCH_PREFIX)) e.plain.push(n.key);
        else if (compRef === ROOT_TOKEN) e.master.push(n.key);
      }
      if (n.children) walk(n.children, compRef);
    }
  })(rawTree, ROOT_TOKEN);

  const file = (keys, id) => {
    for (const k of keys) if (leafFolder[k] !== id){ leafFolder[k] = id; out.changed = true; }
  };
  const sketchSide = (fid, ref) => { const f = folderDefs[fid];
    return !!f && f.comp === ref && !isParamFolderDef(f) && !geoFolderOf(FOLDER_PREFIX + fid); };
  const isMasters = f => f.comp === ROOT_TOKEN && (f.name || "") === MASTERS_FOLDER;
  for (const [ref, e] of Object.entries(byComp)){
    const bounds = e.bounds.filter(k => !isBoundsHome(folderDefs[leafFolder[k]], ref, false));
    if (bounds.length){
      const { id, created } = boundsFolderFor(ref, false);
      if (created) out.madeFolder = true;
      file(bounds, id);
    }
    const masters = e.master.filter(k => { const f = folderDefs[leafFolder[k]];
      return !(f && isMasters(f) && !f.parent); });
    if (masters.length){
      const { id, created } = mastersFolderFor();
      if (created) out.madeFolder = true;
      file(masters, id);
    }
    // A sketch already in a sketch-side folder of its own component was put there.
    const plain = e.plain.filter(k => !sketchSide(leafFolder[k], ref));
    if (plain.length) file(plain, sketchesFolderFor(ref));

    // Folders at the component's top that hold a sketch (nested counted) go inside
    // Sketches — the sketch Bounds folder among them. Empty ones wait for content.
    const tops = Object.values(folderDefs).filter(f => f.comp === ref && !f.parent
      && !isParamFolderDef(f) && !isMasters(f) && (f.name || "") !== SKETCHES_FOLDER);
    if (!tops.length) continue;
    const holds = new Set();
    for (const [k, fid] of Object.entries(leafFolder)){
      if (k.split("|")[1] !== "sk") continue;
      for (let f = folderDefs[fid], i = 0; f && i < 64; f = folderDefs[f.parent], i++)
        holds.add(f.id);
    }
    const nest = tops.filter(f => holds.has(f.id));
    if (!nest.length) continue;
    const home = sketchesFolderFor(ref);
    for (const f of nest){ f.parent = home; out.changed = true; }
  }
  return out;
}
// Run from both tree paths (pollState, refreshFolders). A sketch filed into a hidden
// folder has to go dark with it — the add-in's policy pass sets the bulbs.
function keepSketchesFiled(){
  if (!fileSketches().changed) return;
  saveFolders();
  post({action:"regroup"});
}
function fileNewBodiesPending(){
  if (!newBodiesPending.length || !foldersReady) return false;
  const keep = [];
  let changed = false;
  for (const p of newBodiesPending){
    if (p.bodies.some(b => !tokByKey[b.from]) && Date.now() - p.ts < NEW_BODIES_WAIT_MS){
      keep.push(p); continue;
    }
    const fromOf = {};
    for (const b of p.bodies) fromOf[b.key] = b.from;
    const done = new Set();
    for (const m of p.mirrors){
      // One pair of subgroups per place the originals sat.
      const byPlace = {};
      for (const k of m.made){
        const src = fromOf[k], stok = tokByKey[src];
        if (!src || !stok || leafFolder[k] || cidOfKey(k) !== cidOfKey(src)) continue;
        const place = parentTokOf(stok);
        if (!isFolderTok(place) && !isGeoTok(place)) continue;
        const e = byPlace[place] = byPlace[place] || { made: [], srcs: new Set() };
        e.made.push(k); e.srcs.add(stok);
      }
      for (const [place, e] of Object.entries(byPlace)){
        let newId;
        const f = isFolderTok(place) ? folderOf(place) : null;
        if (f && (f.name || "") === m.srcSide){
          // The originals already are a side group: the copies become its sibling.
          newId = bodySubfolder(parentTokOf(place), m.newSide, place, "after");
        } else {
          const order = childrenTokensOf(place).filter(t => e.srcs.has(t));
          const srcId = bodySubfolder(place, m.srcSide, order[0] || null, "before");
          const stok = FOLDER_PREFIX + srcId;
          orderMoveMany(order, stok, null, "end");
          for (const t of order) setLeafFolder(t, srcId);
          newId = bodySubfolder(place, m.newSide, stok, "after");
        }
        for (const k of e.made){ leafFolder[k] = newId; done.add(k); }
        changed = true;
      }
    }
    for (const b of p.bodies){
      if (done.has(b.key) || leafFolder[b.key] || cidOfKey(b.key) !== cidOfKey(b.from)) continue;
      const fid = leafFolder[b.from];
      if (!fid || !folderDefs[fid]) continue;
      leafFolder[b.key] = fid;
      changed = true;
    }
  }
  newBodiesPending = keep;
  if (!changed) return false;
  saveFolders();
  // A body filed into a hidden group has to go dark with it — the add-in's policy
  // pass reads the groups and sets the bulbs.
  post({action:"regroup"});
  return true;
}
function refreshFolders(){
  rebuildKeyIndex(rawTree);
  migrateLeafKeys();
  migrateCompRefs();
  migrateGeoFolders();
  migrateParamFolders();
  fileBoundsPending();
  keepSketchesFiled();
  hydrateOverlays();
  // Name groups are derived, not stored, so they are rebuilt here rather than
  // migrated anywhere: the component index first (it answers which component an owner
  // level names), then the buckets that read off it.
  rebuildIdentIndex();
  ensureBoundsShow();
  rebuildNameIndex();
  state.tree = withFolders(rawTree);
  reconcileIgnores(state.tree);
  applyPendingDelete(); applyPendingVis();
  render();
  // Needs the drawn tree (where the source bodies sit), so it runs after it, and draws
  // again if it filed anything.
  if (fileNewBodiesPending()) refreshFolders();
}
// A folder that has picked up content stops being ignored — the user can't ignore a
// folder that has something in it, and filing a leaf into one that WAS ignored (drag,
// paste, a bounds sweep, where a new parameter lands) is the same as picking
// "un-ignore" by hand. Runs on every refresh, so it lands on the same tick as whatever
// filed the leaf.
function reconcileIgnores(nodes){
  let changed = false;
  (function walk(ns){
    for (const n of (ns || [])){
      if (n.type === "folder"){
        const f = folderOf(n.token), key = ignoreKeyOf(f);
        if (f && f[key] && folderHasVisibleContent(n)){ f[key] = false; changed = true; }
      }
      if (n.children) walk(n.children);
    }
  })(nodes);
  if (changed) saveFolders();
}

// Build the display tree: a component's foldered leaves are pulled out of its children
// and re-hung under synthetic folder nodes. Nodes are shallow-cloned, so the server's
// own snapshot stays intact for the next rebuild.
// A component whose name contains "disregard" is a PASS-THROUGH: its own row is never
// shown, and everything it holds is lifted into its parent, so the sub-components
// inside it still appear (one level up). Applied recursively, so a disregarded
// component nested inside another resolves too. The Fusion hierarchy is untouched —
// this is only what the browser draws; the nav add-ins skip the same components.
const DISREGARD = "disregard";
// "(disregard)"     → the row goes, its contents move up a level.
// "(disregard all)" → the row AND everything inside it go.
// Tagged rows vanish, so a component can only be tagged FROM the browser; untagging is
// done by editing the name in Fusion, which is the point of keeping it in the name.
let pendingDisregard = {};        // token -> {mode, ts}: applied before Fusion confirms
function disregardMode(n){
  if (!n || n.type !== "component") return null;
  const pend = pendingDisregard[n.token];
  if (pend) return pend.mode;
  const nm = (n.name || "").toLowerCase();
  if (nm.indexOf(DISREGARD + " all") >= 0) return "all";
  if (nm.indexOf(DISREGARD) >= 0) return "self";
  return null;
}
function liftDisregarded(kids){
  const out = [];
  for (const c of (kids || [])){
    const mode = disregardMode(c);
    if (mode === "all") continue;                                  // subtree and all
    if (mode === "self") out.push.apply(out, liftDisregarded(c.children || []));
    else out.push(c);
  }
  return out;
}
// Drop the optimistic flag once Fusion's own name carries the tag (or it didn't take).
function reconcilePendingDisregard(){
  const now = Date.now();
  for (const tok of Object.keys(pendingDisregard)){
    let named = false;
    (function w(ns){ for (const n of ns){
      if (n.token === tok) named = (n.name || "").toLowerCase().indexOf(DISREGARD) >= 0;
      if (n.children) w(n.children); } })(rawTree);
    if (named || now - pendingDisregard[tok].ts > 5000) delete pendingDisregard[tok];
  }
}

// Does this folder have anything to show right now? Recurses, so a folder holding only
// empty folders is empty too.
function folderHasVisibleContent(n){
  for (const c of (n.children || [])){
    if (c.type === "folder"){ if (folderHasVisibleContent(c)) return true; continue; }
    if (rowVisible(c)) return true;
  }
  return false;
}
// Does flatten() draw this row at all? The view toggles (Bounds/Masters/Sketches…)
// apply here, so a folder whose only content is toggled off counts as empty.
// An empty folder is drawn plainly with its "(Name)" parens UNLESS it's ignored
// (skipped unless the bar's toggle is on), it can never hold anything at all (a
// sketch folder whose owner has child components), or it's one the Browser made
// itself (`isAutoFolder`) — those just disappear.
function rowDrawn(n){
  if (!rowVisible(n)) return false;               // display filter only
  if (isBoundsFolder(n) && !showBoundsFolders) return false;
  if (isMastersFolder(n) && !showMasterFolders) return false;
  if (hideSketches && (n.type === "sketch" || isSketchGroup(n))) return false;
  if (n.type === "folder" && !folderShowsRows(n)){
    // Owned by a component that itself has child components — such a component
    // never holds sketches directly, so this folder is never a sketch folder no
    // matter what's ignored (derived here, not stored: a child component moving
    // in or out just changes the answer on the next render).
    if (!n.params && n.ownerHasComps && !geoFolderOf(n.token)) return false;
    if (folderIgnored(n) && !showIgnoredFolders) return false;
    if (isAutoFolder(n)) return false;
  }
  return true;
}
function folderShowsRows(n){ return (n.children || []).some(rowDrawn); }
// The folders fileSketches/boundsFolderFor make on their own: a component's top-level
// Sketches, the root's Masters, and both Bounds groups.
function isAutoFolder(n){
  if (isBoundsFolder(n) || isMastersFolder(n)) return true;
  const f = folderOf(n.token);
  return !!f && !f.parent && !isParamFolderDef(f) && (f.name || "") === SKETCHES_FOLDER;
}

// Does this folder hold a sketch or body whose group toggle is off (Cycle Visibility)?
// Only sketch/body leaves ever carry groupOff, so that's all this looks for; recurses
// into nested folders so a Sketches folder two levels deep still dims. Used by walk()'s
// groupDim — a folder full of switched-off sketches should read as switched off too,
// not sit there looking untouched while everything inside it is dimmed.
function groupOffIn(n){
  if (n.type !== "folder") return false;
  for (const c of (n.children || [])){
    if ((c.type === "sketch" || c.type === "body") && c.groupOff) return true;
    if (c.type === "folder" && groupOffIn(c)) return true;
  }
  return false;
}

// Parameter rows for one container ("" = the top level). A parameter belongs to the
// document, not to a component, so where it sits is purely this overlay.
// A parameter whose container has gone (a folder dissolved by an older build, a
// component that left the model) falls back to the top level rather than vanishing; one
// filed in a folder that is not a parameter group draws in that folder's component.
function paramContainer(name){
  const c = paramPlace[name] || "";
  if (!c || c === ROOT_TOKEN) return "";
  if (isFolderTok(c)){
    const f = folderOf(c);
    if (!f) return "";
    if (isParamFolderDef(f)) return c;
    return (f.comp && f.comp !== ROOT_TOKEN && compTokOf(f.comp)) ? f.comp : "";
  }
  return compTokOf(c) ? c : "";
}
function paramNodesFor(containerRef){
  const want = (!containerRef || containerRef === ROOT_TOKEN) ? "" : containerRef;
  return paramRows.filter(p => !partsOf(p.name) && paramContainer(p.name) === want
                               && !unbornRef(paramOwnerFiled(p.name)))
                  .map(paramNode);
}
// The component a hand-filed parameter was filed under (its folder's, for a folder),
// before paramContainer's fallbacks — "" when unfiled or the folder is gone.
function paramOwnerFiled(name){
  const c = paramPlace[name] || "";
  if (!isFolderTok(c)) return c;
  const f = folderOf(c);
  return f ? (f.comp || "") : "";
}
// Rolled back in the timeline, a component missing from the tree may simply not exist
// YET — so everything tied to it (its folders and parameter groups, the parameters
// filed in them, name groups owned or pinned to it) is hidden until the marker reaches
// it, instead of spilling out at the top level. At the timeline's end a missing
// component is really gone, and its things still fall back to the top level so they
// stay reachable (orphanFolder, ORPHAN_REF, paramContainer).
function unbornRef(ref){
  return !!state.rolledBack && !!ref && ref !== ROOT_TOKEN && ref !== ORPHAN_REF
         && !isFolderTok(ref) && !compTokOf(ref);
}
// One parameter row. A name the convention can read is DRAWN AS ITS LEAF — its name
// word and type, spoken (see ParamNaming.displayLeaf) — because the levels in front
// of it are the groups the row is already sitting in; showing them again would be
// reading the address off an envelope you are holding. The direction (anchor/axis) is
// left off too, unless a sibling is the same name and type on another one: then it is
// the only thing telling the two apart (leafLabel). Everything else (an old-convention
// name, a stray) draws exactly as Fusion has it.
//   .name   what the row says          .param  the real Fusion name
// Every caller that talks to Fusion goes through the token (paramNameOf) or .param,
// never .name — that is the whole reason .param is here.
function paramNode(p){
  const parts = partsOf(p.name);
  return { token: "param:" + p.name, type: "param",
           name: parts ? leafLabel(parts, p.name) : p.name,
           param: p.name, parts: parts || null,
           expr: p.expression || "", unit: p.unit || "",
           comment: p.comment || "", visible: true };
}

// ── name groups: the level segment of a structured name, drawn as the tree ───
/* A parameter named under the structured convention already says where it lives:
 *
 *     Drawer_Front_Curve_Inset_Back
 *     └owner┘└group┘└─── the row ───┘
 *
 * The first level is the OWNER COMPONENT (fusion_value's stepper fixes it from the
 * component path and never lets it be typed), the LAST level is the parameter's own
 * name word — part of the row, "Curve - Inset" (ParamNaming.nameWord) — and every
 * level between is a group inside that component. So the tree for these is not stored anywhere: it is READ OFF
 * THE NAMES, every render. There is nothing to migrate, nothing to go stale, and no
 * second copy of the truth to drift from the first.
 *
 * Which means the traffic runs the other way too. A virtual folder is moved by
 * rewriting folders.json; one of these groups is moved by RENAMING EVERY PARAMETER
 * UNDER IT, and the group moves because its name moved. Dragging a parameter into
 * another component rewrites its owner level; renaming a group rewrites that one word
 * in everything beneath it; exploding one takes the word out altogether. All four go
 * through renamePlan/applyRenamePlan below, so all four get the same collision check
 * and the same refusal.
 *
 * A parameter whose name does NOT parse is untouched by any of this — it keeps the
 * hand filing (paramPlace) it always had, and paramNodesFor above skips the ones that
 * do parse so a row can never be drawn twice.
 */
const NAME_PREFIX = "ng:";
const NG_SEP = "";          // ref from path; neither can contain it
function isNameTok(t){ return typeof t === "string" && t.startsWith(NAME_PREFIX); }
// {ref, path, words} for a name-group token, or null. `path` is the level chain BELOW
// the owner component, "/"-joined — safe, since identifier() leaves no "/" in a level.
function nameTokParts(t){
  if (!isNameTok(t)) return null;
  const rest = t.slice(NAME_PREFIX.length);
  const i = rest.indexOf(NG_SEP);
  if (i < 0) return null;
  const path = rest.slice(i + 1);
  return { ref: rest.slice(0, i), path, words: path ? path.split("/") : [] };
}
function nameTok(ref, words){ return NAME_PREFIX + ref + NG_SEP + words.join("/"); }

let paramNamingReady = false;
// name -> parse() result (or null), so a tree walk that asks about the same parameter
// a dozen times parses it once. Dropped whenever the parameter list or the vocabulary
// changes — the only two things that can change an answer.
let partsCache = new Map();
function partsOf(name){
  if (!paramNamingReady) return null;
  if (partsCache.has(name)) return partsCache.get(name);
  let parts = null;
  try { parts = ParamNaming.parse(name); } catch(_){ parts = null; }
  partsCache.set(name, parts);
  return parts;
}
async function loadParamNaming(){
  try {
    await ParamNaming.loadVocabulary(API + "/param_naming.json");
    paramNamingReady = true;
    partsCache = new Map();
    refreshFolders();
  } catch(_){ paramNamingReady = false; }
}

// identifier(component name) -> compRef, for every component in the tree plus the
// root. Rebuilt with the key index, since that is where compByCid is filled.
// It is NOT one-to-one: several components can read as the same word here (three
// "Steel" components under Roof, Boot Ceiling and Drawer Ceiling), so every one is kept
// in `compsByIdent`, first drawn first. `compByIdent` is just that list's head. Which of
// them a parameter belongs to is its PIN (pinnedOwner, below) — the name alone can't say.
let compByIdent = {}, compsByIdent = {};
function rebuildIdentIndex(){
  compByIdent = {}; compsByIdent = {};
  if (!paramNamingReady) return;
  const add = (key, ref) => {
    if (!key) return;
    (compsByIdent[key] = compsByIdent[key] || []).push(ref);
    if (!(key in compByIdent)) compByIdent[key] = ref;
  };
  const root = (state.rootName || "").trim();
  if (root) add(ParamNaming.identifier(root), ROOT_TOKEN);
  for (const cid of Object.keys(compByCid))
    add(ParamNaming.identifier(compByCid[cid].name || ""), cid);
}
// The component a parameter is pinned to, as a compRef ("" = no pin). A parsed name
// is never hand-filed, so its paramPlace entry is free to say which of several
// same-named components its owner level means: a component ref, ROOT_TOKEN, or a
// folder (read as the folder's component — the filing it carried in from its old name).
function pinnedOwner(name){
  const c = paramPlace[name];
  if (!c) return "";
  if (isFolderTok(c)){ const f = folderOf(c); return f ? (f.comp || ROOT_TOKEN) : ""; }
  return c;
}
// The component a parameter sits in right now, whatever kind of name it has.
function ownerRefOfParam(name){
  const parts = partsOf(name);
  if (parts){
    const ref = homeOfParts(parts, nameIdx, name).ref;
    return ref === ORPHAN_REF ? "" : ref;
  }
  const c = paramContainer(name);
  if (!c) return ROOT_TOKEN;
  if (isFolderTok(c)){ const f = folderOf(c); return f ? (f.comp || ROOT_TOKEN) : ""; }
  return c;
}
// What paramPlace should hold for `newName` so it lands in `owner` — the pin, when the
// name's owner word is shared: `owner` itself if it is one of the components the word
// could mean, else the one of them nearest inside `owner` (a rename in Boot Ceiling to
// Steel_… means Boot Ceiling's Steel, not Roof's). undefined when the name settles it
// alone, or nothing it could mean is near `owner`.
function pinFor(newName, owner){
  const parts = partsOf(newName);
  if (!parts || !owner) return undefined;
  const refs = compsByIdent[parts.levels[0]] || [];
  if (refs.length < 2) return undefined;
  if (refs.includes(owner)) return owner;
  const inside = r => {
    const k = (compByCid[r] || {}).key || "";
    return owner === ROOT_TOKEN || k.includes("/" + owner + ":") || k.includes("|" + owner + ":");
  };
  const depth = r => ((compByCid[r] || {}).key || "").split("/").length;
  const near = refs.filter(r => r !== ROOT_TOKEN && inside(r))
                   .sort((a, b) => depth(a) - depth(b));
  return near.length ? near[0] : undefined;
}
// The identifier a container contributes as a parameter's OWNER level. "" when it
// can't contribute one — an unknown ref, or a root whose name hasn't arrived yet
// (the standalone server preview, or a poll before the add-in's first tick). Callers
// read "" as "this is not somewhere a name can point at" and refuse the gesture,
// which is better than composing a name around the word "Comp".
function identOfRef(ref){
  if (!ref || ref === ROOT_TOKEN){
    const root = (state.rootName || "").trim();
    return root ? ParamNaming.identifier(root) : "";
  }
  const n = compByCid[ref];
  return n ? ParamNaming.identifier(n.name || "") : "";
}

// The bucket orphans go in. An owner level that matches NO component in this document
// — it was renamed or deleted out from under its parameters — draws at the top level
// with its whole chain intact, so the orphans appear as a group named after the
// component they are still claiming rather than scattering loose at the root.
// Nothing is rewritten: put the component back (or rename the group) and they go home.
//
// It is its own ref rather than ROOT_TOKEN so that an orphan chain and a root-owned
// chain can never share a group row. They read differently — an orphan's first word IS
// its owner, a root-owned one's owner is the document — and one token that could mean
// either is one a rename would get wrong. "?" can be neither ROOT_TOKEN nor a
// Component.id, so there is nothing for it to collide with.
const ORPHAN_REF = "?";

// Where a parsed name files: {ref, words} — the container it belongs to, and the group
// chain inside that container.
// The last level is the parameter's NAME word (ParamNaming.nameWord) — part of its row,
// not a group — so it is not in `words`; it rides along as `name`. `index` (optional,
// see nameIdx below) is nameWord()'s own — a level chain TWO OR MORE parameters share
// is a group for all of them, none of them a name.
// `pname`, the parameter's Fusion name, lets its pin pick between same-named owners.
function homeOfParts(parts, index, pname){
  const groups = ParamNaming.groupLevels(parts, index), name = ParamNaming.nameWord(parts, index);
  const refs = compsByIdent[groups[0]];
  if (!refs) return { ref: ORPHAN_REF, bucket: ORPHAN_REF, words: groups.slice(), name };
  const pin = (refs.length > 1 && pname) ? pinnedOwner(pname) : "";
  const ref = refs.includes(pin) ? pin : refs[0];
  return { ref, bucket: (pname && paramFolderOf(pname, ref)) || ref,
           words: groups.slice(1), name };
}
// A parsed parameter filed by hand into a parameter group (⌘G around it, a drop on the
// group) draws inside that group, its name groups rooted there — the group is NOT a
// level of its name. Only while the group belongs to the component the name says: rename
// its owner level away and the filing stops counting. "" when it isn't in one.
// Never a component's Bounds group: that one IS the name's Bounds level (graftFolders
// folds the component's Bounds name group into it), so rooting a parsed name there —
// which is what every bounds filing does — drew a second Bounds group inside the first.
function paramFolderOf(pname, ref){
  const c = paramPlace[pname];
  if (!isFolderTok(c)) return "";
  const f = folderOf(c);
  return (f && isParamFolderDef(f) && (f.comp || ROOT_TOKEN) === ref
          && !isBoundsHome(f, f.comp, true)) ? c : "";
}
// The component a name-group bucket belongs to: the bucket itself, or a parameter
// group's component.
function bucketComp(ref){
  if (!isFolderTok(ref)) return ref;
  const f = folderOf(ref);
  return f ? (f.comp || ROOT_TOKEN) : "";
}
// The full level list a name group stands for — what a parameter dropped into it
// would carry in front of its name word and type.
function levelsOfNameGroup(ng){
  if (ng.ref === ORPHAN_REF) return ng.words.slice();   // already starts with its owner
  const own = identOfRef(bucketComp(ng.ref));
  return own ? [own].concat(ng.words) : [];
}

// compRef -> {"<path>": {subs: [next words, in first-seen order], rows: [param rows]}}.
// Every ancestor bucket exists even when nothing sits directly in it, so a group two
// levels deep still has a group row to hang off.
let ngIndex = {};
// Level chain + type -> how many parameters share it. Two or more means the rows differ
// only by direction, so leafLabel shows it on them.
let leafTwins = new Map();
const twinKey = parts => parts.levels.join("_") + "|" + parts.type;
// Level chain -> how many parsed parameters have exactly it — ParamNaming.nameIndex(),
// rebuilt alongside ngIndex. A chain two or more parameters share is a GROUP for all of
// them (nameWord/groupLevels read it), same reason twinKey shows the direction: the
// word that would otherwise repeat on every row instead becomes what they sit in.
let nameIdx = {};
// `name` (the real Fusion name) applies that parameter's paramShow; without it, the
// default reading — which is what a rename box wants, since parseLeaf reads the type back.
function leafLabel(parts, name){
  return ParamNaming.displayLeaf(parts, { direction: (leafTwins.get(twinKey(parts)) || 0) > 1,
                                          index: nameIdx, show: name ? paramShow[name] : null });
}
function rebuildNameIndex(){
  ngIndex = {};
  leafTwins = new Map();
  nameIdx = {};
  if (!paramNamingReady) return;
  nameIdx = ParamNaming.nameIndex(paramRows.map(p => partsOf(p.name)).filter(Boolean));
  for (const p of paramRows){
    const parts = partsOf(p.name);
    if (!parts) continue;
    leafTwins.set(twinKey(parts), (leafTwins.get(twinKey(parts)) || 0) + 1);
    if (unbornRef(pinnedOwner(p.name))) continue;
    const home = homeOfParts(parts, nameIdx, p.name);
    const buckets = ngIndex[home.bucket] = ngIndex[home.bucket] || {};
    let path = "";
    for (const w of home.words){
      const b = buckets[path] = buckets[path] || { subs: [], rows: [] };
      if (!b.subs.includes(w)) b.subs.push(w);
      path = path ? path + "/" + w : w;
    }
    const leaf = buckets[path] = buckets[path] || { subs: [], rows: [] };
    leaf.rows.push(p);
  }
}
// The rows one bucket draws: its own parameters, then its subgroups. Both are on the
// parameter side, so orderedChildren's pin (that side to the bottom) leaves this order
// alone.
function nameGroupNodes(ref, path){
  const buckets = ngIndex[ref];
  if (!buckets) return [];
  const b = buckets[path];
  if (!b) return [];
  const words = path ? path.split("/") : [];
  const out = b.rows.map(paramNode);
  for (const w of b.subs){
    const sub = words.concat([w]);
    out.push({ token: nameTok(ref, sub), type: "namegroup",
               name: ParamNaming.displayWord(w), word: w, comp: bucketComp(ref),
               path: sub.join("/"), visible: true,
               children: nameGroupNodes(ref, sub.join("/")) });
  }
  return out;
}
// Every parameter ROW under a name group, itself included — what a rename, a move or
// an explode has to rewrite.
function paramsUnderName(tok){
  const ng = nameTokParts(tok);
  if (!ng) return [];
  const out = [];
  for (const p of paramRows){
    const parts = partsOf(p.name);
    if (!parts) continue;
    const home = homeOfParts(parts, nameIdx, p.name);
    if (home.bucket !== ng.ref) continue;
    if (home.words.length < ng.words.length) continue;
    if (!ng.words.every((w, i) => home.words[i] === w)) continue;
    out.push({ row: p, parts, home });
  }
  return out;
}

/* ── rewriting names: the one way any of this ever moves ────────────────────
   Every gesture on this side of the tree — drag a parameter into a component,
   rename a group, drag a group somewhere else, explode one — comes down to the
   same thing: a list of {from, to} parameter renames. Building the list and
   applying it are kept apart so the WHOLE list can be checked before a single
   rename is posted. Fusion has no transaction here: rename three of five and hit
   a collision on the fourth and the model is left half-moved, with two of the
   five parameters now in a group the user never asked for. So nothing goes until
   everything can.

   The check is deliberately strict — a target may not be a live parameter name
   at all, even one this same plan is renaming away. A plan that needed the
   ordering to work out (A→B while B→C) is refused rather than sequenced: the
   add-in's queue is FIFO and would get it right, but "it depends what order they
   went in" is not something a dialog can explain, and the user's own fix (rename
   one of them first) is a clearer thing to be asked for. */

// [{from, to}] with the no-ops dropped, or a string saying why it can't be done.
function checkRenamePlan(plan){
  const moves = plan.filter(m => m.from !== m.to);
  if (!moves.length) return [];
  const bad = moves.find(m => !m.to);
  if (bad) return `“${bad.from}” has no valid name under the new grouping.`;
  const seen = new Map();
  for (const m of moves){
    if (seen.has(m.to))
      return `Two parameters would both be called “${m.to}”:\n`
           + `“${seen.get(m.to)}” and “${m.from}”.`;
    seen.set(m.to, m.from);
  }
  const moving = new Set(moves.map(m => m.from));
  for (const m of moves){
    const clash = paramRows.find(p => p.name === m.to);
    if (!clash) continue;
    return moving.has(m.to)
      ? `“${m.from}” would take the name “${m.to}”, which this same move is `
        + `giving to something else. Rename one of them first.`
      : `“${m.from}” would become “${m.to}”, and a parameter called that `
        + `already exists. Rename one of them first.`;
  }
  return moves;
}

// Run a checked plan. Returns true if it went, false (with the dialog up) if not.
// `what` heads the dialog — "Move", "Rename", "Explode" — so the refusal says which
// gesture was refused, not just that something was.
// A move may carry `owner`, the component it was dropped on: the name can't always say
// which one (see pinFor), so the pin does — and a move whose name doesn't change at all
// (Roof's Steel → Boot Ceiling's Steel) is a re-pin and nothing else.
function applyRenamePlan(plan, what){
  const moves = checkRenamePlan(plan);
  if (typeof moves === "string"){ showBlocked(what, moves); return false; }
  let repinned = false;
  for (const m of plan){
    if (m.from !== m.to || !m.owner) continue;
    const cur = paramPlace[m.from];
    const pin = m.folder || pinFor(m.from, m.owner);
    if (cur === pin || (pin === undefined && !(m.folder === null && isFolderTok(cur)))) continue;
    if (pin === undefined) delete paramPlace[m.from]; else paramPlace[m.from] = pin;
    repinned = true;
  }
  if (repinned){ saveFolders(); if (!moves.length) refreshFolders(); }
  if (!moves.length) return true;
  // Optimistic: the rows are rewritten here so the tree redraws in the right shape on
  // this frame rather than on the params poll a second later. The poll is still the
  // truth — if a rename doesn't take, the next tick puts the old name back.
  for (const m of moves){
    const row = paramByName(m.from);
    const owner = m.owner || ownerRefOfParam(m.from);
    if (row) row.name = m.to;
    carryFilingOnRename(m.from, m.to, owner, m.folder);
    setParam({ name: m.from, newName: m.to });
  }
  partsCache = new Map();
  lastParamsJSON = "";              // don't let the poll's change test skip the echo
  refreshFolders();
  return true;
}

/* The refusal. Deliberately a real modal rather than a line in the bar: the gesture
   did NOT happen, and something that did nothing has to say so somewhere the eye is
   already looking. Esc, Return, the button and a click outside all dismiss it — there
   is only one thing to do with it. */
function showBlocked(what, why){
  const old = document.getElementById("blocked-bg");
  if (old) old.remove();
  document.body.insertAdjacentHTML("beforeend",
    `<div id="blocked-bg"><div id="blocked" role="alertdialog">`
    + `<h2>${escapeHtml(what)} not possible</h2>`
    + `<p>${escapeHtml(why).replace(/\n/g, "<br>")}</p>`
    + `<div style="text-align:right;margin-top:14px;">`
    + `<button class="sbtn" id="blocked-ok">OK</button></div></div></div>`);
  const bg = document.getElementById("blocked-bg");
  bg.addEventListener("mousedown", e => { if (e.target.id === "blocked-bg") hideBlocked(); });
  const ok = document.getElementById("blocked-ok");
  ok.addEventListener("click", hideBlocked);
  ok.focus();
  publishLiveBottom();
}
function hideBlocked(){
  blockedGo = null;
  const m = document.getElementById("blocked-bg");
  if (m){ m.remove(); publishLiveBottom(); }
  const t = document.getElementById("tree"); if (t) t.focus();
}
/* The same furniture as a question: a gesture that would break something asks first
   (an Explode of a group a Combine follows — confirmGroupDelete). Return / the button
   go ahead; Esc, Cancel and a click outside leave everything as it was. */
let blockedGo = null;
function showConfirm(title, html, okLabel, go){
  showBlocked(title, "");
  document.querySelector("#blocked h2").textContent = title;
  document.querySelector("#blocked p").outerHTML = html;
  const ok = document.getElementById("blocked-ok");
  ok.textContent = okLabel;
  ok.insertAdjacentHTML("beforebegin", `<button class="sbtn" id="blocked-cancel">Cancel</button> `);
  document.getElementById("blocked-cancel").addEventListener("click", hideBlocked);
  ok.removeEventListener("click", hideBlocked);
  ok.addEventListener("click", runBlockedGo);
  blockedGo = go;
  ok.focus();
}
function runBlockedGo(){
  const go = blockedGo;
  hideBlocked();
  if (go) go();
}

// ── Groups a feature follows ─────────────────────────────────────────────────
// A live Combine run can take a whole group as its target or tools (AddIns/Combine,
// LINK's tg / og), and an Extrude its bodies ("Bodies are: Group", josh.extrude/groups);
// the add-in lists them on /state as `groupUses` {folder id: [feature names]}. Exploding
// such a group leaves those features frozen as they are, so it asks first — as a
// parameter or sketch with dependants does.
let groupUses = {};
function noteGroupUses(u){ groupUses = u || {}; }
function groupCombines(toks){
  const out = [];
  for (const t of toks){
    const f = folderOf(t); if (!f) continue;
    for (const n of groupUses[f.id] || []) if (!out.includes(n)) out.push(n);
  }
  return out;
}
function confirmGroupDelete(toks, users, go){
  const names = toks.map(t => (folderOf(t) || {}).name || "Group");
  const n = names.length, one = users.length === 1;
  const shown = users.slice(0, 6).map(u => depLine(`<b>${escapeHtml(u)}</b>`, {})).join("")
              + (users.length > 6 ? `<div>and ${users.length - 6} more</div>` : "");
  showConfirm(n === 1 ? "Explode Group" : `Explode ${n} Groups`,
    `<p><b>${escapeHtml(names.join(", "))}</b> ${n === 1 ? "is" : "are"} used by:</p>`
    + `<div class="confirm-users">${shown}</div>`
    + `<p>Exploded, ${one ? "it keeps the bodies it has" : "they keep the bodies they have"} `
    + `now and stop${one ? "s" : ""} following the group.</p>`,
    "Explode", go);
}
function blockedOpen(){ return !!document.getElementById("blocked-bg"); }

/* ── Delete with a substitute ────────────────────────────────────────────────
   A parameter something still references can be deleted once you say what goes in
   its place: every dependant's expression has the name swapped for the substitute
   and then the parameter goes (`_delete_param` in Parameters.py does both, in that
   order, on Fusion's main thread). One question per blocked name — a set of three
   blocked parameters asks three times, "1 of 3" in the heading — and nothing is
   deleted until the last one is answered, so Cancel at any step leaves everything.

   The substitute has to be an existing parameter, and the field completes against
   them the way the value box's does (same ranking: exact, then prefix, then a match
   starting at a word — see fusion_value's score/matchAnchored), one of the same
   unit ahead of one that isn't. Left out of the list, and refused if typed: anything
   being deleted in this same go, and anything that depends on the name being
   replaced (directly or further down), which would make it reference itself. */
let subst = null;   // { names, queue, subs, i, items, idx, touched, tookKey }
function substOpen(){ return !!document.getElementById("subst-bg"); }

function openSubstitute(names, blocked){
  subst = { names, queue: blocked, subs: {}, i: 0, items: [], idx: -1, touched: false,
            tookKey: takeKeyFocus() };
  renderSubstStep();
}
function closeSubstitute(){
  const m = document.getElementById("subst-bg");
  if (m) m.remove();
  if (subst && subst.tookKey) releaseKeyFocus();
  subst = null;
  publishLiveBottom();
  const t = document.getElementById("tree"); if (t) t.focus();
}
// Everything that depends on `name`, all the way down — none of them can stand in for it.
function substDependants(name){
  const out = new Set(), todo = [name];
  while (todo.length){
    for (const d of paramDepsOf(todo.pop())){
      if (d && !out.has(d)){ out.add(d); todo.push(d); }
    }
  }
  return out;
}
function substCandidates(name){
  const going = new Set(subst.names), deps = substDependants(name);
  return paramRows.filter(p => !going.has(p.name) && !deps.has(p.name));
}
function renderSubstStep(){
  const old = document.getElementById("subst-bg");
  if (old) old.remove();
  const name = subst.queue[subst.i];
  const going = new Set(subst.names);
  // A model-parameter dependant (d12 …) is named by where it lives — "Component /
  // Sketch" from the add-in's depWhere — once per sketch; a user parameter by its name.
  // Each is coloured by its kind: a user parameter (no depWhere) in the parameter
  // colour, a sketch's in the sketch colour (depKind).
  // A sketch's also carries its row key (depKey), for Go To (goToDep).
  const pr = paramByName(name) || {}, where = pr.depWhere || {}, kinds = pr.depKind || {},
        keys = pr.depKey || {};
  const deps = new Map();
  for (const d of paramDepsOf(name)){
    if (!d || going.has(d)) continue;
    const u = where[d] || d;
    if (!deps.has(u)) deps.set(u, where[d] ? { kind: kinds[d] || "", key: keys[d] || "" }
                                           : { kind: "param", param: d });
  }
  const users = [...deps.keys()];
  // One dependant per line — a comma run of "Component / Sketch" names is unreadable.
  const shown = users.slice(0, 6).map(u => depLine(`<b>${escapeHtml(u)}</b>`, deps.get(u))).join("")
              + (users.length > 6 ? `<div>and ${users.length - 6} more</div>` : "");
  const step = subst.queue.length > 1 ? ` (${subst.i + 1} of ${subst.queue.length})` : "";
  document.body.insertAdjacentHTML("beforeend",
    `<div id="subst-bg"><div id="subst" role="dialog">`
    + `<h2>Delete Parameter${step}</h2>`
    + (shown ? `<p><b>${escapeHtml(name)}</b> is used by:</p><div id="subst-users">${shown}</div>`
             : `<p><b>${escapeHtml(name)}</b> is used by something.</p>`)
    + `<p>Substitute a parameter for it there:</p>`
    + `<div id="subst-field"><input id="subst-in" autocomplete="off" spellcheck="false" `
    + `placeholder="Parameter name"><div id="subst-ac" class="hide"></div></div>`
    + `<div id="subst-err"></div>`
    + `<div style="text-align:right;margin-top:14px;">`
    + `<button class="sbtn" id="subst-cancel">Cancel</button> `
    + `<button class="sbtn" id="subst-ok">${subst.i + 1 < subst.queue.length ? "Next" : "Delete"}</button>`
    + `</div></div></div>`);
  const bg = document.getElementById("subst-bg");
  bg.addEventListener("mousedown", e => { if (e.target.id === "subst-bg") closeSubstitute(); });
  document.getElementById("subst-cancel").addEventListener("click", closeSubstitute);
  document.getElementById("subst-ok").addEventListener("click", submitSubstStep);
  const inp = document.getElementById("subst-in");
  inp.addEventListener("input", updateSubstAC);
  inp.addEventListener("keydown", substKey);
  const ac = document.getElementById("subst-ac");
  ac.addEventListener("mousedown", e => {
    const r = e.target.closest(".ac-row"); if (!r) return;
    e.preventDefault(); acceptSubstAC(+r.dataset.i);
  });
  subst.items = []; subst.idx = -1; subst.touched = false;
  inp.focus();
  // Activating the app can pull first responder around under us (see beginRename).
  if (subst.tookKey) setTimeout(()=>{ if (substOpen()) inp.focus(); }, 60);
  publishLiveBottom();
}

const SUBST_WORDS = /[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+/g;
function substWordStarts(name){
  const out = []; let m;
  SUBST_WORDS.lastIndex = 0;
  while ((m = SUBST_WORDS.exec(name)) !== null) out.push(m.index);
  return out;
}
// fusion_value's score(), minus the situation: exact 1000, prefix 600, a verbatim
// match starting at a word 400 (earlier beats later), shorter beats longer.
function substScore(name, q){
  const nl = name.toLowerCase(), ql = q.toLowerCase();
  let s, hits;
  if (nl === ql){ s = 1000; hits = [...name].map((_, i) => i); }
  else if (nl.startsWith(ql)){ s = 600; hits = [...ql].map((_, i) => i); }
  else {
    const starts = new Set(substWordStarts(name));
    let at = -1;
    for (let k = nl.indexOf(ql); k >= 0; k = nl.indexOf(ql, k + 1))
      if (starts.has(k)){ at = k; break; }
    if (at >= 0){
      hits = [...ql].map((_, i) => at + i);
      s = 400 - at * 2;
    } else {
      // A capital typed starts a new word: "bootD" = a word starting "boot", then a
      // later one starting "d" (fusion_value's matchWords). Below a verbatim match.
      const pieces = q.match(/[A-Z]?[^A-Z]*/g).filter(Boolean);
      if (pieces.length < 2) return null;
      const ws = substWordStarts(name);
      let from = 0; hits = [];
      for (const piece of pieces){
        const pl = piece.toLowerCase();
        const k = ws.find(k => k >= from && nl.startsWith(pl, k));
        if (k === undefined) return null;
        if (at < 0) at = k;
        for (let i = 0; i < pl.length; i++) hits.push(k + i);
        from = k + pl.length;
      }
      s = 300 - at * 2;
    }
  }
  return { s: s - name.length * 0.4, hits };
}
function updateSubstAC(){
  const inp = document.getElementById("subst-in");
  const q = inp.value.trim();
  document.getElementById("subst-err").textContent = "";
  subst.touched = false;
  if (!q){ subst.items = []; subst.idx = -1; return renderSubstAC(); }
  const unit = (paramByName(subst.queue[subst.i]) || {}).unit || "";
  subst.items = substCandidates(subst.queue[subst.i])
    .map(p => {
      const m = substScore(p.name, q);
      if (!m) return null;
      if ((p.unit || "") === unit) m.s += 50;
      return { p, s: m.s, hits: m.hits };
    })
    .filter(Boolean)
    .sort((a, b) => b.s - a.s)
    .slice(0, 12);
  // Same rule as the value box: the best row is taken by Enter straight away, unless
  // what's typed already IS a parameter — then Enter means that one.
  const exact = subst.items.some(it => it.p.name === q);
  subst.idx = subst.items.length && !exact ? 0 : -1;
  if (exact && subst.items.length === 1) subst.items = [];
  renderSubstAC();
}
function renderSubstAC(){
  const ac = document.getElementById("subst-ac");
  if (!ac) return;
  if (!subst.items.length){ ac.className = "hide"; ac.innerHTML = ""; return; }
  ac.className = "";
  ac.innerHTML = subst.items.map((it, i) => {
    const set = new Set(it.hits);
    const nm = [...it.p.name].map((ch, k) =>
      set.has(k) ? `<b>${escapeHtml(ch)}</b>` : escapeHtml(ch)).join("");
    const marked = subst.touched && i === subst.idx;
    return `<div class="ac-row${marked ? " sel" : ""}" data-i="${i}">`
      + `<span class="ac-name">${nm}</span>`
      + `<span class="ac-val">${escapeHtml(it.p.expression || "")}</span>`
      + `<span class="ac-mark"></span></div>`;
  }).join("");
  const sel = ac.querySelector(".ac-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
function acceptSubstAC(i){
  const it = subst.items[i];
  if (!it) return false;
  const inp = document.getElementById("subst-in");
  inp.value = it.p.name;
  subst.items = []; subst.idx = -1;
  renderSubstAC();
  inp.focus();
  return true;
}
function substKey(e){
  e.stopPropagation();                        // the tree's keys stay out of the field
  const open = subst.items.length > 0;
  if (e.key === "ArrowDown" || e.key === "ArrowUp"){
    e.preventDefault();
    if (!open){ updateSubstAC(); return; }
    const n = subst.items.length, d = e.key === "ArrowDown" ? 1 : -1;
    subst.idx = subst.touched ? (subst.idx + d + n) % n : Math.max(0, subst.idx);
    subst.touched = true;
    renderSubstAC();
  } else if (e.key === "Enter" || e.key === "Tab"){
    e.preventDefault();
    if (open && subst.idx >= 0 && acceptSubstAC(subst.idx)) return;
    if (e.key === "Enter") submitSubstStep();
  } else if (e.key === "Escape"){
    e.preventDefault();
    if (open){ subst.items = []; subst.idx = -1; renderSubstAC(); }
    else closeSubstitute();
  }
}
function submitSubstStep(){
  const name = subst.queue[subst.i];
  const inp = document.getElementById("subst-in");
  const q = inp.value.trim();
  const err = document.getElementById("subst-err");
  const cands = substCandidates(name);
  const hit = cands.find(p => p.name === q)
           || cands.find(p => p.name.toLowerCase() === q.toLowerCase());
  if (!hit){
    err.textContent = !q ? "Type the parameter to use instead."
      : subst.names.includes(q) ? `“${q}” is being deleted too.`
      : paramByName(q) ? `“${q}” depends on ${name}, so it can't stand in for it.`
      : `There is no parameter called “${q}”.`;
    inp.focus();
    return;
  }
  subst.subs[name] = hit.name;
  if (++subst.i < subst.queue.length){ renderSubstStep(); return; }
  const { names, subs } = subst;
  closeSubstitute();
  runParamDeletes(names, subs);
}

// The levels a parameter dropped into `containerTok` would carry: the owner component,
// then the name-group chain. "" (an empty array) means the drop has no home under this
// convention and the caller must refuse it.
function levelsForContainer(containerTok){
  const ng = nameTokParts(containerTok);
  if (ng) return levelsOfNameGroup(ng);
  const bref = boundsParamRef(containerTok);
  if (bref){
    const own = identOfRef(bref);
    return own ? [own, BOUNDS_FOLDER] : [];
  }
  if (!containerTok || containerTok === ROOT_TOKEN){
    const own = identOfRef(ROOT_TOKEN);
    return own ? [own] : [];
  }
  const ref = compRefOf(containerTok);
  if (!ref) return [];
  const own = identOfRef(ref);
  return own ? [own] : [];
}
// The component a drop on `containerTok` means, as a compRef — the owner the moved
// names get pinned to, since their owner level alone may name several (pinFor).
function ownerForContainer(containerTok){
  const ng = nameTokParts(containerTok);
  if (ng) return ng.ref === ORPHAN_REF ? "" : bucketComp(ng.ref);
  const bref = boundsParamRef(containerTok);
  if (bref) return bref;
  if (!containerTok || containerTok === ROOT_TOKEN) return ROOT_TOKEN;
  return compRefOf(containerTok) || "";
}
// The parameter group a drop on `containerTok` files parsed names into (paramFolderOf)
// — the group itself, or the one a name group inside it is rooted in. null = none: the
// move takes them out of whatever group they were in.
function folderForContainer(containerTok){
  const ng = nameTokParts(containerTok);
  if (ng) return isFolderTok(ng.ref) ? ng.ref : null;
  if (boundsParamRef(containerTok)) return null;
  return (isFolderTok(containerTok) && isParamFolder(containerTok)) ? containerTok : null;
}
// One parameter's new level list, given the group chain it should end up under.
// An orphan's chain already begins with its (missing) owner; everything else keeps
// the owner level it has and only the groups after it change.
function levelsFrom(parts, home, words){
  return home.ref === ORPHAN_REF ? words.slice()
                                 : [parts.levels[0]].concat(words);
}
// The same parameter under a different set of GROUP levels — its own name word kept
// on the end, since that is the row, not where it is filed. Read against the CURRENT
// nameIdx: a name word this parameter has where it sits now (nobody else shares its
// chain) rides along to the new place; one already promoted to a group by a sibling
// stays without one.
// A PINNED last level (parts.lastIs, the double underscore) rides along too — the
// same word is still last — unless `lastIs` says otherwise.
function renamedUnder(parts, levels, lastIs = parts.lastIs){
  const name = ParamNaming.nameWord(parts, nameIdx);
  return ParamNaming.compose({ levels: name ? levels.concat([name]) : levels,
                               type: parts.type, lastIs,
                               anchors: parts.anchors, axes: parts.axes });
}
// Move parsed parameters into `containerTok` (a component, a name group, or the top
// level, which is the root component). Returns false and puts the dialog up if it
// can't be done.
function moveParamsByName(names, containerTok){
  const levels = levelsForContainer(containerTok);
  if (!levels.length){
    showBlocked("Move", "A parameter's group comes from its name, and this isn't "
                      + "somewhere a name can point at. Drop it on a component or "
                      + "on one of the groups its name makes.");
    return false;
  }
  const owner = ownerForContainer(containerTok), folder = folderForContainer(containerTok);
  const plan = [];
  for (const nm of names){
    const parts = partsOf(nm);
    if (!parts) continue;
    plan.push({ from: nm, to: renamedUnder(parts, levels), owner, folder });
  }
  return applyRenamePlan(plan, "Move");
}
// Rename a name group: the word changes in every parameter underneath it, at the one
// level the group sits at. `text` is what was typed — spoken words, which are joined
// back the way capitalise.js would have joined them on the way in.
function renameNameGroup(tok, text){
  const ng = nameTokParts(tok);
  if (!ng || !ng.words.length) return false;
  const word = ParamNaming.identifier(
    String(text || "").split(/\s+/).filter(Boolean)
      .map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(""));
  if (!word || word === ng.words[ng.words.length - 1]) return true;
  const depth = ng.words.length - 1;
  const plan = paramsUnderName(tok).map(({ row, parts, home }) => {
    const words = home.words.slice();
    words[depth] = word;
    return { from: row.name, to: renamedUnder(parts, levelsFrom(parts, home, words)) };
  });
  if (!applyRenamePlan(plan, "Rename")) return false;
  // The group's token IS its name, so the renamed group is a new row: stay picked on it.
  swapSelTok(tok, nameTok(ng.ref, ng.words.slice(0, depth).concat(word)));
  return true;
}
// Move a whole name group into another container: every parameter under it keeps the
// part of its chain BELOW the group and hangs it off the new place instead.
function moveNameGroup(tok, containerTok){
  const ng = nameTokParts(tok);
  if (!ng || !ng.words.length) return false;
  if (containerTok === tok || isDescendantTok(tok, containerTok)) return false;
  const base = levelsForContainer(containerTok);
  if (!base.length){
    showBlocked("Move", "A group comes from the names inside it, so it can only go "
                      + "into a component or into another of these groups.");
    return false;
  }
  const keep = ng.words.length - 1;            // the levels above it are replaced
  const owner = ownerForContainer(containerTok), folder = folderForContainer(containerTok);
  const plan = paramsUnderName(tok).map(({ row, parts, home }) => ({
    from: row.name,
    to: renamedUnder(parts, base.concat(home.words.slice(keep))), owner, folder
  }));
  return applyRenamePlan(plan, "Move");
}
// Explode a name group: its own word comes out of every name underneath, so its
// parameters and its subgroups lift into whatever held it. The group stops existing
// because nothing names it any more — there is no row to delete.
function explodeNameGroup(tok){
  const ng = nameTokParts(tok);
  if (!ng || !ng.words.length) return false;
  const depth = ng.words.length - 1;
  const plan = paramsUnderName(tok).map(({ row, parts, home }) => {
    const words = home.words.slice();
    words.splice(depth, 1);
    const levels = levelsFrom(parts, home, words);
    // Taking the last level out would leave a name with no feature at all, which the
    // convention has no reading for. checkRenamePlan refuses the empty target.
    // A pinned group that IS the one exploding takes its pin with it.
    const pin = parts.lastIs === "group" && depth === home.words.length - 1 ? undefined : parts.lastIs;
    return { from: row.name, to: levels.length ? renamedUnder(parts, levels, pin) : "" };
  });
  return applyRenamePlan(plan, "Explode");
}
// Rename a parsed parameter from what its ROW says — the leaf, not the whole name.
// Its levels are wherever it is filed and are not the rename box's business.
function renameParamLeaf(tok, text){
  const nm = paramNameOf(tok), parts = partsOf(nm);
  if (!parts) return "";
  // `parts` as keep: the row hides a direction no sibling needs, so text typed back
  // without one keeps the one the parameter has.
  const leaf = ParamNaming.parseLeaf(text, parts);
  if (!leaf){
    showBlocked("Rename",
      `“${text}” isn't a measurement this convention knows.\n`
      + `A row reads as its name and its type — “Curve - Inset” — with the direction `
      + `after them only where it tells two apart, and the groups it sits in supply `
      + `the rest of the name.`);
    return "";
  }
  const groups = ParamNaming.groupLevels(parts, nameIdx);
  // The pin stays only where the last level is still what it pinned: a name typed onto
  // a pinned group's row is a new name word, unpinned.
  const lastIs = leaf.name ? (parts.lastIs === "name" ? "name" : undefined)
                           : (parts.lastIs === "group" ? "group" : undefined);
  const to = ParamNaming.compose({ levels: leaf.name ? groups.concat([leaf.name]) : groups,
                                   type: leaf.type, lastIs,
                                   anchors: leaf.anchors, axes: leaf.axes });
  // The new NAME on success, "" on a refusal — the caller's cursor has to follow the
  // row, and the row's token is spelled out of the name.
  return applyRenamePlan([{ from: nm, to }], "Rename") ? to : "";
}
// ── Where a brand-new parameter lands ────────────────────────────────────────
// The rule, in order:
//   • no group open                       → the component you are in
//   • exactly one group open              → that group
//   • several open, one holding selection → that one
//   • several holding selection, or none holding any → the last one opened
// "Open" means ON SHOW — expanded, and drawn, with every ancestor expanded too. That is
// precisely the question flatten() already answers, so it is read rather than asked a
// second way. A group opened inside another is naturally preferred by the recency rule,
// since the outer one had to be opened first to reach it.
// Only the groups of the component you are IN count. The rooted view also draws the
// child components and whatever groups are open inside them, and an open group in a
// child used to win here — which is how a parameter made while inside a component
// landed a level down, in one of its children.
// A new parameter never lands in Bounds (or a group inside it) just because it is open:
// Bounds is fed by the bounds filing and by a deliberate drag, not by where you were.
function inBoundsFolder(tok){
  for (let f = isFolderTok(tok) ? folderOf(tok) : null, i = 0;
       f && i < 64; f = f.parent ? folderDefs[f.parent] : null, i++)
    if ((f.name || "") === BOUNDS_FOLDER) return true;
  return false;
}
function openGroupRows(){
  const rows = flatten();
  const mine = compRefOf(activeContainerTok());
  const out = [];
  for (let i = 0; i < rows.length; i++){
    const n = rows[i].node;
    if (n && n.type === "folder" && !collapsed.has(n.token)
        && (folderOf(n.token) || {}).comp === mine
        && paramContainerOk(n.token) && !inBoundsFolder(n.token)) out.push({tok:n.token, node:n, i});
  }
  return out;
}
// Is anything in this group selected? Its OWN rows only, deliberately not recursive: a
// pick inside a subgroup is in the subgroup, and counting it for every group above it
// as well would make an outer group tie with the inner one on "has things selected" and
// then win on recency, since the outer had to be opened first to reach the inner.
function groupHoldsSelection(node){
  if (!selected.size) return false;
  for (const n of node.children || []) if (selected.has(n.token)) return true;
  return false;
}
function newParamContainer(){
  const open = openGroupRows();
  if (!open.length) return state.activeToken || "";     // "" = the top level
  if (open.length === 1) return open[0].tok;
  const withSel = open.filter(g => groupHoldsSelection(g.node));
  const pool = withSel.length ? withSel : open;
  if (pool.length === 1) return pool[0].tok;
  let best = pool[0];
  for (const g of pool){
    const a = expandSeq[g.tok] || 0, b = expandSeq[best.tok] || 0;
    // A group that has simply never been closed carries no stamp and so sorts oldest;
    // the lower row wins that tie, which is the more deeply nested of the two.
    if (a > b || (a === b && g.i > best.i)) best = g;
  }
  return best.tok;
}

/* The document root can hold folders too, and it is not a row — there is no Top Level
   node, so it has no token of its own. ROOT_TOKEN stands in for it wherever a container
   token is wanted, which is what lets the bounds of the root component be gathered up
   the same way a component's are. */
const ROOT_TOKEN = "__root__";

function withFolders(nodes){
  // Unfiled parameters sit at the top level, above the top-level components.
  return graftFeatures(graftFolders(ROOT_TOKEN, nodes));
}

function cloneWithFolders(n){
  if (n.type !== "component") return Object.assign({}, n);
  return Object.assign({}, n, { children: graftFolders(compRefOf(n.token), n.children) });
}
// The refs of every "(disregard)" component lifted out of `kids` — recursively, since
// one nested inside another lifts through both. Their folders and parameters are
// drawn in the container they were lifted into, the same as their sub-components:
// a component tagged disregard after its parameters were grouped used to take those
// groups out of the tree with it, 15 parameters in one document.
function liftedRefsIn(kids){
  const out = [];
  for (const c of (kids || [])){
    if (disregardMode(c) !== "self") continue;
    out.push(compRefOf(c.token));
    out.push.apply(out, liftedRefsIn(c.children));
  }
  return out;
}
// A folder whose owner is not in the tree — a component that was deleted, or that the
// timeline is rolled back past — is drawn at the document root rather than nowhere: a
// folder that cannot be reached cannot be emptied, moved or dissolved, and the
// parameters filed in it are simply gone from view. Its `comp` is NOT rewritten, so it
// goes home the moment its component is back.
function orphanFolder(f){
  return !!f.comp && f.comp !== ROOT_TOKEN && !compTokOf(f.comp);
}

/* Build one container's child list: its own parameters, its leaves, and its folders
   with the leaves that belong in them. Shared by every component AND by the document
   root, which is the whole reason it is a function — the root used to be assembled
   separately and so could not hold a folder at all. */
function graftFolders(compRef, rawKids){
  const refs = new Set([compRef].concat(liftedRefsIn(rawKids)));
  const kids = liftDisregarded(rawKids);
  let mine = [];
  for (const r of refs) mine = mine.concat(paramNodesFor(r));
  // The name-group side: parameters whose names say they belong here, in the groups
  // their own levels make. A "(disregard)" component's lift into this container
  // carries its name groups with it, the same as its folders and its hand-filed
  // parameters. The root container also draws the orphan chains — parameters naming
  // an owner component this document doesn't have.
  let named = [];
  for (const r of refs) named = named.concat(nameGroupNodes(r, ""));
  // Rolled back, neither is drawn: an owner that isn't there may just not exist yet
  // (unbornRef).
  const rolled = !!state.rolledBack;
  if (compRef === ROOT_TOKEN && !rolled) named = named.concat(nameGroupNodes(ORPHAN_REF, ""));
  const defs = Object.values(folderDefs).filter(f =>
    refs.has(f.comp) || (compRef === ROOT_TOKEN && !rolled && orphanFolder(f)));

  const live = new Set(defs.map(f => f.id));
  // The container's Geometry group. A "(disregard)" component lifted into here brings
  // its own group along; its bodies pool into the container's, so one Geometry row is
  // drawn per container, as for everything else that lifts.
  const geos = kids.filter(c => c.type === "geometry");
  const geo  = geos.find(g => geoRefOf(g) === compRef) || geos[0] || null;
  // Likewise one (timeline) Geometry group: the container's own. A lifted component's
  // features are not pooled — they are its own component's, and stay in its timeline.
  const fxs = kids.filter(c => c.type === "features");
  const fx  = fxs.find(g => geoRefOf(g) === compRef) || fxs[0] || null;
  // A folder's parent chain ends either at the component ("") or at the Geometry group
  // (GEO_PARENT); a parent that no longer exists ends the chain at the component. With
  // no Geometry row to draw into, the geo side collapses onto the component too. A
  // parameter group only ever sits in another parameter group, and a sketch folder
  // never does — a parent of the other kind ends the chain at the component as well.
  const parentOf = f => live.has(f.parent)
                          ? (isParamFolderDef(folderDefs[f.parent]) === isParamFolderDef(f)
                               ? f.parent : "")
                      : (f.parent === GEO_PARENT && geo && !isParamFolderDef(f)) ? GEO_PARENT : "";
  const sideOf = f => {
    if (isParamFolderDef(f)) return "params";
    const walked = new Set();
    while (f && !walked.has(f.id)){
      walked.add(f.id);
      const p = parentOf(f);
      if (p === GEO_PARENT) return "geo";
      if (p === "") return "comp";
      f = folderDefs[p];
    }
    return "comp";
  };
  const side = {};
  for (const f of defs) side[f.id] = sideOf(f);
  const inFolder = {};                       // folder id -> its leaf nodes
  const rest = [], loose = [];               // container level / loose in Geometry
  const fileLeaf = (c, want) => {
    const fid = folderIdOfLeaf(c.token);
    if (fid && live.has(fid) && side[fid] === want){
      (inFolder[fid] = inFolder[fid] || []).push(Object.assign({}, c)); return true;
    }
    return false;
  };
  for (const c of kids){
    if (c.type === "geometry"){
      if (c !== geo) { for (const b of (c.children || [])) if (!fileLeaf(b, "geo")) loose.push(Object.assign({}, b)); }
      continue;
    }
    if (c.type === "features" && c !== fx) continue;
    if (c.type === "sketch" && fileLeaf(c, "comp")) continue;
    if (c.type === "body"){ if (!fileLeaf(c, "geo")) loose.push(Object.assign({}, c)); continue; }
    rest.push(cloneWithFolders(c));
  }
  if (geo) for (const b of (geo.children || [])) if (!fileLeaf(b, "geo")) loose.push(Object.assign({}, b));
  // A component with child components of its own never holds sketches directly — its
  // sketch-side folders are dead weight in geometry mode, no matter what's ignored.
  // One value for the whole call: every folder `mk` builds here (nested ones included,
  // since a folder's owner is this same compRef all the way down its parent chain) is
  // owned by this same container. `kids` is post-liftDisregarded, so a child component
  // lifted OUT of a "(disregard)" wrapper still counts for the parent it lands in.
  const ownerHasComps = kids.some(c => c.type === "component");
  // A folder whose parent has gone (or that somehow points into its own subtree) falls
  // back to the container's own level, so a folder can never become unreachable.
  const seen = new Set();
  // A component's Bounds parameter group IS its Bounds level: the name group
  // <Owner>_Bounds_… would make draws inside it instead of as a second "Bounds" row.
  const boundsNamed = {};
  for (const f of defs){
    if (!isBoundsHome(f, f.comp, true)) continue;
    const at = named.findIndex(n => n.type === "namegroup" && n.comp === f.comp
                                    && n.path === BOUNDS_FOLDER);
    if (at >= 0) boundsNamed[f.id] = named.splice(at, 1)[0].children || [];
  }
  const mk = (f) => { seen.add(f.id);
    const params = isParamFolderDef(f);
    return { token: FOLDER_PREFIX + f.id, type:"folder", name:f.name, comp:compRef,
             visible:true, ownerHasComps, params,
             children: (params ? paramNodesFor(FOLDER_PREFIX + f.id)
                                  .concat(nameGroupNodes(FOLDER_PREFIX + f.id, "")) : [])
                         .concat(boundsNamed[f.id] || [], build(f.id), inFolder[f.id] || []) }; };
  const build = (pid) => defs.filter(f => !seen.has(f.id) && parentOf(f) === pid).map(mk);
  const roots = build("");
  const geoKids = geo ? loose.concat(build(GEO_PARENT)) : [];
  const stray = defs.filter(f => !seen.has(f.id)).map(mk);
  const geoRow = geo ? [Object.assign({}, geo, { children: geoKids })] : loose;
  return mine.concat(named, rest, geoRow, roots, stray);
}

// The component a container row belongs to, as a compRef — a folder's owner, or the
// component itself. "" for anything that can't hold a folder. The document root has no
// row and so no token; it still holds folders, and returning "" for it was what made
// ⌘G do nothing at the top level (addFolder reads "" as "nothing to hang a folder off").
function containerCompOf(tok){ return compRefOf(tok); }
// Rows that hold other rows: a component, a folder, the Geometry group, or one of the
// groups a structured parameter name makes.
function isContainerNode(n){
  return !!n && (n.type === "component" || n.type === "folder" || n.type === "geometry"
                 || n.type === "namegroup");
}
// Move `tok` into `containerTok` in the overlay. Position is handled separately by
// orderMove, which must run FIRST — it reads the row's current parent.
function setContainer(tok, containerTok){
  if (isParamTok(tok)){                    // parameters are filed, not re-parented
    const nm = paramNameOf(tok), ref = paramRefOf(containerTok);
    if (ref) paramPlace[nm] = ref; else delete paramPlace[nm];
    return;
  }
  const fid = isFolderTok(containerTok) ? folderIdOf(containerTok) : "";
  if (isFolderTok(tok)){
    const f = folderOf(tok);
    if (f){
      f.parent = isGeoTok(containerTok) ? GEO_PARENT : fid;
      const newComp = containerCompOf(containerTok);
      if (newComp && newComp !== f.comp) retagFolderComp(tok, newComp);
    }
  }
  else { setLeafFolder(tok, fid); stampBodyOf(tok); }
}
// `comp` is the component a folder draws inside. Nested folders have to follow when
// their ancestor moves, or they would be looked up under a component that no longer
// holds their parent and would vanish.
function retagFolderComp(tok, comp){
  const f = folderOf(tok);
  if (!f) return;
  f.comp = comp;
  for (const g of Object.values(folderDefs))
    if (g.parent === f.id) retagFolderComp(FOLDER_PREFIX + g.id, comp);
}
function detachFromOrder(tok){
  const p = parentTokOf(tok);
  if (siblingOrder[p]) siblingOrder[p] = siblingOrder[p].filter(t => t !== tok);
}

// ⌘G — new group. What it does depends on what is selected:
//   • one GROUP, open  → a new group inside it (a component or the Geometry group
//                         counts as open: neither can be wrapped)
//   • one GROUP, shut  → a new group around it
//   • rows of ONE kind → a new group around them, taking the topmost one's place.
//                         Parameters, sketches and bodies each get their own groups, so
//                         a selection mixing kinds is refused, never silently trimmed.
//   • nothing          → an empty group at the top level of the component you are IN
//                         — not wherever the cursor happens to sit.
// `at` (a menu row) overrides all of that: the folder goes in that row. Opens straight
// into rename.
// Its kind follows what it is made from: parameters make a parameter group, sketches a
// sketch folder, bodies a folder in the Geometry group; `params` forces a parameter
// group (the menu's New Parameter Group). Parameters whose names parse (and their name
// groups) are FILED into it like any other — never renamed: the group is not a level of
// their names (paramFolderOf). The container climbs until it is one that kind of
// folder may sit in.
function addFolder(params, at){
  if (!params && !at && groupFeatureSel()) return;    // picked features → a timeline group
  if (!foldersDoc) return;
  const rows = flatten().filter(r => !r.placeholder).map(r => r.node.token);
  const n = at ? nodeByTok(at) : null;
  const sel = at ? [] : rows.filter(t => selected.has(t));
  const lone = sel.length === 1 ? nodeByTok(sel[0]) : null;
  let container, wrap = [];
  if (lone && (lone.type === "component" || lone.type === "geometry"
               || (isGroupTok(lone.token) && !collapsed.has(lone.token)))){
    if (lone.type === "namegroup"){
      showBlocked("New group", "A group made from parameter names only exists while a "
        + "name points at it, so an empty one can't be made inside it. Select the "
        + "parameters you want grouped and press ⌘G.");
      return;
    }
    container = lone.token;
    if (!params) params = lone.type === "folder"    ? !!lone.params
                        : lone.type === "component" ? childCompsIn(lone.token) : false;
  } else if (sel.length){
    const kinds = new Set(sel.map(t => groupKindOf(nodeByTok(t))));
    const kind = kinds.size === 1 ? [...kinds][0] : "";
    if (!kind || (params && kind !== "params")){
      showBlocked("New group", kinds.has("")
        ? "Components, Origin and the Bodies and Geometry groups can't go inside a group."
        : "Parameters, sketches and bodies each get their own groups. "
          + "Select just one kind and press ⌘G again.");
      return;
    }
    // Drop any row whose own group is being wrapped too — it travels inside it.
    wrap = sel.filter(t => !sel.some(o => o !== t && isDescendantTok(o, t)));
    params = kind === "params";
    container = parentTokOf(wrap[0]);
  } else {
    // Nothing selected (the normal state when this arrives FORWARDED from the
    // viewport): the menu's row if there is one, else the top level of the component
    // you are IN — never the top row, which inside a component with children is one
    // of those children.
    container = n ? (isContainerNode(n) ? n.token : parentTokOf(n.token))
                  : activeContainerTok();
    if (!params) params = n ? isParamSide(n) : childCompsIn(container);
  }
  const fits = t => isNameTok(t) ? false
                  : params ? (isFolderTok(t) ? isParamFolder(t) : !isGeoTok(t))
                  : !(isFolderTok(t) && isParamFolder(t));
  for (let i = 0; !fits(container) && i < 64; i++) container = parentTokOf(container);
  if (!containerCompOf(container)) return;       // nothing to hang a folder off

  const id = newFolderId(), ftok = FOLDER_PREFIX + id;
  folderDefs[id] = { id, name:"Folder", comp: containerCompOf(container),
                     parent: isFolderTok(container) ? folderIdOf(container)
                           : isGeoTok(container)    ? GEO_PARENT : "",
                     hidden:false, kind: params ? "params" : "" };
  orderMove(ftok, container, wrap[0] || null, wrap.length ? "before" : "end");
  // A name group is filed by filing every parameter under it (paramFolderOf) — their
  // names, and so the group, are left exactly as they were.
  for (const t of wrap){
    detachFromOrder(t);
    if (isNameTok(t)){ for (const { row } of paramsUnderName(t)) paramPlace[row.name] = ftok; }
    else setContainer(t, ftok);
  }
  siblingOrder[ftok] = wrap.slice();
  // Open the new folder AND whatever it landed in, so its row is actually on screen
  // — beginRename needs the row in the DOM to put a name field on it.
  collapsed.delete(ftok); cancelCollapseTimer(ftok);
  collapsed.delete(container); cancelCollapseTimer(container);
  saveOrder(); saveCollapsed(); saveFolders();
  selected.clear(); cursorTok = ftok;
  refreshFolders();
  scrollTokIntoView(ftok);
  beginRename(ftok);          // name selected, ready to type over
}
// Which kind of group a row goes in: "params", "sketch" (sketches and the folders
// outside Geometry) or "geo" (bodies and the folders inside it). "" for rows no group
// can hold — components, Origin, the Geometry group itself.
function groupKindOf(n){
  if (!n) return "";
  if (isParamSide(n)) return "params";
  if (n.type === "body" || (n.type === "folder" && geoFolderOf(n.token))) return "geo";
  if (n.type === "sketch" || n.type === "folder") return "sketch";
  return "";
}
// Does this container have child components? An empty sketch folder in such a
// component is never drawn (render()), so ⌘G there makes a parameter group instead —
// otherwise it would make something invisible and look like it did nothing.
function childCompsIn(tok){
  const n = (!tok || tok === ROOT_TOKEN) ? null : nodeByTok(tok);
  const kids = n ? (n.children || []) : (!tok || tok === ROOT_TOKEN) ? state.tree : [];
  return kids.some(c => c.type === "component");
}

// ── ⌘X / ⌘V — move parameters between groups ─────────────────────────────────
// Keyboard drag-and-drop, and it runs through the very machinery a real drop does —
// moveKind decides what is legal, orderMoveMany + setContainer carry it out — so the
// two routes can never disagree about where a parameter may land.
//
// PARAMETERS (and the parameter-side groups that hold them) ONLY, on purpose. A parameter belongs to the document rather than to any
// component, so re-filing one is pure overlay and always legal wherever you point it.
// Sketches and bodies belong to a component Fusion owns, and most of the moves you
// could ask for there are refused; a drag shows you that before you commit to it,
// while a keyboard paste with no drop target in sight would just look broken.

// What ⌘X would take right now, in tree order. Its own function because the flag the
// add-in reads to decide whether ⌘X is the browser's business (see syncUiFlags) has to
// answer the same question the action does — two spellings of "is there anything to
// cut" would eventually disagree, and the symptom would be a swallowed ⌘X.
//
// Groups go too — a parameter group (hand-made, `kind: "params"`) or a name group (the
// groups a structured name makes), empty or not — and take everything inside them. A
// row whose group is ALSO picked is dropped from the list: the group carries it, and
// moving it a second time would either fight the group's rename or re-file it out of
// the group it just travelled with.
function cutExists(t){
  if (isParamTok(t)) return !!paramByName(paramNameOf(t));
  if (isNameTok(t))  return paramsUnderName(t).length > 0;
  if (isFolderTok(t)){
    const f = folderOf(t);
    // Bounds is fed by the bounds filing and found by its name; it stays where it is.
    return isParamFolderDef(f) && (f.name || "") !== BOUNDS_FOLDER;
  }
  return false;
}
function cuttableToks(){
  const order = flatten().filter(r => !r.placeholder).map(r => r.node.token);
  const picked = (selected.size ? [...selected] : (cursorTok ? [cursorTok] : []))
                   .filter(cutExists);
  return picked
           .filter(t => !picked.some(g => g !== t && isDescendantTok(g, t)))
           .sort((a, b) => order.indexOf(a) - order.indexOf(b));
}
// ── ⌘C — copy a parameter's name, to paste as a chip elsewhere ────────────────
// The preferred way to bring a parameter from one component's expression into
// another's: paste inserts the PARAMETER ITSELF (a chip, a real reference — see
// fusion_value's paste handler), never a copy of its current value, so the
// dependency this creates stays live. Reuses ⌘X's own "what's under the cursor or
// selection, in tree order" rule (see cuttableToks) so the two behave alike.
function copySelectionName(){
  const names = cuttableToks().map(paramNameOf).filter(Boolean);
  if (!names.length) return;
  fetch(API + "/clipboard", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ text: names.join(" ") })
  }).catch(() => {});
}

function cutSelection(){
  const picked = cuttableToks();
  // Nothing cuttable under the cursor is a MISS, not a cancel: whatever was already
  // cut stays cut, so ⌘X on the wrong row doesn't quietly lose the last one.
  if (!picked.length) return;
  cutToks = picked;
  render();
}
function clearCut(){
  if (!cutToks.length) return;
  cutToks = [];
  render();
}
// Which container a paste lands in: the row the window is on if it can hold things,
// else whatever container that row sits in — so pasting onto a parameter files it
// beside that one. The same rule ⌘G uses to decide where a new folder goes.
function pasteTarget(n){
  if (!n) return activeContainerTok();
  return isContainerNode(n) ? n.token : parentTokOf(n.token);
}
// The right-click menu's Paste reads the row it was opened on a little differently from
// ⌘V: an OPEN group (or an empty one — there is nothing to sit alongside) takes the
// rows inside it; a shut group, or any other row, takes them alongside it, in whatever
// holds it. A shut group is a row like any other in the list, so pasting on it puts
// the rows next to it rather than somewhere you cannot see.
function ctxPasteTarget(n){
  if (!n) return activeContainerTok();
  const open = isContainerNode(n)
               && (!(n.children && n.children.length) || !collapsed.has(n.token));
  return open ? n.token : (parentTokOf(n.token) || n.token);
}
// What a paste into `container` would do with each cut row, the illegal ones dropped.
// A row already in this container is a pure REORDER rather than a re-filing — the
// same distinction dragMoves draws for a drop between siblings. It matters because
// the paste is positional now: pasting under a row in the group you cut from has to
// move the row there, not decide there is nothing to do.
function pastePlan(container){
  return cutToks
    .filter(cutExists)
    .map(t => ({ tok: t, kind: parentTokOf(t) === container ? "order"
                                                            : moveKind(t, container) }))
    .filter(m => m.kind);
}
function pasteCut(anchor, container){
  if (!cutToks.length) return;
  if (anchor === undefined) anchor = anchorNodeOrNull();  // nothing picked → the component you are in
  if (container === undefined) container = pasteTarget(anchor);
  const plan = pastePlan(container);
  cutToks = [];
  if (!plan.length){ render(); return; }

  // Structured parameters and name groups move by being RENAMED (moveKind gives them
  // "name"), and the rename decides whether the paste happens at all — a collision
  // refuses the lot. So they go first, and on a refusal nothing else goes either: the
  // same all-or-nothing a drop gets, for the same reason. Their place inside the target
  // is the group chain in their names, which the rename has just set, so there is no
  // order to write for them and their old tokens (spelled out of the old names) are
  // not carried below. Groups one at a time, each with its own check, as applyDrop
  // does: two groups' plans can't be merged without fighting over each other's rows.
  const named = plan.filter(m => m.kind === "name");
  const rest  = plan.filter(m => m.kind !== "name");
  if (named.length){
    for (const g of named.filter(m => isNameTok(m.tok)))
      if (!moveNameGroup(g.tok, container)) return;
    const params = named.filter(m => isParamTok(m.tok)).map(m => paramNameOf(m.tok));
    if (params.length && !moveParamsByName(params, container)) return;
    collapsed.delete(container); cancelCollapseTimer(container); saveCollapsed();
    if (!rest.length) return;       // applyRenamePlan has already refreshed the tree
  }
  const toks = rest.map(m => m.tok);

  // Position: directly under the row the window is on. If that row IS the container
  // (a group or a component), "under it" is the first line inside it; if it is a row
  // WITHIN the container, they go straight after it. Either way the pasted rows end up
  // on the line below the one that was selected, which is what the gesture looks like.
  const moving = new Set(toks);
  const aTok = anchor && anchor.token;
  let relTok = null, where = "end";
  if (aTok && aTok !== container && !moving.has(aTok)){
    relTok = aTok; where = "after";
  } else {
    const kids = childrenTokensOf(container).filter(t => !moving.has(t));
    if (kids.length){ relTok = kids[0]; where = "before"; }
  }
  orderMoveMany(toks, container, relTok, where);
  // No folder-override dance here, unlike applyDrop: that remembers an item's own
  // visibility while a hidden group forces it off, and a parameter has no visibility
  // for a group to force.
  for (const m of rest) if (m.kind === "virtual") setContainer(m.tok, container);
  // Open the target, or the rows land somewhere you cannot see them.
  collapsed.delete(container); cancelCollapseTimer(container);
  saveOrder(); saveCollapsed(); saveFolders();
  cursorTok = toks[toks.length - 1];
  refreshFolders();
  // The pasted rows become the whole selection — whatever was picked before is dropped,
  // in the VIEWPORT too. setSelection is what does that second part: a parameter has no
  // viewport counterpart, so the push it sends is an empty one, and its pending-selection
  // hold is what stops the empty viewport coming back and wiping the browser's own
  // selection a poll later.
  setSelection(toks);
  scrollTokIntoView(cursorTok);
}

// ── ⇧↑ / ⇧↓ — move the selection up and down the list ────────────────────────
// The keyboard twin of dragging a row, and it goes through the same machinery
// (orderMoveMany on the siblingOrder overlay) so the two can never disagree about what
// "one place up" means. Window-only, like the ordering overlay itself: Fusion has no
// API to reorder occurrences, bodies or sketches, so this is the only thing there is.
//
// ONE PARENT AT A TIME. Rows picked from two different groups have no single list to be
// moved within, so the move is taken to be about the anchor's own group and anything
// selected outside it is left where it is — rather than refusing outright, which is
// what a mixed selection would otherwise turn every press into.
//
// Rows are pinned in bands inside a container (orderedChildren / pinBand), so the
// neighbour a row swaps with has to be in the same band; a parameter at the top of the
// parameters has nowhere further up to go, and the press does nothing rather than
// writing an order the renderer will ignore.
function moveSelectionInList(delta){
  const anchor = anchorNodeOrNull();
  if (!anchor) return;
  // Never on a timeline item: that would reorder the REAL timeline, and a stray ⇧↑ once
  // moved an intersect before the extrude it trimmed and rewrote the model. Dragging
  // (a deliberate gesture, probed first) is the only way to reorder features from here.
  if (anchor.type === "feature") return;
  const parent = parentTokOf(anchor.token);
  const picked = selected.size ? rowsInTreeOrder(selected) : [anchor.token];
  const toks = picked.filter(t => parentTokOf(t) === parent);
  if (!toks.length) return;

  const sibs = childrenTokensOf(parent);
  const moving = new Set(toks);
  const idx = toks.map(t => sibs.indexOf(t)).filter(i => i >= 0);
  if (!idx.length) return;
  // …the same goes for every band pinBand draws (components, Geometry, sketches,
  // Bounds, Origin): a row only ever swaps within its own.
  const band = pinBand(nodeByTok(toks[0]));
  const samePin = t => pinBand(nodeByTok(t)) === band;

  let relTok = null, where;
  if (delta < 0){
    let i = Math.min.apply(null, idx) - 1;
    while (i >= 0 && moving.has(sibs[i])) i--;
    if (i < 0 || !samePin(sibs[i])) return;
    relTok = sibs[i]; where = "before";
  } else {
    let i = Math.max.apply(null, idx) + 1;
    while (i < sibs.length && moving.has(sibs[i])) i++;
    if (i >= sibs.length || !samePin(sibs[i])) return;
    relTok = sibs[i]; where = "after";
  }

  orderMoveMany(toks, parent, relTok, where);
  saveOrder();
  refreshFolders();
  scrollTokIntoView(cursorTok || toks[0]);
}

// Delete / Backspace with no modifier — FOLDERS ONLY. Geometry still needs ⌘D, so a
// stray keypress in this window can never delete anything from the model.
function deleteFolderCursor(){
  const picked = selected.size ? [...selected] : (cursorTok ? [cursorTok] : []);
  const fgs = picked.filter(isFGroupTok);
  if (fgs.length) explodeFeatureGroups(fgs);
  const toks = picked.filter(isFolderTok);
  if (toks.length) deleteFolders(toks);
}

// Ungroup (the `ungroup` master shortcut, ⌘⇧G): folders, timeline groups and name
// groups — but only when every selected row IS a group. A row picked inside a group
// never ungroups the group around it.
function ungroupSelection(){
  const picked = selected.size ? [...selected] : (cursorTok ? [cursorTok] : []);
  const isNG = t => { const n = nodeByTok(t); return !!n && n.type === "namegroup"; };
  if (!picked.length || !picked.every(t => isFolderTok(t) || isFGroupTok(t) || isNG(t))) return;
  const fgs = picked.filter(isFGroupTok);
  if (fgs.length) explodeFeatureGroups(fgs);
  const toks = picked.filter(isFolderTok);
  if (toks.length) deleteFolders(toks);
  // A name group is a rewrite of the names under it, one plan per group.
  for (const t of picked.filter(isNG)) explodeNameGroup(t);
}

// Deleting a folder dissolves it: its contents move back up into whatever contained
// the folder, in its place. Never reaches Fusion.
function deleteFolders(toks){
  for (const t of toks){
    const f = folderOf(t); if (!f) continue;
    const parentTok = parentTokOf(t);
    const node = nodeByTok(t);
    const kids = node ? orderedChildren(t, node.children || []).map(c => c.token) : [];
    const list = childrenTokensOf(parentTok);
    const i = list.indexOf(t);
    siblingOrder[parentTok] = (i < 0 ? list.concat(kids)
                                     : list.slice(0, i).concat(kids, list.slice(i + 1)))
                              .filter(x => x !== t);
    const pid = isFolderTok(parentTok) ? folderIdOf(parentTok) : "";
    // Re-home everything pointing at this folder, including rows not currently in the
    // tree (a rolled-back timeline, another document's leftovers).
    for (const k of Object.keys(leafFolder))
      if (leafFolder[k] === f.id){ if (pid) leafFolder[k] = pid; else delete leafFolder[k]; }
    // Nested folders keep their side: one dissolved at the top of the Geometry group
    // leaves its subfolders in the group, not out at the component.
    const gpid = pid || (isGeoTok(parentTok) ? GEO_PARENT : "");
    // Parameters are filed by CONTAINER REF rather than by folder id, so they need
    // their own pass — without it, dissolving a folder left its parameters pointing at
    // a folder that no longer exists and they dropped out of the tree entirely.
    const up = paramRefOf(parentTok);
    for (const k of Object.keys(paramPlace))
      if (paramPlace[k] === t){ if (up) paramPlace[k] = up; else delete paramPlace[k]; }
    for (const g of Object.values(folderDefs)) if (g.parent === f.id) g.parent = gpid;
    delete folderDefs[f.id];
    delete siblingOrder[t];
    delete ordKeys[saveKeyOf(t)];    // …and from the persisted side, which is a union
    collapsed.delete(t); cancelCollapseTimer(t);
    selected.delete(t);
    if (cursorTok === t) cursorTok = parentTok;
  }
  saveOrder(); saveCollapsed(); saveFolders();
  refreshFolders();
}

// Clicking a folder opens or closes just that folder — its siblings are left alone, so
// several groups in a component can be open at once. Clicking the component above
// still closes them all (revealActiveSpine).
function toggleFolderOpen(tok){
  if (collapsed.has(tok)){ collapsed.delete(tok); cancelCollapseTimer(tok); revealSelectionIn(tok); }
  else { collapsed.add(tok); rememberCollapse(tok); clearSelectionForCollapse(tok); }
  saveCollapsed(); render();
}

// Every body/sketch at or under `node`, however deeply foldered — `node` itself when
// it IS one, so this works for a dragged row as well as for a whole folder.
function leavesUnder(node){
  const out = [];
  (function w(n){
    if (!n) return;
    if (n.type === "param") return;                 // no visibility of its own
    if (n.type === "sketch" || n.type === "body"){ out.push(n); return; }
    for (const c of (n.children || [])) w(c);
  })(node);
  return out;
}

// A group has no light bulb of its own, and its hide is now exactly one flag on the
// group. It is NOT written onto the members' bulbs here.
//
// It used to be: hiding a group switched every member off in Fusion and kept a `snap`
// of what each had been set to, so unhiding could put them back. That made the group
// hide indistinguishable from a hide of each item — the add-in read the dark bulbs and
// filed them all as "the user hid this" — and it needed a second loop
// (enforceFolderHides) re-imposing the rule on every poll, with a retry timer, because
// isolate, the sketch-edit self-heal and Fusion's own browser kept switching members
// back on underneath it.
//
// The add-in reads this flag itself and derives every leaf bulb from it, so all of that
// is gone: flip the flag, save, and ask for a policy pass. The members' bulbs are
// flipped optimistically here only so the rows repaint on the gesture rather than on
// the round trip — the add-in is what actually decides them.
function toggleVisFolder(tok){
  // A group's hide acts on the sketches and bodies inside it, and a parameter group has
  // none — nothing to act on, so it does nothing at all.
  const f = folderOf(tok); if (!f || isParamFolderDef(f)) return;
  f.hidden = !f.hidden;
  const now = Date.now();
  for (const l of leavesUnder(nodeByTok(tok)))
    pendingVis[l.token] = { visible: !f.hidden && !l.userHidden, ts: now };
  saveFolders(); applyPendingVis(); refreshFolders();
  post({action:"regroup"});
}

// The nearest enclosing group that is hidden, if any — walked through folderDefs
// rather than the tree, because a drag's destination isn't in the tree until the next
// rebuild. Used only to decide whether a move needs a policy pass; nothing writes a
// member's bulb on its behalf any more.
// The Geometry-group side, walked through folderDefs the same way: is this folder (or
// one above it) filed in the Geometry group?
function geoFolderOf(containerTok){
  let f = isFolderTok(containerTok) ? folderOf(containerTok) : null;
  const walked = new Set();
  while (f && !walked.has(f.id)){
    walked.add(f.id);
    if (f.parent === GEO_PARENT) return f;
    f = f.parent ? folderDefs[f.parent] : null;
  }
  return null;
}
function hiddenFolderAtOrAbove(containerTok){
  let f = isFolderTok(containerTok) ? folderOf(containerTok) : null;
  while (f){
    if (f.hidden) return f;
    f = f.parent ? folderDefs[f.parent] : null;
  }
  return null;
}
function overridingFolderOf(tok){
  for (let p = parentTokOf(tok); isFolderTok(p); p = parentTokOf(p)){
    const f = folderOf(p);
    if (f && f.hidden) return f;
  }
  return null;
}

// ── tree helpers ─────────────────────────────────────────────────────────────
// Apply the window-only ordering overlay to a list of sibling nodes: known tokens
// sort by their saved index; unknown (new) ones keep their natural order at the end.
function orderedChildren(parentTok, children){
  if (!children || children.length < 2) return children;
  const ord = siblingOrder[parentTok];
  let out = children;
  if (ord && ord.length){
    const pos = new Map(ord.map((t,i)=>[t,i]));
    out = children.slice().sort((a,b)=>
      (pos.has(a.token)?pos.get(a.token):1e9) - (pos.has(b.token)?pos.get(b.token):1e9));
  }
  // Every container draws in fixed bands, whatever the ordering overlay says (pinBand).
  // Sort is stable, so the order WITHIN each band is the overlay's.
  return boundsSketchesInAxisOrder(out.slice().sort((a, b) => pinBand(a) - pinBand(b)));
}
// Bounds sketches always read X, Y, Z, whatever order they were made or dragged in: the
// slots they occupy among their siblings stay put, and they are re-dealt into them by name.
function isBoundsSketchNode(n){
  return n && n.type === "sketch" && (n.name || "").startsWith(BOUNDS_SKETCH_PREFIX);
}
function boundsSketchesInAxisOrder(list){
  const slots = [];
  list.forEach((n, i) => { if (isBoundsSketchNode(n)) slots.push(i); });
  if (slots.length < 2) return list;
  const sorted = slots.map(i => list[i]).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  slots.forEach((i, k) => { list[i] = sorted[k]; });
  return list;
}
// The band a row is pinned to inside its container: components, then Operations (the
// timeline features, still "Geometry" / features in the code), then Bodies, then the sketch side (sketches and sketch folders), then the parameter side (parameters,
// name groups, parameter groups), then Origin, always the last row. A Bounds folder
// heads its own side — the sketches' one above the other sketches, the Bounds
// parameter group above the other parameters — so it is a band of its own (the .5).
function pinBand(n){
  if (!n) return 2;
  if (n.type === "component") return 0;
  if (n.type === "geometry") return 1;
  if (n.type === "features") return 0.75;       // above Bodies (under child components)
  if (n.type === "origin") return 4;
  if (n.type === "construction") return 3.75;   // just above Origin
  const side = isParamSide(n) ? 3 : 2;
  return isBoundsFolder(n) ? side - 0.5 : side;
}
// Token of a node's parent ("__root__" for top-level nodes).
function parentTokOf(tok){
  let res = "__root__", done = false;
  (function w(ns, pt){ for (const n of ns){ if (done) return;
    if (n.token === tok){ res = pt; done = true; return; }
    if (n.children) w(n.children, n.token); } })(state.tree, "__root__");
  return res;
}
// Is `tok` somewhere inside `ancestorTok`'s subtree (excluding itself)?
function isDescendantTok(ancestorTok, tok){
  const a = nodeByTok(ancestorTok); if (!a || !a.children) return false;
  let found = false;
  (function w(ns){ for (const n of ns){ if (found) return;
    if (n.token === tok){ found = true; return; } if (n.children) w(n.children); } })(a.children);
  return found;
}
// Finder-style rooting: the component you are INSIDE is the top row of the tree — its
// parents, and their siblings, aren't drawn at all. You are "in" it the way you are in
// a Finder folder, and D (out one level) is how you come back out. Only COMPONENTS
// root the view: a group is opened, never entered, so opening one still shows the
// component that holds it and the group's siblings beside it. Root active → the whole
// top level, exactly as before.
function displayRoot(){
  const vt = viewTok();                  // the peek, in suspend mode
  if (!vt) return null;
  const n = nodeByTok(vt);
  return (n && n.type === "component") ? n : null;
}
function flatten(){
  // Ordered list of visible rows (respecting collapse), with depth + meta.
  // effHidden = grayed in the window because it's not visible in the viewport:
  //   • the node itself OR an ancestor is hidden (Fusion doesn't flag descendants), or
  //   • Isolate is on and the node is NOT in the active component's subtree
  //     (everything outside it — including ancestors above the active comp — is
  //     hidden by isolate even though the user didn't hide it).
  // Only apply isolate-based graying when an occurrence is active (not at root).
  // At root, isolate is meaningless — nothing is outside the active component.
  // All of it keyed on viewTok: a suspend-mode peek is what the viewport isolates onto.
  const vt = viewTok();
  const isolate = !!(state.toggles && state.toggles.isolate) && !!vt;
  // Under isolate, the active component's ANCESTORS (the chain from root down to it)
  // stay visible — only off-path siblings are hidden. Without this, isolate greyed
  // every parent up to Top Level even though they're really shown in the viewport.
  const activePath = isolate ? activePathSet() : null;
  // The sketch currently being edited must never be greyed — it's the one sketch
  // that stays visible while its siblings are hidden (sketch_shown defaults off
  // during an edit, so every sketch row carries groupOff otherwise).
  const editingSk = !!(state.editingSketch && state.editSketchToken);
  // The components you are INSIDE read as context rather than content, so everything
  // ABOVE the active one greys — even though it is genuinely visible in the viewport.
  // Deliberately not part of `ancestorHidden`: this must not propagate, or the active
  // component and its contents (which sit under these very rows) would grey too.
  const insideOf = new Set(activePathList().slice(0, -1));
  const out = [];
  function walk(nodes, depth, ancestorHidden, inActive, userHiddenAnc, parentTok){
    for (const n of orderedChildren(parentTok, nodes)){
      if (!rowDrawn(n)) continue;
      const hasKids = n.children && n.children.length > 0;
      // (a peek keeps the real active component lit too — so is its subtree)
      const nodeInActive = inActive || n.token === vt || n.token === state.activeToken;
      const onActivePath = nodeInActive || (activePath && activePath.has(n.token));
      const isolateHidden = isolate && !onActivePath;
      const isEditedSketch = editingSk && n.type === "sketch"
        && n.token === state.editSketchToken
        && (n.occToken || "") === (state.activeToken || "");
      // groupOff = a body/sketch whose global group toggle is off → shown but greyed
      // (strikeExemptFromGroup/groupDim below spell out exactly how). A folder has no
      // bulb, so it greys off its contents instead.
      // A group's visibility is its OWN state, independent of what is inside it:
      // hiding every item in a group individually does NOT grey the group, and a group
      // can be toggled visible while nothing in it is (that just changes nothing in the
      // viewport). It used to also grey when its contents were all hidden, which
      // conflated the two.
      // Visibility means nothing to a parameter, so a parameter group always reads as
      // visible.
      const folderHidden = n.type === "folder" && !n.params
        && !!(folderOf(n.token) || {}).hidden;
      // strikeExemptFromGroup: this row's OWN invisibility is fully explained by Cycle
      // Visibility's (AddIns/Visibility Snapshot.py, cycle_visibility) temporary
      // bodies/sketches-off for the active context — groupOff on a sketch/body leaf, or
      // on the Bodies/Geometry group rows themselves (their bulb IS the Bodies-folder
      // bulb the add-in flips for that policy) — rather than the item's own light bulb,
      // so !n.visible alone shouldn't strike it. A leaf the user ALSO hid BY HAND
      // (n.userHidden) keeps its strike regardless — the group toggling back on
      // wouldn't bring that one back, so it must still read as genuinely hidden.
      const strikeExemptFromGroup = !!n.groupOff
        && !((n.type === "sketch" || n.type === "body") && n.userHidden);
      // selfOff = this row is switched off by what the USER set, read from the
      // add-in's record rather than from the live bulb. Components, sketches and
      // bodies carry that record (`userHidden`: the leaf intent record, and for a
      // component its bulb with isolation's switching subtracted via the isolate
      // ledger), so navigation — isolate, the per-component sketch/body folder
      // policy — never strikes or un-strikes them, and a jump draws the right strike
      // on the same frame instead of when Fusion's bulbs settle. Everything else
      // (Bodies group, Origin/Construction rows, features) has no record and still
      // reads its live `visible`, as does a row from an add-in that doesn't send one.
      const hasIntent = (n.type === "component" || n.type === "sketch" || n.type === "body")
        && typeof n.userHidden === "boolean";
      const selfOff = hasIntent ? n.userHidden : (!n.visible && !strikeExemptFromGroup);
      // offView = switched off → struck through (.offview). Parameters have no
      // visibility, so they never strike.
      const offView = !isEditedSketch && !isParamSide(n)
        && (ancestorHidden || selfOff || isolateHidden || folderHidden);
      const effHidden = isEditedSketch ? false
        : (ancestorHidden || selfOff || isolateHidden || folderHidden
           || insideOf.has(n.token));
      // groupDim: Cycle Visibility turned this row's group off for the active
      // component — dim it (.groupdim, 50% darker, same hue/saturation), ADDITIVE
      // with whatever else the row is (struck for a real per-item hide, context-grey,
      // or plain normal) rather than a substitute for it — unlike
      // strikeExemptFromGroup above, this is unconditional on userHidden: a sketch
      // hidden by hand still reads as switched off by the group too, when it is. A
      // folder picks it up from holding any switched-off sketch/body (groupOffIn), so
      // a whole Sketches folder dims along with its contents instead of looking
      // untouched while everything inside it is greyed. An EMPTY sketch folder has no
      // leaf to pick it up from, so it reads the sketches toggle itself (the same flag
      // every sketch row's groupOff comes from).
      const groupDim = !!n.groupOff || groupOffIn(n)
        || (isSketchGroup(n) && !!state.toggles && state.toggles.sketches === false);
      // userHidden = the user turned this item off, NOT policy.
      //
      // For a sketch or body this is now READ, not guessed: the add-in stamps the
      // recorded intent on the row. Inferring it from a dark bulb is what made
      // everything inside a hidden group read as user-hidden — a bulb is dark for
      // four different reasons (own hide, hidden group, folder toggle, isolate) and
      // the row cannot tell them apart by looking.
      //
      // A component has no such record, so its own bulb is still the best available
      // signal: isolate forces OFF-PATH occurrence bulbs off (policy), so !visible is
      // only trusted when the node isn't isolate-hidden off-path.
      // Propagates to all descendants ("incl. its children").
      const selfUserHidden = hasIntent ? !!n.userHidden
        : (n.type === "sketch" || n.type === "body") ? false
        : (!n.visible && !isolateHidden && !n.groupOff);
      const userHidden = userHiddenAnc || selfUserHidden;
      out.push({node:n, depth, hasKids, effHidden, offView, userHidden, groupDim});
      // user-hidden propagates to descendants; isolate is recomputed per node.
      // A hidden group is inherited by its contents: nothing inside one is ever
      // visible (the add-in's policy pass is what keeps the model in step), so
      // nothing inside one is ever drawn as visible either.
      // A Geometry group is open only while something holds it open (openGeos) —
      // never by default, since opening it is what fetches its rows.
      const open = n.type === "features" ? geoOpen(n) : !collapsed.has(n.token);
      if (hasKids && (n.type !== "folder" || folderShowsRows(n)) && open)
        walk(n.children, depth+1, ancestorHidden || (hasIntent ? selfOff : !n.visible) || folderHidden,
             nodeInActive, userHidden, n.token);
      else if ((((n.type === "component" && n.empty) || n.type === "folder") && open)
               || (n.type === "features" && open && n.featsLoaded))
        out.push({placeholder:true, depth:depth+1});   // "(empty)" line
    }
  }
  // Rooted: the component you are in is the tree, not a row IN it — only its contents
  // are drawn, flush at depth 0. Its ancestors are visible in the viewport (activating
  // a component reveals its path), so nothing hidden is inherited from above.
  const rooted = displayRoot();
  if (rooted){
    walk(rooted.children || [], 0, false, true, false, rooted.token);
    if (!out.length) out.push({placeholder:true, depth:0});   // nothing in here yet
  } else {
    walk(state.tree, 0, false, false, false, "__root__");
  }
  return out;
}
function findRow(rows, tok){ return rows.findIndex(r => r.node && r.node.token === tok); }
// Tokens on the path from the root down to (and including) the view — plus, during a
// suspend-mode peek, the active component's path, which the peek keeps lit.
function activePathSet(){
  const out = new Set(pathToTok(viewTok()));
  if (state.activeToken && state.activeToken !== viewTok())
    for (const t of pathToTok(state.activeToken)) out.add(t);
  return out;
}
// Ordered token path top-level → `tok` (inclusive). [] if it isn't in the tree.
function pathToTok(tok){
  let path = null;
  (function w(ns, acc){
    for (const n of ns){
      if (path) return;
      const a = acc.concat(n.token);
      if (n.token === tok){ path = a; return; }
      if (n.children) w(n.children, a);
    }
  })(state.tree, []);
  return path || [];
}
// Ordered token path top-level → the view (inclusive): the active component, or a
// suspend-mode peek. [] at the root.
function activePathList(){ return pathToTok(viewTok()); }

// Clean spine: expand the chain down to (and the contents of) the active component
// and collapse every off-path sibling branch. Driven automatically whenever the
// active component changes (a viewport jump) so the browser follows the viewport.
function revealActiveSpine(){
  if (!state.tree || !state.tree.length) return;
  closeAllGeos();                         // a Geometry group is shut like everything else
  try { tidySpine(); } finally { reopenClickedGeo(); }     // …but the one just opened with F
}
function tidySpine(){
  if (!viewTok()){
    // Document root active → spine is just the top level; collapse all subtrees.
    for (const n of state.tree)
      if (n.children && n.children.length) tidyCollapse(n.token);
    saveCollapsed(); return;
  }
  const path = activePathList();
  if (!path.length) return;
  const pathSet = new Set(path);
  // Collapse off-path top-level branches.
  for (const n of state.tree)
    if (!pathSet.has(n.token) && n.children && n.children.length) tidyCollapse(n.token);
  // Walk the spine: expand each node on it (the last is the active comp → its
  // contents show) and collapse its off-path child-component branches. Body/sketch
  // leaves have no children, so they stay visible.
  for (let i = 0; i < path.length; i++){
    collapsed.delete(path[i]); cancelCollapseTimer(path[i]);
    const node = nodeByTok(path[i]);
    if (node && node.children)
      for (const c of node.children){
        const onPath = (i + 1 < path.length) && c.token === path[i+1];
        // Everything off the path closes, all the way down — groups (even empty ones),
        // the Geometry group, child components and whatever is open inside them — so
        // arriving at a level, down or back up, always shows it fully collapsed, however
        // it was left.
        if (!onPath && ((c.children && c.children.length) || c.type === "folder"
                        || c.type === "namegroup")){
          tidyCollapse(c.token);
          (function deep(ns){ for (const x of (ns || []))
            if (x.children && x.children.length){ tidyCollapse(x.token); deep(x.children); } })(c.children);
        }
      }
  }
  saveCollapsed();
}
// A pick made in the VIEWPORT should be findable here: open every group and component
// above it so the row is actually on screen, then scroll to it.
//
// Only when the whole selection sits in ONE container. A selection spread across
// different folders has no single place to open, and expanding all of them at once would
// thrash the whole tree open — so a mixed pick is left where it is and simply
// highlighted. Rows sitting directly in a component rather than in a folder share that
// component as their container, so picking several of those still reveals normally.
// The component a row lives in, as a compRef: its own for a body or sketch, the
// component it was made in for a group.
function ownerOccOf(tok){
  if (isFolderTok(tok)) return (folderOf(tok) || {}).comp || "";
  const n = nodeByTok(tok);
  return n ? compRefOf(n.occToken || "") : "";
}
function revealSelection(toks){
  toks = (toks || []).filter(t => nodeByTok(t));
  if (!toks.length) return;
  // Only for a pick inside the component you are ALREADY in. A pick anywhere else is
  // the front half of a jump down into that component, and arriving somewhere new
  // should show it tidy rather than with the group the picked body happens to live in
  // thrown open. The jump lands a tick after the selection does — the add-in holds it
  // back while the click is fresh — so waiting for the activation to show up is too
  // late; what the row belongs to is known right now.
  if (toks.some(t => ownerOccOf(t) !== compRefOf(state.activeToken || ""))) return;
  if (new Set(toks.map(parentTokOf)).size !== 1) return;
  // …and only inside a top-level group you already have open. A shut Bodies / Geometry /
  // sketch folder stays shut; opening it by hand then opens down to the pick
  // (revealSelectionIn).
  const top = topGroupOf(toks[0]);
  if (top && groupShut(top)) return;
  openAbove(toks);
}
// The top-level group a row sits in within the component you are in — the first row
// under it on the path (Bodies, Geometry, a sketch folder, Origin…). "" for a row loose
// in the component, which is always drawn.
function topGroupOf(tok){
  const path = pathToTok(tok);
  const i = state.activeToken ? path.indexOf(state.activeToken) : -1;
  if (state.activeToken && i < 0) return "";
  const g = path[i + 1];
  return g && g !== tok ? g : "";
}
function groupShut(tok){
  const n = nodeByTok(tok);
  return n && n.type === "features" ? !geoOpen(n) : collapsed.has(tok);
}
// A group just opened by hand: open on down to whatever is picked inside it (one
// container only, as revealSelection).
function revealSelectionIn(tok){
  const toks = [...selected].filter(t => nodeByTok(t) && isDescendantTok(tok, t));
  if (!toks.length || new Set(toks.map(parentTokOf)).size !== 1) return;
  openAbove(toks);
}
function openAbove(toks){
  // The spine-tidy collapses off-path branches on the next activation; protect what we
  // are about to open so it does not shut again a moment later.
  protectPath(toks[0], 4000);
  let opened = false;
  for (const t of toks)
    for (const anc of pathToTok(t).slice(0, -1))
      if (collapsed.has(anc)){ collapsed.delete(anc); cancelCollapseTimer(anc); opened = true; }
  if (opened) saveCollapsed();
  // The caller renders straight after this, so scroll on the next frame.
  requestAnimationFrame(()=>scrollTokIntoView(toks[0]));
}

// D collapsed a container: make the container itself the selection. Closing a group is
// a step OUTWARDS, so the thing you are now "on" is the group — not whatever is sealed
// inside it, which you can no longer see. setSelection does the viewport half for us: a
// component gets selected there, and a group hands over everything showing inside it,
// which is what a picked group means open or closed.
function selectAfterCollapse(tok){
  if (!tok || !nodeByTok(tok)) return;
  selHold = null;
  cursorTok = tok;
  selAnchor = tok;
  // Suspend mode: picked here only — the running command's selection is its own.
  if (suspended){ selected = new Set([tok]); render(); }
  else setSelection([tok]);     // sets `selected`, pushes to the viewport, renders
  scrollCursorIntoView();
}

// Collapsing a group hides whatever was picked inside it, and a selection you can no
// longer see is one you can still act on by accident. Clear it — here and in the
// viewport — but only when the selection really was inside the group being closed.
// Used by the twirl / Enter-on-a-folder route; D goes through selectAfterCollapse.
function clearSelectionForCollapse(tok){
  if (!tok || (!selected.size && !cursorTok)) return;
  const inside = t => t && (t === tok || isDescendantTok(tok, t));
  if (![...selected].some(inside) && !inside(cursorTok)) return;
  selHold = null;
  selected.clear();
  cursorTok = null;
  post({action:"deselect"});
}

function scrollTokIntoView(tok){
  const el = document.querySelector(`.row[data-tok="${cssEsc(tok)}"]`);
  if (el) el.scrollIntoView({block:"nearest"});
}

// ── feature rows: a component's timeline features ────────────────────────────
// A component's Geometry group (fx:<id>, under its Bodies group) holds its timeline
// features as `feature` rows, in timeline order. Opening the group (a click, F on one of
// the component's bodies, or a face picked in the viewport) is what fetches them: the
// rows come from the Timeline add-in (8736/feats), which scans only the components
// posted to it with `watch` — and places every new timeline item at the end of its
// PHASE (AddIns/Phases.py: create components, arrange, sketches, independent,
// dependent, post-dependent, duplication). Clicking one selects it in the viewport,
// like the native timeline; dragging (never ⇧↑/⇧↓ — too easy to hit by accident)
// reorders the REAL timeline, never across a phase line — the add-in checks both with
// Fusion's own canReorder. Open groups are held in memory only, like bodies used to be:
// arriving at a level shuts everything, these included (revealActiveSpine).
const FEAT_PREFIX = "ft:";
let openGeos = new Set();            // Geometry group tokens (fx:<id>)
let featsByGeo = {};                 // fx token → [{tok, name, type, phase, pinned, health, msg, suppressed, past}]
let featsJSON = "";
let featProbe = null;                // {id, ok:Set|null, after:Set|null}
let featActionAt = 0;                // when this window last asked the add-in to act
let featActionKind = "";          // …and what it asked (a refused move says nothing)
let featNoticeAt = null;             // newest notice seen (null = adopt the first)
// A dropped move Fusion is still working on: the rows draw where they were dropped, with
// a spinner, until the add-in's verdict for this id comes back on /feats (`reorder`,
// published with the rows it produced). Refused → the real order again + `featMoveErr`,
// its reason in red on the row for FEAT_ERR_MS.
let featMove = null;                 // {id, geoTok, toks:[featTok], mode:'before'|'after', anchor, at}
let featMoveErr = null;              // {geoTok, toks:[featTok], text}
const FEAT_ERR_MS = 6000;
const FEAT_MOVE_MAX_MS = 180000;     // no verdict by then (add-in gone/reloaded) → give up
let featWatchAt = 0;

function isFeatTok(t){ return typeof t === "string" && t.startsWith(FEAT_PREFIX); }
function geoOpen(n){ return !!n && n.type === "features" && openGeos.has(n.token); }
function endFeatMove(mv, text){
  if (featMove === mv) featMove = null;
  if (text){
    const er = featMoveErr = { geoTok: mv.geoTok, toks: mv.toks, text };
    setTimeout(() => { if (featMoveErr === er){ featMoveErr = null; refreshFolders(); } }, FEAT_ERR_MS);
  }
  refreshFolders();
}
function featPost(body){
  featActionAt = Date.now();
  featActionKind = body.action || "";
  fetch(TIMELINE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)}).catch(()=>{});
}
// `geoTok` on a feature (and a feature group) is the Geometry group it is drawn in.
// The rows' order with a pending move applied: the movers, in their own order, lifted
// out and put down before/after the row they were dropped on.
function movedOrder(list, mv){
  const moving = new Set(mv.toks);
  const rest = list.filter(f => !moving.has(f.tok)), go = list.filter(f => moving.has(f.tok));
  const i = rest.findIndex(f => f.tok === mv.anchor);
  if (i < 0 || !go.length) return list;
  rest.splice(i + (mv.mode === "after" ? 1 : 0), 0, ...go);
  return rest;
}
function featNodesFor(b){
  const mv = featMove && featMove.geoTok === b.token ? featMove : null;
  const er = featMoveErr && featMoveErr.geoTok === b.token ? featMoveErr : null;
  let list = featsByGeo[b.token] || [];
  if (mv) list = movedOrder(list, mv);
  const errAt = er ? list.findIndex(f => er.toks.includes(f.tok)) : -1;
  const fs = list.map((f, i) => ({
    type: "feature", token: FEAT_PREFIX + b.token + "|" + f.tok, featTok: f.tok,
    geoTok: b.token, occToken: b.occToken || "", name: f.name, ftype: f.type,
    op: f.op || "",
    key: f.key || "", compRef: f.compRef || "", icon: f.icon || "", pos: i,
    phase: f.phase, pinned: !!f.pinned, health: f.health || "", msg: f.msg || "",
    suppressed: !!f.suppressed, past: !!f.past, unseen: !!f.unseen, visible: true, children: [],
    moving: !!mv && mv.toks.includes(f.tok), moveErr: i === errAt ? er.text : "" }));
  // Rolled back part-way through this list: its last feature before the marker carries
  // the marker dot (`.tl-marker`). None past (the marker at or after them all — the
  // usual end-of-timeline case) or all past → no dot.
  const p = fs.findIndex(f => f.past);
  if (p > 0) fs[p - 1].marker = true;
  return TL_SHARED ? groupFeatures(b, fs, tlGroups) : fs;
}

// ── feature groups: the vertical timeline's own groups ───────────────────────
// A component's features file into the SAME window-only groups the vertical timeline
// draws (fusion_timeline, groups.json on 8736; the data model is fusion_ui/tl_groups.js):
// a feature row carries its timeline row's key (compRef|name, from the add-in), so a
// group made here shows there and the other way round. Only the groups that hold one
// of the component's features draw in its Geometry group, holding just those — so an unnamed group is
// named (autoGroupName) after what it holds HERE. `fgroup` rows (fg:<geoTok>|<gid>)
// open and shut through `collapsed` like any group; ⌘G groups picked features
// (groupFeatureSel), ⌫/⌘D/Explode dissolves (explodeFeatureGroups), rename names it
// ("" or "Group" hands it back to the auto-name). Features keep timeline order; a
// group sits at its earliest member. Between siblings whose phase differs (independent
// / dependent / post-dependent) the lower row carries `.phase-edge`, a hairline.
const FG_PREFIX = "fg:";
// tl_icons.js / tl_groups.js come from 8732's shared-asset list, which a Browser server
// started before they existed doesn't have (404). Without them the feature rows draw
// flat and plain rather than the whole page dying on a missing global.
const TL_SHARED = typeof groupComp === "function" && typeof glyphSVG === "function";
let tlGroups = { groups: {}, member: {}, compCollapsed: {} };
let tlGroupsDoc = null;              // the doc whose groups are loaded (null: none / loading)
let tlGroupsRev = null;              // the server's groups revision our copy matches
let featDoc = "";                    // the doc /feats last reported
function isFGroupTok(t){ return typeof t === "string" && t.startsWith(FG_PREFIX); }

async function loadTlGroups(doc, rev){
  if (!TL_SHARED) return;
  tlGroupsDoc = null; tlGroupsRev = rev == null ? null : rev;
  try {
    const all = await (await fetch(TIMELINE_API + "/groups", {cache:"no-store"})).json();
    tlGroups = normData(((all && all.byDoc) || {})[doc]);
  } catch(_){ tlGroups = emptyData(); }
  tlGroupsDoc = doc;
  refreshFolders();
}
function saveTlGroups(){
  // Never before this doc's groups have loaded: that would write an empty copy over them.
  if (!featDoc || tlGroupsDoc !== featDoc) return false;
  fetch(TIMELINE_API + "/groups", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({docId: featDoc, data: tlGroups})})
    .then(r => r.json()).then(r => { if (r && typeof r.rev === "number") tlGroupsRev = r.rev; })
    .catch(()=>{});
  return true;
}

// Pure: a Geometry group's feature nodes (timeline order, `pos`) → its children, grouped.
const FEAT_HEALTH = { "": 0, warning: 1, error: 2 };
function groupFeatures(b, feats, data){
  const top = [], made = {};
  const groupNode = gid => {
    if (made[gid]) return made[gid];
    const g = data.groups[gid], p = String(g.parent || "");
    const n = made[gid] = { type: "fgroup", token: FG_PREFIX + b.token + "|" + gid, gid,
      geoTok: b.token, occToken: b.occToken || "", label: String(g.name || "").trim(),
      visible: true, children: [], pos: Infinity, health: "" };
    (p.startsWith("g:") ? groupNode(p.slice(2)).children : top).push(n);
    return n;
  };
  for (const f of feats){
    const gid = data.member[f.key];
    // Filed only into a group of its own component (a broken chain → null); else loose.
    const ok = f.key && gid && data.groups[gid] && groupComp(data, gid) === f.compRef;
    (ok ? groupNode(gid).children : top).push(f);
  }
  const rowsIn = n => n.children.flatMap(c => c.type === "feature" ? [c] : rowsIn(c));
  const phaseOf = n => n.type === "feature" ? n.phase : phaseOf(n.children[0]);
  const settle = kids => {
    for (const c of kids){
      if (c.type !== "fgroup") continue;
      settle(c.children);
      for (const k of c.children){
        c.pos = Math.min(c.pos, k.pos);
        if (k.marker) c.marker = true;           // drawn on the group while it's shut
        if ((FEAT_HEALTH[k.health] || 0) > (FEAT_HEALTH[c.health] || 0)) c.health = k.health;
      }
      const rows = rowsIn(c);
      c.named = !AUTO_NAME.test(c.label);
      c.name = c.named ? c.label : autoGroupName(rows.map(r => r.ftype));
      // One kind → its colour (the timeline's rule); its most frequent type's icon.
      const ks = new Set(rows.map(r => kindCls(r.ftype)));
      c.kindCls = ks.size === 1 ? [...ks][0] : "";
      const count = new Map();
      for (const r of rows){
        const t = glyphKey(r.ftype, r.op), e = count.get(t) || { r, k: 0 };
        e.k++; count.set(t, e);
      }
      let best = null;
      for (const e of count.values()) if (!best || e.k > best.k) best = e;
      c.iconType = best ? glyphKey(best.r.ftype, best.r.op) : ""; c.icon = best ? best.r.icon : "";
    }
    kids.sort((x, y) => (x.pos - y.pos) || ((x.type === "fgroup") - (y.type === "fgroup")));
    kids.forEach((c, i) => { c.phaseEdge = i > 0 && phaseOf(c) !== phaseOf(kids[i - 1]); });
  };
  settle(top);
  return top;
}
// Every feature row under an open body (or group), however deeply grouped.
function featsUnder(n){
  return (n && n.children || []).flatMap(c => c.type === "feature" ? [c]
                                            : c.type === "fgroup" ? featsUnder(c) : []);
}
// ⌘G on picked features / feature groups: a new unnamed group around them, in the one
// group they share (else at their component's level) — groupAround's rule. True when
// the pick was ours to handle (even if refused).
function groupFeatureSel(){
  if (!TL_SHARED) return false;
  const toks = selected.size ? [...selected] : (cursorTok ? [cursorTok] : []);
  const ns = toks.map(nodeByTok).filter(Boolean);
  if (!ns.length || ns.some(n => n.type !== "feature" && n.type !== "fgroup")) return false;
  if (tlGroupsDoc === null || tlGroupsDoc !== featDoc){ showBlocked("Group", "The timeline's groups haven't loaded yet."); return true; }
  const feats = ns.filter(n => n.type === "feature"), gs = ns.filter(n => n.type === "fgroup");
  const refs = new Set(feats.map(f => f.compRef).concat(gs.map(g => groupComp(tlGroups, g.gid))));
  if (refs.size !== 1 || refs.has(null) || refs.has("")){
    showBlocked("Group", "Features from different components can't share a group."); return true;
  }
  const homes = new Set(ns.map(n => { const p = parentTokOf(n.token); return isFGroupTok(p) ? nodeByTok(p).gid : ""; }));
  const home = homes.size === 1 ? [...homes][0] : "";
  const id = newGroupId(tlGroups);
  tlGroups.groups[id] = { name: "", parent: home ? "g:" + home : "c:" + [...refs][0] };
  for (const f of feats) tlGroups.member[f.key] = id;
  for (const g of gs) tlGroups.groups[g.gid].parent = "g:" + id;
  saveTlGroups();
  const tok = FG_PREFIX + ns[0].geoTok + "|" + id;
  refreshFolders();
  cursorTok = tok; selected = new Set([tok]); render();
  beginRename(tok);
  return true;
}
function explodeFeatureGroups(toks){
  let any = false;
  for (const t of toks){
    const n = nodeByTok(t);
    if (n && n.type === "fgroup") any = explodeGroup(tlGroups, n.gid) || any;
    selected.delete(t);
    if (cursorTok === t) cursorTok = null;
  }
  if (any){ saveTlGroups(); refreshFolders(); }
}
// The rename box's answer for a group: "" / "Group" hand it back to its auto-name.
function renameFeatureGroup(n, v){
  const g = tlGroups.groups[n.gid]; if (!g) return;
  if (!n.named && v === n.name) return;          // the auto-name confirmed untouched
  g.name = v; saveTlGroups(); refreshFolders();
}
// Called from withFolders: every OPEN Geometry group gets its feature rows as children
// (a copy of the node — rawTree is server truth and never written).
function graftFeatures(nodes){
  if (!openGeos.size) return nodes;
  return (function g(ns){
    let changed = false;
    const out = ns.map(n => {
      if (n.type === "features"){
        if (!openGeos.has(n.token)) return n;
        changed = true;
        return Object.assign({}, n, { children: featNodesFor(n),
                                      featsLoaded: n.token in featsByGeo });
      }
      if (n.children && n.children.length){
        const kids = g(n.children);
        if (kids !== n.children){ changed = true; return Object.assign({}, n, { children: kids }); }
      }
      return n;
    });
    return changed ? out : ns;
  })(nodes);
}
// The open Geometry groups, as the Timeline add-in is told to watch them: the group's
// token (what the rows come back keyed by) and its component's id.
function geoWatch(){
  const out = [];
  (function w(ns){ for (const n of ns || []){
    if (n.type === "features"){
      if (openGeos.has(n.token)) out.push({ key: n.token, comp: n.compId || "" });
    } else if (n.children) w(n.children); } })(state.tree);
  return out;
}
function postWatch(){
  featWatchAt = Date.now();
  fetch(TIMELINE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"watch", comps: geoWatch()})}).catch(()=>{});
}
function openGeo(n){
  if (!n || n.type !== "features" || geoOpen(n)) return;
  openGeos.add(n.token);
  postWatch(); refreshFolders(); pollFeats();
}
function closeGeo(tok){
  if (geoClick && geoClick.tok === tok) geoClick = null;
  if (!openGeos.delete(tok)) return;
  postWatch(); refreshFolders();
}
// The Geometry group of the component a body is drawn in (beside its Bodies group), or
// null when that component has no features.
function geoOfBody(b){
  const path = b ? pathToTok(b.token) : [];
  for (let i = path.length - 2; i >= 0; i--){
    const g = nodeByTok(path[i]);
    if (!g || g.type !== "geometry") continue;
    const fx = nodeByTok(FX_PREFIX + (g.compId || ""));
    return fx && fx.type === "features" ? fx : null;
  }
  return null;
}
// F on a body opens its component's Geometry group. Selecting a body outside the active
// component makes the add-in jump down into that component — whose spine tidy shuts
// every group — so the group is held open through that one jump (reopenClickedGeo, from
// revealActiveSpine).
let geoClick = null;                 // {tok, until}
function openGeoOfBody(b){
  const g = geoOfBody(b);
  if (!g) return null;
  if (ownerOccOf(b.token) !== compRefOf(state.activeToken || ""))
    geoClick = { tok: g.token, until: Date.now() + 4000 };
  openGeo(g);
  return g;
}
// A face, edge or vertex picked in the VIEWPORT, in the component you are already in,
// opens that component's Geometry group and lands on the feature that made the part
// (state.selPick.feat, the Timeline add-in's Trace logic), picked HERE only: the
// viewport keeps the face. F on it then edits that feature's sketch. A whole body, or a
// pick in another component, opens nothing.
let pickFeat = null;                 // {tok, featTok, occTok, until}: row to land on once loaded
// A face / edge / vertex pick Enter should edit (pickEditLive): the pick it came from and
// the rows the cursor may sit on meanwhile — the body, then its feature row.
let pickEdit = null;                 // {pickKey, cursors}
// The body a face / edge / vertex pick belongs to stays LIT while its feature row holds
// the pick here: it is what the viewport has selected. Drawn only — never in `selected`,
// or ⌘D / ⌘G / the menu would take the body along with the feature.
let pickBody = null;                 // {tok: body row, row: the feature row it handed to}
function pickBodyLit(tok){
  return !!pickBody && tok === pickBody.tok && selected.has(pickBody.row)
    && (state.selTokens || []).includes(tok);
}
function openPickedFeature(){
  pickEdit = null; pickBody = null;
  const pick = state.selPick || {};
  if (pick.kind !== "part") return;
  pickEdit = { pickKey: JSON.stringify(pick), cursors: [cursorTok] };
  // Only into a Geometry group that is already open: a shut one stays shut, and the body
  // row keeps the pick until the group is opened by hand (landPickIn).
  const g = pickedGeo();
  if (g && geoOpen(g)) handPickToFeature(g);
}
// The Geometry group whose feature the viewport's face / edge / vertex pick hands to —
// null for a whole body, a pick outside the active component, or no features.
function pickedGeo(){
  const pick = state.selPick || {}, toks = state.selTokens || [];
  if (pick.kind !== "part" || !pick.feat || toks.length !== 1) return null;
  const b = nodeByTok(toks[0]);
  if (!b || b.type !== "body") return null;
  if (ownerOccOf(b.token) !== compRefOf(state.activeToken || "")) return null;
  return geoOfBody(b);
}
// Opening a shut Geometry group by hand while the pick openPickedFeature left on its
// body still stands (same viewport pick, nothing else picked here): open it onto the
// feature, as if it had been open all along. False → open it the ordinary way.
function landPickIn(n){
  const g = pickedGeo();
  if (!g || g.token !== n.token || !pickEdit
      || pickEdit.pickKey !== JSON.stringify(state.selPick || {})) return false;
  const b = (state.selTokens || [])[0];
  if (!(selected.size === 1 && selected.has(b))) return false;
  handPickToFeature(g);
  return true;
}
function handPickToFeature(g){
  const pick = state.selPick || {}, b = nodeByTok((state.selTokens || [])[0]);
  openGeo(g);
  const t = FEAT_PREFIX + g.token + "|" + pick.feat;
  selected = new Set([t]); cursorTok = t; selAnchor = t;
  pickBody = { tok: b.token, row: t };
  pickEdit.cursors.push(t);
  pickFeat = { tok: t, featTok: pick.feat, occTok: g.occToken || "", until: Date.now() + 4000 };
  landPickedFeature();
}
// The picked feature's row arrives with the group's /feats poll: open any timeline group
// it sits in and scroll to it.
function pickEditLive(){
  return !!pickEdit && JSON.stringify(state.selPick || {}) === pickEdit.pickKey
    && (!cursorTok || pickEdit.cursors.includes(cursorTok));
}
function landPickedFeature(){
  if (!pickFeat) return;
  // render() drops a cursor whose row isn't drawn yet, so it's the SELECTION that says
  // the pick still stands; the cursor is put back once the row exists.
  if (Date.now() > pickFeat.until || !(selected.size === 1 && selected.has(pickFeat.tok))){
    pickFeat = null; return;
  }
  if (!nodeByTok(pickFeat.tok)) return;
  cursorTok = selAnchor = pickFeat.tok;
  let opened = false;
  for (const anc of pathToTok(pickFeat.tok).slice(0, -1))
    if (collapsed.has(anc)){ collapsed.delete(anc); cancelCollapseTimer(anc); opened = true; }
  if (opened) saveCollapsed();
  const t = pickFeat.tok; pickFeat = null;
  render(); requestAnimationFrame(() => scrollTokIntoView(t));
}
function reopenClickedGeo(){
  if (!geoClick || Date.now() > geoClick.until || !nodeByTok(geoClick.tok)) return;
  openGeos.add(geoClick.tok);
  for (const anc of pathToTok(geoClick.tok).slice(0, -1)){ collapsed.delete(anc); cancelCollapseTimer(anc); }
  saveCollapsed(); postWatch();
}
function closeAllGeos(){
  if (!openGeos.size) return false;
  openGeos.clear(); postWatch();
  return true;
}
// The open Geometry group a picked row stands in: a feature's own group, or the group.
function openGeoOfSelection(){
  const toks = selected.size ? [...selected] : (cursorTok ? [cursorTok] : []);
  for (const t of toks){
    const n = nodeByTok(t); if (!n) continue;
    const g = (n.type === "feature" || n.type === "fgroup") ? nodeByTok(n.geoTok) : n;
    if (geoOpen(g)) return g;
  }
  return null;
}
// Every feature row (in any open Geometry group) for these timeline feature tokens.
function quietFeatRows(featToks){
  const want = new Set(featToks), out = [];
  (function w(ns){ for (const n of ns){
    if (n.type === "feature" && want.has(n.featTok)) out.push(n.token);
    if (n.children) w(n.children);
  } })(state.tree || []);
  return out;
}
function selectFeature(n){
  if (suspended || !n) return;
  // The viewport now holds a feature, which the selection mirror has no row for, plus
  // the sketch it was made from (Timeline add-in's _select_feature) — it comes back as
  // an empty pick or just that sketch. Hold this row through that one echo, whichever
  // it is (anyEcho), so the sketch's row doesn't take the pick over.
  pendingSel = { toks:[n.token], key:"[]", ts: Date.now(), sentTs: Date.now(), anyEcho: true };
  featPost({action:"selectfeature", tok: n.featTok, occTok: n.occToken || ""});
}
// Enter and the menu's Edit Feature: that feature type's own edit command.
function editFeature(n){
  if (suspended || !n) return;
  render(); featPost({action:"edit", tok: n.featTok, occTok: n.occToken || "", sketch: false});
}
function featToksOf(toks){
  const out = [];
  for (const t of toks){
    const n = nodeByTok(t);
    if (n && n.type === "feature" && out.indexOf(n.featTok) < 0) out.push(n.featTok);
  }
  return out;
}
function sortPhases(){ featPost({action:"sortphases"}); pollFeats(); }
async function pollFeats(){
  if (featMove && Date.now() - featMove.at > FEAT_MOVE_MAX_MS)
    endFeatMove(featMove, "No answer from Fusion — showing the timeline as it is.");
  const recent = Date.now() - featActionAt < 8000 || !!featMove;
  if (!openGeos.size && !recent) return;
  if (openGeos.size && Date.now() - featWatchAt > 2000) postWatch();   // survives an add-in reload
  try {
    const r = await (await fetch(TIMELINE_API + "/feats", {cache:"no-store"})).json();
    const doc = r.doc_id || "";
    if (doc !== featDoc){ featDoc = doc; if (doc) loadTlGroups(doc, r.groupsRev); }
    else if (doc && tlGroupsDoc === doc && typeof r.groupsRev === "number"
             && r.groupsRev !== tlGroupsRev) loadTlGroups(doc, r.groupsRev);   // changed in the timeline
    const j = JSON.stringify(r.feats || {});
    const rr = r.reorder, done = featMove && rr && rr.id === featMove.id ? featMove : null;
    if (j !== featsJSON){
      featsJSON = j; featsByGeo = r.feats || {};
      if (done) featMove = null;             // the new rows ARE the answer; no flash back
      carryRenamedFeature(); refreshFolders(); landPickedFeature();
    }
    if (done) endFeatMove(done, rr.text || "");
    if (r.probe && featProbe && r.probe.id === featProbe.id && !featProbe.ok){
      featProbe.ok = new Set(r.probe.ok || []); featProbe.after = new Set(r.probe.after || []);
      featProbe.why = r.probe.why || "";
    }
    const n = r.notice;
    if (featNoticeAt === null) featNoticeAt = n ? n.at : 0;
    else if (n && n.at > featNoticeAt){
      featNoticeAt = n.at;
      // Only what THIS window asked for: the add-in's placement reports belong to
      // whoever is watching the timeline, not to a modal here.
      // A refused move just doesn't happen — the rows stay put, no dialog.
      if (recent && featActionKind !== "featreorder") showBlocked("That", n.text);
    }
  } catch(_){}
}
// A drag of feature rows: probe on start, drop only between sibling features of the
// same body where the add-in said Fusion would take it.
function startFeatDrag(toks){
  const n = nodeByTok(toks[0]);
  const body = n && nodeByTok(n.geoTok);
  const sibs = featsUnder(body);
  featProbe = { id: Date.now() + ":" + Math.random(), ok: null, after: null };
  featPost({action:"featprobe", id: featProbe.id, toks: featToksOf(toks),
            targets: sibs.filter(s => toks.indexOf(s.token) < 0).map(s => s.featTok)});
  pollFeats();
}
function featDrop(e){
  if (featMove) return null;           // one move at a time: the next waits for Fusion
  const el = document.elementFromPoint(e.clientX, e.clientY);
  const row = el && el.closest(".row");
  if (!row || row.dataset.type !== "feature" || drag.toks.indexOf(row.dataset.tok) >= 0) return null;
  const t = nodeByTok(row.dataset.tok), src = nodeByTok(drag.tok);
  if (!t || !src || t.geoTok !== src.geoTok) return null;
  const rc = row.getBoundingClientRect();
  const side = (e.clientY - rc.top) / rc.height > 0.5 ? "after" : "before";
  if (featProbe && featProbe.ok){
    const okSet = side === "before" ? featProbe.ok : featProbe.after;
    if (!okSet.has(t.featTok)) return null;
  }
  return { mode: side, tok: t.token, feat: true, featTok: t.featTok };
}

// ── render ───────────────────────────────────────────────────────────────────
// A feature row's icon, the vertical timeline's: our glyph (fusion_ui/tl_icons.js) in
// the row's colour, else Fusion's own icon off the timeline's server, else `generic`.
function featIcon(type, rel, generic){
  if (!TL_SHARED) return "";
  const g = type ? glyphSVG(type) : "";
  if (g) return `<span class="tico">${g}</span>`;
  if (!rel) return `<span class="tico ${generic}"></span>`;
  const src = TIMELINE_API + "/icon/" + rel.split("/").map(encodeURIComponent).join("/");
  return `<span class="tico"><img src="${escapeHtml(src)}" draggable="false" `
       + `onerror="this.parentNode.classList.add('${generic}');this.remove()"></span>`;
}
function render(){
  if (editing || drag) return;  // don't rebuild the DOM out from under a rename input or a drag
  resetIgnoredOnComponentChange();
  applyPendingVis(); applyPendingRename();
  const tree = document.getElementById("tree");
  if (!state.tree || state.tree.length === 0){
    tree.innerHTML = '<div id="waiting">no components</div>';
    return;
  }
  const rows = flatten();
  const cutSet = new Set(cutToks);
  // cursorTok === undefined → first load (no selection). null → explicitly
  // deselected (clicked empty space). A stale token resets to deselected.
  if (cursorTok !== null && cursorTok !== undefined && findRow(rows, cursorTok) < 0)
    cursorTok = null;

  const scroll = tree.scrollTop;
  let html = "";
  // A placeholder always follows its parent's row and takes that row's colour.
  let lastKind = "", lastHidden = false;
  for (const r of rows){
    const pad = 10 + r.depth*17;
    if (r.placeholder){
      const pcls = (lastKind ? " k-" + lastKind : "") + (lastHidden ? " hidden" : "");
      html += `<div class="row placeholder${pcls}" style="padding-left:${pad}px">`
            + `<span class="name">(empty)</span></div>`;
      continue;
    }
    const n = r.node;
    const cls = ["row"];
    if (selected.has(n.token) || n.token === cursorTok || pickBodyLit(n.token)) cls.push("cursor");
    // Active = optimistic: keyed SOLELY off state.activeToken (set immediately on
    // click / activate AND refreshed from the server each poll). Using only this —
    // not the server tree's n.active flag — means the newly-clicked row bolds AND
    // the previously-active row un-bolds straight away, without waiting for the
    // viewport to catch up (n.active lingers true on the old row until the poll).
    // While a sketch is being edited, the emphasis moves OFF the active (parent)
    // component and ONTO the edited sketch row.
    const editingSk = !!(state.editingSketch && state.editSketchToken);
    if (editingSk){
      if (n.type === "sketch" && n.token === state.editSketchToken
          && (n.occToken || "") === (state.activeToken || "")) cls.push("active");
    } else if (n.token === state.activeToken){
      cls.push("active");
    }
    if (cutSet.has(n.token)) cls.push("cut");
    if (extrude.on && n.type === "body" && extrude.keys.has(n.key)) cls.push("xpart");
    if (r.userHidden) cls.push("userhidden");
    // Off the viewport → struck through, NOT greyed; grey is left for context rows
    // (the components you are inside), which are visible.
    if (r.offView) cls.push("offview");
    else if (r.effHidden) cls.push("hidden");
    // groupDim (Cycle Visibility) is additive, not part of the offview/hidden
    // either-or above — a row struck for a real hide can ALSO be dimmed for being
    // switched off by the group, at once.
    if (r.groupDim) cls.push("groupdim");
    // Colour says what a row is, or what a group holds (`k-*` in index.css): bodies,
    // the Bodies group and every folder inside it amber (k-bodies); the Geometry
    // (features) group indigo like the timeline's body features; sketches and the folders
    // outside it yellow; parameters, name groups and parameter groups green.
    // Components stay white; Origin is purpley-pink (styled directly off data-type,
    // not a k-* kind — it isn't body/sketch/param).
    // A feature (and a group of one kind of them) wears the timeline's kind colour:
    // body features blue, Move white (kindCls, fusion_ui/tl_icons.js).
    const kind = n.type === "feature" ? (TL_SHARED ? kindCls(n.ftype).replace(/^k-/, "") : "body")
               : n.type === "fgroup" ? (n.kindCls || "").replace(/^k-/, "")
               : (n.type === "body" || n.type === "geometry") ? "bodies"
               : n.type === "features" ? "body"
               : isParamSide(n) ? "param"
               : n.type === "sketch" ? "sketch"
               : n.type === "folder" ? (geoFolderOf(n.token) ? "bodies" : "sketch")
               : "";
    if (kind) cls.push("k-" + kind);
    lastKind = kind; lastHidden = !!r.effHidden && !r.offView;
    // Parens mean "nothing in it" — any empty folder, ignored or
    // not (an ignored one only ever gets this far when it's empty anyway:
    // reconcileIgnores clears the flag the moment it has real content).
    const shownName = (n.type === "folder" && !folderShowsRows(n))
      ? "(" + n.name + ")" : n.type === "feature" ? displayName(n.name)
      : n.type === "fgroup" ? n.name : spaceParens(n.name);
    let marks = "", lead = "";
    if (n.type === "feature") lead = featIcon(TL_SHARED ? glyphKey(n.ftype, n.op) : n.ftype, n.icon, "g-feature");
    if (n.type === "fgroup"){
      lead = featIcon(n.iconType, n.icon, "g-folder");
      if (n.health) marks += `<span class="feat-mark msg-${n.health}">!</span>`;
    }
    // The timeline marker: a dot in the indent, left of the icon, on the last feature
    // before it (featNodesFor) — or on the shut feature group that holds that feature.
    if (n.marker && (n.type === "feature" || collapsed.has(n.token))){
      cls.push("tl-marker");
      lead = `<span class="tl-dot" style="left:${pad - 9}px"></span>` + lead;
    }
    // A hairline between siblings of different phases (independent / dependent /
    // post-dependent) — groupFeatures marks the lower one.
    if (n.phaseEdge) cls.push("phase-edge");
    if (n.type === "feature"){
      // A problem marker after the name, never a colour on it (the timeline's rule).
      // No phase mark — the phase lines already say where each phase starts.
      if (n.health) marks += `<span class="feat-mark msg-${n.health}" title="${escapeHtml(n.msg)}">!</span>`;
      // A dropped move Fusion hasn't finished (featMove), or its refusal (featMoveErr).
      if (n.moving){ cls.push("feat-moving"); marks += `<span class="feat-spin" title="Fusion is moving this"></span>`; }
      if (n.moveErr) marks += `<span class="feat-err" title="${escapeHtml(n.moveErr)}">${escapeHtml(n.moveErr)}</span>`;
      // Suppressed dims; every body it touches hidden (the add-in's `unseen`) strikes,
      // like any other row that isn't drawn.
      if (n.suppressed) cls.push("suppressed");
      if (n.past) cls.push("past");
      if (n.unseen && !cls.includes("offview")) cls.push("offview");
    }
    html += `<div class="${cls.join(' ')}" data-tok="${escapeHtml(n.token)}" data-type="${n.type}" `
          + `data-sm="${n.sheetmetal?1:0}" style="padding-left:${pad}px">`
          + `${lead}<span class="name">${escapeHtml(shownName)}</span>${marks}`
          + `</div>`;
  }
  tree.innerHTML = html;
  tree.scrollTop = scroll;
  layoutPhaseEdges(tree);
  syncUiFlags();
  publishLiveBottom();
}
// A phase line spans only the text it separates: it starts at the row's indent (its
// icon) and ends where the longer of the two rows' content ends — this row's and the
// one drawn above it. Measured after each render into the ::before's --edge-l/--edge-w.
function layoutPhaseEdges(tree){
  // Page x of a row's content end (rects, not offsetLeft — the edge row is its own
  // offsetParent, the row above usually isn't).
  const end = r => { const c = r.lastElementChild; return c ? c.getBoundingClientRect().right : 0; };
  tree.querySelectorAll(".row.phase-edge").forEach(r => {
    const x0 = r.getBoundingClientRect().left;
    const first = r.firstElementChild;
    const left = first ? first.getBoundingClientRect().left - x0 : 0;
    const above = r.previousElementSibling;
    const right = Math.max(end(r), above && above.classList.contains("row") ? end(above) : 0) - x0;
    r.style.setProperty("--edge-l", left + "px");
    r.style.setProperty("--edge-w", Math.max(0, right - left) + "px");
  });
}
// Tells the native shell (FusionDock) where visible list content actually ends, so it
// can let clicks, scrolls and 3-finger gestures below that point fall through to
// Fusion's viewport instead of landing on this (empty) window — the same idea as the
// dock's own bottom-right notch, extended down here. Posts through
// webkit.messageHandlers, wrapped in try/catch: the standalone FusionBrowserApp shell
// and a plain browser (server.py's own preview) have no such handler, so this is a
// silent no-op there. rAF-throttled and only sent when the value actually changed —
// most callers (a poll's render()) fire every tick regardless of whether anything moved.
let liveBottomRaf = null;
let lastLiveBottom = null;
function publishLiveBottom(){
  if (liveBottomRaf != null) return;
  liveBottomRaf = requestAnimationFrame(() => {
    liveBottomRaf = null;
    const px = computeLiveBottom();
    if (px === lastLiveBottom) return;
    lastLiveBottom = px;
    try { window.webkit.messageHandlers.liveBottom.postMessage(px); } catch(_){}
  });
}
// -1 means "whole window live" (ordinary Browser behaviour: empty space deselects,
// double/right-click on it go to root / up one level). A real value is the bottom
// edge, in page pixels from the top, of the last row — everything below it becomes a
// pass-through band once the shell picks this up.
function computeLiveBottom(){
  const tree = document.getElementById("tree");
  if (!tree) return -1;
  const rows = tree.querySelectorAll(".row");
  if (rows.length === 0) return -1;                             // #waiting
  if (tree.scrollHeight > tree.clientHeight) return -1;          // list overflows
  if (document.getElementById("settings-bg")) return -1;         // settings dialog up
  if (blockedOpen()) return -1;                                  // refusal dialog up
  if (substOpen()) return -1;                                    // substitute dialog up
  if (press || drag || swDrag) return -1;                        // mid press/drag
  let bottom = rows[rows.length - 1].getBoundingClientRect().bottom;
  if (ctxOpen()){
    // Safe either way: when the menu doesn't reach past the last row this is just
    // lastRowBottom again, and when it does, the band starts below the menu instead
    // of getting cut off under it.
    bottom = Math.max(bottom, document.getElementById("ctx").getBoundingClientRect().bottom);
    // An open flyout can hang below the menu itself (placeFly publishes when it opens).
    for (const f of document.querySelectorAll("#ctx .ctx-fly.open"))
      bottom = Math.max(bottom, f.getBoundingClientRect().bottom);
  }
  return bottom;
}
// Tell the add-in what this window is holding, so ⌘X / ⌘V pressed with the VIEWPORT
// focused can be handed over — but only in the cases where they can only mean the
// browser. Posted from render(), which every selection change, cut and clear passes
// through, and only when a flag actually flips: a couple of requests per session.
let lastUiFlags = "", lastUiFlagsTs = 0;
function syncUiFlags(){
  const j = JSON.stringify({ canCut: cuttableToks().length > 0,
                             hasCut: cutToks.length > 0,
                             showOrigins, showConstruction });
  const now = Date.now();
  // Re-posted every couple of seconds even when nothing changed. The flags live in the
  // add-in's server module, so reloading the add-in resets them to false while this
  // window still believes it has said so — change-detection alone would then leave ⌘X
  // dead from the viewport until the next time the selection happened to move.
  if (j === lastUiFlags && now - lastUiFlagsTs < 2000) return;
  lastUiFlags = j; lastUiFlagsTs = now;
  fetch(API + "/uiflags", {method:"POST", headers:{"Content-Type":"application/json"},
                           body: j}).catch(()=>{});
}

// Each a named function so a Control Room hotkey and the menu tail's view toggles
// (viewCtxItems/VIEW_ACTS) can reach it the same way.
function toggleIgnoredFolders(){
  showIgnoredFolders = !showIgnoredFolders;
  render();
}
function toggleBoundsFolders(){
  showBoundsFolders = !showBoundsFolders;
  try { localStorage.setItem('browserShowBoundsFolders', showBoundsFolders ? '1' : '0'); } catch(_){}
  render();
}
function toggleMasterFolders(){
  showMasterFolders = !showMasterFolders;
  try { localStorage.setItem('browserShowMasterFolders', showMasterFolders ? '1' : '0'); } catch(_){}
  render();
}
function toggleSketches(){
  hideSketches = !hideSketches;
  try { localStorage.setItem('browserHideSketches', hideSketches ? '1' : '0'); } catch(_){}
  render();
}
function toggleBodiesGroup(){
  showBodiesGroup = !showBodiesGroup;
  try { localStorage.setItem('browserShowBodiesGroup', showBodiesGroup ? '1' : '0'); } catch(_){}
  render();
}
function toggleGeometryGroup(){
  showGeometryGroup = !showGeometryGroup;
  try { localStorage.setItem('browserShowGeometryGroup', showGeometryGroup ? '1' : '0'); } catch(_){}
  render();
}
function toggleConstruction(){
  showConstruction = !showConstruction;
  try { localStorage.setItem('browserShowConstruction', showConstruction ? '1' : '0'); } catch(_){}
  render();
}
function toggleOrigins(){
  showOrigins = !showOrigins;
  try { localStorage.setItem('browserShowOrigins', showOrigins ? '1' : '0'); } catch(_){}
  render();
}


// A bracketed tag Fusion appends ("Legs(Mirror)") is set off from the text on either
// side with a space; the brackets keep hugging what's inside. Display only — the
// component's real name is untouched. The occurrence ":1" is not text, so it stays glued.
function spaceParens(s){
  return (s || "")
    .replace(/([^\s(])\(/g, "$1 (")
    .replace(/\)([^\s):,.;])/g, ") $1");
}

function escapeHtml(s){ return (s||"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function nodeByTok(tok){
  let found = null;
  (function w(ns){ for (const n of ns){ if (n.token===tok){found=n;return;} if(n.children) w(n.children);} })(state.tree);
  return found;
}
// ── cursor movement (window-internal; never touches Fusion selection) ─────────
function moveCursor(delta){
  const rows = flatten().filter(r => !r.placeholder);   // placeholders aren't selectable
  if (!rows.length) return;
  // Step from the cursor if there is one, otherwise from the LAST thing selected.
  // That fallback matters because clicking a component or group selects without setting
  // a cursor — stepping from the top or bottom of the whole tree there is never what you
  // meant. (It used to matter far more: the cursor was nulled on every blur, so a step
  // driven from the viewport could never get past its first row.)
  let anchor = cursorTok;
  if (!anchor && selected.size) anchor = [...selected][selected.size - 1];
  let i = rows.findIndex(r => r.node.token === anchor);
  if (i < 0){
    // Cold start: nothing is highlighted, so the press LANDS on an end row rather than
    // stepping past it — C on the top row (skipping to the second read as losing the
    // first, since the implicit top row F acts on is never drawn), R on the bottom one.
    // F still reads that implicit top row unchanged.
    i = delta > 0 ? 0 : rows.length - 1;
  } else {
    i = (i + delta + rows.length) % rows.length;        // wrap around (like Parameters)
  }
  landCursor(rows[i].node.token);
}
// Space + C / Space + R: to the bottom / top of the cursor's section — one band of
// pinBand (components, Geometry, sketches, parameters, Origin) among its siblings —
// then the next section's, then up a level (Keys.sectionJump). Wraps: past the very
// end it lands on the first row, past the start on the last top-level row. Stepped
// from the same anchor C and R use.
function jumpCursor(dir){
  const rows = flatten().filter(r => !r.placeholder);
  if (!rows.length) return;
  let anchor = cursorTok;
  if (!anchor && selected.size) anchor = [...selected][selected.size - 1];
  const at = rows.findIndex(r => r.node.token === anchor);
  const i = Keys.sectionJump(rows.map(r => ({ depth: r.depth, band: pinBand(r.node) })), at, dir);
  if (i < 0 || (i === at && cursorTok === anchor)) return;
  landCursor(rows[i].node.token);
}
function landCursor(tok){
  cursorTok = tok;
  selAnchor = cursorTok;             // ⇧-click after stepping measures from here
  selected = new Set([cursorTok]);   // stepping REPLACES the selection, never adds to it
  render(); scrollCursorIntoView();
  pushCursorSelection();
}
// Stepping selects in the VIEWPORT too, not just here. C/R is a navigation tool driven
// from either surface, and from the viewport a highlight you can only see in the browser
// would not be worth a keystroke. Debounced: a held key steps every 55 ms and each push
// is a real Fusion selection round trip, so only the row you land on needs to travel.
const SEL_PUSH_DEBOUNCE = 120;
let selPushTimer = null;
// Stepping claims the new row IMMEDIATELY and only defers the trip to Fusion. The claim
// is what the mirror checks before it overwrites the selection, so while it lagged
// behind the debounce every step left a gap in which the echo of the PREVIOUS push
// still counted as current — and on quick C/R the highlight jumped back a row and then
// forward again. Nothing was ever functionally wrong; the window was just briefly
// believing a viewport that had not caught up yet.
// A timeline feature has no viewport item of its own (viewportItemsFor gives nothing,
// so selectmany would just clear the viewport) — it goes through the Timeline add-in
// instead, on the same debounce.
function pushCursorSelection(){
  if (!cursorTok) return;
  const n = nodeByTok(cursorTok);
  if (n && n.type === "feature"){
    selHold = null;
    selected = new Set([cursorTok]);
    pendingSel = { toks:[cursorTok], key:"[]", ts: Date.now(), sentTs: 0 };
    if (selPushTimer){ clearTimeout(selPushTimer); selPushTimer = null; }
    selPushTimer = setTimeout(()=>{ selPushTimer = null; selectFeature(n); }, SEL_PUSH_DEBOUNCE);
    render();
    return;
  }
  setSelection([cursorTok], true);
}
// The row an action acts on when the cursor is not sitting on one. Same rule C and R
// step from, in the same order: the cursor, else the LAST thing selected — which is how
// a pick made in the viewport reaches this window — else the top row, which is the
// implicit default selection and is never drawn as a highlight. Because of the middle
// case, pressing Go In with a body selected in the viewport still descends toward that
// body, rather than jumping to the top of the list.
function anchorNode(){
  const n = anchorNodeOrNull();
  if (n) return n;
  const rows = flatten().filter(r => !r.placeholder);
  return rows.length ? rows[0].node : null;
}
// The same, without the top-row fallback. For anything that decides WHERE something
// goes — a new group, a paste, a new parameter — the top row is the wrong default:
// inside a component that has children, that row is the first child component (or a
// group inside it), and the thing you made landed a level below where you are. With
// nothing picked, "where you are" is the active component, and the callers say so.
function anchorNodeOrNull(){
  let n = nodeByTok(cursorTok);
  if (n) return n;
  if (selected.size){
    n = nodeByTok([...selected][selected.size - 1]);
    if (n) return n;
  }
  return null;
}
// The container the active component stands for: its row token, or the document root.
function activeContainerTok(){ return viewTok() || ROOT_TOKEN; }   // the rooted one

// A face / edge / vertex picked in the viewport goes in two presses, for Go In and Edit
// alike. The first runs Select Feature Behind Pick (Timeline's trace): the feature, its
// sketch and the edges it made. While that selection stands (selQuiet, and nothing
// picked here since but its rows) this is its feature's token, and the second press
// acts on it — Go In edits the sketch, Edit the feature.
function tracedFeat(){
  const feats = state.selQuiet ? (state.selFeats || []) : [];
  if (!feats.length) return null;
  const rows = new Set(quietFeatRows(feats));
  return [...selected].every(t => rows.has(t)) ? feats[feats.length - 1] : null;
}

function expandOrInto(){
  const traced = tracedFeat();
  // _edit_source prefers the sketch the trace selected.
  if (traced){ featPost({action:"editsource", tok: traced, occTok: ""}); return; }
  if (pickEditLive() && !suspended){ featPost({action:"trace"}); return; }
  // A viewport pick's feature row whose Geometry group hasn't loaded yet: F still
  // means that feature (openPickedFeature).
  if (pickFeat && selected.has(pickFeat.tok) && !nodeByTok(pickFeat.tok)){
    featPost({action:"editsource", tok: pickFeat.featTok, occTok: pickFeat.occTok});
    return;
  }
  if (!anchorNodeOrNull() && editFromViewport()) return;
  const n = anchorNode(); if (!n) return;
  cursorTok = n.token;
  if (!selected.has(n.token)) selected = new Set([n.token]);
  // F on a PARAMETER opens its value for editing — "go in" on a parameter is its
  // expression, the same thing Enter does with the cursor on one.
  if (n.type === 'param'){ render(); openParamEditor(n.token); return; }
  if (n.type === 'sketch'){ enterSketch(n); return; }      // F on a sketch → enter edit
  // F on a BODY selects the WHOLE body. A face or edge picked in the viewport resolves
  // to its owning body ROW here (see _selection_rows), so the row looks identical either
  // way — F is how you promote that partial pick to the entire body. When the whole body
  // is already selected this re-selects the same body and lands on exactly the state it
  // was already in, so it reads as doing nothing, which is what it should do.
  // It must NEVER activate the body's owner: that clears the selection, which is what
  // made F look like it deselected the body.
  // …and OPENS its component's Geometry group, the timeline features (a click never
  // does this).
  if (n.type === 'body'){ selectBody(n); openGeoOfBody(n); return; }
  // F on a timeline FEATURE edits the sketch it was made from (Enter edits the feature).
  // No sketch behind it (Mirror, Pattern, Fillet…) → the add-in flashes the HUD cross.
  if (n.type === 'feature'){
    render(); featPost({action:"editsource", tok: n.featTok, occTok: n.occToken || ""});
    return;
  }
  // F on a component ACTIVATES it — you jump in, and the tree re-roots on it. Merely
  // expanding the row was the old behaviour; with the rooted view, "into" means the
  // same thing here as it does in the viewport. D is the way back out.
  // Landing inside, the top row is picked, so the next C/R/F starts from something
  // real. Posted after the activate, which clears Fusion's selection (FIFO queue).
  if (n.type === 'component'){
    activateComponentTok(n.token);
    const top = flatten().find(r => !r.placeholder);
    if (top) selectAfterCollapse(top.node.token);
    return;
  }
  // F on a Geometry group opens it (openGeos, not `collapsed` — see graftFeatures).
  if (n.type === 'features'){ openGeo(n); render(); return; }
  const effectiveKids = (n.children && n.children.length)
                      || (n.type === 'component' && n.empty) || n.type === 'folder'
                      || n.type === 'namegroup';
  // Groups OPEN when closed, and that is ALL F does to a group. On one already open it
  // does nothing: moving the cursor onto the first child would silently change what is
  // selected — you asked to go in, not to pick the thing at the top — and C is right
  // there for stepping onto it. Nothing under the cursor changes without you saying so.
  if (effectiveKids && collapsed.has(n.token)){
    collapsed.delete(n.token); cancelCollapseTimer(n.token); saveCollapsed();
    // Now that it is open it stands for its contents, so re-push the selection.
    setSelection([n.token]);
  }
}

// Space + F: Go In, and keep going while the level you land in holds ONE row. A
// component is entered (the page changes), a group opened; the first level with two
// or more rows stops it. Entered a component there → its top row is picked; opened a
// group there → that group stays picked, open. One BODY at the bottom → it is picked
// and opened, as F does. Any other lone row all the way down → picked, not entered or
// edited. On a row that has no level under it (a sketch, a parameter…) it is plain F.
function goInDeep(){
  let n = anchorNodeOrNull() || anchorNode();
  if (!n || !(n.type === 'component' || isGroupTok(n.token))) { expandOrInto(); return; }
  // The rows directly under `tok` as now drawn — a rooted component's are the top level.
  const kidsOf = tok => {
    const rows = flatten().filter(r => !r.placeholder);
    if (displayRoot() && displayRoot().token === tok) return rows.filter(r => r.depth === 0).map(r => r.node);
    const i = rows.findIndex(r => r.node.token === tok);
    if (i < 0) return [];
    const out = [], d = rows[i].depth;
    for (let j = i + 1; j < rows.length && rows[j].depth > d; j++)
      if (rows[j].depth === d + 1) out.push(rows[j].node);
    return out;
  };
  let paged = false;
  for (let guard = 0; guard < 64; guard++){
    let kids;
    if (n.type === 'component'){
      activateComponentTok(n.token);
      paged = true;
      kids = kidsOf(n.token);
    } else if (isGroupTok(n.token)){
      if (collapsed.has(n.token)){ collapsed.delete(n.token); cancelCollapseTimer(n.token); saveCollapsed(); }
      paged = false;
      kids = kidsOf(n.token);
    } else if (n.type === 'body'){
      // A lone body is gone into the way F goes into one: picked whole, and its
      // component's Geometry group opened. The rows arrive later (pollFeats), so it's the end.
      // Claim the body as the pending pick too: a group picked just before (the one we
      // started on) is otherwise still pending, and the body's viewport echo restored
      // it — the group AND the body read as selected.
      cursorTok = n.token; selAnchor = n.token; selected = new Set([n.token]); selHold = null;
      const key = JSON.stringify([n.token]);
      pendingSel = { toks:[n.token], key, ts: Date.now(), sentTs: Date.now() };
      selectBody(n); openGeoOfBody(n); render(); scrollCursorIntoView();
      return;
    } else break;                                   // a leaf: one row all the way down
    if (kids.length !== 1){
      if (paged){ if (kids.length) selectAfterCollapse(kids[0].token); else render(); }
      else selectAfterCollapse(n.token);
      return;
    }
    n = kids[0];
  }
  selectAfterCollapse(n.token);
}

// F with no row picked here, but a sketch or a timeline feature picked in the viewport
// whose row isn't drawn — its body never opened, or the pick made by Trace / the dock's
// timeline — edits that sketch, just as F on its row would. Without this F fell through
// to the top row. A feature goes to the Timeline add-in, which edits the sketch its
// click selected (or the one it finds behind the feature).
function editFromViewport(){
  const sk = (state.selTokens || []).map(nodeByTok)
               .filter(n => n && n.type === 'sketch').pop();
  if (sk){ enterSketch(sk); return true; }
  const feats = state.selQuiet ? (state.selFeats || []) : [];
  if (feats.length){
    featPost({action:"editsource", tok: feats[feats.length - 1], occTok: ""});
    return true;
  }
  return false;
}

// Exit the sketch currently being edited — reuse Go Up a Level, which exits the
// sketch and lands in its parent component (with the camera/marker/visibility
// cleanup that add-in does).
function exitSketchEdit(){ post({action:"command", cmd_id:"GoToParentComponentCmd"}); }
// The open group each picked row sits in. A row that IS an open group stands for
// itself — D closes the group you are on, not the one around it — and a row that sits
// in no group contributes nothing. Nested groups resolve to the nearest OPEN one,
// which is the only one you can actually see the row through.
// A group is any of the rows that hold leaves inside a component: a folder (sketch
// folder or parameter group), a name group, the Geometry group — or a timeline group
// of an open body's features.
function isGroupTok(t){ return isFolderTok(t) || isNameTok(t) || isGeoTok(t) || isFGroupTok(t); }
function openGroupsOfSelection(){
  const toks = (selected.size ? [...selected] : (cursorTok ? [cursorTok] : []))
                 .filter(t => t && nodeByTok(t));
  const out = [];
  for (const t of toks){
    let g = "";
    if (isGroupTok(t) && !collapsed.has(t)) g = t;
    else for (let p = parentTokOf(t); isGroupTok(p); p = parentTokOf(p))
      if (!collapsed.has(p)){ g = p; break; }
    if (g && out.indexOf(g) < 0) out.push(g);
  }
  return out;
}

// D closes what is open here before it will leave the level: a level with groups still
// hanging open is not tidied up yet, so stepping out of it would leave them to be found
// open again on the way back in. One press closes one LEVEL, deepest first — every open
// group (and open Geometry group, held open by openGeos rather than `collapsed`) drawn
// at the greatest depth — so repeated presses fold the tree back up the way it was
// opened. Nothing is selected inside them — that case is handled above and returns — so
// the selection is left exactly as it is.
function collapseOpenGroups(){
  const open = flatten().filter(r => !r.placeholder
    && ((isGroupTok(r.node.token) && !collapsed.has(r.node.token)) || geoOpen(r.node)));
  if (!open.length){
    // Geometry groups open but not drawn (their component is shut) — nothing to fold
    // level by level.
    const geos = closeAllGeos();
    if (geos) refreshFolders();
    return geos;
  }
  const deepest = Math.max(...open.map(r => r.depth));
  let geos = false, groups = false;
  for (const r of open){
    if (r.depth !== deepest) continue;
    if (geoOpen(r.node)){ openGeos.delete(r.node.token); geos = true; }
    else { collapsed.add(r.node.token); rememberCollapse(r.node.token); groups = true; }
  }
  if (geos){ postWatch(); refreshFolders(); }
  if (groups){ saveCollapsed(); render(); }
  return true;
}

function collapseOrOut(){
  // D while editing → exit sketch (not in suspend mode: there it peeks up, below)
  if (state.editingSketch && !suspended){ exitSketchEdit(); return; }
  const rooted = displayRoot();
  // A feature (or an open Geometry group) picked → D shuts that group and picks it —
  // unless it sits in an open feature group: that closes first, like any group (below).
  const og = openGroupsOfSelection().some(isFGroupTok) ? null : openGeoOfSelection();
  if (og){ closeGeo(og.token); selectAfterCollapse(og.token); return; }
  // Something picked inside a group → D closes that group. Read from the SELECTION,
  // not the cursor: a pick made in the viewport arrives as a selection without moving
  // the cursor, so D on a body you had just clicked in the canvas stepped out of the
  // component instead of closing the group the body was sitting in.
  const groups = openGroupsOfSelection();
  if (groups.length){
    for (const g of groups){ collapsed.add(g); rememberCollapse(g); }
    saveCollapsed();
    // One group closed → it takes the place of what was sealed inside it, the same as
    // collapsing the row you are on. Several closed at once → there is no single row
    // to land on, so nothing stays picked.
    if (groups.length === 1) selectAfterCollapse(groups[0]);
    else {
      selHold = null;
      selected.clear(); cursorTok = null;
      render(); post({action:"deselect"});
    }
    return;
  }
  // Nothing under the cursor → D leaves the component: that is the state right after
  // you step into one, and pressing D there means "back out", not "do nothing".
  const n = nodeByTok(cursorTok);
  if (!n){ if (!collapseOpenGroups() && rooted) goUpOneLevel(true); return; }
  const effectiveKids = (n.children && n.children.length)
                      || (n.type === 'component' && n.empty) || n.type === 'folder'
                      || n.type === 'namegroup';
  if (effectiveKids && !collapsed.has(n.token)){
    collapsed.add(n.token); rememberCollapse(n.token);
    saveCollapsed();
    selectAfterCollapse(n.token);        // the group you just closed is what you are on
  }
  else {
    // collapse the parent and move cursor to it
    let parent=null;
    (function w(ns,p){ for(const x of ns){ if(x.token===cursorTok){parent=p;return;} if(x.children) w(x.children,x);} })(state.tree,null);
    // A top row's parent is the component we're rooted in, which isn't drawn — moving
    // the cursor onto it would park it on an invisible row, so D backs out instead.
    if (rooted && (!parent || parent.token === rooted.token)){
      if (!collapseOpenGroups()) goUpOneLevel(true);
      return;
    }
    if (parent){
      collapsed.add(parent.token); rememberCollapse(parent.token); saveCollapsed();
      // Was a sketch/body inside the group picked? It is sealed in now, so the group
      // takes its place as the selection and the item is dropped.
      selectAfterCollapse(parent.token);
    }
  }
}
// Holding C / R scrolls the selection, the way holding an arrow key does. The OS
// drives the repeat: the app turns press-and-hold off for itself
// (letHeldKeysRepeat, fusion_ui/WindowShell.swift), which is what used to swallow
// letter-key repeat — so each repeat is just another keydown, at the system rate.
// (This used to be a JS timer standing in for the missing repeats.)
function stepCursor(delta){ moveCursor(delta); }

function scrollCursorIntoView(){
  const el = document.querySelector(`.row[data-tok="${cssEsc(cursorTok)}"]`);
  if (el) el.scrollIntoView({block:"nearest"});
}
function cssEsc(s){ return (s||"").replace(/["\\]/g, "\\$&"); }

// ── actions ──────────────────────────────────────────────────────────────────
// Enter / double-click action, dispatched by item type:
//   component / Top Level → activate    body → select in viewport    sketch → edit
//   feature → Edit Feature
// A face / edge / vertex picked in the viewport, the cursor not moved off it: two
// presses, as Go In (tracedFeat). The first traces it — a mirrored copy lands on what
// it mirrors, nothing behind it puts up the HUD cross — and the second edits the
// feature the trace selected, in the occurrence it found it in (edittraced). Whether
// its row has loaded, or exists under this body at all, doesn't matter. True when it
// acted (or would have, suspended).
function editViewportPick(){
  const traced = tracedFeat();
  if (traced){
    if (!suspended) featPost({action:"edittraced", tok: traced});
    return true;
  }
  if (pickEditLive()){
    if (!suspended) featPost({action:"trace"});
    return true;
  }
  return false;
}
function editTimelineItem(){
  if (editViewportPick()) return;
  const n = anchorNodeOrNull();
  if (n && n.type === "feature")     editFeature(n);
  else if (n && n.type === "sketch") enterSketch(n);
}
function activateCursor(){
  // No cursor and nothing picked → do nothing: activating root here made a bare Enter
  // jump to Top Level. Root stays
  // reachable deliberately: Top Level is a real row (C / ↓ lands on it), and
  // double-click or right-click on empty space go through activateRootUI().
  if (editViewportPick()) return;
  // No cursor, but a row picked — from the viewport, or the cursor dropped by a render
  // while its row wasn't drawn: that row, as F does (anchorNodeOrNull).
  if (!cursorTok){
    const n = anchorNodeOrNull();
    if (!n) return;
    cursorTok = n.token;
  }
  const n = nodeByTok(cursorTok);
  if (n && n.type === "param")       openParamEditor(cursorTok);
  else if (n && (n.type === "folder" || n.type === "geometry" || n.type === "namegroup"))
    toggleFolderOpen(cursorTok);
  else if (n && n.type === "body")   selectBody(n);
  else if (n && n.type === "feature") editFeature(n);  // Enter = Edit Feature (F = its sketch)
  else if (n && n.type === "sketch") enterSketch(n);   // Enter = enter sketch edit
  else activateComponentTok(cursorTok);
}
// Activate a component with optimistic UI: re-root the tree on it, reveal its path and
// un-grey it right away, then tell Fusion. Shared by Enter and F.
function activateComponentTok(tok){
  if (suspended){ peekTo(tok); return; }   // suspend mode: look, don't go — see peekTo
  // A real activation (suspend mode's escape hatch, jumpToCursor) supersedes a peek;
  // the add-in drops its own in _activate_target_inner.
  state.peekToken = null; pendingPeek = null;
  selected.clear();
  state.activeToken = tok; prevActiveToken = tok;
  holdActivate(tok);
  revealActiveSpine(); markActivePathVisible(); render();   // optimistic bold + spine + un-grey
  post({action:"activate", token: tok});
}
// Make `toks` (display order) the selection, here AND in the viewport. Rows Fusion
// can't select are kept in the browser set and simply left out of the push; if none
// of them can be selected the viewport is cleared, which is what "only groups are
// picked" should look like there.
// What a selected row means IN THE VIEWPORT: a row that IS a Fusion entity — a body,
// sketch or component — and a BODIES group (the Bodies row, or a folder filed in it),
// which stands for every showing body inside it, nested folders included. Every other
// group (sketch folders, parameter groups, Geometry) owns no entity and pushes nothing.
function isBodiesGroup(n){
  return !!n && (n.type === "geometry" || (n.type === "folder" && !n.params
                                           && bodiesIn(n).length > 0));
}
function bodiesIn(n){
  const out = [];
  (function w(ns){ for (const c of ns || []){
    if (c.type === "body"){ if (c.visible !== false) out.push(c); }
    else if (c.type === "folder") w(c.children);
  } })(n.children);
  return out;
}
function viewportItemsFor(toks){
  const out = [], seen = new Set();
  const push = n => {
    if (!n || seen.has(n.token)) return;
    if (isBodiesGroup(n)){ bodiesIn(n).forEach(push); return; }
    if (n.type !== "body" && n.type !== "sketch" && n.type !== "component") return;
    seen.add(n.token);
    out.push({ token:n.token, occToken:n.occToken || "" });
  };
  for (const t of toks) push(nodeByTok(t));
  return out;
}

// `defer` holds the trip to Fusion back by SEL_PUSH_DEBOUNCE, so a burst of steps sends
// one selection instead of one per row. Everything else — what this window claims to
// have selected — takes effect straight away either way.
function setSelection(toks, defer){
  selHold = null;                  // a fresh pick releases the post-hide hold
  selected = new Set(toks);
  const items = viewportItemsFor(toks);
  const key = JSON.stringify(items.map(i => i.token));
  pendingSel = { toks:[...toks], key, ts: Date.now(), sentTs: 0 };
  const send = () => {
    if (pendingSel) pendingSel.sentTs = Date.now();
    post({action:"selectmany", items});
  };
  if (selPushTimer){ clearTimeout(selPushTimer); selPushTimer = null; }
  if (defer) selPushTimer = setTimeout(()=>{ selPushTimer = null; send(); }, SEL_PUSH_DEBOUNCE);
  else send();
  render();
}
// A selection set in the order the rows are drawn (a Set keeps click order).
function rowsInTreeOrder(toks){
  const rows = flatten().filter(r => !r.placeholder).map(r => r.node.token);
  return [...toks].sort((a, b) => rows.indexOf(a) - rows.indexOf(b));
}
// The display rows between two tokens, inclusive — what ⇧-click selects.
function rowsBetween(a, b){
  const rows = flatten().filter(r => !r.placeholder).map(r => r.node.token);
  const i = rows.indexOf(a), j = rows.indexOf(b);
  if (i < 0 || j < 0) return [b];
  return rows.slice(Math.min(i,j), Math.max(i,j) + 1);
}
function selectBody(n){ post({action:"selectbody", token:n.token, occToken:n.occToken || ""}); }
function selectSketch(n){ post({action:"selectsketch", token:n.token, occToken:n.occToken || ""}); }
function enterSketch(n){
  // Suspend mode: the open sketch / command must stay put — peek at the sketch's
  // component instead of entering it.
  if (suspended){ peekTo(n.occToken || "__root__"); return; }
  // Optimistic: move the bold+underline emphasis onto this sketch row immediately
  // (and mark its owning occurrence active), rather than waiting for the viewport →
  // poll round-trip. Held until confirmed by pollState (see pendingEditSketch).
  state.editingSketch = true;
  state.editSketchToken = n.token;
  if (n.occToken) { state.activeToken = n.occToken; prevActiveToken = n.occToken; }
  pendingEditSketch = { token:n.token, occToken:n.occToken || "", ts:Date.now() };
  // Bodies and Geometry are about to vanish from the tree (rowVisible) — stop polling
  // the feature rows their groups would otherwise still be fetching while hidden.
  closeAllGeos();
  render();
  post({action:"entersketch", token:n.token, occToken:n.occToken || ""});
}

// Activating a (possibly hidden) component reveals it and its ancestor path in the
// viewport. Optimistically clear their grey right away via pendingVis, so the text
// goes regular immediately instead of after the poll round-trip. Reconciled/expired
// like any other pendingVis entry (dropped once the server confirms, or after 5s).
function markActivePathVisible(){
  const now = Date.now();
  for (const tok of activePathList()) pendingVis[tok] = { visible:true, ts: now };
  applyPendingVis();
}
// Going to root un-isolates the model, so every component that isolate had hidden
// (everything off the CURRENT active path) goes back to what the user set — shown,
// unless its `userHidden` intent says they hid it (the marks below all do the same).
// Set optimistically now — the bulbs (n.visible) lag a sync cycle behind. Must run
// BEFORE activeToken is cleared (activePathSet reads it).
function markUnisolatedVisible(){
  if (!(state.toggles && state.toggles.isolate) || !state.activeToken) return;
  const path = activePathSet();
  const now = Date.now();
  (function w(ns){ for (const n of ns){
    if (n.type === "component" && !path.has(n.token)) pendingVis[n.token] = { visible: !n.userHidden, ts: now };
    if (n.children) w(n.children);
  }})(state.tree);
  applyPendingVis();
}
// Optimistically mark EVERY component visible (un-grey) — used when going to the top
// level (from the viewport or the browser), which un-isolates the whole model.
function markAllVisible(){
  const now = Date.now();
  (function w(ns){ for (const n of ns){
    if (n.type === "component") pendingVis[n.token] = { visible: !n.userHidden, ts: now };
    if (n.children) w(n.children);
  }})(state.tree);
  applyPendingVis();
}
// Optimistically mark a component and all its descendant components visible (un-grey)
// — used when moving UP a level, where the new active's whole subtree becomes visible.
function markSubtreeVisible(tok){
  const node = nodeByTok(tok); if (!node) return;
  const now = Date.now();
  (function w(n){ if (n.type === "component") pendingVis[n.token] = { visible: !n.userHidden, ts: now };
    if (n.children) for (const c of n.children) w(c); })(node);
  applyPendingVis();
}

// Activate the document root (top level) with optimistic UI. Right-click on empty
// space, and go-up-one-level when already at a top-level component, land here.
function activateRootUI(){
  if (suspended){ peekTo("__root__"); return; }
  state.peekToken = null; pendingPeek = null;
  markUnisolatedVisible();
  selHold = null; pendingSel = null;
  selected.clear(); cursorTok = null;
  state.activeToken = ""; prevActiveToken = "";
  holdActivate("");
  revealActiveSpine(); render();
  post({action:"deselect"});
  post({action:"activate", token:"__root__"});
}
// Clear the selection only — browser highlight AND viewport selection — without
// changing the active component. Left-click in empty space lands here.
function deselectAll(){
  if (suspended) return;   // the current viewport selection must stay exactly as it is
  // Deselecting only deselects: the group the selection was sitting in stays open, as
  // it always did for Geometry. Closing groups is D's job.
  selHold = null; pendingSel = null;
  selected.clear(); cursorTok = null;
  render();
  post({action:"deselect"});
}
// Go up one level: activate the parent of the current active component. Already at
// root → nothing to do. Active is a top-level component → its parent is the root.
// `pickLeft`: select the component just left, so you land ON it — D / Go Out passes it
// (collapseOrOut); right-click on empty space doesn't, and neither does the viewport
// two-finger tap (that runs Fusion's Go Up directly and never comes through here).
// Posted after the activate, which clears Fusion's selection; the add-in's queue is FIFO.
function goUpOneLevel(pickLeft){
  const path = activePathList();                 // [topLevel … view]; [] at the root
  if (!path.length) return;                      // already at the top level
  const leftTok = path[path.length - 1];
  const parentTok = path.length >= 2 ? path[path.length - 2] : "";
  if (suspended){
    // Suspend mode: the parent of the VIEW. The component left is picked here only
    // (selectAfterCollapse) — the running command's selection is its own.
    peekTo(parentTok || "__root__");
    if (pickLeft) selectAfterCollapse(leftTok);
    return;
  }
  if (!parentTok){                               // active is top-level → up = root
    activateRootUI();
    if (pickLeft) selectAfterCollapse(leftTok);
    return;
  }
  // pendingSel too: a feature row picked just before holds itself through the viewport's
  // next echo (selectFeature's anyEcho), which would re-light it on the far side.
  selHold = null; pendingSel = null;
  selected.clear(); cursorTok = null;
  state.activeToken = parentTok; prevActiveToken = parentTok;
  holdActivate(parentTok);
  markActivePathVisible(); markSubtreeVisible(parentTok);   // un-grey parent + its subtree
  revealActiveSpine(); render();
  post({action:"activate", token: parentTok});
  if (pickLeft) selectAfterCollapse(leftTok);
}

// ⌘E acts on the SELECTION when there is one, falling back to the cursor row. It used
// to read the cursor ONLY, so anything picked with ⌘-click — which builds a selection
// without moving the cursor — could not be toggled at all. Folders toggle their own
// state; parameters have no visibility and are skipped. Several rows move TOGETHER:
// all visible → hide them all, otherwise show them all.
// Suspend mode's escape hatch: run whatever an ordinary (unsuspended) Enter would do
// to the cursor/selection right now — jump the viewport to it, enter its sketch,
// select its body — without actually leaving suspend mode, so browsing continues
// right after. "There is always a way to go back" cuts both ways: this is the way
// forward, one row at a time, without giving up the mode itself.
function jumpToCursor(){
  const was = suspended;
  suspended = false;
  try { activateCursor(); } finally { suspended = was; }
}

function toggleVisCursor(){
  if (suspended) return;   // visibility is a Fusion-side change, held back like the rest
  const toks = (selected.size ? [...selected] : (cursorTok ? [cursorTok] : []))
                 .filter(t => t && t !== "__root__" && !isParamTok(t));
  if (!toks.length) return;
  for (const t of toks.filter(isFolderTok)) toggleVisFolder(t);
  const nodes = toks.filter(t => !isFolderTok(t)).map(nodeByTok).filter(Boolean);
  // A timeline feature has no bulb — its hide is Suppress (a feature group: its
  // features). All of them suppressed → unsuppress, else suppress, as the menu does.
  const feats = [];
  for (const n of nodes){
    if (n.type === "feature") feats.push(n);
    else if (n.type === "fgroup") feats.push(...n.children.filter(c => c.type === "feature"));
  }
  if (feats.length){
    const on = !feats.every(f => f.suppressed);
    featPost({action:"suppress", toks: [...new Set(feats.map(f => f.featTok))], on});
    pollFeats();
  }
  const leaves = nodes.filter(n => n.type !== "feature" && n.type !== "fgroup");
  if (!leaves.length) return;
  // Decide from what the row DISPLAYS, not from the raw bulb.
  //
  // While isolate is on, an off-path row is never greyed (flatten() attributes its dark
  // bulb to isolate policy rather than to the user), so a row can read as visible while
  // its bulb is already off. Taking the target from the bulb then inverted the gesture:
  // pressing hide on a row that looked visible computed "make visible" and switched it
  // back ON. Reading the displayed state keeps the gesture honest — hide always means
  // hide. `flip` still consults the bulb, because that is what actually has to change.
  const shownByTok = new Map(flatten().filter(r => !r.placeholder)
                                      .map(r => [r.node.token, !(r.effHidden || r.userHidden)]));
  // A component row has no `own`: its `visible` IS its own bulb (the occurrence's
  // isLightBulbOn). Reading `own` alone made every component look already-off, so a
  // hide found nothing to flip.
  const ownOf = l => l.type === "component" ? !!l.visible : !!l.own;
  const isShown = l => shownByTok.has(l.token) ? shownByTok.get(l.token) : ownOf(l);
  const target = !leaves.every(isShown);
  // `flip` reads the OWN bulb, never the effective one: a row is dark for four
  // different reasons and only its own bulb is what a hide/show actually writes.
  const flip = leaves.filter(l => ownOf(l) !== target);
  if (!flip.length) return;
  const now = Date.now();
  for (const l of flip) pendingVis[l.token] = { visible: target, user: !target, ts: now };
  applyPendingVis(); render();
  // Keep these rows picked here even once Fusion drops the hidden ones from its own
  // selection, so pressing the toggle again unhides exactly what you just hid.
  selHold = new Set(selected);
  // Absolute — this is the gesture that STATES the user hid or showed something, and
  // one of only three things that write the intent record (the others being ⌘E in the
  // viewport, and adopting a change made outside our code).
  post({action:"setvis", tokens: flip.map(l => l.token), visible: target});
}
// Cmd+D — delete the rows this window is on: its own selection if it has one,
// otherwise the cursor row. The window's cursor/selection is NOT a Fusion
// selection, so this can't be a forwarded hotkey — the add-in re-selects these
// exact rows in the viewport and runs Fusion's own Delete on them (undoable with
// Cmd+Z like any other delete).
// Parameters are deleted through their own server, and only when nothing OUTSIDE the
// set still references them — Fusion's deleteMe() refuses otherwise. A dependant that
// is itself being deleted doesn't count: the add-in sends every dependant by name
// (`depNames`; model parameters like d12 are in there too and never match a user
// parameter, so they block as they should), and the set is deleted dependants-first so
// each deleteMe() finds nothing left pointing at it. The rows go immediately; a forced
// resync a moment later brings back anything Fusion did refuse.
//
// What a delete of exactly these names would leave behind: {ok: names in an order that
// is safe to delete, blocked: [{name, ext: [external dependants]}]}. A name is blocked
// when a dependant is outside the set — including a dependant that is in the set but
// is itself blocked, since that one stays and keeps pointing at it (hence the loop).
function paramDepsOf(n){
  const pr = paramByName(n) || {};
  // An add-in that predates depNames only sends the count: treat any dependant as
  // external, which is the old (always-blocked) behaviour.
  return Array.isArray(pr.depNames) ? pr.depNames : ((pr.deps || 0) > 0 ? [null] : []);
}
function paramDeletePlan(names){
  const want = new Set(names.filter(n => paramByName(n)));
  const depsOf = paramDepsOf;
  const blocked = new Map();
  for (;;){
    let changed = false;
    for (const n of want){
      const ext = depsOf(n).filter(d => d === null || !want.has(d));
      if (!ext.length) continue;
      blocked.set(n, ext.map(d => d === null ? "?" : d)); want.delete(n); changed = true;
    }
    if (!changed) break;
  }
  return { ok: dependantsFirst(want), blocked: [...blocked].map(([name, ext]) => ({ name, ext })) };
}
// Dependants first: peel off anything nothing left in the set depends on.
function dependantsFirst(names){
  const out = [];
  const left = new Set(names);
  while (left.size){
    const free = [...left].filter(n => !paramDepsOf(n).some(d => left.has(d)));
    if (!free.length){ out.push(...left); break; }   // a cycle can't happen; don't hang
    for (const n of free){ out.push(n); left.delete(n); }
  }
  return out;
}
// A delete that would leave dependants behind asks what to put in their place first
// (openSubstitute), one blocked name at a time; the add-in rewrites every dependant's
// expression to the substitute and then deletes (`_delete_param` in Parameters.py).
function deleteParams(toks){
  const names = toks.map(paramNameOf).filter(n => paramByName(n));
  const { blocked } = paramDeletePlan(names);
  if (blocked.length){ openSubstitute(names, blocked.map(b => b.name)); return; }
  runParamDeletes(names, {});
}
function runParamDeletes(names, subs){
  const order = dependantsFirst(names);
  if (!order.length) return;
  for (const name of order){
    paramRows = paramRows.filter(x => x.name !== name);
    if (paramPlace[name] !== undefined){ delete paramPlace[name]; saveFolders(); }
  }
  selected.clear(); refreshFolders();
  // Strictly one after the other: the add-in runs its queue in arrival order, and
  // parallel fetches aren't guaranteed to arrive in the order they were issued.
  (async ()=>{
    for (const name of order){
      const body = subs[name] ? { name, replacement: subs[name] } : { name };
      await fetch(PARAMS_API + "/delete", {method:"POST",
        headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)})
        .catch(()=>{});
    }
    setTimeout(()=>{ lastParamsJSON = ""; }, 1500);   // re-sync against what Fusion did
  })();
}

function deleteSelection(){
  deleteTokens((selected.size ? [...selected] : (cursorTok ? [cursorTok] : []))
                 .filter(t => t && t !== "__root__"));
}
// Delete whatever these rows are. Folders dissolve in this window, parameters go to
// the Parameters server, everything else is a real Fusion delete (undoable with ⌘Z).
// A mixed set takes the window-only ones first, so one keystroke can't sweep geometry
// away alongside a folder — unless `all` is set, which is the context menu's "Delete N
// Selected": there the label said exactly what goes, so everything goes.
function deleteTokens(toks, all, asked){
  const folders = toks.filter(isFolderTok);
  if (folders.length){
    const users = asked ? [] : groupCombines(folders);
    if (users.length){ confirmGroupDelete(folders, users, () => deleteTokens(toks, all, true)); return; }
    deleteFolders(folders); if (!all) return;
  }
  const fgs = toks.filter(isFGroupTok);          // a feature group only dissolves
  if (fgs.length){ explodeFeatureGroups(fgs); if (!all) return; }
  const params = toks.filter(isParamTok);
  if (params.length){ deleteParams(params); if (!all) return; }
  // Feature rows are timeline items: the Timeline add-in deletes the features.
  const feats = featToksOf(toks);
  if (feats.length){ featPost({action:"delete", toks: feats}); pollFeats(); }
  const items = [];
  for (const t of toks){
    if (isFolderTok(t) || isParamTok(t) || isFeatTok(t) || isFGroupTok(t)) continue;
    // A name group is nothing but the names under it, so there is no row to delete and
    // nothing to send Fusion. Emptying one means moving its parameters out; taking the
    // level out of their names is Explode, on its menu.
    if (isNameTok(t)) continue;
    const n = nodeByTok(t);
    if (n) items.push({ token: n.token, occToken: n.occToken || "" });
  }
  if (!items.length) return;

  // Keep the cursor where the deleted row was, so a run of deletes stays under
  // the same finger: remember the row index, then take whatever lands there.
  const rows = flatten().filter(r => !r.placeholder);
  const idx  = rows.findIndex(r => r.node.token === cursorTok);
  const cursorGoing = items.some(it => it.token === cursorTok);

  const now = Date.now();
  for (const it of items) pendingDelete[it.token] = now;
  applyPendingDelete();
  selected.clear();
  if (cursorGoing){
    const after = flatten().filter(r => !r.placeholder);
    cursorTok = after.length ? after[Math.min(Math.max(idx, 0), after.length - 1)].node.token
                             : null;
  }
  render();
  post({ action: "delete", items });
}
// ⌘R renames a parameter in place. It posts to the Parameters server, which owns
// parameters — this window only files them. Editing a VALUE goes through the value
// dialog instead (openParamEditor), which posts its own edit to the same server.
// NOTE this is a CROSS-ORIGIN post (this page is served from 8732), so that server has
// to answer the CORS preflight or the request is never sent at all — silently.
function setParam(body){
  fetch(PARAMS_API + "/set", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)}).catch(()=>{});
}
// Enter / F on a parameter row: hand it to the value dialog, which is the editor for
// this kind of thing everywhere else too. The add-in on the other end has to open the
// session on Fusion's main thread, so this only asks — the window appears a tick later,
// centred, with both boxes filled in.
function openParamEditor(tok){
  const name = paramNameOf(tok);
  if (!name || !paramByName(name)) return;
  fetch(VALUE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ action:"openparam", name, filed: filedPlace(name) })})
    .catch(()=>{});
}
// Where an old-convention parameter is hand-filed, as the value dialog's name stepper
// wants it for a rename: the owning component's raw name and the parameter-group chain
// inside it, outermost first. A name that parses has no filing (its levels are its
// place), so it gets null, as does anything whose owner can't be named.
function filedPlace(name){
  if (partsOf(name)) return null;
  const c = paramContainer(name);
  const groups = [];
  let ref = c;
  if (isFolderTok(c)){
    let f = folderOf(c);
    ref = f ? f.comp : "";
    const seen = new Set();
    while (f && isParamFolderDef(f) && !seen.has(f)){
      seen.add(f);
      groups.unshift(f.name || "");
      f = f.parent ? (folderDefs[f.parent] || folderOf(f.parent)) : null;
    }
  }
  const owner = (!ref || ref === ROOT_TOKEN) ? (state.rootName || "").trim()
                                            : ((compByCid[ref] || compByTok[ref] || {}).name || "");
  return owner ? { owner, groups: groups.filter(Boolean) } : null;
}

// The line that placed a sketch, opened again in the box that placed it. Same
// arrangement as openParamEditor above: this only ASKS, and the add-in on the other end
// arms the session on Fusion's main thread a tick later.
function redefinePlane(tok){
  fetch(VALUE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ action:"opensketchplane", sketch: tok })}).catch(()=>{});
}

// A component's bounds, opened in the panel that set them (Add Bounds to Component's,
// in edit mode when it already has some). Same arrangement again: this only asks.
function editBounds(tok){
  fetch(VALUE_API + "/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ action:"openaddbounds", comp: tok, edit: true })}).catch(()=>{});
}

// Move a parameter's FILING across a rename, and put it back if the rename never lands.
//
// The filing is keyed by name, so it has to travel or the row keeps its old name, finds
// nothing filed under it, and drops to the top level. It is moved optimistically because
// that is this window's standing rule — but a rename can still be refused downstream
// (Fusion will not take the name) and nothing tells this window, so the optimistic move
// would strand the row at the top level for good. That is precisely what a failed rename
// used to look like. If the new name has not appeared by the time two polls have gone by,
// the filing goes back where it was.
//
// It also carries the OWNER. A structured name's first level is a component's name, and
// several components can share one (three "Steel"s), so a name alone can land in the
// wrong one. `owner` (default: the component it sits in under its OLD name) becomes the
// new name's pin when the name could mean more than one component (pinFor) — so a
// rename that spells out the component it is already in keeps it there, and a drag
// passes the component it was dropped on.
// `folder` (a move's folderForContainer) says which parameter group the new name is
// filed in — null takes it out of the one it was in; undefined (a rename in place)
// keeps whatever it had.
function carryFilingOnRename(oldName, newName, owner, folder){
  if (!newName || newName === oldName) return;
  // The shown/hidden parts go with the name, unconditionally (a rename refused
  // downstream leaves an orphan entry under the new name — harmless, never drawn).
  if (paramShow[oldName]){ paramShow[newName] = paramShow[oldName]; delete paramShow[oldName]; saveFolders(); }
  const had = paramPlace[oldName];
  const pin = pinFor(newName, owner === undefined ? ownerRefOfParam(oldName) : owner);
  const put = folder ? folder
            : pin !== undefined ? pin
            : (folder === null && isFolderTok(had || "")) ? undefined : had;
  if (put === undefined && had === undefined) return;
  if (put !== undefined) paramPlace[newName] = put;
  delete paramPlace[oldName];
  saveFolders();
  setTimeout(() => {
    if (paramByName(newName) || !paramByName(oldName)) return;   // it took
    if (had !== undefined) paramPlace[oldName] = had;
    delete paramPlace[newName];
    saveFolders(); refreshFolders();
  }, 2000);
}

// ⌘R on a parameter: rename it in place. It used to take a `field` and open the
// EXPRESSION in the same way for Enter / F; that half is gone — a value is edited in the
// value dialog now (see openParamEditor), which is the same editor a dimension uses.
function beginParamRename(tok){
  if (editing) return;
  const name = paramNameOf(tok);
  const row = rowEl(tok), pr = paramByName(name);
  if (!row || !pr) return;
  const el = row.querySelector(".name");
  if (!el) return;
  // A structured name is renamed AS THE ROW READS IT — the type and the direction,
  // "Inset - Back". Its levels are the groups it is filed in and are changed by moving
  // it, not by typing; opening the box on the whole underscored name would invite an
  // edit to the levels that the tree would then have to disagree with. Everything else
  // opens on its real name, as it always did.
  const parts = partsOf(name);
  editing = true;
  const inp = document.createElement("input");
  inp.className = "name-input";
  inp.value = parts ? leafLabel(parts) : pr.name;
  const was = inp.value;
  el.replaceWith(inp); inp.focus(); inp.select();
  // Activating the app can pull first responder around under us, so re-assert the caret
  // once it has settled — otherwise the box opens focused and silently loses it.
  const tookKey = takeKeyFocus(), openedAt = Date.now();
  if (tookKey) setTimeout(()=>{ if (editing) { inp.focus(); inp.select(); } }, 60);
  const commit = (save)=>{
    if (!editing) return; editing = false;
    if (tookKey) releaseKeyFocus();
    const v = inp.value.trim();
    if (save && v && v !== was){
      holdSelectionThroughRename();
      if (parts){
        // Through the rename plan, so a leaf that would collide with a sibling is
        // refused with the same dialog a move gets — and so is one that isn't a
        // measurement this convention knows.
        const to = renameParamLeaf(tok, v);
        if (to) swapSelTok(tok, "param:" + to);
        cursorTok = "param:" + (to || name);          // "" came back → it was refused
      } else {
        carryFilingOnRename(name, v);       // before anything re-renders
        swapSelTok(tok, "param:" + v);
        pr.name = v; cursorTok = "param:" + v;
        setParam({ name: name, newName: v });
      }
    }
    refreshFolders();
  };
  inp.addEventListener("keydown", e=>{
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); commit(true); }
    else if (e.key === "Escape"){ e.preventDefault(); commit(false); }
  });
  inp.addEventListener("blur", ()=>{
    // The blur that comes FROM taking key focus is not the user leaving the field —
    // committing on it would make the box flash open and shut. Re-assert instead.
    if (tookKey && Date.now() - openedAt < 250){
      if (editing) setTimeout(()=>{ if (editing) { inp.focus(); inp.select(); } }, 0);
      return;
    }
    commit(true);
  });
  inp.addEventListener("blur", ()=>setTimeout(()=>{
    const t=document.getElementById("tree"); if(t&&!editing)t.focus(); }, 0), {once:true});
}

// The row ⌘R acts on: the keyboard cursor, else whatever is selected. The selection
// fallback matters because clicking a component or a group no longer moves the cursor
// (they act rather than select), so Option-clicking one and pressing ⌘R had nothing to
// work with and silently did nothing.
function renameTarget(){
  if (cursorTok) return cursorTok;
  if (!selected.size){
    // Nothing selected at all: fall back to the sketch being edited, so ⌘R renames
    // it without having to click it in the browser first. A real selection always
    // wins — this only fires when cursorTok and selected are both empty.
    if (state.editingSketch && state.editSketchToken) return state.editSketchToken;
    return null;
  }
  const rows = flatten().filter(r => !r.placeholder && selected.has(r.node.token));
  return rows.length ? rows[0].node.token : [...selected][0];
}
// Real keyboard focus for an input this window has just opened.
//
// A rename started from the VIEWPORT opens its box in a window that is not the key
// window: the caret appears and the text is selected, but every keystroke still goes to
// Fusion, so you had to click the box before you could type. DOM focus cannot fix that —
// only the application can take key focus — so the page asks the Swift shell for it.
// Only for an action that arrived FROM the viewport — an edit started inside the dock
// already has the keyboard and must not be disturbed. That is read from the forwarding
// path rather than from document.hasFocus(), which a WKWebView can report as true while
// its application is inactive; gating on it meant focus was never actually requested.
// The answer says whether focus was taken, and the caller hands it back on the way out,
// so finishing a rename leaves the keyboard where it started.
function takeKeyFocus(){
  if (!forwardedDepth) return false;
  try { window.webkit.messageHandlers.focus.postMessage(true); return true; } catch(_){ return false; }
}
function releaseKeyFocus(){
  try { window.webkit.messageHandlers.focus.postMessage(false); } catch(_){}
}

// ── a rename keeps its row picked ─────────────────────────────────────────────
// Renaming is not a new pick. Two things used to drop the row anyway: a row whose token
// carries its name (parameters, name groups — and a timeline item if Fusion hands its
// feature a new entityToken) comes back as a NEW row, and the viewport echo that
// follows a rename (a feature's picked sketch/edges re-resolved) looked like a fresh
// viewport pick and replaced the selection. swapSelTok follows the token;
// renameHold keeps this window's selection through the echoes for RENAME_HOLD_MS.
const RENAME_HOLD_MS = 3000;
let renameHold = null;          // {toks: Set, cursor, until, feat?: {geoTok, oldTok, name}}
function swapSelTok(from, to){
  if (!from || !to || from === to) return;
  if (selected.delete(from)) selected.add(to);
  if (cursorTok === from) cursorTok = to;
  if (selAnchor === from) selAnchor = to;
  if (renameHold){
    if (renameHold.toks.delete(from)) renameHold.toks.add(to);
    if (renameHold.cursor === from) renameHold.cursor = to;
  }
}
function holdSelectionThroughRename(feat){
  renameHold = { toks: new Set(selected), cursor: cursorTok,
                 until: Date.now() + RENAME_HOLD_MS, feat: feat || null };
}
// A pick made here since (a click on another row, a step) ends the hold: its viewport
// echo is that pick's, and restoring the renamed row would yank the selection back.
function renameHeld(){
  if (!renameHold || Date.now() >= renameHold.until) return false;
  if (cursorTok !== renameHold.cursor || selected.size !== renameHold.toks.size
      || [...selected].some(t => !renameHold.toks.has(t))){ renameHold = null; return false; }
  return true;
}
// Called with each fresh /feats, before it draws: the renamed feature under the same
// body, found by its new name, takes the old row's place if its token moved.
function carryRenamedFeature(){
  const f = renameHold && renameHold.feat;
  if (!f) return;
  const list = featsByGeo[f.geoTok];
  if (!list) return;
  if (list.some(x => x.tok === f.oldTok)){ renameHold.feat = null; return; }   // token held
  const hit = list.find(x => x.name === f.name);
  if (!hit) return;                                  // not landed yet — next poll
  renameHold.feat = null;
  swapSelTok(FEAT_PREFIX + f.geoTok + "|" + f.oldTok, FEAT_PREFIX + f.geoTok + "|" + hit.tok);
}

function beginRename(tok){
  if (editing) return;
  // ⌘R with the pointer on a design swatch renames the swatch (§ appearances palette).
  if (!tok && palHover && palHover.src === "design" && showAppearances){
    beginSwatchRename(palHover); return;
  }
  tok = tok || renameTarget();
  if (!tok || tok === "__root__") return;
  if (isParamTok(tok)){ beginParamRename(tok); return; }
  let el = document.querySelector(`.row[data-tok="${cssEsc(tok)}"] .name`);
  if (!el){
    // The row can be off-screen behind a collapsed ancestor — e.g. the edited-sketch
    // fallback in renameTarget() reaches sketches that were never clicked open. Open
    // its spine and try once more before giving up.
    let opened = false;
    for (const anc of pathToTok(tok).slice(0, -1))
      if (collapsed.has(anc)){ collapsed.delete(anc); cancelCollapseTimer(anc); opened = true; }
    if (opened){ saveCollapsed(); render(); }
    el = document.querySelector(`.row[data-tok="${cssEsc(tok)}"] .name`);
    if (el) scrollTokIntoView(tok);
  }
  if (!el) return;
  const n = nodeByTok(tok); if(!n) return;
  if (n.fixed) return;                     // Origin rows — Fusion owns these names
  editing = true;
  const inp = document.createElement("input");
  inp.className = "name-input"; inp.value = n.name;
  el.replaceWith(inp); inp.focus(); inp.select();
  // Activating the app can pull first responder around under us, so re-assert the caret
  // once it has settled — otherwise the box opens focused and silently loses it.
  const tookKey = takeKeyFocus(), openedAt = Date.now();
  if (tookKey) setTimeout(()=>{ if (editing) { inp.focus(); inp.select(); } }, 60);
  const commit = (save)=>{
    if (!editing) return; editing = false;
    if (tookKey) releaseKeyFocus();
    const v = inp.value.trim();
    if (save && (v || n.type === "fgroup") && v !== n.name){
      holdSelectionThroughRename(n.type === "feature"
        ? { geoTok: n.geoTok, oldTok: n.featTok, name: v } : null);
      if (n.type === "fgroup") renameFeatureGroup(n, v);   // "" → back to its auto-name
      else if (n.type === "feature"){
        // A timeline item: renamed by the Timeline add-in, and its group filing follows
        // the new key (compRef|name), as a rename in the timeline does.
        featPost({action:"rename", tok: n.featTok, name: v});
        if (n.key && carryGroupKey(tlGroups, n.key, n.compRef + "|" + v)) saveTlGroups();
        pollFeats();
      } else if (isFolderTok(tok)){                 // window-only folder → never reaches Fusion
        const f = folderOf(tok);
        if (f){ f.name = v; n.name = v; saveFolders(); }
      } else if (isNameTok(tok)){
        // A name group has no name of its own to change — the word is in every
        // parameter beneath it, and renaming the group is renaming all of them.
        renameNameGroup(tok, v);
      } else {
        pendingRename[tok] = { name: v, was: n.name, ts: Date.now() };
        n.name = v;
        post({action:"rename", token:tok, newName:v});
      }
    }
    render();
  };
  inp.addEventListener("keydown", e=>{
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); commit(true); }
    else if (e.key === "Escape"){ e.preventDefault(); commit(false); }
  });
  inp.addEventListener("blur", ()=>{
    // The blur that comes FROM taking key focus is not the user leaving the field —
    // committing on it would make the box flash open and shut. Re-assert instead.
    if (tookKey && Date.now() - openedAt < 250){
      if (editing) setTimeout(()=>{ if (editing) { inp.focus(); inp.select(); } }, 0);
      return;
    }
    commit(true);
  });
  // Re-focus the tree so keyboard nav resumes after editing
  inp.addEventListener("blur", ()=>setTimeout(()=>{ const t=document.getElementById("tree"); if(t&&!editing)t.focus(); }, 0), {once:true});
}

// ── context menu ─────────────────────────────────────────────────────────────
// Wall-clock instant the menu became "armed" for click-to-dismiss. Compared against
// the add-in's viewport-click stamp (both epoch ms).
//
// This is deliberately NOT the last-seen click stamp: the click that OPENS the menu
// can itself be stamped by the event tap, and baselining on the previous stamp made it
// look like a brand-new viewport click on the next poll, dismissing the menu ~350 ms
// after it appeared. Arming on wall-clock at open ignores the gesture's own click while
// still catching any later one.
let lastClickTs  = 0;
let ctxOpenedAt  = 0;
const CTX_ARM_GRACE_MS = 150;   // absorb a stamp landing just after we re-arm

// A click on EMPTY canvas deselects everything in Fusion, and the selection mirror
// carries that here — but only when the viewport's selection CHANGES. Parameters and
// groups (a parameter group always; any group whose contents the viewport doesn't
// hold) push an empty selection, so the empty click that follows changes nothing
// there and the mirror never fires: the row stayed picked. So a fresh click stamp
// that landed on the canvas, with the viewport still holding nothing, clears this
// window's selection too — the same thing that click did to the geometry. A click
// that picks something needs none of this: the mirror replaces the selection with it.
//
// The event tap stamps clicks in THIS window as well (see the note above), and Qt
// reports the canvas under them, so a stamp that coincides with one of our own
// presses is ours, not the viewport's.
let localPressAt = 0;
const OWN_CLICK_MS = 400;
document.addEventListener("pointerdown", () => { localPressAt = Date.now(); }, true);
function canvasClickDeselect(s){
  if (!s.clickCanvas || (s.selTokens || []).length) return;
  if (Math.abs(s.clickTs - localPressAt) < OWN_CLICK_MS) return;
  if (!selected.size && !selHold && !pendingSel) return;
  selHold = null; pendingSel = null;
  selected.clear(); cursorTok = null; selAnchor = null;
  render();
}

// Right-click on a row opens this. Items are built per node; more will be added
// over time. Escape, a click outside, or a click in the Fusion viewport dismisses it.
//
// Opened on a row that is part of a multi-selection, the menu is FOR THE SELECTION:
// Delete takes every selected row (deleteTokens sorts out the mix, window-only kinds
// first, exactly as ⌘D does), and the per-type items — show/hide, disregard, the
// component policies — take every selected row of the held row's type. Items that only
// make sense for one row (rename, new folder inside, the plane line, sheet metal) stay
// on the held row. placeCtx stashes both lists on the menu, so the click handler never
// has to re-derive them from a selection the click itself may have changed.
function showCtx(x, y, node){
  if (!node) return false;
  const items = [];
  const multi = selected.has(node.token) && selected.size > 1;
  const toks  = multi ? rowsInTreeOrder(selected) : [node.token];
  const same  = toks.filter(t => (nodeByTok(t) || {}).type === node.type);
  const n = same.length;
  // "Delete Folder" / "Delete 3 Folders" / "Delete 5 Selected" (a mix).
  const delLabel = (one, many) =>
    toks.length === 1 ? `Delete ${one}` :
    n === toks.length  ? `Delete ${n} ${many}` : `Delete ${toks.length} Selected`;
  if (node.type === "folder"){
    // A folder is this window's own: everything here is local, nothing touches Fusion.
    if (!multi){
      items.push(`<div class="ctx-item" data-act="renamefolder">Rename</div>`);
      items.push(`<div class="ctx-item" data-act="newsubfolder">` +
                 (node.params ? "New Parameter Group Inside" : "New Folder Inside") + `</div>`);
    }
    // A folder with something in it can't be ignored (there's nothing to hide), so
    // the tick reads the held row's
    // flag but the item itself is disabled whenever the held row already has content.
    // The flip (below) still only touches the selected folders that are empty, so a
    // mixed multi-select doesn't stomp one that has content.
    items.push(`<div class="ctx-item check${folderIgnored(node) ? " on" : ""}` +
               `${folderHasVisibleContent(node) ? " disabled" : ""}" data-act="ignore">` +
               `<span class="ctx-check">✓</span><span>Ignore</span></div>`);
    // Removing a folder never deletes what is in it — its rows lift into whatever held
    // it (deleteFolders) — so the item says Explode, like a name group's. A mixed
    // selection still reads Delete: the other rows in it really are deleted.
    // A group a Combine or Extrude follows says so, as a sketch's Delete lists its dependants;
    // the item then asks before it explodes (confirmGroupDelete).
    const users = groupCombines(same);
    const uses = users.length
      ? `… — used by ${users.length} feature${users.length === 1 ? "" : "s"}`
        + `<div class="ctx-sub">${users.slice(0, 4).map(u => depLine(escapeHtml(u), {})).join("")}`
        + (users.length > 4 ? `<div>and ${users.length - 4} more</div>` : "") + `</div>`
      : "";
    items.push(`<div class="ctx-item" data-act="delrow">` +
               (n === toks.length ? (n === 1 ? "Explode Group" : `Explode ${n} Groups`)
                                  : delLabel("", "")) + uses + `</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "fgroup"){
    // A timeline group (the vertical timeline's own): window-only, it only dissolves.
    if (!multi) items.push(`<div class="ctx-item" data-act="renamefgroup">Rename</div>`);
    items.push(`<div class="ctx-item" data-act="fgroupwrap">Group</div>`);
    items.push(`<div class="ctx-item" data-act="delrow">` +
               (n === toks.length ? (n === 1 ? "Explode Group" : `Explode ${n} Groups`)
                                  : delLabel("", "")) + `</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "namegroup"){
    // A name group has no existence apart from the names under it, so its menu is
    // short and every item on it is a rewrite of those names. There is no Delete: a
    // group with nothing in it does not exist to be deleted, and one with parameters
    // in it can only be taken apart — which is what Explode does.
    if (!multi){
      items.push(`<div class="ctx-item" data-act="renamenamegroup">Rename</div>`);
      items.push(`<div class="ctx-item" data-act="explodenamegroup">Explode Group</div>`);
    }
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "feature"){
    const chk = (act, label, on) =>
      `<div class="ctx-item check${on ? " on" : ""}" data-act="${act}">` +
      `<span class="ctx-check">✓</span><span>${label}</span></div>`;
    if (!multi) items.push(`<div class="ctx-item" data-act="featedit">Edit Feature</div>`);
    items.push(`<div class="ctx-item" data-act="fgroupwrap">Group</div>`);
    // Moves the timeline marker to just after the held feature (Timeline's `rollto`).
    if (!multi) items.push(`<div class="ctx-item" data-act="featroll">Roll Timeline Here</div>`);
    items.push(`<div class="ctx-item" data-act="featsuppress">` +
               (node.suppressed ? "Unsuppress" : "Suppress") + `</div>`);
    // Which side of the phase lines it lives on (AddIns/Phases.py). Automatic is what
    // the add-in works out; the other three pin it by hand, and it moves at once.
    items.push(chk("phase-auto", "Phase: Automatic", !node.pinned));
    for (const [p, label] of [[4, "Independent"], [5, "Dependent"], [6, "Post-dependent"]])
      items.push(chk("phase-" + p, "Phase: " + label, node.pinned && node.phase === p));
    items.push(`<div class="ctx-item" data-act="featdelete">${delLabel("Feature", "Features")}</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "sketch" || node.type === "body"){
    // Every sketch can have its plane set in the plane box. One the box placed opens on
    // the line that placed it (shown on the item); any other opens on a line read back
    // from where it already sits, restricted to its own axis — a plane slides, never turns.
    if (!multi && node.type === "sketch"){
      items.push(`<div class="ctx-item" data-act="redefineplane">Set Plane…` +
                 (node.datum ? ` — ` + escapeHtml(node.datum) : ``) + `</div>`);
    }
    if (node.type === "body"){
      // Fusion's own Texture Map Controls, opened on these bodies.
      items.push(`<div class="ctx-item" data-act="texturemap">Texture Map…</div>`);
      // Display Detail: the level picked here is set through Fusion's own Display
      // Detail Control, driven by the add-in without showing it (_detail_step).
      items.push(ctxFly("Display Detail",
        [["auto", "Auto"], ["low", "Low"], ["medium", "Medium"], ["high", "High"]].map(([lv, label]) =>
          `<div class="ctx-item" data-act="detail-${lv}">${label}</div>`)));
    }
    const del = node.type === "body" ? delLabel("Body", "Bodies") : delLabel("Sketch", "Sketches");
    items.push(`<div class="ctx-item" data-act="delrow" data-label="${escapeHtml(del)}">${del}</div>`);
    placeCtx(x, y, node, items, toks, same);
    // Like a parameter's Delete, a sketch's says what would be left without it — but
    // only Fusion knows what stands on a sketch, so the add-in is asked now and the
    // item fills in when the answer comes back on /state (noteSketchDeps). Sketches in
    // the selection don't count against each other: they go together.
    const sks = toks.filter(t => (nodeByTok(t) || {}).type === "sketch");
    if (sks.length){
      sketchDepsSeq++;
      post({action: "sketchdeps", tokens: sks, seq: sketchDepsSeq});
    }
    return true;
  }
  if (node.type === "param"){
    // Fusion refuses to delete a parameter anything still references, so a delete that
    // would leave dependants behind says so and ends in "…": it asks what to put in
    // their place first (openSubstitute). A dependant that is itself in the selection
    // doesn't count — the whole set goes together, dependants first — so what asks is
    // only what the delete would leave behind (paramDeletePlan).
    const plan = paramDeletePlan(same.map(paramNameOf));
    if (plan.blocked.length){
      const ext = new Set(plan.blocked.flatMap(b => b.ext));
      const why = `${ext.size} dependant${ext.size === 1 ? "" : "s"}${multi ? " outside selection" : ""}`;
      items.push(`<div class="ctx-item" data-act="delrow">${delLabel("Parameter", "Parameters")}… — ${why}</div>`);
    } else {
      items.push(`<div class="ctx-item" data-act="delrow">${delLabel("Parameter", "Parameters")}</div>`);
    }
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "origin" || node.type === "construction"){
    items.push(`<div class="ctx-item" data-act="togglevis">` +
               (node.own ? "Hide All" : "Show All") + `</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "geometry"){
    // The group's bulb is Fusion's Bodies-folder bulb: in the active component that
    // is the body toggle itself (the add-in flips the toggle — see _set_bodies_folder).
    items.push(`<div class="ctx-item" data-act="togglevis">` +
               (node.own ? "Hide Bodies" : "Show Bodies") + `</div>`);
    if (!multi)
      items.push(`<div class="ctx-item" data-act="newsubfolder">New Folder Inside</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type === "originent" || node.type === "conplane"){
    items.push(`<div class="ctx-item" data-act="togglevis">` +
               (node.own ? "Hide" : "Show") + `</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  if (node.type !== "component"){
    // Anything else selected alongside real rows (the root, say) still gets Delete for
    // the rest of the selection. With nothing selected there is nothing row-specific to
    // offer, and the menu is just the view toggles — which is a menu worth having, so
    // this no longer refuses to open one.
    if (multi)
      items.push(`<div class="ctx-item" data-act="delrow">${delLabel("", "Selected")}</div>`);
    return placeCtx(x, y, node, items, toks, same);
  }
  items.push(hideEdgesFly(node));
  // The label is the action: "on" (the "on" class is the state the click flips) reads
  // "Show …" because the next click undoes the policy.
  items.push(
    `<div class="ctx-item${node.hideBody ? " on" : ""}" data-act="hidebody">` +
    `${node.hideBody ? "Show" : "Hide"} Entirely when component is not active</div>`);
  items.push(`<div class="ctx-item" data-act="disregard">Disregard (keep contents)</div>`);
  items.push(`<div class="ctx-item" data-act="disregardall">Disregard with contents</div>`);
  if (!multi)
    items.push(`<div class="ctx-item" data-act="editbounds">Edit Bounds…</div>`);
  if (!multi && node.sheetmetal)
    items.push(`<div class="ctx-item" data-act="sheetmetal">Edit Sheet Metal Rule</div>`);
  if (!multi)
    items.push(`<div class="ctx-item" data-act="newparamgroup">New Parameter Group</div>`);
  if (multi)
    items.push(`<div class="ctx-item" data-act="delrow">${delLabel("Component", "Components")}</div>`);
  return placeCtx(x, y, node, items, toks, same);
}
// A component's Hide Edges ▸ flyout (AddIns/Edge Policy.py). "When Not Active" is the
// old flag; under it every other component in the model (`state.comps`, the add-in's
// list — disregarded ones included), ticked = this component's edges go while THAT one
// is drawn in the viewport. Any one ticked and showing is enough. Ids, not names, so a
// rename on either side changes nothing. Ticked ones head the list as it opens; a
// click flips its tick in place and leaves the menu open (several are usually wanted).
function hideEdgesFly(node){
  const chk = (attrs, label, on) =>
    `<div class="ctx-item check${on ? " on" : ""}" ${attrs}>` +
    `<span class="ctx-check">✓</span><span>${escapeHtml(label)}</span></div>`;
  const when = new Set(node.hideEdgesWhen || []);
  const all = state.comps || [];
  const lead = compPathLeads(all);           // over the whole list, the held one included
  const comps = all.filter(([id]) => id !== node.compId);
  comps.sort((a, b) => (when.has(b[0]) - when.has(a[0])));   // stable: names stay sorted
  const n = when.size + (node.hideEdges ? 1 : 0);
  const label = (id, name) =>
    (lead.get(id) || []).map(p => `<span class="ctx-path">${escapeHtml(spaceParens(p))}</span>` +
                                  `<span class="ctx-path-sep">›</span>`).join("") +
    escapeHtml(spaceParens(name));
  return ctxFly(`Hide Edges${n ? ` · ${n}` : ""}`, [
    chk(`data-act="edgepolicy"`, "When Not Active", node.hideEdges),
    `<div class="ctx-sep"></div>`,
    `<div class="ctx-head">When visible:</div>`,
    ...comps.map(([id, name]) =>
      `<div class="ctx-item check${when.has(id) ? " on" : ""}" data-act="edgewhen" ` +
      `data-trig="${escapeHtml(id)}"><span class="ctx-check">✓</span>` +
      `<span class="ctx-name">${label(id, name)}</span></div>`),
  ]);
}
// For `state.comps` ([id, name, ancestors outermost-first]): id -> the ancestors to
// draw before the name. A name nobody else has gets none; a shared one gets the fewest
// nearest ancestors that tell it from every other of that name ("Roof › Steel" vs
// "Boot Ceiling › Steel"), the whole path if nothing shorter does.
function compPathLeads(comps){
  const byName = new Map();
  for (const c of comps){
    const k = (c[1] || "").toLowerCase();
    if (!byName.has(k)) byName.set(k, []);
    byName.get(k).push(c);
  }
  const out = new Map();
  for (const group of byName.values()){
    if (group.length < 2) continue;
    const tail = (c, d) => (c[2] || []).slice(Math.max(0, (c[2] || []).length - d));
    const key = (c, d) => tail(c, d).join("\u0000").toLowerCase();
    for (const c of group){
      const len = (c[2] || []).length;
      // One sitting at the top has no parent to show: say so, rather than draw it
      // bare beside "Outer › Leg Wing" as if it were the only one.
      if (!len){ out.set(c[0], [(state.rootName || "Top Level").trim()]); continue; }
      let d = 1;
      while (d < len && group.some(o => o !== c && key(o, d) === key(c, d))) d++;
      out.set(c[0], tail(c, d));
    }
  }
  return out;
}
// The tail every menu carries: what the TREE is showing, as opposed to what the row
// under the pointer is. They sit below a separator, in the same order every time, so
// the row-specific items above them never shuffle the view items around under the
// pointer.
function viewCtxItems(){
  const chk = (act, label, on) =>
    `<div class="ctx-item check${on ? " on" : ""}" data-act="${act}">` +
    `<span class="ctx-check">✓</span><span>${label}</span></div>`;
  return [
    chk("view-ignored", "Ignored Folders", showIgnoredFolders),
    chk("view-bounds",  "Bounds Folders",  showBoundsFolders),
    chk("view-masters", "Master Sketches", showMasterFolders),
    chk("view-sketches", "Sketches",       !hideSketches),
    chk("view-bodies",  "Bodies",          showBodiesGroup),
    chk("view-geometry", "Operations",     showGeometryGroup),
    chk("view-origins", "Origins",         showOrigins),
    chk("view-construction", "Construction Planes", showConstruction),
    chk("view-appearances", "Appearances", showAppearances),
    // The one that isn't a view state but a sweep — no tick, so it reads as a verb.
    `<div class="ctx-item" data-act="view-filebounds">File Sketches</div>`,
    // Every timeline item to the end of its phase (AddIns/Phases.py), in one go.
    `<div class="ctx-item" data-act="view-sortphases">Sort Timeline into Phases</div>`,
    // Not the tree at all: the spinner bin/fusion_busy draws by the pointer.
    chk("view-busy", "Busy Spinner", busySpinner),
    // The detail behind the last HUD cross, for when it faded before it was clicked.
    `<div class="ctx-item" data-act="view-lasterror">Show Last Error</div>`,
    // The document's versions and the open documents (AddIns/Versions' panel, in
    // the value window) — instead of Fusion's Data Panel.
    `<div class="ctx-item" data-act="view-versions">Versions…</div>`,
    // Last: swap the dock to the vertical timeline (its menu ends with the way back).
    `<div class="ctx-item" data-act="view-timeline">Show Timeline</div>`,
  ];
}
// ⌘X / ⌘V on the menu. Cut takes the menu's rows (the held row, or the selection it is
// part of) by cuttableToks' rule, without touching the selection; Paste appears while a
// cut is pending and lands where ctxPasteTarget says, disabled if nothing cut can go there.
function ctxCutToks(toks){
  const picked = toks.filter(cutExists);
  return picked.filter(t => !picked.some(g => g !== t && isDescendantTok(g, t)));
}
function clipCtxItems(node, toks){
  const out = [];
  const cut = ctxCutToks(toks);
  if (cut.length)
    out.push(`<div class="ctx-item" data-act="cut">Cut${cut.length > 1 ? ` ${cut.length}` : ""}</div>`);
  if (cutToks.length){
    const ok = pastePlan(ctxPasteTarget(node)).length > 0;
    out.push(`<div class="ctx-item${ok ? "" : " disabled"}" data-act="paste">Paste</div>`);
  }
  return out;
}
// What each of those items does. Same functions the bar's buttons and the Control Room
// hotkeys call, so the three surfaces can't drift.
const VIEW_ACTS = {
  "view-ignored":    () => toggleIgnoredFolders(),
  "view-bounds":     () => toggleBoundsFolders(),
  "view-masters":    () => toggleMasterFolders(),
  "view-sketches":   () => toggleSketches(),
  "view-bodies":     () => toggleBodiesGroup(),
  "view-geometry":   () => toggleGeometryGroup(),
  "view-origins":    () => toggleOrigins(),
  "view-construction": () => toggleConstruction(),
  "view-appearances": () => toggleAppearances(),
  "view-filebounds": () => fileAllBounds(),
  "view-timeline":   () => toggleDockMode(),
  "view-sortphases": () => sortPhases(),
  "view-busy":       () => toggleBusySpinner(),
  "view-lasterror":  () => fetch(API + "/lasterror", {method: "POST"}).catch(() => {}),
  "view-versions":   () => fetch(VALUE_API + "/cmd", {method: "POST",
                          headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({action: "versionsshow"})}).catch(() => {}),
};
// Fill the menu, remember which row(s) it belongs to, and keep it inside the window.
// `toks` is what Delete takes; `same` is what the per-type items take (see showCtx).
function placeCtx(x, y, node, items, toks, same){
  items = clipCtxItems(node, toks || [node.token]).concat(items);
  const view = viewCtxItems();
  const all  = items.length ? items.concat(`<div class="ctx-sep"></div>`, view) : view;
  const ctx = document.getElementById("ctx");
  ctx.innerHTML = all.join("");
  ctx.dataset.tok = node.token;
  ctx.dataset.compId = node.compId || "";
  ctx.dataset.toks = JSON.stringify(toks || [node.token]);
  ctx.dataset.same = JSON.stringify(same || [node.token]);
  ctxOpenedAt = Date.now();          // only clicks after THIS instant dismiss it
  // Show first so we can measure, then keep it inside the window.
  ctx.style.left = "0px"; ctx.style.top = "0px"; ctx.style.display = "block";
  const r = ctx.getBoundingClientRect();
  const lx = Math.max(2, Math.min(x, window.innerWidth  - r.width  - 2));
  const ly = Math.max(2, Math.min(y, window.innerHeight - r.height - 2));
  ctx.style.left = lx + "px"; ctx.style.top = ly + "px";
  publishLiveBottom();
  return true;
}
// One bulleted dependant (Delete's menu item and its substitute dialog), coloured by
// kind — 'sketch' / 'param' take the Browser's row colours; a feature stays plain. A
// parameter carries its name and a sketch its row key, which is what Go To follows.
function depLine(html, dep){
  dep = dep || {};
  const k = dep.kind === "sketch" || dep.kind === "param" ? ` k-${dep.kind}` : "";
  const go = dep.kind === "param" && dep.param ? ` data-param="${escapeHtml(dep.param)}"`
           : dep.key ? ` data-key="${escapeHtml(dep.key)}"` : "";
  return `<div class="dep-line${k}"${go}>• ${html}</div>`;
}
// The answer to the menu's `sketchdeps` question (showCtx). One not for the latest
// question, or arriving after the menu shut, is dropped.
let sketchDepsSeq = 0;
function noteSketchDeps(d){
  if (!d || d.seq !== sketchDepsSeq || !ctxOpen()) return;
  const it = document.querySelector('#ctx .ctx-item[data-act="delrow"][data-label]');
  if (!it || it.dataset.deps === String(d.seq)) return;
  it.dataset.deps = String(d.seq);
  const deps = new Map();
  (d.names || []).forEach((n, i) => {
    if (!deps.has(n)) deps.set(n, { kind: (d.kinds || [])[i] || "", key: (d.keys || [])[i] || "" });
  });
  const names = [...deps.keys()];
  if (!names.length) return;
  const multi = JSON.parse(document.getElementById("ctx").dataset.toks || "[]").length > 1;
  const shown = names.slice(0, 4).map(n => depLine(escapeHtml(n), deps.get(n))).join("")
              + (names.length > 4 ? `<div>and ${names.length - 4} more</div>` : "");
  it.innerHTML = `${it.dataset.label} — ${names.length} dependant${names.length === 1 ? "" : "s"}`
               + (multi ? " outside selection" : "")
               + `<div class="ctx-sub">${shown}</div>`;
  // The item grew: keep the menu inside the window.
  const ctx = document.getElementById("ctx"), r = ctx.getBoundingClientRect();
  ctx.style.left = Math.max(2, Math.min(r.left, window.innerWidth  - r.width  - 2)) + "px";
  ctx.style.top  = Math.max(2, Math.min(r.top,  window.innerHeight - r.height - 2)) + "px";
}
// A menu item that opens a flyout of more items on hover. The flyout is a child of
// its item, so hovering it keeps the item hovered, and a click on one of its items is
// found by the menu's own handler like any other (closest(".ctx-item")).
function ctxFly(label, items){
  return `<div class="ctx-item fly" data-act="fly"><span>${label}</span><span class="ctx-arrow">›</span>` +
         `<div class="ctx-fly">${items.join("")}</div></div>`;
}
// Open beside its item: right if it fits, else left, else as far right as it can go —
// and publish, since the window's live band has to start below it before the pointer
// travels there (computeLiveBottom).
function placeFly(item){
  const fly = item.querySelector(".ctx-fly"); if (!fly) return;
  fly.style.left = "0px"; fly.style.top = "0px"; fly.classList.add("open");
  const ir = item.getBoundingClientRect(), fr = fly.getBoundingClientRect();
  let x = ir.right - 2;
  if (x + fr.width > window.innerWidth - 2) x = ir.left - fr.width + 2;
  if (x < 2) x = Math.max(2, window.innerWidth - fr.width - 2);
  const y = Math.max(2, Math.min(ir.top - 5, window.innerHeight - fr.height - 2));
  fly.style.left = x + "px"; fly.style.top = y + "px";
  publishLiveBottom();
}
function hideCtx(){ document.getElementById("ctx").style.display = "none"; publishLiveBottom(); }
function ctxOpen(){ return document.getElementById("ctx").style.display === "block"; }

// ── event wiring: press → click or drag ───────────────────────────────────────
// A press becomes a CLICK on mouseup if the pointer didn't move past a threshold,
// otherwise a DRAG. Like Parameters, a drag can either REORDER (drop between rows →
// blue insertion line) or REPARENT (drop into a component → subtle blue fill). The
// cursor stays the normal arrow and a ghost of the item follows it. Reorder within a
// parent is a window-only overlay (no Fusion API); reparent / cross-parent moves call
// moveToComponent. Dragging never changes the selection.
// "Select this, don't open it" — clicking an expandable row (component or folder)
// normally acts on it (activate / fold / unfold) and leaves the selection alone; this
// inverts that. It is OPTION-click, because Karabiner rewrites a held Space into
// left_option while Fusion is frontmost ("Fusion, Space Becomes Shift Option, Equals,
// and Itself" — a real space only arrives via to_if_alone on a quick tap). Holding
// Space over this window therefore ARRIVES as Option, and the page never sees a space
// at all. With this window (or Control Room) frontmost instead, the hold is only F20,
// and remapd puts the Option on the click itself (the tap callback's mouse block).
// Keys.spaceHeld() (fusion_ui/keys.js) still covers the case where the dock
// genuinely has focus and Space comes through as Space; either one selects.
document.addEventListener("keydown", e=>{
  if (editing || (e.target && e.target.tagName === "INPUT")) return;
  if (e.code === "Space" || e.key === " ") e.preventDefault();
}, true);

const DRAG_THRESH = 5;
let press = null;            // {tok, type, x, y, meta} pending press (pre-threshold)
let drop  = null;            // current drop plan (see computeDrop)

function rowEl(tok){ return document.querySelector(`.row[data-tok="${cssEsc(tok)}"]`); }

// A row a click can't open or shut — it holds nothing. Clicking one that is already
// the whole selection deselects it (plain or Space-click alike).
const LEAF_TYPES = new Set(["body", "sketch", "param", "feature", "originent", "conplane"]);
function isLeafRow(n){ return !!n && LEAF_TYPES.has(n.type); }
function soleSelected(tok){ return selected.size === 1 && selected.has(tok); }

// A click selects / activates by type:
//   regular on component → activate (drill in); on body/sketch → select it in the
//     browser + viewport (the add-in then jumps down to its parent component).
//   Cmd on anything → select in the BROWSER ONLY, accumulating (no viewport change).
//   empty space → clear the selection.
function handleClick(p){
  // Empty space just deselects. Going to the top level is the THREE-FINGER TAP only,
  // and that is recognised from the event itself (a synthetic double-click) — never
  // from our own timing. The old 450ms pairing here fired on any two ordinary clicks,
  // which is exactly the manual double-click that must not jump to the top.
  if (!p.tok){ deselectAll(); return; }
  const n = nodeByTok(p.tok);
  // Extrude open: a body, or a group of them, is added to / taken out of the operation
  // — plain or ⌘ alike, nothing selected, nothing activated. Option still selects.
  if (!(Keys.spaceHeld() || p.alt) && extrudeClick(n)) return;
  if (Keys.spaceHeld() || p.alt){
    // Option+click (which is what a held Space becomes) selects, whatever the row is —
    // nothing expands, collapses or activates. This is how components and groups get
    // selected. A bodies group also selects its bodies in the viewport (viewportItemsFor).
    // A row that holds nothing (a body, a sketch, a feature…) is selected the way a
    // plain click selects it, viewport included. Any row that is already the whole
    // selection — group or not — is deselected instead.
    const leaf = isLeafRow(n);
    if (soleSelected(p.tok)){ deselectAll(); return; }
    cursorTok = p.tok; selAnchor = p.tok;
    if (isBodiesGroup(n) || (leaf && (n.type === "body" || n.type === "sketch"))){
      setSelection([p.tok]); return;
    }
    selected = new Set([p.tok]);
    render();
    if (leaf && n.type === "feature") selectFeature(n);
    return;
  }
  if (p.shift && (selAnchor || cursorTok)){
    // Shift+left-click = the whole run between the anchor and this row, as in Finder.
    // The anchor is the last row picked without shift; the range replaces whatever
    // was selected, and this row becomes the cursor (but NOT the anchor, so a second
    // shift-click re-measures from the same place).
    const anchor = selAnchor || cursorTok;
    cursorTok = p.tok;
    setSelection(rowsBetween(anchor, p.tok));
    return;
  }
  if (p.meta){
    // Cmd+left-click = SELECT only (never activate / enter): toggle this row in or
    // out of the selection, whatever its type. The whole set is pushed to the
    // viewport together — pushing just the clicked row made Fusion report a
    // single-row selection, and the mirror then wiped the accumulation, which is why
    // bodies and sketches would not add up while groups (which Fusion never sees) did.
    if (selected.has(p.tok)) selected.delete(p.tok); else selected.add(p.tok);
    selAnchor = p.tok; cursorTok = p.tok;
    setSelection(rowsInTreeOrder(selected));
    return;
  }
  if (!n){ render(); return; }
  selAnchor = p.tok;                       // a plain click is where ⇧ measures from
  if (n.type === "param"){
    const again = soleSelected(p.tok);
    cursorTok = p.tok; selected = new Set([p.tok]); render();
    // While a value is being edited, the click also puts this parameter in at the value
    // box's caret. The value server says no (`taken: false`) when nothing is open —
    // and only then does a click on the parameter already selected deselect it.
    fetch(VALUE_API + "/insert", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ name: paramNameOf(p.tok) })})
      .then(r => r.json()).then(d => { if (again && !d.taken) deselectAll(); })
      .catch(() => { if (again) deselectAll(); });
    return;
  }
  // A row that holds nothing, clicked while it is the whole selection: deselect it.
  if (isLeafRow(n) && soleSelected(p.tok)){ deselectAll(); return; }
  if (n.type === "feature"){
    cursorTok = p.tok; selected = new Set([p.tok]); render(); selectFeature(n);
    return;
  }
  // A feature group picks itself and opens or shuts, like a body.
  // (Not toggleFolderOpen: its collapse clears a pick inside — here, the group itself.)
  if (n.type === "fgroup"){
    cursorTok = p.tok; selected = new Set([p.tok]);
    if (collapsed.has(p.tok)){ collapsed.delete(p.tok); cancelCollapseTimer(p.tok); }
    else { collapsed.add(p.tok); rememberCollapse(p.tok); }
    saveCollapsed(); render();
    return;
  }
  // A name group opens and shuts like a folder — it is a group, whatever made it.
  if (n.type === "folder" || n.type === "namegroup"){ toggleFolderOpen(n.token); return; }
  // The Origin group is a browser-only grouping like a virtual folder — click
  // expands/collapses it (bulk show/hide is the context menu's "Show All"/"Hide
  // All"). An entity underneath it has no useful Fusion SELECTION — it's
  // construction geometry, not something you'd extrude or dimension from a click —
  // so a click there just flips its own visibility instead.
  if (n.type === "origin" || n.type === "construction"){ toggleFolderOpen(n.token); return; }
  if (n.type === "originent" || n.type === "conplane"){ post({action:"togglevis", tokens:[n.token]}); return; }
  // The Bodies group likewise: open/close on click, show/hide from the menu or ⌘E.
  if (n.type === "geometry"){ toggleFolderOpen(n.token); return; }
  // The Geometry group opens or shuts on click, like Bodies — nothing selected (Option
  // still selects it). Held open by openGeos, since opening it is what fetches its rows.
  // It has no bulb. Opening it onto a viewport pick's feature lands there instead
  // (landPickIn); shutting it drops a pick inside, as a folder's collapse does.
  if (n.type === "features"){
    if (geoOpen(n)){ clearSelectionForCollapse(n.token); closeGeo(n.token); }
    else if (!landPickIn(n)) openGeo(n);
    render();
    return;
  }
  if (n.type === "component"){
    if (n.token === viewTok()){
      // Already where you are (the active component — or the peek, in suspend mode): a
      // click folds it away and back rather than re-activating it. You stay exactly
      // where you are — nothing is posted to Fusion.
      if (collapsed.has(n.token)){ collapsed.delete(n.token); cancelCollapseTimer(n.token); }
      else { collapsed.add(n.token); rememberCollapse(n.token); clearSelectionForCollapse(n.token); }
      saveCollapsed(); render();
      return;
    }
    // Suspend mode: look, don't go. The optimistic activation below used to run anyway —
    // it underlined the row, its post was dropped, and the hold snapped back 10 s later.
    if (suspended){ peekTo(n.token); return; }
    selected.clear();
    state.activeToken = n.token; prevActiveToken = n.token;   // optimistic bold + spine
    holdActivate(n.token);
    revealActiveSpine(); markActivePathVisible(); render();
    post({action:"activate", token:n.token});
  } else if (n.type === "body" && ownerOccOf(n.token) !== compRefOf(viewTok())){
    // A body in a component you are NOT in (the peek counts, in suspend mode): jump
    // straight into its own component — nothing selected, here or in the viewport.
    selHold = null; selected.clear(); cursorTok = null;
    if (n.occToken) activateComponentTok(n.occToken); else activateRootUI();
    post({action:"deselect"});
    requestAnimationFrame(() => scrollTokIntoView(n.token));
  } else {
    cursorTok = p.tok;
    selected = new Set([p.tok]);                              // body / sketch
    render();
    // A click only ever SELECTS a sketch (in the browser and the viewport) — opening
    // it for edit is Enter, on the row the cursor is already sitting on. A body is only
    // selected: its features are in the component's Geometry group, not under it.
    if (n.type === "body") selectBody(n); else selectSketch(n);
  }
}

// ── drag: ghost, drop computation, paint, apply ───────────────────────────────
function showGhost(name, x, y){
  const g = document.getElementById("drag-ghost");
  g.textContent = name || ""; g.style.display = "block"; moveGhost(x, y);
}
function moveGhost(x, y){
  const g = document.getElementById("drag-ghost");
  g.style.left = (x + 12) + "px"; g.style.top = (y + 6) + "px";
}
function hideGhost(){ document.getElementById("drag-ghost").style.display = "none"; }

// Build the drop plan for the row under the cursor:
//   {mode:'into', tok}                          → reparent the dragged item into tok
//   {mode:'before'|'after', tok, parent, cross} → place as a sibling of tok
// Component rows: top 30% = before; an expanded component = into (below the top);
// a collapsed component = into (middle) / after (bottom 30%). Leaf rows: before/after.
function computeDrop(e){
  const el = document.elementFromPoint(e.clientX, e.clientY);
  const row = el && el.closest(".row");
  if (drag.type === "feature") return featDrop(e);
  if (!row) return planOut();              // off the rows / out of the window
  if (row.classList.contains("placeholder")) return null;
  const tok = row.dataset.tok, type = row.dataset.type;
  if (drag.toks.indexOf(tok) >= 0) return null;      // never onto a row being dragged
  const rc = row.getBoundingClientRect();
  const rel = (e.clientY - rc.top) / rc.height;
  if (type === "component" || type === "folder" || type === "geometry"
      || type === "namegroup"){
    const node = nodeByTok(tok);
    const expanded = node && node.children && node.children.length && !collapsed.has(tok);
    if (rel < 0.30) return planSibling("before", tok);
    if (expanded)   return planInto(tok);
    if (rel > 0.70) return planSibling("after", tok);
    return planInto(tok);
  }
  return planSibling(rel > 0.5 ? "after" : "before", tok);   // body / sketch leaf
}
// The document root is spelled two ways and they must compare equal: a LEAF on the root
// carries occToken "" (it has no occurrence to name), while a FOLDER on the root carries
// comp "__root__" (the sentinel standing in for a row that does not exist). Comparing
// them raw made a top-level sketch impossible to drag into a top-level folder — a move
// that touches nothing in Fusion at all, refused by an accident of spelling.
function sameOwner(a, b){
  const norm = x => (!x || x === ROOT_TOKEN) ? "" : x;
  return norm(a) === norm(b);
}

// What dropping the dragged row into `containerTok` would mean:
//   'virtual' → window-only (in or out of a folder, within the one component)
//   'fusion'  → a real Fusion reparent
//   null      → not a legal drop
// Where a parameter may be filed by hand: a component or a parameter group (the
// Bounds one included) — never a sketch folder, the Geometry group or the Origin group.
function paramContainerOk(containerTok){
  if (isGeoTok(containerTok) || geoFolderOf(containerTok)) return false;
  if (isFolderTok(containerTok) && !isParamFolder(containerTok)) return false;
  const n = nodeByTok(containerTok);
  if (n && ["origin", "originent", "construction", "conplane"].includes(n.type)) return false;
  return true;
}
// Can a name-derived row land here? Only somewhere a LEVEL can point: a component,
// another name group, a component's Bounds parameter group (it stands for the Bounds
// level — boundsParamRef), or the top level — which is the root component, and so
// needs a root name to have arrived before it means anything.
function nameDropOk(containerTok){
  if (!containerTok || containerTok === ROOT_TOKEN) return !!identOfRef(ROOT_TOKEN);
  if (isNameTok(containerTok)) return true;
  const bref = boundsParamRef(containerTok);
  if (bref) return !!identOfRef(bref);
  // A parameter group: filed there by hand (folderForContainer), named for its component.
  if (isFolderTok(containerTok))
    return isParamFolder(containerTok) && !!identOfRef(compRefOf(containerTok));
  const n = nodeByTok(containerTok);
  return !!(n && n.type === "component");
}
function moveKind(dragTok, containerTok){
  // Into itself — a ⌘V with the cursor still on the group that was cut. A folder
  // taking itself as parent would drop out of the tree for good.
  if (dragTok === containerTok) return null;
  // A name group, and a parameter whose name parses, move by being RENAMED: where
  // they sit IS a reading of the name, so the drop rewrites the name and the row
  // follows it. Nothing is filed and nothing is stored — see the name-groups section.
  // This is checked before the hand-filing branch below so that a structured
  // parameter never takes the paramPlace path, which would give it two homes.
  if (isNameTok(dragTok) || (isParamTok(dragTok) && partsOf(paramNameOf(dragTok)))){
    if (parentTokOf(dragTok) === containerTok) return null;            // no-op
    if (isDescendantTok(dragTok, containerTok)) return null;           // into itself
    return nameDropOk(containerTok) ? "name" : null;
  }
  if (isParamTok(dragTok)){
    // A parameter belongs to the document rather than to any component, so filing it
    // is always window-only and every container is fair game — including back out to
    // the top level, which nothing else can be dropped on — bar the groups that stand
    // for something else (paramContainerOk).
    if (!containerTok) return null;
    if (!paramContainerOk(containerTok)) return null;
    if (parentTokOf(dragTok) === containerTok) return null;                  // no-op
    return (containerTok === "__root__" || isFolderTok(containerTok)
            || containerCompOf(containerTok)) ? "virtual" : null;
  }
  if (!containerTok) return null;
  if (containerTok === ROOT_TOKEN){
    // Fusion has no API to reparent INTO the doc root, but filing a folder or one of
    // the root's own sketches there is window-only — out of a folder, to the top.
    if (parentTokOf(dragTok) === ROOT_TOKEN) return null;          // already there
    if (isFolderTok(dragTok)) return folderOf(dragTok) ? "virtual" : null;
    const r = nodeByTok(dragTok);
    return r && r.type === "sketch" && sameOwner(ROOT_TOKEN, r.occToken || "") ? "virtual" : null;
  }
  if (isDescendantTok(dragTok, containerTok)) return null;        // not into its own subtree
  if (parentTokOf(dragTok) === containerTok) return null;         // already there → no-op
  const targetComp = containerCompOf(containerTok);
  // Which side of the component a container is on: inside the Geometry group (bodies
  // live there) or outside it (sketches do). Read through folderDefs, since a drop's
  // destination may not be in the tree yet.
  const geoSide = isGeoTok(containerTok) || !!geoFolderOf(containerTok);
  if (isFolderTok(dragTok)){
    // A folder can be re-filed into ANY component or folder — it used to be pinned to
    // the component it was created in, which silently refused perfectly reasonable
    // drops. Its `comp` (and its nested folders') follows the move. Sketches and
    // bodies filed into it do NOT follow: they belong to their own component and
    // Fusion owns that, so they show there while the folder lives elsewhere and drop
    // back into it if it ever moves back (the filing itself is kept, not discarded).
    // The two kinds keep to their own side: a parameter group only into a component or
    // another parameter group, a sketch folder never into a parameter group.
    if (!folderOf(dragTok) || isNameTok(containerTok)) return null;
    if (isParamFolder(dragTok)
          ? (geoSide || (isFolderTok(containerTok) && !isParamFolder(containerTok)))
          : isParamFolder(containerTok)) return null;
    return (isFolderTok(containerTok) || targetComp) ? "virtual" : null;
  }
  const n = nodeByTok(dragTok); if (!n) return null;
  if (isParamFolder(containerTok) || isNameTok(containerTok)) return null;
  if (n.type === "sketch" || n.type === "body"){
    if (sameOwner(targetComp, compRefOf(n.occToken || ""))){
      // A move within the component: a body only ever lands in its Geometry group or a
      // folder inside it, a sketch only outside. A body dropped on its own component
      // row is refused — "out of the group" means "loose in Geometry", which is the
      // Geometry row.
      if (n.type === "body")  return geoSide ? "virtual" : null;
      return geoSide ? null : "virtual";
    }
    if (isFolderTok(containerTok)) return null;  // a folder belonging to another component
    if (n.type !== "body") return null;          // sketches can't be reparented in Fusion
    // Another component's Geometry row stands for that component; the root has no
    // occurrence to move into.
    if (isGeoTok(containerTok)) return compTokOf(targetComp) === ROOT_TOKEN ? null : "fusion";
    return "fusion";
  }
  if (n.type === "component") return isFolderTok(containerTok) ? null : "fusion";
  return null;
}
// One entry per dragged row that can legally land in `container`, in the order the
// rows are drawn. Each keeps its OWN kind, so a mixed selection moves the parts that
// can move (a sketch into a group, say) and quietly leaves behind the parts that
// can't, instead of the whole drop being refused. allowOrder marks a row that is
// already in the container as a pure reorder — right for a before/after drop, wrong
// for an into drop (there, being already inside means there is nothing to do).
function dragMoves(container, allowOrder){
  const out = [];
  for (const t of (drag.toks || [drag.tok])){
    const kind = (allowOrder && parentTokOf(t) === container) ? "order"
                                                             : moveKind(t, container);
    if (kind) out.push({ tok:t, kind });
  }
  return out;
}
function planInto(tok){
  const moves = dragMoves(tok, false);
  return moves.length ? { mode:"into", tok, moves } : null;
}
function planSibling(side, targetTok){
  const targetParent = parentTokOf(targetTok);
  const moves = dragMoves(targetParent, true);
  return moves.length ? { mode:side, tok:targetTok, parent:targetParent, moves } : null;
}
// Dropped off the rows — out of the window, or on empty space: everything goes to the
// top level of the component you are IN (not the document's). Bodies and geo-side
// folders land loose in its Geometry group, since that is their top level; the rest
// go on the component itself. Rows already there drop out as no-ops (moveKind).
function planOut(){
  const comp = activeContainerTok();
  const geo = childrenTokensOf(comp).find(isGeoTok) || null;
  const all = drag.toks || [drag.tok];
  const toGeo = t => isFolderTok(t) ? !!geoFolderOf(t)
                                    : (nodeByTok(t) || {}).type === "body";
  const parts = [];
  for (const [container, toks] of [[geo, all.filter(toGeo)], [comp, all.filter(t => !toGeo(t))]]){
    if (!container || !toks.length) continue;
    const moves = toks.map(t => ({ tok:t, kind: moveKind(t, container) })).filter(m => m.kind);
    if (moves.length) parts.push({ mode:"into", tok:container, moves });
  }
  return parts.length ? { mode:"out", parts } : null;
}

function paintDrop(){
  document.querySelectorAll(".row.dropinto").forEach(r=>r.classList.remove("dropinto"));
  const line = document.getElementById("drop-line");
  line.style.display = "none";
  if (!drop) return;
  if (drop.mode === "out"){
    for (const p of drop.parts){ const r = rowEl(p.tok); if (r) r.classList.add("dropinto"); }
    return;
  }
  if (drop.mode === "into"){
    const r = rowEl(drop.tok); if (r) r.classList.add("dropinto");
    return;
  }
  const r = rowEl(drop.tok); if (!r) return;
  const rc = r.getBoundingClientRect();
  const treeRc = document.getElementById("tree").getBoundingClientRect();
  const pad = parseInt(r.style.paddingLeft) || 10;
  line.style.left  = (treeRc.left + pad) + "px";
  line.style.width = Math.max(0, treeRc.right - treeRc.left - pad - 10) + "px";
  line.style.top   = ((drop.mode === "after" ? rc.bottom : rc.top) - 1) + "px";
  line.style.display = "block";
}

// Current child tokens of a parent in displayed (overlay-applied) order.
function childrenTokensOf(parentTok){
  const arr = parentTok === "__root__" ? state.tree
            : (nodeByTok(parentTok)?.children || []);
  return orderedChildren(parentTok, arr).map(n=>n.token);
}
// Record `toks` at the requested position in newParent's overlay order (and drop them
// from their old parents'). where = 'end' | 'before' | 'after' (relative to relTok).
// The whole group is placed in ONE pass, keeping the rows together and in the order
// given: doing them one at a time re-read the container's children between calls, and
// since a row moved a moment ago isn't in the tree until the next rebuild, each call
// dropped the previous one from the list and the group landed interleaved.
function orderMoveMany(toks, newParentTok, relTok, where){
  for (const tok of toks){
    const oldParent = parentTokOf(tok);
    if (siblingOrder[oldParent])
      siblingOrder[oldParent] = siblingOrder[oldParent].filter(t=>t!==tok);
  }
  const moving = new Set(toks);
  const list = childrenTokensOf(newParentTok).filter(t=>!moving.has(t));
  const i = (where === "end" || !relTok) ? -1 : list.indexOf(relTok);
  if (i < 0) list.push.apply(list, toks);
  else list.splice(i + (where === "after" ? 1 : 0), 0, ...toks);
  siblingOrder[newParentTok] = list;
  saveOrder();
}
function orderMove(tok, newParentTok, relTok, where){
  orderMoveMany([tok], newParentTok, relTok, where);
}
function applyDrop(d, plan){
  if (!plan){ render(); return; }
  if (plan.feat){
    // The real timeline moves; the rows follow on the next /feats.
    // Drawn at the drop at once, with a spinner, until Fusion's verdict (featMove).
    const toks = featToksOf(d.toks || [d.tok]), src = nodeByTok(d.tok);
    const id = Date.now() + ":" + Math.random();
    const body = { action:"featreorder", id, toks };
    body[plan.mode] = plan.featTok;
    featMoveErr = null;
    featMove = { id, geoTok: src ? src.geoTok : "", toks, mode: plan.mode,
                 anchor: plan.featTok, at: Date.now() };
    featPost(body); refreshFolders(); pollFeats();
    return;
  }
  if (plan.mode === "out"){ for (const p of plan.parts) applyDrop(d, p); return; }
  const into      = plan.mode === "into";
  const container = into ? plan.tok : plan.parent;
  // The name-derived rows go first, before a single order entry is written: they are
  // all one rewrite, they get one collision check and one refusal between them (see
  // applyRenamePlan), and a refusal has to leave the drop as if it never happened —
  // which it cannot do if the ordering overlay has already been written under the
  // tokens the rename was about to invalidate. A refusal stops the whole drop,
  // including whatever else was in the selection, for the same reason.
  const named = plan.moves.filter(m => m.kind === "name");
  if (named.length){
    const groups = named.filter(m => isNameTok(m.tok));
    const params = named.filter(m => isParamTok(m.tok)).map(m => paramNameOf(m.tok));
    // One group at a time: two groups moving into the same place would each want to
    // rewrite the other's parameters, and the plans would fight. Dragging several is
    // rare enough that taking the first and saying nothing would be worse than simply
    // doing them in order, each with its own check.
    for (const g of groups) if (!moveNameGroup(g.tok, container)) return;
    if (params.length && !moveParamsByName(params, container)) return;
    return;                        // their place inside the target IS their new name
  }
  // Position first, for every row at once: it reads their CURRENT parents, so it has
  // to run before setContainer.
  orderMoveMany(plan.moves.map(m => m.tok), container,
                into ? null : plan.tok, into ? "end" : plan.mode);
  let virtual = false;
  let regroup = false;
  for (const m of plan.moves){
    if (m.kind === "virtual"){            // in/out of a folder — this window only
      virtual = true;
      // Moving in or out of a hidden group used to write the item's bulb here and
      // patch the group's remembered snapshot. Membership is the only thing that
      // changes now — what the item's bulb should be follows from it, and the
      // add-in works that out.
      const from = overridingFolderOf(m.tok);    // read BEFORE the move
      setContainer(m.tok, container);
      const to = hiddenFolderAtOrAbove(container);
      if (from !== to){
        const n = nodeByTok(m.tok);
        if (n) pendingVis[m.tok] = { visible: !to && !n.userHidden, ts: Date.now() };
        regroup = true;
      }
    } else if (m.kind === "fusion"){
      post({action:"reparent", token:m.tok,
            targetToken: isGeoTok(container) ? compTokOf(containerCompOf(container)) : container});
    }
  }
  if (virtual){
    saveFolders(); applyPendingVis(); refreshFolders();
    if (regroup) post({action:"regroup"});
    return;
  }
  render();   // same-parent reorder shows instantly; a reparent settles on the next poll
}

// The row menu opens on right-click only (the two-finger click). Press-and-hold used
// to open it too; that was taken out — a long press is just a slow click now.
document.getElementById("tree").addEventListener("mousedown", e=>{
  if (e.button !== 0) return;
  hideCtx();
  const row = e.target.closest(".row");
  if (row && row.classList.contains("placeholder")){ press = null; return; }
  press = { tok: row ? row.dataset.tok : null, type: row ? row.dataset.type : null,
            x: e.clientX, y: e.clientY, meta: e.metaKey, alt: e.altKey,
            shift: e.shiftKey };
  publishLiveBottom();
});

document.addEventListener("mousemove", e=>{
  if (!press || !press.tok) return;
  if (!drag){
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_THRESH) return;
    if ((nodeByTok(press.tok) || {}).fixed) return;   // Origin rows — nothing to reparent
    if (press.type === "fgroup") return;               // a feature group doesn't drag (yet)
    // Pressing on a row that is part of a multi-row selection drags the WHOLE
    // selection; pressing anywhere else drags just that row (the selection is left
    // alone either way). Order follows the tree, not the order they were picked, so
    // dropping them keeps them in the order they were drawn in, and a row whose own
    // ancestor is coming along is dropped from the list — it moves with it.
    let toks = [press.tok];
    if (selected.has(press.tok) && selected.size > 1){
      const order = flatten().filter(r => !r.placeholder).map(r => r.node.token);
      toks = [...selected].filter(t => nodeByTok(t) || isParamTok(t))
                          .sort((a, b) => order.indexOf(a) - order.indexOf(b));
      toks = toks.filter(t => !toks.some(o => o !== t && isDescendantTok(o, t)));
    }
    // Feature rows drag only with the other features of their own body.
    if (press.type === "feature"){
      const b = (nodeByTok(press.tok) || {}).geoTok;
      toks = toks.filter(t => { const x = nodeByTok(t) || {}; return x.type === "feature" && x.geoTok === b; });
      startFeatDrag(toks);
    }
    drag = { tok: press.tok, type: press.type, toks };    // freezes poll-driven render
    for (const t of toks){ const r = rowEl(t); if (r) r.classList.add("drag-source"); }
    showGhost(toks.length > 1 ? toks.length + " items"
                              : (nodeByTok(drag.tok) || {}).name, e.clientX, e.clientY);
  }
  moveGhost(e.clientX, e.clientY);
  drop = computeDrop(e);
  paintDrop();
});

document.addEventListener("mouseup", (e)=>{
  if (!press) return;
  const p = press, d = drag, plan = drop;
  press = null; drag = null; drop = null;
  publishLiveBottom();
  if (d){
    for (const t of (d.toks || [d.tok])){ const r = rowEl(t); if (r) r.classList.remove("drag-source"); }
    hideGhost(); paintDrop();        // drop is null now → clears line/highlight
    applyDrop(d, plan);
    return;
  }
  // Three-finger tap on empty space → top level. e.detail / the DOM dblclick below are
  // the SYSTEM's notion of a double-click, and the double-click interval on this machine
  // is set so short that only BTT's synthetic pair can satisfy it — a human double-click
  // never does. The dock shell's native monitor is the reliable path (WebKit drops rapid
  // synthetic clicks); these two are backups for when the page runs on its own.
  if (!p.tok && e.detail >= 2){ activateRootUI(); return; }
  handleClick(p);                    // didn't cross the threshold → treat as a click
});

// Double-click empty space → jump to the top level (document root). On a row it's
// just a text-selection suppressor.
document.getElementById("tree").addEventListener("dblclick", e=>{
  e.preventDefault();
  if (!e.target.closest(".row")) activateRootUI();
});
// Right-click. Over a ROW it is an ordinary right-click and opens that row's menu —
// the only way in (press-and-hold no longer opens it).
// Over EMPTY SPACE it still goes up one level, which is what a two-finger click means
// in the viewport: the gesture keeps its meaning wherever there is nothing under it,
// and means "this row" wherever there is.
//
// The right-click NEVER changes the selection — neither in this window nor in Fusion.
// It used to pick the row first, so the menu was for something visibly picked, but that
// made asking a row a question destructive: the selection you had built up was gone by
// the time the menu appeared. The menu already knows which row it belongs to (placeCtx
// stashes it) and every item acts on that row, so the highlight was never load-bearing.
// A row inside a multi-selection still gets the menu FOR THE SELECTION (see showCtx);
// one outside it gets a menu for itself alone and leaves the selection standing.
document.getElementById("tree").addEventListener("contextmenu", e=>{
  e.preventDefault();
  const row = e.target.closest(".row");
  if (!row){                                          // empty space
    hideCtx();                                        // only the left button did this before
    goUpOneLevel();                                   // right-click → up one level
    return;                                           // (top level is now double-click empty)
  }
  if (row.classList.contains("placeholder")) return;
  const tok = row.dataset.tok;
  const n = nodeByTok(tok);
  if (!n) return;
  press = null;
  if (showCtx(e.clientX, e.clientY, n)) ctxOpenedAt = Date.now();
});
// ── Go To a dependant ─────────────────────────────────────────────────────────────
// The dependants listed under a sketch's Delete item and in the Delete Parameter dialog
// (depLine) are picked with a click; a right-click picks one and offers Go To — a
// parameter opens in its editor, a sketch is revealed here and opened. Capture phase, so
// a click on a line inside the Delete item picks the line and never deletes.
document.addEventListener("mousedown", e => {
  const t = e.target.closest ? e.target : e.target.parentElement;
  const menu = document.getElementById("dep-menu");
  if (menu && menu.contains(t)){
    e.preventDefault(); e.stopPropagation();
    const it = t.closest(".ctx-item");
    if (it && !it.classList.contains("disabled") && e.button === 0){
      const line = document.querySelector(".dep-line.sel");
      menu.remove();
      if (line) goToDep(line);
    }
    return;
  }
  if (menu) menu.remove();
  const line = t && t.closest(".dep-line");
  if (!line) return;
  e.preventDefault(); e.stopPropagation();
  document.querySelectorAll(".dep-line.sel").forEach(l => l.classList.remove("sel"));
  line.classList.add("sel");
}, true);
document.addEventListener("contextmenu", e => {
  const t = e.target.closest ? e.target : e.target.parentElement;
  const line = t && t.closest(".dep-line");
  if (!line && !(t && t.closest("#dep-menu"))) return;
  e.preventDefault(); e.stopPropagation();
  if (line) showDepMenu(e.clientX, e.clientY, line);
}, true);
function showDepMenu(x, y, line){
  const old = document.getElementById("dep-menu"); if (old) old.remove();
  const can = !!(line.dataset.param || line.dataset.key);
  document.body.insertAdjacentHTML("beforeend",
    `<div id="dep-menu"><div class="ctx-item${can ? "" : " disabled"}">Go To</div></div>`);
  const m = document.getElementById("dep-menu"), r = m.getBoundingClientRect();
  m.style.left = Math.max(2, Math.min(x, window.innerWidth  - r.width  - 2)) + "px";
  m.style.top  = Math.max(2, Math.min(y, window.innerHeight - r.height - 2)) + "px";
}
function goToDep(line){
  const param = line.dataset.param, key = line.dataset.key;
  if (param){
    hideCtx(); if (subst) closeSubstitute();
    openParamEditor("param:" + param);
    return;
  }
  const n = key && sketchNodeByKey(key);
  if (!n) return;
  hideCtx(); if (subst) closeSubstitute();
  openAbove([n.token]);
  enterSketch(n);
}
// A sketch row by its durable key. A component placed more than once has a row per
// placement — the one in the component you are in wins, else the first.
function sketchNodeByKey(key){
  const hits = [];
  (function w(ns){ for (const n of ns){ if (n.type === "sketch" && n.key === key) hits.push(n);
                                        if (n.children) w(n.children); } })(state.tree);
  return hits.find(n => n.occToken === (state.activeToken || "")) || hits[0] || null;
}
document.getElementById("ctx").addEventListener("mousedown", e=>{
  e.preventDefault(); e.stopPropagation();          // don't let it reach the tree
  const ctx = document.getElementById("ctx");
  const it = e.target.closest(".ctx-item"); if(!it || it.classList.contains("disabled")) return;
  if (it.dataset.act === "swreplace"){ hideCtx(); setReplace(ctxSwatch); return; }   // the palette's
  if (it.dataset.act === "swedit"){ hideCtx(); post({action: "editappearance", appearance: swSpec(ctxSwatch)}); return; }
  if (it.dataset.act === "swrename"){ hideCtx(); beginSwatchRename(ctxSwatch); return; }
  if (it.dataset.act === "swdefault" || it.dataset.act === "swnodefault"){
    const sw = it.dataset.act === "swdefault" ? ctxSwatch : null;
    hideCtx();
    post({action: "defaultappearance", appearance: sw ? swSpec(sw) : null});
    appearances.default = sw ? {name: sw.name, id: sw.id, rgb: sw.rgb, tex: sw.tex} : null;   // until the fresh list lands
    renderPalette(); palName(null);
    return;
  }
  const tok  = ctx.dataset.tok;                                   // the held row
  const toks = JSON.parse(ctx.dataset.toks || "null") || [tok];   // Delete's rows
  const same = JSON.parse(ctx.dataset.same || "null") || [tok];   // per-type rows
  // The view toggles at the foot of every menu are about the tree, not about `tok` —
  // handled before anything reads the row at all.
  const vact = VIEW_ACTS[it.dataset.act || ""];
  if (vact){ hideCtx(); vact(); return; }
  // Folder items are window-only and some of them replace the menu with an input,
  // so they hide it themselves and return before the shared hideCtx() below.
  if (it.dataset.act === "delrow"){ hideCtx(); deleteTokens(toks, true); return; }
  if (/^(feat|phase-)/.test(it.dataset.act || "")){
    // Feature rows act through the Timeline add-in, on the features themselves.
    hideCtx();
    const act = it.dataset.act, held = nodeByTok(tok) || {};
    const ftoks = featToksOf(same);
    if (act === "featedit") editFeature(held);
    else if (act === "featroll") featPost({action:"rollto", tok: held.featTok});
    else if (act === "featsuppress") featPost({action:"suppress", toks: ftoks, on: !held.suppressed});
    else if (act === "featdelete") featPost({action:"delete", toks: featToksOf(toks)});
    else for (const ft of ftoks)
      featPost({action:"setphase", tok: ft, phase: act === "phase-auto" ? null : +act.slice(6)});
    pollFeats();
    return;
  }
  if (it.dataset.act === "cut"){
    hideCtx();
    const cut = ctxCutToks(toks);
    if (cut.length){ cutToks = cut; render(); }
    return;
  }
  if (it.dataset.act === "paste"){
    hideCtx();
    const n = nodeByTok(tok);
    if (n) pasteCut(n, ctxPasteTarget(n));
    return;
  }
  if (it.dataset.act === "disregard" || it.dataset.act === "disregardall"){
    // The add-in appends the tag to the REAL component name — the name shown here has
    // its dedup suffix stripped, so it can't be used to build one.
    const mode = it.dataset.act === "disregardall" ? "all" : "self";
    hideCtx();
    const now = Date.now();
    for (const t of same){
      pendingDisregard[t] = { mode, ts: now };     // the rows go now
      if (cursorTok === t) cursorTok = null;
      selected.delete(t);
      post({action:"disregard", token: t, mode});
    }
    refreshFolders();
    return;
  }
  if (it.dataset.act === "ignore"){
    // Tick flips from the HELD row's state, same convention as edgepolicy/hidebody
    // below; the flip skips any selected folder that has content — it can't be
    // ignored regardless of what the held one says.
    const on = !folderIgnored(nodeByTok(tok) || {});
    hideCtx();
    for (const t of same){
      const nd = nodeByTok(t); if (!nd || folderHasVisibleContent(nd)) continue;
      const f = folderOf(t); if (!f) continue;
      f[ignoreKeyOf(f)] = on;
      if (on && !showIgnoredFolders){          // the row is about to vanish
        if (cursorTok === t) cursorTok = null;
        selected.delete(t);
      }
    }
    saveFolders();
    refreshFolders();
    return;
  }
  if (it.dataset.act === "togglevis"){ hideCtx(); post({action:"togglevis", tokens: same}); return; }
  if (it.dataset.act === "redefineplane"){ hideCtx(); redefinePlane(tok); return; }
  if (it.dataset.act === "editbounds"){ hideCtx(); editBounds(tok); return; }
  if (it.dataset.act === "renamefolder"){ hideCtx(); beginRename(tok); return; }
  if (it.dataset.act === "renamenamegroup"){ hideCtx(); beginRename(tok); return; }
  if (it.dataset.act === "renamefgroup"){ hideCtx(); beginRename(tok); return; }
  if (it.dataset.act === "fgroupwrap"){
    // ⌘G on the menu's rows: the features and feature groups among them.
    hideCtx();
    const mine = toks.filter(t => isFeatTok(t) || isFGroupTok(t));
    selected = new Set(mine); cursorTok = tok;
    groupFeatureSel();
    return;
  }
  if (it.dataset.act === "explodenamegroup"){ hideCtx(); explodeNameGroup(tok); return; }
  if (it.dataset.act === "newsubfolder"){
    selected.clear(); cursorTok = tok; hideCtx(); addFolder(false, tok); return;
  }
  if (it.dataset.act === "newparamgroup"){
    selected.clear(); cursorTok = tok; hideCtx(); addFolder(true, tok); return;
  }
  if (it.dataset.act === "fly") return;       // its flyout opens on hover; the item itself does nothing
  if (/^detail-/.test(it.dataset.act || "")){
    post({action:"displaydetail", items: viewportItemsFor(same), level: it.dataset.act.slice(7)});
  } else if (it.dataset.act === "texturemap"){
    post({action:"texturemap", items: viewportItemsFor(same)});
  } else if (it.dataset.act === "sheetmetal"){
    post({action:"sheetmetal", token:tok});
  } else if (it.dataset.act === "edgepolicy" || it.dataset.act === "hidebody"
             || it.dataset.act === "edgewhen"){
    // The label shows the HELD row's state; the flip applies to every selected component,
    // so a mixed selection all ends up the same way as the held one.
    const act = it.dataset.act, trig = it.dataset.trig;
    const on = !it.classList.contains("on");
    for (const t of same){
      const n = nodeByTok(t); if (!n || !n.compId) continue;
      if (act === "edgewhen"){
        if (trig === n.compId) continue;
        const w = new Set(n.hideEdgesWhen || []);
        if (on) w.add(trig); else w.delete(trig);
        n.hideEdgesWhen = [...w].sort();
        post({action: act, compId: n.compId, trigger: trig, on: on});
      } else {
        if (act === "edgepolicy") n.hideEdges = on;
        else n.hideBody = on;
        post({action: act, compId: n.compId, on: on});
      }
    }
    // The Hide Edges flyout's ticks stay open for the next one: flip in place.
    if (act !== "hidebody"){
      it.classList.toggle("on", on);
      const fly = it.closest(".ctx-item.fly"), held = nodeByTok(tok);
      if (fly && held){
        const k = (held.hideEdgesWhen || []).length + (held.hideEdges ? 1 : 0);
        fly.firstElementChild.textContent = `Hide Edges${k ? ` · ${k}` : ""}`;
      }
      return;
    }
  }
  hideCtx();
});
// A flyout opens when the pointer settles on its item and stays open until it settles on
// ANOTHER item of the menu — crossing items on the way to the flyout, or leaving the
// menu for the page, shuts nothing (fusion_ui/menu_aim.js).
document.getElementById("ctx").addEventListener("mouseover", e=>{
  const it = e.target.closest(".ctx-item"); if (!it) return;
  if (e.relatedTarget && it.contains(e.relatedTarget)) return;
  const ctx = document.getElementById("ctx"), open = ctx.querySelector(".ctx-fly.open");
  const aim = window.MenuAim || { hover: (x, act) => act(), cancel(){} };   // served once the server restarts
  if (open && open.parentElement.contains(it)) { aim.cancel(); return; }   // its item or inside it
  const f = it.classList.contains("fly") ? it : null;
  aim.hover(it, () => {
    for (const o of ctx.querySelectorAll(".ctx-fly.open")) if (!f || o.parentElement !== f) o.classList.remove("open");
    if (f) placeFly(f);
  }, () => ctx.querySelector(".ctx-fly.open"));
});
window.addEventListener("mousedown", e=>{ if(!e.target.closest("#ctx")) hideCtx(); });
window.addEventListener("resize", publishLiveBottom);
// Clicking into Fusion (or anywhere outside this window) blurs it — dismiss straight
// away rather than waiting up to a poll interval for the clickTs signal. Belt and
// braces: if the window is non-activating and never gets blur, clickTs still covers it.
window.addEventListener("blur", hideCtx);

// ── appearances palette ──────────────────────────────────────────────────────
// Fusion's own Appearance dialog can't drop here: its drag carries an empty Qt
// text/plain, so which swatch it was is known only inside Fusion. This window draws its
// own swatches instead (the add-in's `appearances` → /appearances), and a swatch dropped
// on a row paints every body under it: a body, a Bodies group or a folder in one (nested
// folders too), or a component (its whole subtree) — hidden bodies included. Dropped on
// a row that is part of a multi-row selection, it paints the whole selection. At the TOP
// of the window: below the last row is the viewport's (computeLiveBottom).
let showAppearances = false;
try { showAppearances = localStorage.getItem('browserShowAppearances') === '1'; } catch(_){}
let appearances = { design: [], library: [] }, appRev = -1, appDoc = null;
let swDrag = null;           // {sw, x, y, live} — a swatch press/drag in progress
let palItems = [];           // the swatches drawn, in order (a .swatch's data-i)
let appPollTimer = null;
// Right-click a design swatch → Replace: that swatch is held here until the next swatch
// clicked (design or Library) takes its place on everything wearing it
// (`replaceappearance`). Esc or a click outside the palette gives up.
let swReplace = null, ctxSwatch = null;
const swSpec = sw => ({src: sw.src, index: sw.index, name: sw.name, id: sw.id});
const sameSw = (a, b) => !!a && !!b && a.src === b.src && a.index === b.index && a.name === b.name;
// Default for New Bodies (right-click any swatch → Browser.py `_set_default_appearance`,
// kept as name + id + look): the swatch that matches all three wears the marker.
const swLook = sw => JSON.stringify([sw.rgb || null, sw.tex || null]);
const isDefaultSw = sw => { const d = appearances.default;
                            return !!d && d.id === sw.id && d.name === sw.name && swLook(d) === swLook(sw); };
function setReplace(sw){
  swReplace = sw;
  document.getElementById("palette").classList.toggle("replacing", !!sw);
  renderPalette(); palName(null);
}
function palName(a){
  const el = document.getElementById("pal-name");
  if (!el) return;
  const label = x => x.name + (x.src === "lib" ? " — Library" : "");
  el.textContent = swRename ? `Rename ${swRename.sw.name}`
                 : swReplace ? `Replace ${swReplace.name} With ${a && !sameSw(a, swReplace) ? label(a) : "…"}`
                 : a ? label(a) + (isDefaultSw(a) ? " — New Bodies" : "")
                 : appearances.default ? `New Bodies: ${appearances.default.name}` : "";
}

// Rename a design swatch — its right-click Rename, or ⌘R with the pointer on it
// (beginRename hands over; the palette has no selection, so the hovered swatch is the
// target). A swatch has no label to edit in place, so the search box is the name field
// for the moment: it takes the name, selected; Enter or a click away commits, Esc gives
// up, and either way the search comes back as it was. `renameappearance` → Browser.py
// `_rename_appearance`, which renames every appearance the swatch deduped.
let palHover = null;         // the swatch under the pointer — ⌘R's target
let swRename = null;         // {sw, q} — the swatch being renamed, the search it hid
const palQuery = () => swRename ? swRename.q : document.getElementById("pal-search").value;
function beginSwatchRename(sw){
  if (editing || !sw || sw.src !== "design" || !showAppearances) return;
  const inp = document.getElementById("pal-search"), pal = document.getElementById("palette");
  if (!inp) return;
  if (swReplace) setReplace(null);
  const st = swRename = { sw, q: inp.value };
  editing = true;                          // the window's keys stand down, as for a row
  pal.classList.add("renaming");
  inp.value = sw.name;
  renderPalette(); palName(null);
  inp.focus(); inp.select();
  const tookKey = takeKeyFocus(), openedAt = Date.now();   // see beginRename
  if (tookKey) setTimeout(() => { if (swRename === st){ inp.focus(); inp.select(); } }, 60);
  const commit = save => {
    if (swRename !== st) return;
    swRename = null; editing = false;
    inp.removeEventListener("keydown", onKey); inp.removeEventListener("blur", onBlur);
    if (tookKey) releaseKeyFocus();
    const v = inp.value.trim();
    inp.value = st.q; pal.classList.remove("renaming");
    if (save && v && v !== sw.name){
      post({action: "renameappearance", appearance: swSpec(sw), name: v});
      sw.name = v;                         // until the add-in's fresh list lands
    }
    renderPalette(); palName(null);
    if (document.activeElement === inp) inp.blur();
  };
  const onKey = e => {
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); commit(true); }
    else if (e.key === "Escape"){ e.preventDefault(); commit(false); }
  };
  const onBlur = () => {
    if (tookKey && Date.now() - openedAt < 250){       // the blur of taking key focus
      setTimeout(() => { if (swRename === st){ inp.focus(); inp.select(); } }, 0);
      return;
    }
    commit(true);
  };
  inp.addEventListener("keydown", onKey);
  inp.addEventListener("blur", onBlur);
}

function toggleAppearances(){
  showAppearances = !showAppearances;
  try { localStorage.setItem('browserShowAppearances', showAppearances ? '1' : '0'); } catch(_){}
  renderPalette();
  if (showAppearances){ appDoc = null; pollAppearances(); }
  publishLiveBottom();
}
// While the palette is up: ask the add-in for a fresh list whenever the document changes
// (a paint refreshes it by itself), and pick up a new rev.
function pollAppearances(){
  clearTimeout(appPollTimer); appPollTimer = null;
  if (!showAppearances) return;
  const doc = state.doc_id || "";
  if (doc !== appDoc){ appDoc = doc; post({action: "appearances"}); }
  fetch(API + "/appearances?since=" + appRev).then(r => r.json()).then(d => {
    if (d && d.lists && d.rev !== appRev){ appRev = d.rev; appearances = d.lists; renderPalette(); }
  }).catch(() => {}).finally(() => { appPollTimer = setTimeout(pollAppearances, 700); });
}
function swatchStyle(sw){
  if (sw.tex) return `background-image:url('${API}/apptex?p=${encodeURIComponent(sw.tex)}')`;
  if (sw.rgb) return `background:rgb(${sw.rgb.join(",")})`;
  return "";
}
function renderPalette(){
  const pal = document.getElementById("palette");
  if (!showAppearances){ pal.hidden = true; return; }
  pal.hidden = false;
  if (!pal.firstChild){
    pal.innerHTML = `<div class="pal-head"><span class="pal-title">Appearances</span>` +
      `<label class="pal-find" title="Search Library"><svg viewBox="0 0 16 16" aria-hidden="true">` +
      `<circle cx="6.5" cy="6.5" r="4.5"/><path d="M10 10l4 4"/></svg>` +
      `<input id="pal-search" spellcheck="false"></label></div>` +
      `<div id="pal-grid"></div><div id="pal-name"></div>`;
    document.getElementById("pal-search").addEventListener("input", renderPalette);
  }
  const q = palQuery().trim().toLowerCase();
  const hit = a => !q || a.name.toLowerCase().includes(q);
  const mark = a => sameSw(a, swReplace) || (swRename && sameSw(a, swRename.sw));
  const cell = (a, i) => `<div class="swatch${mark(a) ? " replacing" : ""}${isDefaultSw(a) ? " default" : ""}"` +
                         ` data-i="${i}" style="${swatchStyle(a)}"></div>`;
  const ours = appearances.design || [], lib = appearances.library || [];
  palItems = ours.filter(hit).concat(q ? lib.filter(hit).slice(0, 80) : []);
  const nOurs = ours.filter(hit).length;
  let html = palItems.slice(0, nOurs).map(cell).join("");
  if (q && palItems.length > nOurs)
    html += `<div class="pal-sep">Library</div>` +
            palItems.slice(nOurs).map((a, j) => cell(a, nOurs + j)).join("");
  if (!palItems.length) html = `<div class="pal-empty">${q ? "No Match" : appRev < 0 ? "…" : "None Applied"}</div>`;
  document.getElementById("pal-grid").innerHTML = html;
  palName(palHover);
}

// The body rows a drop on `n` paints (see the section head).
function paintItemsFor(n){
  const out = [], seen = new Set();
  const walk = c => {
    if (!c) return;
    if (c.type === "body"){
      if (!seen.has(c.token)){ seen.add(c.token); out.push({token: c.token, occToken: c.occToken || ""}); }
    } else if ((c.type === "folder" && !c.params) || c.type === "geometry" || c.type === "component"){
      (c.children || []).forEach(walk);
    }
  };
  const many = selected.has(n.token) && selected.size > 1;
  (many ? [...selected].map(nodeByTok) : [n]).forEach(walk);
  return out;
}
function swDropRow(e){
  const el = document.elementFromPoint(e.clientX, e.clientY);
  const row = el && el.closest("#tree .row");
  if (!row || row.classList.contains("placeholder")) return null;
  const n = nodeByTok(row.dataset.tok);
  return n && paintItemsFor(n).length ? row : null;
}
function paintSwDrop(row){
  document.querySelectorAll("#tree .row.dropinto").forEach(r => { if (r !== row) r.classList.remove("dropinto"); });
  if (row) row.classList.add("dropinto");
}

document.getElementById("palette").addEventListener("mousedown", e => {
  const s = e.target.closest(".swatch");
  if (!s || e.button !== 0) return;
  e.preventDefault();                    // no text selection / input blur mid-drag
  swDrag = { sw: palItems[+s.dataset.i], x: e.clientX, y: e.clientY, live: false };
  publishLiveBottom();
});
document.getElementById("palette").addEventListener("mouseover", e => {
  const s = e.target.closest(".swatch");
  palHover = s && palItems[+s.dataset.i] || null;
  palName(palHover);
});
document.getElementById("palette").addEventListener("mouseleave", () => { palHover = null; });
document.getElementById("palette").addEventListener("contextmenu", e => {
  e.preventDefault();
  const s = e.target.closest(".swatch");
  const a = s && palItems[+s.dataset.i];
  if (!a) return;
  // A Library swatch isn't on anything yet — it only offers the default.
  const items = (a.src === "design" ? `<div class="ctx-item" data-act="swedit">Edit…</div>` +
                                      `<div class="ctx-item" data-act="swreplace">Replace</div>` +
                                      `<div class="ctx-item" data-act="swrename">Rename</div>` : "") +
                (isDefaultSw(a) ? `<div class="ctx-item" data-act="swnodefault">Clear Default for New Bodies</div>`
                                : `<div class="ctx-item" data-act="swdefault">Default for New Bodies</div>`);
  swDrag = null; ctxSwatch = a;
  const ctx = document.getElementById("ctx");
  ctx.innerHTML = items;
  ctx.style.left = "0px"; ctx.style.top = "0px"; ctx.style.display = "block";
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.max(2, Math.min(e.clientX, window.innerWidth  - r.width  - 2)) + "px";
  ctx.style.top  = Math.max(2, Math.min(e.clientY, window.innerHeight - r.height - 2)) + "px";
  ctxOpenedAt = Date.now();
  publishLiveBottom();
});
window.addEventListener("mousedown", e => {
  if (swReplace && !e.target.closest("#palette, #ctx")) setReplace(null);
});
document.addEventListener("mousemove", e => {
  if (!swDrag) return;
  if (!swDrag.live){
    if (Math.hypot(e.clientX - swDrag.x, e.clientY - swDrag.y) < DRAG_THRESH) return;
    swDrag.live = true;
    showGhost(swDrag.sw.name, e.clientX, e.clientY);
  }
  moveGhost(e.clientX, e.clientY);
  paintSwDrop(swDropRow(e));
});
document.addEventListener("mouseup", e => {
  if (!swDrag) return;
  const d = swDrag; swDrag = null;
  publishLiveBottom();
  if (!d.live){                          // a click: the pick while replacing
    if (swReplace){
      if (!sameSw(d.sw, swReplace))
        post({action: "replaceappearance", old: swSpec(swReplace), new: swSpec(d.sw)});
      setReplace(null);
    }
    return;
  }
  hideGhost(); paintSwDrop(null);
  const row = swDropRow(e);
  if (!row) return;
  const items = paintItemsFor(nodeByTok(row.dataset.tok));
  const sw = d.sw;
  post({action: "paint", items, appearance: swSpec(sw)});
});

// ── keyboard ─────────────────────────────────────────────────────────────────
function matchesHotkey(e){
  const cur = seqOf(e);
  for (const [name, seq] of Object.entries(hotkeys)){
    if (seq && normSeq(seq) === cur) return name;
  }
  return null;
}

// id -> what that action does HERE. The ids are declared once in the shared manifest
// (fusion_ui/actions.json), which is also what the Fusion add-in reads to create a
// CommandDefinition for each one; this table is the half that is genuinely this window's
// own code. An id with no entry here is simply not ours to run, and gets forwarded.
const ACTION_IMPL = {
  // Not a keyboard action: pushed here over /uiaction by the Dimension Value add-in
  // right after it creates a user parameter, so the row lands beside the sketch it was
  // made for instead of unfiled at the top level. It arrives with an explicit container
  // token rather than using this window's selection, because the add-in knows which
  // sketch the dimension belonged to and the window does not.
  'BrowserFileParamCmd': (e, arg) => {
    if (!arg || !arg.name) return;
    // By component id when the add-in sends one; the occurrence token is the older
    // form of the same message, and resolves only while that occurrence is in the tree.
    const ref = arg.compId ? (compByCid[arg.compId] ? arg.compId : "")
                           : paramRefOf(arg.token || "");
    if (ref) paramPlace[arg.name] = ref; else delete paramPlace[arg.name];
    saveFolders();
    refreshFolders();
  },

  // Pushed by the New Component flow the moment a component's bounds are built. This
  // window is the single writer of folders.json, so the filing has to happen HERE
  // rather than by a second process editing the file underneath it — the same reason
  // BrowserFileParamCmd exists. Find-or-create, so rebuilding a component's bounds
  // refiles them into the folder that is already there rather than making a second one.
  //
  // It names the component by ID and the sketches by durable KEY, because it arrives
  // BEFORE the tree has the new rows: the add-in posts it the moment the component is
  // built, and the window sees the rows a tick or two later. So it is queued, and
  // fileBoundsPending() has a go on every refresh until the component's row is there
  // to hang the folder off. (A folder records the token of the row it draws under, and
  // that token is only known once the row exists.)
  // Pushed by Jump to Name (../jump/server.py, port 8741) once a name has been picked:
  // go there the way the keys would. A component is gone into as F goes into one
  // (activated — the isolate follows — and its top row picked); a folder's component
  // is activated, the folder and every group above it opened, and the folder picked.
  'BrowserJumpToCmd': (e, arg) => {
    if (!arg) return;
    const tok = arg.folder ? FOLDER_PREFIX + arg.folder : arg.token;
    const n = tok && nodeByTok(tok);
    if (!n) return;
    if (n.type === 'component'){
      activateComponentTok(n.token);
      const top = flatten().find(r => !r.placeholder);
      if (top) selectAfterCollapse(top.node.token);
      return;
    }
    const path = pathToTok(tok);
    const comp = path.slice(0, -1).reverse().map(nodeByTok).find(x => x && x.type === 'component');
    if (comp && comp.token !== state.activeToken) activateComponentTok(comp.token);
    protectPath(tok, 4000);
    for (const t of path) if (collapsed.has(t)){ collapsed.delete(t); cancelCollapseTimer(t); }
    saveCollapsed();
    selectAfterCollapse(tok);
  },

  'BrowserFileBoundsCmd': (e, arg) => {
    if (!arg || !arg.compId) return;
    boundsPending.push({ compId: arg.compId, params: arg.params || [],
                         sketchKeys: arg.sketchKeys || [], ts: Date.now(),
                         // Add Bounds to Component: the component already has content,
                         // so its new Bounds groups arrive shut rather than pushing it down.
                         collapse: !!arg.collapse });
    refreshFolders();
  },

  // Pushed by the Browser add-in (New Bodies.py) after a command made bodies: which
  // existing body each came from, and for a big mirror the side names. See
  // fileNewBodiesPending.
  'BrowserFileNewBodiesCmd': (e, arg) => {
    if (!arg || !Array.isArray(arg.bodies)) return;
    newBodiesPending.push({ bodies: arg.bodies, mirrors: arg.mirrors || [], ts: Date.now() });
    refreshFolders();
  },

  // Pushed the same way by the same add-in's NEW PARAMETER command. That one has no
  // sketch behind it, so it names no container and this window picks one instead —
  // which it can, because which group is open is a fact that exists only here.
  'BrowserPlaceNewParamCmd': (e, arg) => {
    if (!arg || !arg.name) return;
    const where = paramRefOf(newParamContainer());
    if (where) paramPlace[arg.name] = where; else delete paramPlace[arg.name];
    saveFolders();
    // Draw the row now rather than on the Parameters server's next poll, 700 ms away:
    // the standing rule here is that what you just did is already on screen. Clearing
    // lastParamsJSON makes the next poll land even if it reports the same rows, so the
    // optimistic one is replaced by the real one either way.
    if (!paramByName(arg.name)){
      paramRows = paramRows.concat([{ name: arg.name, expression: arg.expression || "",
                                      unit: arg.unit || "", comment: "", deps: 0 }]);
      lastParamsJSON = "";
    }
    refreshFolders();
  },

  // ⌘X / ⌘V arriving from the VIEWPORT. Fusion's own Cut and Paste still own those
  // chords; the add-in hands them over only when this window is holding something they
  // can only mean for it (see syncUiFlags), so these ids are forwarded rather than
  // bound — there is no keymap entry for them anywhere.
  'BrowserCutCmd':   () => cutSelection(),
  'BrowserPasteCmd': () => pasteCut(),
  // Pushed by the Sketch Clipboard add-in on every sketch copy: the newest clipboard is
  // now that sketch, so a parameter still waiting here for a ⌘V is let go — a paste
  // places whatever was taken LAST (the ledger, "Who owns ⌘V" in server.py).
  'BrowserClearCutCmd': () => clearCut(),

  // Pushed by the value dialog the moment it commits an edit to an existing parameter,
  // so the row carries the new value (and name) on the keystroke rather than waiting for
  // the Parameters server's next poll — the same standing rule the inline box it
  // replaced already followed.
  'BrowserParamEditedCmd': (e, arg) => {
    if (!arg || !arg.name) return;
    const pr = paramByName(arg.name);
    if (pr) pr.expression = arg.expression || pr.expression;
    const nn = arg.newName || "";
    if (nn && nn !== arg.name){
      if (pr) pr.name = nn;
      carryFilingOnRename(arg.name, nn);
      const wasCursor = cursorTok === "param:" + arg.name;
      if (wasCursor) cursorTok = "param:" + nn;
      if (selected.delete("param:" + arg.name)) selected.add("param:" + nn);
    }
    lastParamsJSON = "";                  // make the next poll land and reconcile
    refreshFolders();
  },

  // Pushed by FusionValue's stepper on commit: which parts of this parameter's row are
  // shown or hidden over the default (⌘E / right-click on a crumb chip). `show` is
  // {name?, type?, dir?} booleans — empty clears it. `from` is the name it had before a
  // rename the same commit made, so an older entry under it goes.
  'BrowserParamShowCmd': (e, arg) => {
    if (!arg || !arg.name) return;
    if (arg.from && arg.from !== arg.name) delete paramShow[arg.from];
    const s = {};
    for (const k of ["name", "type", "dir"])
      if (arg.show && typeof arg.show[k] === "boolean") s[k] = arg.show[k];
    // `lv`: parent/group words hidden from TYPING in FusionValue (never from this tree).
    if (arg.show && Array.isArray(arg.show.lv) && arg.show.lv.length)
      s.lv = arg.show.lv.filter(w => typeof w === "string");
    // A bounds parameter keeps even an empty entry: none at all means "give it the
    // bounds default" (ensureBoundsShow), which would undo what was just shown.
    if (Object.keys(s).length || isBoundsParamName(arg.name)) paramShow[arg.name] = s;
    else delete paramShow[arg.name];
    saveFolders();
    refreshFolders();
  },

  'BrowserNavDownCmd': () => stepCursor(1),
  'BrowserNavUpCmd':   () => stepCursor(-1),
  'BrowserNavDown2Cmd': () => { stepCursor(1); stepCursor(1); },
  'BrowserNavUp2Cmd':   () => { stepCursor(-1); stepCursor(-1); },
  'BrowserNavBottomCmd': () => jumpCursor(1),
  'BrowserNavTopCmd':    () => jumpCursor(-1),

  // Dual actions. These ids are EXISTING Fusion commands that already hold the key, so
  // nothing was created for them and nothing is forwarded either way: pressed in the
  // viewport Fusion runs its own command, pressed here we run ours. Same intent, each
  // side resolving "the thing you mean" in its own terms — and since stepping now pushes
  // the cursor into the viewport selection, the two sides are pointed at the same row.
  // Go in / come back out. These have their OWN ids rather than reusing Fusion's
  // ActivateAndIsolateCmd / GoToParentComponentCmd, because a browser-homed id gets
  // VETOED in Fusion whenever it fires — which would also hijack those commands' toolbar
  // buttons, the other add-ins that call them, and this window's own exitSketchEdit
  // (collapseOrOut -> exitSketchEdit -> veto -> forward -> collapseOrOut, forever).
  // Go Down a Level and Go Up a Level stay intact as ordinary Fusion commands.
  'BrowserGoInCmd':         () => expandOrInto(),
  'BrowserGoInDeepCmd':     () => goInDeep(),
  'BrowserGoOutCmd':        () => collapseOrOut(),
  // Edit edits a TIMELINE item: the feature behind a viewport pick, else a feature or
  // sketch row. On anything else — a group, folder, component, parameter — it does
  // nothing: it must not fall through to Enter's row actions (unfold a group, activate
  // a component). It arrives here forwarded from Fusion (Space → the reserved surrogate
  // while idle), not as a keydown.
  'BrowserEditCmd':         () => editTimelineItem(),
  // Still answered locally if their Fusion keys are pressed while this window is focused.
  'ActivateAndIsolateCmd':  () => expandOrInto(),
  'GoToParentComponentCmd': () => collapseOrOut(),
  'deselectAllCmd':         () => deselectAll(),     // A — clears BOTH sides already
  // Escape / Deselect (home 'fusion'): this window's own escapes first, else Fusion's.
  'EscapeOrDeselectCmd':    () => { if (!escapeHere()) post({action:"command", cmd_id:"EscapeOrDeselectCmd"}); },
  // Super Escape (home 'fusion'): everything this window has open, at once, then Fusion's
  // own (its command, the Value window). Forwarded from Fusion = Fusion already did its part.
  'SuperEscapeCmd':         () => { escapeAllHere(); if (!forwardedDepth) post({action:"command", cmd_id:"SuperEscapeCmd"}); },

  // The ⌘ actions. The first three are dual against commands that already existed in
  // Fusion but had never been bound to anything — so these keys used to work ONLY in
  // this window, which is the same drift by another route. The last two have no Fusion
  // behaviour at all (virtual folders and this window's settings panel are window-only
  // concepts): they are browser-homed, so pressing the key in the viewport forwards it
  // here rather than doing nothing.
  'BrowserRenameSelectionCmd':    () => beginRename(),
  'BrowserToggleVisSelectionCmd': () => toggleVisCursor(),
  'DeleteSelectionCmd':           () => deleteSelection(),
  'BrowserNewFolderCmd':          () => addFolder(),
  'BrowserNewParamGroupCmd':      () => addFolder(true),
  'BrowserSettingsCmd':           () => openSettings(),
  // The view toggles, so each is bindable from Control Room too — same reasoning
  // as the pair above: none of these exist in Fusion's own terms.
  'BrowserToggleIgnoredCmd':      () => toggleIgnoredFolders(),
  'BrowserToggleBoundsCmd':       () => toggleBoundsFolders(),
  'BrowserFileAllBoundsCmd':      () => fileAllBounds(),
  'BrowserSortPhasesCmd':         () => sortPhases(),
  'BrowserToggleMastersCmd':      () => toggleMasterFolders(),
  'BrowserToggleOriginsCmd':      () => toggleOrigins(),
  'BrowserToggleAppearancesCmd':  () => toggleAppearances(),
  'BrowserToggleSketchGroupsCmd': () => toggleSketches(),
  // Suspend mode: browse without touching Fusion's viewport/selection/sketch at all
  // (see the "Suspend mode" section above), and its one escape hatch.
  'BrowserSuspendNavCmd':         () => toggleSuspend(),
  'BrowserJumpToCursorCmd':       () => jumpToCursor(),
  // Swap the dock to the vertical timeline. Pressed in the viewport the Timeline add-in
  // takes it at commandStarting; pressed here it goes straight to that add-in's server.
  'DockTimelineToggleCmd':        () => toggleDockMode(),
};

// Keys this window answers for itself, so a Fusion binding on them does not cross while
// it is focused: the key already means something here that a global binding would wreck
// (Return commits a rename, Backspace deletes a folder, Escape closes a menu). None of
// these is ever going into Fusion's keymap, so unlike the letters they stay hard-coded —
// that is the whole distinction. Every LETTER now lives in Fusion's keymap and nowhere
// else. The arrows are not held back and do nothing here: they are not stepping keys.
const WINDOW_STRUCTURAL_KEYS = new Set(
  ["enter","escape","backspace","delete"," "]);

// Escape / Deselect: this window's own things are escaped here — a field (as Esc in
// it), a menu, the replace picker, the refusal, settings, Suspend, a pending cut. Only
// with none of those does it cross to Fusion, which escapes a running command or else
// deselects (handing deselectAllCmd back here). Returns whether it escaped anything.
function escapeHere(){
  if (ctxOpen()){ hideCtx(); return true; }
  if (swReplace){ setReplace(null); return true; }
  if (blockedOpen()){ hideBlocked(); return true; }
  if (document.getElementById('settings-bg')){ closeSettings(); return true; }
  if (suspended){ setSuspended(false); return true; }
  if (cutToks.length){ clearCut(); return true; }
  return false;
}

// Super Escape's half here: a rename or the substitute field cancelled the way their own
// Esc cancels, the palette search cleared, then every escapeHere layer in one go.
function escapeAllHere(){
  const box = editing && document.querySelector(".name-input");
  if (box) box.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
  if (substOpen()) closeSubstitute();
  const pal = document.getElementById("pal-search");
  if (pal && document.activeElement === pal){ pal.value = ""; pal.blur(); renderPalette(); }
  for (let i = 0; i < 10 && escapeHere(); i++);
}

document.addEventListener("keydown", e=>{
  // Escape / Deselect pressed in one of this page's fields (rename, substitute, palette
  // search) is Esc for that field — its own handler, so each cancels its own way. A
  // printable bare key bound to it still types there.
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)
      && !(e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey)
      && Keys.lookup(fusionKeys, e) === "EscapeOrDeselectCmd"){
    e.preventDefault();
    e.target.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    return;
  }
  if (editing) return;                 // rename input handles its own keys
  if (substOpen()) return;             // the substitute field handles its own keys
  if (e.target && e.target.id === "pal-search"){   // the palette's search box
    if (e.key === "Escape"){ e.target.value = ""; e.target.blur(); renderPalette(); }
    return;
  }

  if (ctxOpen()){                      // context menu open: Esc closes it, nothing else
    if (e.key === 'Escape'){ e.preventDefault(); hideCtx(); }
    return;
  }
  if (swReplace && e.key === 'Escape'){ e.preventDefault(); setReplace(null); return; }

  // The refusal dialog says one thing and has one button, so every key that could
  // mean "yes, I read it" dismisses it — and nothing else gets through while it is up.
  if (blockedOpen()){
    if (blockedGo && e.key === 'Enter'){ e.preventDefault(); runBlockedGo(); }
    else if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' '){
      e.preventDefault(); hideBlocked();
    }
    return;
  }
  if (document.getElementById('settings-bg')){      // settings open: only Esc closes it
    if (e.key === 'Escape' && !hotkeyPanel.capturing()){ e.preventDefault(); closeSettings(); }
    return;
  }

  // 1) Forwarded Fusion toggle/close hotkeys take priority (they have modifiers).
  const hk = matchesHotkey(e);
  if (hk){
    e.preventDefault();
    if (hk === "close"){ try{ window.webkit.messageHandlers.close.postMessage(""); }catch(_){} return; }
    if (TOGGLE_CMD[hk]) post({action:"command", cmd_id:TOGGLE_CMD[hk]});
    return;
  }

  // 2) The ⌘ combos used to be hard-coded here — ⌘R rename, ⌘⇧E toggle, ⌘D delete,
  // ⌘G folder, ⌘, settings. They are ordinary registry entries now (see ACTION_IMPL),
  // so they come through the same lookup as every other key and rebinding one in the
  // Shortcuts window moves it here too.

  // 2) ⌘X / ⌘V — cut and paste parameters between groups. These two are WINDOW-LOCAL
  // and deliberately stay out of Fusion's keymap, which is otherwise the rule here.
  // Two reasons, both about ⌘V in particular: binding it in Fusion would put a global
  // shortcut on the chord every text field in Fusion needs, and neither action means
  // anything in the viewport anyway — a parameter is not something you can select
  // there. So they belong with Enter/⌫/Escape in the window-local set rather than with
  // the letters. Checked BEFORE the keymap lookup, so a Fusion binding on ⌘X can't
  // cross while this window has the keyboard. (Rename is handled by the `editing`
  // guard at the top: while a name field is up, ⌘X/⌘V are the Edit menu's.)
  // 2.2) ⇧↑ / ⇧↓ — move what is picked up and down its own list. Window-local for the
  // same reason ⌘X/⌘V are: the ordering overlay exists only here (Fusion has no API to
  // reorder these rows), so the chord can mean nothing in the viewport. Checked before
  // the keymap lookup, so a Fusion binding on ⇧↑ cannot cross while this window has the
  // keyboard — and the bare arrows are untouched, which is deliberate (see below).
  if (e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey &&
      (e.key === "ArrowUp" || e.key === "ArrowDown")){
    e.preventDefault();
    moveSelectionInList(e.key === "ArrowDown" ? 1 : -1);
    return;
  }

  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey){
    const ck = (e.key || "").toLowerCase();
    // With nothing of this window's to act on, ⌘V / ⌘C are the Sketch Clipboard's, as
    // they are in the viewport (remapd sends them there while Fusion is idle): a paste
    // with no cut pending places the last sketch copied — or refuses, if the newest
    // thing taken was a cut here that has already gone (Clipboard Owner.json) — and a
    // copy with no parameter picked copies the sketch the cursor has selected.
    if (ck === "x"){ e.preventDefault(); cutSelection(); return; }
    if (ck === "v"){
      e.preventDefault();
      if (cutToks.length) pasteCut();
      else post({action:"command", cmd_id:"SketchClipboardPasteCmd"});
      return;
    }
    // ⌘C — copy a parameter's name onto the system clipboard, window-local for the
    // same reason ⌘X/⌘V are: it means nothing in the viewport, Fusion's own Copy
    // owns the chord for everything else, and this window has to see it before any
    // keymap lookup could hand it off.
    if (ck === "c"){
      e.preventDefault();
      if (cuttableToks().some(paramNameOf)) copySelectionName();
      else post({action:"command", cmd_id:"SketchClipboardCopyCmd"});
      return;
    }
  }

  // 2.5) Anything in Fusion's keymap — bare keys included. ONE lookup serves both
  // directions: an id naming one of our own actions runs right here in JS, anything
  // else is forwarded to Fusion. That is what removes the second keymap. The map is
  // re-fetched on a timer, so a shortcut changed in the Shortcuts window takes effect
  // without reloading anything. (Cmd+R/Cmd+E are consumed by the Swift shell before
  // they reach here, so they stay window-local.)
  const k = (e.key||"").toLowerCase();
  // Return and Space are held back from FUSION's commands (Return would cross as
  // CommitCommand), but one of our own actions bound to them runs: Browser: Edit
  // rebound to Space in the Shortcuts window has to take Space here too.
  if ((k === "enter" || k === " ") && !e.metaKey && !e.ctrlKey && !e.altKey){
    const cid = Keys.lookup(fusionKeys, e);
    if (cid && ACTION_IMPL[cid]){ e.preventDefault(); ACTION_IMPL[cid](e); return; }
  }
  if (!WINDOW_STRUCTURAL_KEYS.has(k)){
    // Space held spells "space+<key>", which /fusionkeys maps to what remapd's
    // `fusion-space` row makes it in the viewport (Keys' HELD SPACE).
    const cid = Keys.lookup(fusionKeys, e);
    if (cid){
      e.preventDefault();
      if (ACTION_IMPL[cid]) ACTION_IMPL[cid](e);
      else post({action:"command", cmd_id:cid});
      return;
    }
  }

  // 3) The keys held back above, handled here. No letters left: C/R/F/D/A all reach this
  // window through Fusion's keymap now, so rebinding one in the Shortcuts window moves it
  // on BOTH surfaces at once. What remains is the structural keys, which are
  // window-local by nature and stay written down here. The arrow keys are deliberately
  // NOT here: C/R/F/D are the stepping keys, and arrows in this window used to shadow
  // whatever Fusion (or the viewport's orbit) meant by them.
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  // Bare Return is Edit only while Browser: Edit has no key of its own in the keymap
  // (or the keymap hasn't loaded) — once it's rebound, Return stops meaning it here.
  if (k === "enter"){
    e.preventDefault();
    if (!Object.values(fusionKeys).includes("BrowserEditCmd")) activateCursor();
  }
  else if (k === "backspace" || k === "delete"
           || e.code === "Backspace" || e.code === "Delete"){
    e.preventDefault(); deleteFolderCursor();
  }
  else if (k === "escape"){
    // Suspend mode first: Escape backing out of a temporary mode takes priority over
    // whatever else it would otherwise have closed, same as any modal escape hatch.
    if (suspended){ setSuspended(false); return; }
    hideCtx(); clearCut();
  }
});

// WebKit eats ⌘R (reload) and ⌘E (Find) before the page ever sees a keydown, so the
// Swift shell intercepts them and hands them here. It passes the CHORD, not an action:
// that WebKit swallows those two chords is a fact about WebKit, but what they MEAN is
// the keymap's business, so the shell no longer names an action and dispatch happens
// through the registry like any other key.
window.__key = function(chord){
  const cid = fusionKeys[normSeq(chord || "")];
  if (!cid) return;
  if (ACTION_IMPL[cid]) ACTION_IMPL[cid](null);
  else post({action:"command", cmd_id:cid});
};
// Kept for the older shell (and for the Params pane, which has no __key of its own):
// a build that still calls these by name keeps working.
window.__toggleVis = toggleVisCursor;
window.__rename    = beginRename;
// Invoked by the FusionDock shell when it detects a double-click over the browser pane
// (WebKit is unreliable at delivering rapid synthetic clicks to the web content, so the
// shell watches the native events and calls this). A double-click anywhere in the browser
// pane — empty space OR on a row — jumps to the top level.
window.__dockDblClick = function(){ activateRootUI(); };
// A click the dock shell took natively while FusionValue holds the keyboard (the page
// gets no web events while its app is inactive, and activating would take the keyboard
// off the value box). Same as an ordinary click on whatever row is at (x, y) — so a
// parameter goes into the value (handleClick's /insert), a group opens or shuts.
window.__valueClick = function(x, y){
  const el = document.elementFromPoint(x, y);
  const row = el && el.closest(".row");
  if (!row || row.classList.contains("placeholder")) return;
  handleClick({ tok: row.dataset.tok, type: row.dataset.type, x, y });
};

// The cursor deliberately SURVIVES losing focus. It used to be nulled here so it never
// lingered while you worked in Fusion, but C/R can now be pressed with the viewport
// focused — and this window is blurred the whole time that is true, so a cursor that
// died on blur could never advance past its first row. It is the same cursor either way,
// which is the point: one action, one binding, whichever surface you press it on.

// ── settings / hotkeys (Cmd-,) ───────────────────────────────────────────────
// Shared actions use the same ids as the Parameters window and a shared settings
// file, so a binding added in one window applies in the other. Custom bindings are
// additive (defaults always stay), so nothing existing can break.
const ACTIONS = {
  // The letters are gone from these defaults on purpose: C/R/F/D are Fusion bindings now
  // (see ACTION_IMPL), so listing them here would be the second keymap all over again —
  // rebind one in the Shortcuts window and this panel would still claim the old letter.
  // Only the arrow aliases, which Fusion never sees, are this panel's to describe.
  'nav-down': { label:'Move selection down', scope:'shared',  def:['↓'], run:()=>moveCursor(1) },
  'nav-up':   { label:'Move selection up',   scope:'shared',  def:['↑'], run:()=>moveCursor(-1) },
  'expand':   { label:'Expand / into',       scope:'shared',  def:['→'], run:()=>expandOrInto() },
  'collapse': { label:'Collapse / out',      scope:'shared',  def:['←'], run:()=>collapseOrOut() },
  // Rename's key is the GENERAL chord (fusion_ui/general.json): it reaches this
  // window through Fusion's keymap (BrowserRenameSelectionCmd → ACTION_IMPL), so
  // here it is only shown, read from /general, never spelled.
  'rename':   { label:'Rename',              scope:'shared',  def:[], general:'rename', run:()=>beginRename() },
  'settings': { label:'Open settings',       scope:'shared',  def:['⌘,'],    run:()=>openSettings() },
  'browser:activate':   { label:'Activate / select / edit', scope:'browser', def:['↵'],   run:()=>activateCursor() },
  'browser:toggle-vis': { label:'Toggle visibility',        scope:'browser', def:['⌘⇧E'], run:()=>toggleVisCursor() },
  'browser:new-folder': { label:'New folder',               scope:'browser', def:['⌘G'],  run:()=>addFolder() },
  'browser:new-param-group':{ label:'New parameter group',  scope:'browser', def:[],      run:()=>addFolder(true) },
  'browser:file-bounds':{ label:'File sketches', scope:'browser', def:[],      run:()=>fileAllBounds() },
  'browser:del-folder': { label:'Explode group',            scope:'browser', def:['⌫'],   run:()=>deleteFolderCursor() },
  // Ungroup's key is a GENERAL chord too, but window-only (no Fusion command carries
  // it), so this panel dispatches it itself — `dispatch`, see fusion_ui/keys.js.
  'ungroup':  { label:'Ungroup',             scope:'shared',  def:[], general:'ungroup', dispatch:true, run:()=>ungroupSelection() },
  'browser:ignored-folders':{ label:'Show ignored folders', scope:'browser', def:[],      run:()=>toggleIgnoredFolders() },
  'browser:bounds-folders': { label:'Show bounds folders',  scope:'browser', def:[],      run:()=>toggleBoundsFolders() },
  'browser:master-folders': { label:'Show master folders',  scope:'browser', def:[],      run:()=>toggleMasterFolders() },
  'browser:sketch-groups':  { label:'Show sketches',        scope:'browser', def:[],      run:()=>toggleSketches() },
  'browser:bodies-group':   { label:'Show Bodies group',    scope:'browser', def:[],      run:()=>toggleBodiesGroup() },
  'browser:geometry-group': { label:'Show Geometry group',  scope:'browser', def:[],      run:()=>toggleGeometryGroup() },
  'browser:origins':        { label:'Show origins',         scope:'browser', def:[],      run:()=>toggleOrigins() },
};
// Fusion-owned shortcuts shown read-only (managed in Fusion's keyboard settings).
const FUSION_KEYS = [
  ['close','Open / close this window'], ['bodies','Toggle bodies'],
  ['sketches','Toggle sketches'], ['origins','Toggle origins'], ['isolate','Isolate active'],
];

// The dialog, the custom bindings and the general chords are fusion_ui/keys.js's
// panel — the same one the Params window uses. A general chord is only SHOWN here:
// it reaches this window through Fusion's keymap (see ACTION_IMPL).
const hotkeyPanel = Keys.panel({
  api: API,
  actions: ACTIONS,
  sections: [['Shared (both windows)', 'shared'], ['Browser window', 'browser']],
  blocked: () => !!document.getElementById('settings-bg') || !!editing
                 || (document.activeElement || {}).id === 'pal-search',
  // Read-only Fusion-managed shortcuts.
  extraHtml: ({ esc, keyRow }) => {
    const fk = FUSION_KEYS.filter(([n]) => hotkeys[n]);
    if (!fk.length) return '';
    return `<div class="set-sec">Fusion-managed (edit in Fusion)</div>` +
      fk.map(([n, label]) => keyRow(label, `<span class="key def">${esc(seqLabel(hotkeys[n]))}</span>`)).join('');
  },
  buttonClass: 'sbtn',
});
function openSettings(){ hotkeyPanel.open(); }
function closeSettings(){ hotkeyPanel.close(); }

// ── boot ─────────────────────────────────────────────────────────────────────
hotkeyPanel.load();
document.addEventListener('keydown', hotkeyPanel.onCaptureKey, true);
document.addEventListener('keydown', hotkeyPanel.onCustomKey, true);
// The naming convention's vocabulary. Once, at boot, and again every so often so a
// type added in Control Room ▸ Fusion ▸ Parameter Names starts grouping without a
// reload. Until it lands, every name reads as unstructured and the tree is exactly
// what it was before name groups existed — no half state.
loadParamNaming(); setInterval(loadParamNaming, 30000);
pollParams();  setInterval(pollParams, 700);
pollHotkeys(); setInterval(pollHotkeys, 3000);
setInterval(hotkeyPanel.loadGeneral, 5000);      // a verb changed in Control Room takes effect here
setSuspended(false);   // a reloaded window starts unsuspended — make Fusion agree
pollState();  setInterval(pollState, 80);
setInterval(pollFeats, 200);                      // only fetches while a body is open
renderPalette(); pollAppearances();               // the appearances palette, if left open
