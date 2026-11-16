"use strict";
/* ─────────────────────────────────────────────────────────────────────────────
   The dialog.

   State comes from GET /state (polled); actions go to POST /cmd. The native window
   is shown, hidden and positioned by the host app — the page asks for that through
   webkit.messageHandlers.session, because a web page cannot move its own window.

   Key model, in full:
     Esc            cancel the whole dialog
     Tab / ⇧Tab     cycle the two fields, wrapping in both directions
     ↑ ↓            move through the suggestion list (↓ opens it when closed)
     Enter          accept the highlighted suggestion if the list is open with one
                    highlighted; otherwise commit, from either field. Tab is the way to
                    the name field — see the Enter handler.
     ⇧Enter         newline in the value field (it is the multi-line one)
   ──────────────────────────────────────────────────────────────────────────── */

const API = "";
// The other windows' servers this one talks to directly (ports: OPS.md), in one place.
const PARAMS_API    = "http://127.0.0.1:8731";   // Parameters: /set, for a group rename
const BROWSER_API   = "http://127.0.0.1:8732";   // Browser: /uiaction, /paramshow
const SHORTCUTS_API = "http://127.0.0.1:8733";   // Shortcuts: the command catalog, /press
// Control Room's own server — reached directly, cross-origin, for the per-row sheet's
// Shortcut row (see "The per-row sheet" below). Bound to 127.0.0.1 same as this one.
const FUSION_API    = "http://127.0.0.1:8735";

const $      = id => document.getElementById(id);
const panel  = $("panel"),  head = $("head"),  dTitleEl = $("dtitle");
const valueEl= $("value"),  nameEl = $("pname");
const fValue = $("fvalue"), fName  = $("fname");
const acEl   = $("ac"),     errEl  = $("err");
const prevEl = $("preview"), infoEl = $("sinfo");
const fAngle = $("fangle"), angleEl = $("angle");
const fFlex = $("fflex"), flexOnEl = $("flexon"), flexMinEl = $("flexmin"), flexMaxEl = $("flexmax");
const fSName = $("fsname"), sNameEl = $("sname");   // the plane box's sketch name
const sameEl = $("same"),   pickEl = $("pick");
const fSteps = $("fsteps"), stepCrumbEl = $("stepcrumb"), stepChoicesEl = $("stepchoices");
const stepTitleEl = $("steptitle"), stepLabelEl = $("steplabel"), stepErrEl = $("steperr");
const sPanel = $("spanel"), sQueryEl = $("squery"), sResultsEl = $("sresults");
const sSheetEl = $("ssheet");
const vPanel = $("vpanel"), vTitleEl = $("vdtitle"), vConfirmEl = $("vconfirm");
const vBodyEl = $("vbody"), vErr = $("verr");
const mPanel = $("mpanel"), mExprEl = $("mexpr"), mEditEl = $("medit"), mNameEl = $("mname"), mErr = $("merr");
const mTitleEl = $("mdtitle");
const mkBtn  = $("mkparam");

let params      = [];      // autocomplete source, refreshed from /state
let acFiltering = true;    // situation/unit/frame filters on the list — the right-click
                           // menu's "Filter suggestions"; back on every session
let unit        = "";      // the dimension's unit — what a new parameter is created in
let axis        = "";      // 'X' | 'Y' | 'Z' | ''
let datum       = "";      // 'zero' | a face name | '' — what the dimension measures FROM
let datumFaces  = [];      // bound faces sitting AT the origin datum too (['top'])
let symmetric   = false;   // is the measured axis symmetric on this component?
let end         = "";      // 'high' | 'low' | '' — which side of the origin along `axis`.
                           // Together they are the SITUATION a parameter family is
                           // resolved against: (Y, high) picks EdgeBackOffsetY out of
                           // the family EdgeOffset.
let situAxes    = [];      // every world axis this dimension is within 45° of (45°
                           // itself included — see namingSituation()), falling back to
                           // [axis] when the add-in sends only the single-axis fields.
let situEnds    = {};      // {axis: end} for each of situAxes, from the add-in's own
                           // per-axis _end_of(); falls back to {axis: end}.
let lastSeq     = null;    // session edge-detect
let uiStamp     = null;    // mtime of the files this page was built from, so it can
                           // notice it has gone stale — see poll()
let lastErrSeq  = 0;
let making      = null;    // {a, b, expr} while #mpanel is up — see "Make a parameter"
let makePending = "";      // the name asked for, until Fusion says it exists
let lastMadeSeq = 0;
let lastInsertSeq = null;  // a Browser click to put in at the caret — see insertParam
let isOpen      = false;
let acItems     = [];      // current suggestions
let acIndex     = -1;      // highlighted suggestion, -1 = none
// Has the list been STEPPED INTO — an arrow key, or a row under the pointer? The mark
// on a row says "this is the one", and that is only true once you have said so. A list
// that has merely opened marks nothing, though acIndex is still 0 and Enter still takes
// it: the default is a convenience, not a choice, and it stops looking like one.
let acTouched   = false;
let committing  = false;   // a commit is in flight; ignore further Enters
let isDriven    = false;   // read-only dimension: the sketch's constraints own its value
let drivenExpr  = "";      // …and what it read at open — commit refuses an edited one
// 'dim'       — editing a selected sketch dimension; the name field is an optional extra.
// 'param'      — making a user parameter and nothing else, so the name is compulsory and
//                the window opens centred rather than beside a dimension on the canvas.
// 'editparam'  — editing a parameter that already exists (the Browser's Enter / F on a
//                parameter row). Both boxes come prefilled; changing the name renames it.
// 'sketch'     — placing a sketch: one line, a datum word and how far in from it. Same
//                panel with the name row given over to showing what it resolved to.
let mode        = "dim";
let nameRequired = false;
// The datum words this component offers, from its bounds: a symmetric axis has a
// midline and a side, an asymmetric one has two named faces. Computed on the Fusion
// side, because only it knows the bounds.
let datums      = [];
let curDatum    = null;    // the entry the first word names, or null
let redefine    = false;   // moving the plane an existing sketch already sits on
let flat        = false;   // Project Bounds' key: no angle offered at all, row hidden
let sketchToken = "";      // that sketch, for the commit
let origName    = "";      // the name this session started with — the one taken name
                           // that is not a clash, since it is this parameter's own

/* ── Explicit tags ──────────────────────────────────────────────────────────
   The naming convention carries the situation IN the name, and the engine reads it
   off — WingEdgeOffsetY is a Y parameter and nothing has to be told so. But plenty of
   perfectly good names say nothing: Steel, Clearance, GlueTape, Thickness. Those are
   the ones the list gets wrong, because a parameter that says nothing matches every
   situation and so turns up everywhere.

   A tag is that missing word, stored beside the parameter instead of inside its name.
   It feeds exactly the same machinery the derived tags do — see analysisOf, which
   merges the two — so tagging Thickness with Z narrows every Z dimension's list and
   drops it from the X ones, with no second set of rules anywhere.

   Kept per DOCUMENT (two files can each have a Thickness and mean different axes by
   it) in fusion_ui/tags.json, written by this window's server. Nothing about it
   touches the Fusion document: it is a note about a parameter, not a change to it. */
let tagMap = {};           // {paramName: {axis: 'X', …}} for the current document
let tagDoc = null;         // the document those tags were fetched for
let vocabSpec = null;      // variables.json, for the words a tag may take

function host(name, body){
  if (name === "session" && body && body.open) noteOpenCaret();
  try { window.webkit.messageHandlers[name].postMessage(body); } catch (_) {}
}

/* Where the panel actually is inside its (much larger, transparent) window. The host
   makes everything OUTSIDE this rectangle click- and scroll-through, so a 2-finger zoom
   or a click over the empty space reaches Fusion's canvas rather than this window's
   web view. The union of every visible top-level element, so a menu or dropdown that
   hangs below the panel is still ours. Re-sent whenever the DOM or a size changes. */
(function(){
  let queued = false, last = "";
  function send(){
    queued = false;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    const kids = document.body ? document.body.children : [];
    for (let i = 0; i < kids.length; i++){
      const el = kids[i];
      if (el.tagName === "SCRIPT" || el.tagName === "STYLE") continue;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.pointerEvents === "none") continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
      x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    }
    const rect = x1 > x0 ? { x: x0, y: y0, w: x1 - x0, h: y1 - y0 } : null;
    const key = JSON.stringify(rect);
    if (key === last) return;
    last = key;
    host("rect", rect || { w: 0, h: 0 });
  }
  function queue(){ if (!queued){ queued = true; requestAnimationFrame(send); } }
  window.addEventListener("load", function(){
    try {
      const mo = new MutationObserver(queue);
      mo.observe(document.body, { subtree: true, childList: true, attributes: true });
      const ro = new ResizeObserver(queue);
      ro.observe(document.body);
      for (const el of document.body.children) ro.observe(el);
    } catch (_) {}
    queue();
  });
  window.addEventListener("resize", queue);
})();

/* Every session places its caret while the window is still hidden and the app inactive
   — the host only shows and activates it on the message above. Activation then lands
   the caret back at 0, chips to its right (the same WebKit reset __refocus meets), but
   only sometimes. A one-shot put-back from the host (__shown) did not hold: the reset
   arrives from the web process's own focus change, asynchronously, and as often as not
   AFTER the host's evaluateJavaScript had already run — so it lost the race.

   So the caret is GUARDED rather than restored once: from the moment it is placed until
   the user's own first keystroke or click in the page, any collapse to 0 that nobody
   asked for is put back on the next frame. Only a collapse to 0 — the reset's exact
   signature — is undone, so nothing the page itself does with the selection is fought;
   and a real Home / click-at-the-start is a key or a click, which lifts the guard first.
   __refocus arms the same guard for the trip back from the viewport.

   While it is armed the guard IS where the caret belongs — __refocus restores from it,
   not from lastSel. The host's reclaim answers our own activation with a __refocus a
   beat after __shown, and lastSel was still the 0 of the focus that preceded the
   caret being placed (a hidden page gets no selectionchange for a setSelectionRange),
   so __refocus put the caret at 0 itself and then re-armed the guard AT 0 — which
   switches it off. That was the "always left of the chips" bug. The guard also follows
   the page's own deliberate moves (anything but the reset), so it never holds a stale
   position — an insertParam from a Browser click stays where it went. */
let caretGuard = null;
// The session opened with the value box's whole value selected as text (a measure the
// pointer left, see openSession) and the user hasn't touched the page since: every
// put-back and __refocus restores that selection rather than a caret.
let openSelectAll = false;
function guardCaret(el){
  caretGuard = null;
  if (!el || el.selectionStart == null) return;
  try { caretGuard = { el, a: el.selectionStart, b: el.selectionEnd }; } catch (_) { return; }
  // Where the caret was placed is where it was last seen — whatever trackFocus missed.
  lastFocus = el; lastSel = [caretGuard.a, caretGuard.b];
}
function noteOpenCaret(){ guardCaret(document.activeElement); }
function enforceCaret(){
  const c = caretGuard;
  if (!c) return;
  if (!isOpen || !c.el.isConnected){ caretGuard = null; return; }
  if (c.a === 0 && c.b === 0) return;
  const el = c.el, act = document.activeElement;
  // The page moving focus to another field on purpose ends the guard; focus merely
  // LOST (nothing, or the body) is the reset and is put back.
  if (act !== el && act && act !== document.body){ caretGuard = null; return; }
  const allHeld = openSelectAll && el === valueEl;
  const reset = act !== el ||
                (el.selectionStart === 0 && el.selectionEnd === 0 && el.value.length > 0) ||
                (allHeld && !(el.selectionStart === 0 && el.selectionEnd === el.value.length));
  // The page moving the caret itself: the guard follows, rather than holding the old spot.
  if (!reset){ c.a = el.selectionStart; c.b = el.selectionEnd; return; }
  if (document.activeElement !== el) el.focus();
  const len = el.value.length;
  const a = Math.min(c.a, len), b = Math.min(c.b, len);
  // At the end with nothing held is chips.js's caretAtEnd — its state goes back too, as
  // does a session's whole-value selection (selectAllAsText's open edit).
  if (allHeld) selectAllAsText(el);
  else if (!(a === len && b === len && typeof Chips !== "undefined" && Chips.caretAtEnd(el)))
    try { el.setSelectionRange(a, b); } catch (_) {}
  refreshFocusStyle();
}
let enforceQueued = false;
function queueEnforceCaret(){
  if (!caretGuard || enforceQueued) return;
  enforceQueued = true;
  requestAnimationFrame(() => { enforceQueued = false; enforceCaret(); });
}
document.addEventListener("selectionchange", queueEnforceCaret);
window.addEventListener("focus", () => { enforceCaret(); queueEnforceCaret(); });
// The user's own input is the end of it: from here the caret is theirs.
for (const ev of ["keydown", "mousedown"])
  document.addEventListener(ev, () => { caretGuard = null; openSelectAll = false; }, true);
window.__shown = () => { enforceCaret(); queueEnforceCaret(); };
function post(body){
  return fetch(API + "/cmd", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).catch(() => {});
}

/* ── Name conventions, and the family behind them ───────────────────────────
   Josh's convention carries the SITUATION in the name: EdgeBackOffsetY is the back Y
   edge, EdgeOffsetX the X one. fusion_ui/naming.js strips those situational words to
   leave a STEM, so names that differ only in their situation become one family:

       EdgeBackOffsetY  EdgeFrontOffsetY  EdgeOffsetX  EdgeOffsetZ
           └─────────── family "EdgeOffset", four members ────────┘

   The dimension being edited says which situation we are in — the axis it measures
   along and which side of the origin it sits on — and that picks the member. So you
   type "EdgeOffset" and Fusion is handed EdgeBackOffsetY.

   THE REAL NAME IS WHAT GETS INSERTED, not the stem. Two reasons: Fusion only knows
   real names, and seeing which member you got is the only way to notice the situation
   was read wrongly. The row shows both — the stem you typed, then what it resolved to.

   Every real name stays in the list as well, so a stem is an extra way in and never
   the only one: typing EdgeBackOffsetY out in full still matches it exactly, and wins,
   because an exact match outranks a fuzzy one.                                       */

let vocabReady = false;

/* The engine is served from fusion_ui by this window's server. If it is missing or the
   fetch fails, vocabReady stays false and every family path below is skipped — the list
   degrades to plain parameter names, which is exactly what it was before families. */
async function loadNaming(){
  try {
    if (typeof Naming === "undefined") return;
    // Fetched here as well as through the engine, because the TAG UI needs the words
    // themselves — which axes exist, and what each one is called — and offering a
    // hard-coded X/Y/Z beside a vocabulary file that defines them would be two
    // answers to one question.
    vocabSpec = await (await fetch(API + "/variables.json", { cache: "no-store" })).json();
    Naming.setVocabulary(vocabSpec);
    vocabReady = true;
    invalidateEntries();
  } catch (_) { vocabReady = false; }
}

// The structured feature/type/anchor convention — a separate engine from the one
// above, only used by the name stepper (and the situational filter it feeds, see
// conflictsWithSituation). Same degrade-quietly rule: missing or unreachable just
// means new-style parameters are treated as plain names and the stepper stays hidden.
let paramNamingReady = false;
async function loadParamNaming(){
  try {
    if (typeof ParamNaming === "undefined") return;
    const spec = await (await fetch(API + "/param_naming.json", { cache: "no-store" })).json();
    ParamNaming.setVocabulary(spec);
    paramNamingSpec = spec;   // kept for anchor axis/end lookups (see anchorInfo)
    paramNamingReady = true;
    invalidateEntries();
  } catch (_) { paramNamingReady = false; }
}

// Words exempt from that convention (fusion_ui/param_exempt.json, edited in Control
// Room ▸ Fusion ▸ Exempt Names). Re-read on every session open so an edit there takes
// without a relaunch; unreachable keeps the last list.
let paramExemptWords = [];
async function loadParamExempt(){
  try {
    const doc = await (await fetch(API + "/param_exempt.json", { cache: "no-store" })).json();
    paramExemptWords = (doc.words || []).filter(w => typeof w === "string" && NAME_RE.test(w));
  } catch (_) {}
}

/* THE SITUATION THE NAMING RULES JUDGE AGAINST: every world axis the dimension is
   within 45° of (45° itself included — a dimension running exactly diagonal between
   two axes matches BOTH their pools), which end on each, and what the dimension is
   measured FROM. Each type in param_naming.json says when it is suggested in terms of
   these (Control Room ▸ Fusion ▸ Parameter Names sets them), and both questions below
   go through that one rule set.

   axis/end are the add-in's nearest-axis answer, kept for anything that only ever
   wanted one; situAxes/situEnds are the fuller 45° pool the add-in's own
   _situation_of() computes (Dimension Value.py). ParamNaming.fits() falls back to
   axis/end when a caller sends neither, so this keeps working even before the add-in
   side of this is deployed.

   currentFrame() already works the datum out — including the flip a span word in the
   expression causes — and says "origin", a bound word, or "" for a dimension whose
   datum isn't known yet. Anything that isn't the origin is a bound as far as these
   rules are concerned; "" disqualifies nothing. */
function namingSituation(){
  const frame = currentFrame();
  // situAxes/situEnds matter only when `axis` itself is blank — a single definite axis
  // is already the unambiguous answer (see _situation_of), and the pool exists purely
  // to say which SEVERAL axes a diagonal dimension (no one nearest axis) is between.
  const axes = axis ? [axis] : situAxes.slice();
  const ends = axis ? (end ? { [axis]: end } : {}) : Object.assign({}, situEnds);
  // A face at the origin makes it both datums at once — the faces ride along so
  // ParamNaming.fits() accepts a bounds type anchored on one (see datumFaces).
  const faces = (frame === "origin") ? datumFaces.slice() : [];
  return { axis: axis, end: end, axes: axes, ends: ends, faces: faces,
           datum: !frame ? "" : (frame === "origin" ? "origin" : "bounds") };
}

/* Is `parsed` (a ParamNaming.parse() result) wrong for the situation? */
function paramNamingSituationBad(parsed){
  return !ParamNaming.fits(parsed, namingSituation());
}

/* The words one dimension of the vocabulary offers — ["X","Y","Z"] for the axis. */
function tagWords(dimension){
  if (!vocabSpec || !vocabSpec.dimensions) return [];
  for (const d of vocabSpec.dimensions){
    if (d.id === dimension) return (d.values || []).map(v => v.word);
  }
  return [];
}

/* What a tag word means: {dimension, axis, end}. The vocabulary already says, which is
   why a direction tag can be stored later without anything here learning six words. */
function tagMeaning(word){
  if (!vocabSpec || !vocabSpec.dimensions) return null;
  const w = String(word || "").toLowerCase();
  for (const d of vocabSpec.dimensions){
    for (const v of (d.values || [])){
      if (String(v.word).toLowerCase() === w){
        return { dimension: d.id || "", axis: v.axis || "", end: v.end || "" };
      }
    }
  }
  return null;
}

function tagsOf(name){ return tagMap[name] || null; }

/* Fetched per document, and again after every change so what is on screen is what is
   on disk. Cheap: one small file, read when the session opens rather than polled. */
async function loadTags(doc){
  tagDoc = doc;
  try {
    const r = await (await fetch(API + "/tags", { cache: "no-store" })).json();
    tagMap = (r && r.tags) || {};
  } catch (_) { tagMap = {}; }
  invalidateTags();
}

/* Tags feed the analysis, and the analysis is cached — so changing one has to throw
   both away, along with the entry list built from them. */
function invalidateTags(){
  analysisCache.clear();
  invalidateEntries();
  renderAC();
  refreshChips();
}

/* Toggle one axis tag on whichever parameter is being pointed at: the highlighted
   suggestion if the list is open, otherwise the chip the caret is on. Returns false
   when neither names a parameter, so the key falls through to whatever else wants it.

   Written optimistically — the map is updated before the POST answers, so the row and
   the chip restripe on the keystroke rather than a round trip later. The reply is
   authoritative and replaces it. */
function toggleAxisTag(word){
  const name = tagTarget();
  if (!name) return false;
  const cur = Object.assign({}, tagMap[name] || {});
  const meaning = tagMeaning(word);
  const dim = (meaning && meaning.dimension) || "axis";
  if (cur[dim] === word) delete cur[dim];
  else cur[dim] = word;
  if (Object.keys(cur).length) tagMap[name] = cur; else delete tagMap[name];
  invalidateTags();
  fetch(API + "/tags", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ name: name, tags: cur })
  }).then(r => r.json()).then(r => {
    if (r && r.ok && r.tags){ tagMap = r.tags; invalidateTags(); }
  }).catch(() => {});
  return true;
}

/* Which parameter a tag key is about. The highlighted suggestion first — you are
   looking straight at it — and the chip under the caret otherwise, which is how an
   expression already typed gets tagged. Only real parameters: a function or a constant
   is Fusion's, not the document's, and has nothing to be told. */
function tagTarget(){
  if (acItems.length && acIndex >= 0){
    const e = acItems[acIndex].entry;
    if (!e.kind && !e.datum && params.some(p => p.name === e.target)) return e.target;
  }
  if (typeof Chips !== "undefined" && document.activeElement){
    try {
      const c = Chips.chipAtCaret(document.activeElement);
      if (c && c.kind === "param") return c.name;
    } catch (_) {}
  }
  return null;
}

/* CamelCase word split, for match highlighting. Same rule as the engine's. */
const WORDS = /[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+/g;
function wordsOf(name){
  const out = [];
  let m;
  WORDS.lastIndex = 0;
  while ((m = WORDS.exec(name)) !== null) out.push([m[0], m.index]);
  return out;
}
/* Word start positions, for word-boundary matching. "BoxWidthX" → 0,3,8. */
function wordStarts(name){ return wordsOf(name).map(w => w[1]); }

/* Find `q` inside `name` VERBATIM, at a position that starts a word. Returns the
   matched indices (for highlighting) and where the match began, or null.

   Two conditions, and both matter:

   CONTIGUOUS. What you type has to be in the name as you typed it. "we" must not reach
   [W]ing[E]dgeConvexityZ by collecting the initials of two separate words — that name
   does not contain "we" at all, and treating scattered letters as a match makes the list
   feel like it is guessing.

   AT A WORD START. Every word in a parameter name begins with a capital, so a fragment
   landing in the middle of one — Po|we|rboard — is a coincidence of letters rather than
   something the name is called. Matching it buried the parameters actually being asked
   for.

       "we"      -> Wing[We]bThickness      the word Web
       "we"      -> Po we rboard            rejected, mid-word
       "we"      -> WingEdgeConvexityZ      rejected, not contiguous
       "offsetb" -> Edge[Offsetb]ackY       fine to run past a word end, having started one

   Every occurrence is tried, not just the first: a fragment can appear mid-word once and
   at a word start later on.                                                            */
function matchAnchored(name, q, starts){
  const nl = name.toLowerCase(), ql = q.toLowerCase();
  const isStart = new Set(starts);
  for (let at = nl.indexOf(ql); at >= 0; at = nl.indexOf(ql, at + 1)){
    if (!isStart.has(at)) continue;
    const hits = [];
    for (let i = 0; i < ql.length; i++) hits.push(at + i);
    return { hits, at };
  }
  return null;
}

/* A CAPITAL in what you type starts a new word. "bootD" is two pieces, "boot" and "D",
   and matches a name that has a word starting "boot" and, somewhere AFTER it, a word
   starting "d" — BootDoorWidth, but also BootLatchDoorWidth. Each piece is held to
   matchAnchored's rules (verbatim, at a word start); only the words BETWEEN pieces may
   be skipped. A fragment with no capital past its first letter is one piece, so this
   never widens the lowercase case — "we" still does not collect WingEdge's initials.
   Returns { hits, at } like matchAnchored, or null (also for a single piece). */
function queryPieces(q){ return q.match(/[A-Z]?[^A-Z]*/g).filter(Boolean); }
function matchWords(name, q, starts){
  const pieces = queryPieces(q);
  if (pieces.length < 2) return null;
  const nl = name.toLowerCase(), hits = [];
  let from = 0, first = -1;
  for (const piece of pieces){
    const pl = piece.toLowerCase();
    const at = starts.find(k => k >= from && nl.startsWith(pl, k));
    if (at === undefined) return null;
    if (first < 0) first = at;
    for (let i = 0; i < pl.length; i++) hits.push(at + i);
    from = at + pl.length;
  }
  return { hits, at: first };
}

/* ── The name as it is read ─────────────────────────────────────────────────
   The list SHOWS a parameter the way the Browser does — BootCeiling_Dimension_Span_X is
   "Boot Ceiling - Dimension - Width" — and what you type is matched against that reading
   as well as against the real name. The Space key commits in the value box, so a typed
   query is always one word: it is matched against the reading with its spaces and dashes
   squeezed out ("ceilingdimw" → Boot [Ceiling] - [Dim]ension - [W]idth), every word of
   the reading counting as a word start. Accepting still writes the real name.       */
function spokenOne(name){
  if (typeof ParamNaming === "undefined") return name;
  return ParamNaming.words(name) || name;
}
// Every parameter's reading at once, since whether a last level is a NAME or a group
// depends on its siblings (ParamNaming.nameIndex) — the same answer the Browser draws.
// The root's owner word (Dimension Value's / Bounds.py's ROOT_OWNER, squashed): never
// read — a parameter at the top level reads as if it had no owner at all.
const ROOT_OWNER_WORD = "TopLevel";
// `ownerFirst` (a Set of names, the suggestion list's bounds parameters) reads those as
// their component and their own leaf only — "Nose - Right" — see entries().
// `withOwner` (the suggestion list): every parameter reads with its component first,
// even one whose parent chip is hidden from typing, unless a word it already shows
// starts with it (BootCeiling in Boot) — so the list tells two components' "Width"s
// apart while the chip in the box still says just "Width".
// What a parameter's value reads as in a list. A driven-dimension LINK's expression is its
// dimension's own parameter — the link's name with the first letter's case flipped
// (Driven Link.py) — so shown raw it reads as the parameter naming itself; it shows the
// measurement instead, bracketed the way Fusion brackets a reference dimension.
function shownValue(p){
  if (p && p.linked && p.measured) return "(" + p.measured + ")";
  return (p && p.expression) || "";
}
function spokenNames(names, ownerFirst, withOwner){
  const out = new Map();
  if (typeof ParamNaming === "undefined"){ for (const n of names) out.set(n, n); return out; }
  const parsed = paramNamingReady ? names.map(n => ParamNaming.parse(n)) : [];
  const idx = ParamNaming.nameIndex(parsed.filter(Boolean));
  names.forEach((n, i) => {
    const parts = parsed[i];
    if (!parts){ out.set(n, spokenOne(n)); return; }
    // The parameter's own shown/hidden parts (the stepper's crumb chips, kept by the
    // Browser): name/type/direction as its row reads them, and `lv` — the parent and
    // group words hidden from typing (see typingHidden).
    const show = browserShow[n] || null;
    const lv = typingHidden(n);
    const owner = parts.levels[0];
    let levels = ownerFirst && ownerFirst.has(n) ? parts.levels.slice(0, 1)
      : ParamNaming.groupLevels(parts, idx).filter(w => !lv.includes(w));
    if (owner === ROOT_OWNER_WORD) levels = levels.filter(w => w !== ROOT_OWNER_WORD);
    else if (withOwner && owner &&
             !levels.some(w => w.toLowerCase().startsWith(owner.toLowerCase())))
      levels = [owner].concat(levels);
    out.set(n, levels
      .map(ParamNaming.words)
      .concat([ParamNaming.displayLeaf(parts, { index: idx, show })]).join(" - "));
  });
  return out;
}
/* HIDDEN FROM TYPING — right-click (or ⌘E) on a parent or group chip in the stepper's
   crumb hides that word from how the parameter is TYPED: its reading in the list and on
   its chip leaves it out, and it is matched only against that reading, never the real
   name — so a parameter whose parent and groups are hidden is typed by its own name.
   The Browser still files it in those groups; they are only left out here. Kept per
   parameter as `lv` (level words) in the Browser's show entry, beside name/type/dir,
   read from its /paramshow every session open (`loadBrowserShow`); `browserShowRev`
   keys the caches built from it. */
let browserShow = {}, browserShowRev = 0;
function typingHidden(name){
  const s = browserShow[name];
  return (s && Array.isArray(s.lv)) ? s.lv : [];
}
async function loadBrowserShow(){
  try {
    const sh = await (await fetch(BROWSER_API + "/paramshow", { cache: "no-store" })).json();
    const next = (sh && typeof sh === "object") ? sh : {};
    if (JSON.stringify(next) === JSON.stringify(browserShow)) return;
    browserShow = next; browserShowRev++;
    invalidateEntries();
    if (typeof Chips !== "undefined") Chips.refresh(valueEl);
  } catch (_) {}
}
// The reading a parameter CHIP shows (chips.js opts.label): spokenNames over the whole
// list, worked out once per list — `params` is replaced wholesale on every poll that
// changes it, so its identity is the cache key. A bounds parameter's chip reads as its
// list row does, component first ("Nose - Right"; a top-level one just "Right").
let chipLabelMap = null, chipLabelFor = null, chipLabelReady = false, chipLabelRev = -1;
let numberByName = null, numberByNameFor = null;
function chipLabel(name){
  if (isNumberParam(name)) return numberReading(name);
  if (chipLabelFor !== params || chipLabelReady !== paramNamingReady ||
      chipLabelRev !== browserShowRev){
    chipLabelMap = spokenNames(params.filter(p => !isNumberParam(p.name)).map(p => p.name),
      new Set(params.filter(p => isBoundsParam({ p })).map(p => p.name)));
    chipLabelFor = params;
    chipLabelReady = paramNamingReady;
    chipLabelRev = browserShowRev;
  }
  return chipLabelMap.get(name) || null;
}

/* A NUMBER PARAMETER (AddIns/Number Params.py): a typed number the add-ins made a
   parameter nobody names, Num_<8 hex>. Its chip reads as the number — without the
   field's own unit, as a typed one would ("10 mm" in a mm field is "10") — in its own
   colour (chips.js .chip-numparam), and it is never offered as a suggestion. Retyping
   the chip changes that parameter: the commit hands the old one back (plan()). */
const NUMBER_PARAM = /^Num_[0-9a-f]{8}$/;
function isNumberParam(name){ return NUMBER_PARAM.test(name || ""); }
function numberReading(name){
  if (numberByNameFor !== params){
    numberByName = new Map(params.filter(p => isNumberParam(p.name)).map(p => [p.name, p]));
    numberByNameFor = params;
  }
  const p = numberByName.get(name);
  if (!p) return null;
  const m = /^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*$/.exec(p.expression || "");
  if (!m) return (p.expression || "").trim() || null;
  const own = m[2] || "";
  return (!own || own === unit || (own === "mm" && !unit)) ? m[1] : m[1] + " " + own;
}
/* Score `q` against a reading, on the same scale and rules score() uses for the real
   name: whole, prefix, verbatim from a word start, word by word at the capitals typed.
   Returns { s, hits } — hits in the READING's own positions, for highlighting — or null. */
function matchSpoken(shown, q){
  const flat = [], map = [], starts = [];
  for (let i = 0; i < shown.length; i++){
    if (!/[A-Za-z0-9]/.test(shown[i])) continue;
    if (i === 0 || !/[A-Za-z0-9]/.test(shown[i - 1])) starts.push(flat.length);
    flat.push(shown[i]); map.push(i);
  }
  // Letters and digits only, like the reading — an opened short name's stand-ins too.
  const f = flat.join(""), qq = q.replace(/[^A-Za-z0-9]/g, "");
  if (!qq) return null;
  let s, m;
  if (f.toLowerCase() === qq.toLowerCase()){ s = 1000; m = { hits: map.map((_, k) => k) }; }
  else if ((m = matchAnchored(f, qq, starts))) s = m.at === 0 ? 600 : 400 - m.at * 2;
  else if ((m = matchWords(f, qq, starts))) s = 300 - m.at * 2;
  else return null;
  return { s: s - f.length * 0.4, hits: m.hits.map(k => map[k]) };
}

/* ── The candidate list ─────────────────────────────────────────────────────
   One entry per thing you can type. `label` is what you type against and what is shown;
   `target` is the real parameter name that gets written into the box. They differ only
   for a resolved family stem.                                                        */
let acEntries = null, acEntriesKey = "";

function invalidateEntries(){ acEntries = null; acEntriesKey = ""; }

function situation(){
  return {
    axis, end,
    // Phase 1 has no per-component bounds records, so nothing yet knows whether an axis
    // is symmetric here. Passing true keeps span-tagged members (…Width, …Height)
    // eligible, which is what lets a family like BootLatchHead resolve at all. The flag
    // exists for the BARE bounds vocabulary — typing "width" on its own — which arrives
    // with bounds records; filtering here would only hide working families.
    // The real flag now, read off the component's bounds record. The span words are
    // only part of the vocabulary on a symmetric axis: naming a midline on an axis with
    // no modelling midline would be offering something that does not exist.
    symmetric
  };
}

/* Cached: analysing 100 names on every keystroke is wasted work, and the answer only
   changes when the parameter list or the situation does. */
function entries(){
  const key = params.map(p => p.name).join(" ") + "|" + axis + "|" + end +
              "|" + (vocabReady ? "1" : "0") +
              // A tag changes which member a family resolves to, so the list built
              // from the old tags is not the list these tags would build.
              "|" + Object.keys(tagMap).length + "|" + browserShowRev;
  if (acEntries && acEntriesKey === key) return acEntries;

  const out = [];
  const named = params.filter(p => !isNumberParam(p.name));     // never suggested
  if (vocabReady){
    const byName = new Map(named.map(p => [p.name, p]));
    const fams = Naming.families(named.map(p => p.name));
    for (const stem of Object.keys(fams)){
      const members = fams[stem];
      // A family of one is just a parameter with a shorter alias — offering the stem
      // would only hide the name it stands for, so it is left out.
      if (members.length < 2) continue;
      // A stem that IS an existing parameter name must never be offered as a stem.
      // WingWebThickness and WingFrontWebThickness form the family "WingWebThickness",
      // which is also a real parameter — so typing that name in full and accepting
      // would have quietly swapped in WingFrontWebThickness, changing which parameter
      // you meant without saying so. A name that exists means the parameter that has it.
      if (byName.has(stem)) continue;
      // With no axis detected there is no situation to resolve against; the members are
      // all still in the list below under their own names.
      const hit = axis ? Naming.resolve(members, situation()) : null;
      if (!hit) continue;
      const p = byName.get(hit.name);
      // A family is typed by its full stem — the very words a hidden-from-typing
      // parameter asked to be spared — so such a member is offered by its reading only.
      if (p && !typingHidden(hit.name).length)
        out.push({ label: stem, target: hit.name, p: p, stem: true });
    }
  }
  // A bounds parameter reads as its bare face ("Right") everywhere else — the Browser
  // row, the chip — because its component and Bounds group are hidden from typing. In
  // the list that would be a dozen identical "Right"s, so here it reads (and is typed)
  // as its component and that face: "Nose - Right". The situation filters are untouched.
  // Every other parameter reads with its component first too (spokenNames' withOwner),
  // and one at the top level with no owner at all.
  const boundsNames = new Set(params.filter(p => isBoundsParam({ p })).map(p => p.name));
  const spoken = spokenNames(named.map(p => p.name), boundsNames, true);
  for (const e of out) if (e.stem){ e.shown = spokenOne(e.label); e.targetShown = spoken.get(e.target); }
  for (const p of named)
    out.push({ label: p.name, target: p.name, p: p, stem: false, shown: spoken.get(p.name),
               // Something hidden from typing: the reading is the ONLY thing it matches.
               readingOnly: typingHidden(p.name).length > 0 });

  /* Fusion's own words — the functions and the constants. They are as much THINGS as
     a parameter is (that is why they chip), so they are completed the same way rather
     than being left as spelling you have to get right from memory. A function is
     accepted as a CALL: choosing cos means cos(), with the caret between the brackets
     waiting for the angle. See acceptAC. */
  if (typeof Chips !== "undefined"){
    for (const v of Chips.vocabulary()){
      out.push({
        label: v.name, target: v.name, stem: false, kind: v.kind,
        p: { expression: v.kind === "fn"
               ? (v.takes > 1 ? v.name + "(a, b)" : v.name + "( )")
               : "constant" }
      });
    }
  }

  acEntries = out; acEntriesKey = key;
  return out;
}

/* The analysis of a real name, cached, for the wrong-axis penalty below.

   Two sources, one answer. The NAME is read by the engine, and whatever the name does
   not say is taken from the parameter's explicit tags — so a tag is not a second kind
   of thing the filters have to know about, it is the same field arriving by another
   road. An explicit tag WINS: it was typed by a person about this parameter, while the
   derived one is an inference from spelling.

   A parameter with tags but no readable name gets a SYNTHETIC analysis — enough for
   the axis filter to work, marked so the frame filter knows to keep out of it. Frame
   is about what a value is measured FROM, which the tags here do not claim to say, and
   defaulting it to "origin" would silently filter on something nobody stated. */
const analysisCache = new Map();
function analysisOf(name){
  if (analysisCache.has(name)) return analysisCache.get(name);
  let derived = null;
  if (vocabReady){
    try { derived = Naming.analyse(name); } catch (_) { derived = null; }
  }
  const explicit = tagMap[name];
  let out = derived;
  if (explicit && Object.keys(explicit).length){
    out = Object.assign({ stem: name, frame: "", tags: {}, axis: "", end: "", hits: [] },
                        derived || {}, { synthetic: !derived });
    out.tags = Object.assign({}, out.tags);
    for (const dim of Object.keys(explicit)){
      const word = explicit[dim];
      const meaning = tagMeaning(word);
      out.tags[dim] = word;
      if (meaning){
        if (meaning.axis) out.axis = meaning.axis;
        if (meaning.end)  out.end  = meaning.end;
      }
    }
    out.explicit = true;
  }
  analysisCache.set(name, out);
  return out;
}

/* A bounds or span parameter, by the comment Bounds.py stamps on it (see its own
   _BOUNDS_COMMENT) — "Bounds of Drawer" / "Span of Drawer, derived from its faces",
   the id in brackets ignored here. These are exempt from part of the situation and
   frame filters below: a face is how the OTHER face is reached from any datum on the
   same axis, which is exactly the wrong parameter to be filtering out. */
const BOUNDS_COMMENT = /^(Bounds|Span) of /;
function isBoundsParam(entry){
  return !!(entry.p && BOUNDS_COMMENT.test(entry.p.comment || ""));
}

/* Unit families. A dimension in mm and a parameter in cm are both LENGTHS and are freely
   interchangeable in a Fusion expression, so comparing the unit strings would wrongly
   hide the cm one. A unit this table has not heard of compares by its exact string,
   which is the conservative answer. */
const UNIT_FAMILY = {
  mm: "length", cm: "length", m: "length", km: "length", um: "length", nm: "length",
  in: "length", ft: "length", yd: "length", mil: "length",
  deg: "angle", rad: "angle", grad: "angle"
};
function unitFamily(u){
  const k = String(u == null ? "" : u).trim().toLowerCase();
  return k ? (UNIT_FAMILY[k] || k) : "";
}

/* The innermost function call the caret sits inside, as the text before its opening
   bracket, or null. Bracket-counting backwards, so "100 * tan(Ta|" finds tan and
   "tan(sqrt(Ta|" finds sqrt — the innermost call is the one whose argument you are
   actually typing. */
function enclosingCall(){
  const el = acField || valueEl;
  const t = el.value, c = el.selectionStart;
  let depth = 0;
  for (let i = c - 1; i >= 0; i--){
    const ch = t[i];
    if (ch === ")") depth++;
    else if (ch === "("){
      if (depth === 0) return t.slice(0, i);
      depth--;
    }
  }
  return null;
}

/* sin, cos and tan take an ANGLE whatever the dimension is measured in — tan(Taper)
   inside a millimetre expression is the ordinary way to use a taper, and filtering the
   degree parameters out of it was this rule's one wrong answer.

   asin, acos and atan are the mirror image: their argument is a ratio, which is as
   likely to be written as one length over another as it is to be a bare number, so
   there they have no opinion at all.

   The leading [^A-Za-z0-9_] is what keeps these apart — "asin" must not read as "sin",
   and "atan" must not read as "tan". */
const TRIG_TAKES_ANGLE = /(^|[^A-Za-z0-9_])(sin|cos|tan)$/i;
const TRIG_TAKES_RATIO = /(^|[^A-Za-z0-9_])(asin|acos|atan)$/i;

/* Is this a trigonometry expression at all — any sin/cos/tan (arc or hyperbolic too)
   anywhere in it? Then the situation and frame filters stand aside for the whole field:
   trig is where lengths on OTHER axes are the point — atan(Height / Depth) on an X
   dimension, Rise / tan(Pitch) — so a Y or Z parameter there is not a wrong-situation
   answer, it is the answer. The unit rule above is separate and still applies. */
const TRIG_CALL = /(^|[^A-Za-z0-9_])a?(sin|cos|tan)h?\s*\(/i;
function inTrigExpression(){
  return TRIG_CALL.test((acField || valueEl).value);
}

function expectedUnitFamily(){
  // The angle field is always measured in degrees, whatever `unit` the offset line
  // is in — the two fields describe the same plane but never the same quantity.
  if (acField === angleEl) return "angle";
  const before = enclosingCall();
  if (before !== null){
    if (TRIG_TAKES_RATIO.test(before)) return "";      // no opinion
    if (TRIG_TAKES_ANGLE.test(before)) return "angle";
  }
  return unitFamily(unit);
}

/* Is this parameter measured in something this POSITION cannot be? An angle has no
   business being offered for a length, and on an angular dimension the ninety-odd
   millimetre parameters are pure noise.

   Unitless is compatible with everything, on the same principle the axis filter uses:
   only an EXPLICIT disagreement counts. A bare count is a scalar and can multiply
   anything, so hiding it would be wrong. Likewise a session with no unit of its own
   (a new parameter, whose unit is inferred at commit) filters nothing.                */
function conflictsWithUnit(entry){
  const want = expectedUnitFamily();
  if (!want) return false;
  const has = unitFamily(entry.p && entry.p.unit);
  if (!has) return false;
  return has !== want;
}

/* ── Frames of reference ────────────────────────────────────────────────────
   A dimension is measured FROM something — its datum — and so is every parameter. A
   dimension starting at the origin wants values measured from the origin; one starting
   at the back bound wants values measured in from the back. Mixing the two produces a
   number that is right arithmetically and wrong on the model.

   THE FRAME MOVES AS YOU TYPE. Reaching the far side of the axis changes where you are
   measuring from: on a dimension out of the origin, "Width" takes you to the side bound,
   and everything after it is relative to THAT. So the moment a span parameter for this
   axis appears in the expression, the frame flips and the filter reverses with it.

   Only the origin datum can be detected at this stage; a dimension tied to a BOUND needs
   the component's bounds record, which does not exist yet. Until then `datum` is '' for
   those and no frame filtering happens at all — the behaviour that was already correct
   before any of this. */

/* Every parameter named anywhere in the expression. Not just the token being typed:
   the flip is about what is ALREADY there. */
function namesInExpression(){
  const out = new Set();
  const text = valueEl.value;
  const tok = tokenAtCaret();
  for (const p of params){
    // The token under the caret is half-typed and is not yet a statement of anything.
    if (tok && p.name === text.slice(tok.a, tok.b)) continue;
    if (new RegExp("(^|[^A-Za-z0-9_])" + p.name + "([^A-Za-z0-9_]|$)").test(text)){
      out.add(p.name);
    }
  }
  return out;
}

/* Has the expression already crossed to the far side of this axis? True once it names a
   parameter tagged with a SPAN word for the measured axis — Width, Height, Depth are
   defined as the distance from one reference to the other, so naming one moves you. */
function frameHasFlipped(){
  if (!vocabReady || !axis) return false;
  for (const name of namesInExpression()){
    const a = analysisOf(name);
    if (a && a.tags && a.tags.span && a.axis === axis) return true;
  }
  return false;
}

/* The frame the caret is measuring in: "origin", a direction word, or "" for no
   opinion. The specific bound matters, not merely that it is one — measuring from the
   FRONT face wants values measured from the front, and a back-framed parameter is as
   wrong there as an origin-framed one.

   Flipping crosses to the far side: from a bound, to its opposite; from the origin, to
   whichever face this dimension is nearer, since that is the one a span reaches. */
const ANY_BOUND = "*bound*";

function currentFrame(){
  if (!datum) return "";
  const base = datum === "zero" ? "origin" : datum;
  if (!frameHasFlipped()) return base;
  // ANY_BOUND when the face cannot be named: flipping out of the origin needs to know
  // which end this dimension is nearer, and on a dimension sitting exactly astride the
  // origin there is no answer. "Somewhere other than the origin" is still true, and
  // still filters out every origin-framed parameter, which is the useful half.
  if (base === "origin") {
    return (Naming.directionAt(axis, end) || ANY_BOUND).toLowerCase();
  }
  return (Naming.oppositeDirection(base) || ANY_BOUND).toLowerCase();
}

/* One name for a frame, so two ways of saying the same bound compare equal. On a
   SYMMETRIC axis the faces are the same distance away and the span word names that
   bound, so "from the right", "from the left" and "from the width" are ONE frame rather
   than three. Six of the document's nine bound-framed parameters are the ...OffsetWidth
   shape, so without this the frame is wrong for most of the ones that have one. */
function canonicalFrame(word){
  const w = String(word || "origin").toLowerCase();
  if (w === "origin" || w === ANY_BOUND) return w;
  return symmetric ? "side" : w;
}

function conflictsWithFrame(entry){
  const want = currentFrame();
  if (!want) return false;
  // A face IS how the other face is reached, from any datum this component has — the
  // dimension in front of you might be measured from the origin, from this face, or
  // from the opposite one, and the face parameter is the right answer in every one of
  // those. Filtering it by frame at all was the bug: on a bound-framed dimension every
  // origin-framed face vanished, which was every face there was (see isBoundsParam).
  if (isBoundsParam(entry)) return false;
  // A name in the structured convention has had its frame judged already —
  // namingSituation() carries the datum into ParamNaming.fits (conflictsWithSituation).
  // The old engine's guess here read TopLevel_Materials_HIPS_Thickness as origin-framed
  // and took it off every plane line measured from a face.
  if (paramNamingReady && ParamNaming.parse(entry.target)) return false;
  const a = analysisOf(entry.target);
  if (!a) return false;
  // Measuring from the origin where a face also sits: that face's frame is this one too.
  if (want === "origin" && datumFaces.includes(String(a.frame || "").toLowerCase())) return false;
  // Tagged, but with a name the engine could make nothing of: the tags say which axis,
  // never what the value is measured from. Filtering on a frame nobody stated would be
  // inventing an opinion.
  if (a.synthetic) return false;
  // A span parameter is what MOVES you between frames, so it belongs to neither and is
  // never filtered — without it the flip could not be reached from the origin frame.
  // Unless it names a frame ITSELF, which on a symmetric axis it does.
  if (a.tags && a.tags.span && a.frame === "origin") return false;
  const has = canonicalFrame(a.frame);
  const wants = canonicalFrame(want);
  if (wants === ANY_BOUND) return has === "origin";
  return has !== wants;
}

/* Does this entry belong to a DIFFERENT situation from the one we are in?
   Such entries are REMOVED, not merely ranked down. Ranking them down does nothing when
   they are the only things that match: on an X dimension "de" matched five Y-axis Depth
   parameters and, with nothing to outrank them, offered all five.

   Only an explicit disagreement counts. A parameter with no axis is general and stays;
   one with no end stays whatever end we are at. So the test is "it says something, and
   what it says is not this", never "it fails to say what I wanted".

   A stem entry is exempt because it was RESOLVED to this situation — that is what makes
   it a stem entry at all.                                                              */
function conflictsWithSituation(entry, hits){
  if (entry.stem) return false;
  // The structured feature/type/anchor convention (param_naming.js) has its own,
  // simpler situational rule than the old engine's stem/tag analysis below: each type
  // states when it is suggested (param_naming.json's `suggest`), and the answer is one
  // ParamNaming.fits() call. Checked first because a name that parses here never
  // reaches the old engine's guess at all — the two conventions are not blended for
  // one parameter.
  if (paramNamingReady){
    const parsed = ParamNaming.parse(entry.target);
    if (parsed) return paramNamingSituationBad(parsed);
  }
  const a = analysisOf(entry.target);
  if (!a) return false;                     // engine unavailable: filter nothing

  const axisBad = axis && a.axis && a.axis !== axis;
  // A bounds/span parameter's END half does not apply: BracketLeft and BracketRight
  // are both legitimate answers on an X dimension whichever end it is measured from —
  // only the axis a face sits on can actually disagree with the one being dimensioned.
  // Filtering on end here removed the one face you meant along with the ones you didn't
  // (see isBoundsParam).
  const endBad  = !isBoundsParam(entry) && end && a.end && a.end !== end;
  if (!axisBad && !endBad) return false;

  // The escape hatch: a wrong-situation parameter is still reachable, but only once you
  // have IDENTIFIED it — matched the whole of a variable word AND at least one character
  // outside it. "powerboardwidth" reaches PowerboardWidth from a Y dimension; "width"
  // does not, and neither does "y".
  //
  // Naming a variable is not asking for a parameter, it is starting a search, and the
  // weaker versions of this rule all failed on that. A prefix of the NAME let everything
  // through, since "wi" prefixes half the document. Merely OVERLAPPING the variable put
  // all five Y-axis Depth parameters back, since "de" starts Depth. Covering the whole
  // variable word still let "y" through, because the axis words are one character long
  // and typing one is no evidence of anything.
  if (hits && hits.length){
    const matched = new Set(hits);
    for (const h of a.hits){
      const lo = h[2], hi = lo + h[1].length - 1;
      let whole = true;
      for (let i = lo; i <= hi && whole; i++) if (!matched.has(i)) whole = false;
      if (!whole) continue;
      const beyond = hits.some(i => i < lo || i > hi);
      if (beyond) return false;
    }
  }
  return true;
}

/* Score one entry against the typed fragment. Higher is better; null = no match. */
function score(entry, q){
  const lit = entry.readingOnly ? null : scoreLiteral(entry, q);
  // The reading too (see matchSpoken), and the better of the two scores. The row draws
  // the reading, so its highlights (`shownHits`) always come from the reading — none when
  // only the real name matched ("span" reaching "… - Width"). `hits` stays in the REAL
  // name's positions, because that is what the situation filter's escape hatch reads.
  if (!entry.readingOnly && (!entry.shown || entry.shown === entry.label)) return lit;
  const sp = matchSpoken(entry.shown, q);
  if (!sp) return lit && Object.assign(lit, { shownHits: [] });
  const s = sp.s + adjust(entry);
  return { entry, s: lit ? Math.max(lit.s, s) : s, hits: lit ? lit.hits : [],
           shownHits: sp.hits };
}

// The lifts and penalties that aren't about how well the letters matched — shared by
// the real name and the reading.
function adjust(entry){
  let s = 0;
  // A family that RESOLVED in this situation is the answer we want offered, so it is
  // lifted above its own members. Not above an exact match on a real name, though:
  // 1000 stays out of reach, so typing a name in full still wins.
  if (entry.stem) s += 200;

  // Fusion's own words rank below the document's. Typing "co" is far more often the
  // start of a parameter you named than of a cosine, and the penalty is small enough
  // that typing "cos" in full still puts the function first — nothing else matches it.
  if (entry.kind) s -= 120;

  // A parameter TAGGED for the situation we are in was tagged for exactly this moment,
  // so it is lifted above the untagged names it competes with. Small: enough to sort
  // two equal matches, never enough to beat a better one.
  if (axis && !entry.kind){
    const ax = analysisOf(entry.target);
    if (ax && ax.explicit && ax.axis === axis) s += 60;
  }
  return s;
}

function scoreLiteral(entry, q){
  const name = entry.label, nl = name.toLowerCase(), ql = q.toLowerCase();
  let s, hits;

  if (nl === ql){
    s = 1000; hits = name.split("").map((_, i) => i);
  } else if (nl.startsWith(ql)){
    s = 600;  hits = ql.split("").map((_, i) => i);
  } else {
    // Verbatim, and beginning at a word start — see matchAnchored. An earlier match
    // outranks a later one, so a name whose SECOND word you typed beats one where the
    // fragment turns up four words in.
    const starts = wordStarts(name);
    const m = matchAnchored(name, q, starts);
    if (m){
      hits = m.hits;
      s = 400 - m.at * 2;
    } else {
      // Word by word, split at the capitals you typed — see matchWords. Below a
      // verbatim match: the name that has "bootd" as you typed it is the closer answer.
      const w = matchWords(name, q, starts);
      if (!w) return null;
      hits = w.hits;
      s = 300 - w.at * 2;
    }
  }

  s -= name.length * 0.4;                 // prefer the shorter of two equal matches

  s += adjust(entry);

  // Nothing here penalises a wrong axis any more — conflictsWithSituation removes those
  // outright, which is the only thing that works when they are all there is to offer.
  return { entry, s, hits };
}

/* ── Which field the autocomplete is serving ────────────────────────────────
   The value box was the only field that ever completed, so everything below addressed
   it by name. The New Component panel has six bounds fields that hold expressions like
   "Width" or "HalfDepth", and they want exactly the same completion — which is the
   whole reason that panel lives in this window rather than one of its own. So the
   autocomplete now follows focus.

   acField is the input being completed; acBox is the bordered wrapper the list hangs
   off. Both default to the value box, so the dimension and parameter sessions behave
   exactly as before.                                                                  */
let acField = null, acBox = null;

function attachAC(field, box){
  if (acField === field) return;
  acField = field; acBox = box;
  acItems = []; acIndex = -1; renderAC();
}

/* ── The token under the caret ──────────────────────────────────────────────
   The value field holds an expression, so completion works on the identifier the
   caret sits in ("Width/2 + Ga|" completes "Ga"), not on the whole field.          */
// With chips.js's short-name stand-ins (no-break space/hyphen, low comma): a chip opened
// with F is its READING, and all of it is the word being completed — matchSpoken reads
// past the stand-ins as it does past any punctuation.
const IDENT = /[A-Za-z0-9_\u202f\u2011\u201a]/;
function tokenAtCaret(){
  const el = acField || valueEl;
  const t = el.value, c = el.selectionStart;
  if (el.selectionEnd !== c) return null;              // a selection, not a caret
  let a = c; while (a > 0 && IDENT.test(t[a-1])) a--;
  let b = c; while (b < t.length && IDENT.test(t[b])) b++;
  const word = t.slice(a, b);
  // Don't autocomplete a bare number — "25" should stay 25, not offer every parameter
  // with a 2 or a 5 in its name.
  if (!word || !/[A-Za-z_]/.test(word)) return null;
  return { a, b, word };
}

/* The FIRST word of a plane line, wherever the caret sits inside it — or null once the
   caret has moved past it into the offset expression.

   Its own function rather than tokenAtCaret, for two reasons: a datum is a whole word
   (so "Width" is one token even though tokenAtCaret would stop at any punctuation), and
   an EMPTY one still counts. Typing nothing at all should show the vocabulary on offer,
   which is exactly the moment tokenAtCaret refuses to return anything. */
function datumToken(){
  // Not just the sketch box's mode — the caret has to actually BE in it. Now that the
  // angle field completes too (see updateAC), activeToken() can be asked for a token
  // while angleEl is focused, and without this it would answer with whatever the
  // value line's caret was last left at rather than "there is no datum here".
  if (mode !== "sketch" || document.activeElement !== valueEl) return null;
  const t = valueEl.value, c = valueEl.selectionStart;
  if (valueEl.selectionEnd !== c) return null;
  const lead = t.match(/^\s*/)[0].length;
  if (c < lead) return null;
  let b = lead;
  while (b < t.length && !/\s/.test(t[b])) b++;
  if (c > b) return null;                          // past the first word: an expression
  return { a: lead, b: b, word: t.slice(lead, b) };
}

/* Whichever token the keys should act on. Everything that completes goes through this,
   so the datum and the expression halves of a plane line behave the same way. */
function activeToken(){
  return datumToken() || tokenAtCaret();
}

/* Park the suggestion list directly on the bottom edge of the value field. Recomputed
   rather than fixed, because the value field grows as the expression wraps.

   offsetTop, NOT getBoundingClientRect(): `top` on an absolutely positioned element is
   measured from the containing block, while the rect is measured from the viewport. If
   the page is ever scrolled — focusing the textarea is enough to do it — mixing the two
   walks the list upward a little further on every render until it covers the panel.

   But offsetTop is measured from the offsetPARENT, and a field's offsetParent is the
   panel (.fpanel is a positioning context, for the width grip), while #ac hangs off the
   page. The panel is a gutter and the body's margin down from the top of that page — so
   the field's own offsetTop alone put the list some thirty pixels high, which on a
   thirty-pixel row is straight over the box it is completing. Summing the chain is what
   closes the gap. */
function pageTop(el){
  let y = 0;
  for (let n = el; n; n = n.offsetParent) y += n.offsetTop;
  return y;
}
function placeAC(){
  const box = acBox || fValue;
  acEl.style.top = (pageTop(box) + box.offsetHeight) + "px";
}

// Text or an attribute value, made safe to write into innerHTML.
const escHtml = v => String(v).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function renderAC(){
  if (!acItems.length){ acEl.className = "hide"; acEl.innerHTML = ""; return; }
  acEl.className = "";
  placeAC();
  acEl.innerHTML = acItems.map((it, i) => {
    // The reading when there is one (see matchSpoken), highlighted by its own hits.
    const spoken = !!it.shownHits;
    const nm = spoken ? it.entry.shown : it.entry.label;
    const set = new Set(spoken ? it.shownHits : it.hits);
    let html = "";
    for (let k = 0; k < nm.length; k++){
      const ch = escHtml(nm[k]);
      html += set.has(k) ? "<b>" + ch + "</b>" : ch;
    }
    // What the stem resolved to. Shown only when it differs from what you typed against,
    // so the row is quiet for an ordinary parameter.
    const res = it.entry.stem
      ? '<span class="ac-res">' + escHtml(it.entry.targetShown || it.entry.target) + "</span>"
      : "";
    // A DATUM ROW SAYS NOTHING BUT THE WORD. It used to carry the face parameter the
    // word resolves to (TestRight under "Width"), which is the answer to a question
    // nobody asks at this point: the word IS the vocabulary, and the parameter behind it
    // is an implementation detail of the component's bounds. The composed expression is
    // still spelled out in the readout under the line, where it is about the whole line
    // rather than about one row. An ordinary parameter row keeps its value — there the
    // number is how you tell two similar names apart.
    const val = it.entry.datum
      ? "" : escHtml((it.entry.p && shownValue(it.entry.p)) || "");
    const marked = acTouched && i === acIndex;
    return '<div class="ac-row' + (marked ? " sel" : "") + '" data-i="' + i + '">' +
             '<span class="ac-name">' + html + "</span>" + res +
             '<span class="ac-val">' + val + "</span>" +
             (marked ? tagPills(it.entry) : "") +
             '<span class="ac-mark"></span>' +
           "</div>";
  }).join("");
  const sel = acEl.querySelector(".ac-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

/* The axis tags, offered on the row you are about to take. Clickable, and the same
   three things ⌘1/⌘2/⌘3 toggle — a key for the hands already on the keyboard, a target
   for the hand already on the mouse, and one lit square either way saying which it is.

   Only for parameters: a function or a constant belongs to Fusion, not to this
   document, and a datum word is a face rather than a parameter. */
function tagPills(entry){
  if (entry.kind || entry.datum) return "";
  if (!params.some(p => p.name === entry.target)) return "";
  const words = tagWords("axis");
  if (!words.length) return "";
  const cur = (tagMap[entry.target] || {}).axis || "";
  return '<span class="ac-tags">' + words.map(w =>
    '<i class="ac-tag' + (cur === w ? " on" : "") + '" data-ax="' +
    w.replace(/[&<>"]/g, "") + '">' + escHtml(w) + "</i>").join("") +
    "</span>";
}

function updateAC(){
  const el = acField || valueEl;

  // A rebuilt list is a new list: what it offers has just changed under the mark, so the
  // mark goes and is earned again by an arrow key (see acTouched). Every path out of
  // here ends in renderAC, so this is the one place it needs saying.
  acTouched = false;

  // The datum half of a plane line completes against the words this component offers,
  // not against parameters: "Width" is a face here and means nothing in the parameter
  // list. Past the first word it is an ordinary expression again.
  const dtok = document.activeElement === valueEl ? datumToken() : null;
  if (dtok){
    acItems = datumSuggestions(dtok.word);
    acIndex = acItems.length ? 0 : -1;
    renderAC();
    return;
  }

  // The Name field completes against existing parameters, not against the value
  // field's vocabulary — picking one is "edit this parameter instead", not "insert
  // this word here". See acceptAC. The stepper's feature step offers the owner's
  // existing groups instead (stepLevelSuggestions); its later steps run their OWN list
  // (#stepchoices, see renderSteps) and leave #ac empty so the two never draw at once.
  if (document.activeElement === nameEl){
    acItems = !stepperOn ? nameSuggestions(nameEl.value)
      : stepAcClosed ? []
      : stepStage === "level" && stepTyping ? stepLevelSuggestions()
      : stepStage === "parent" && stepFinding ? stepCompSuggestions() : [];
    acIndex = acItems.length ? 0 : -1;
    renderAC();
    return;
  }

  const tok = document.activeElement === el ? tokenAtCaret() : null;
  if (!tok){ acItems = []; acIndex = -1; renderAC(); return; }

  const trig = inTrigExpression();
  // A short name in the field reads back as its REAL name — "HIPS" typed out in full is
  // TopLevel_Materials_HIPS_Thickness here — and scored as that, nothing reading
  // "HIPS - Top" matched it, so the list went empty. It is scored as its reading too.
  const reading = params.some(p => p.name === tok.word) ? chipLabel(tok.word) : null;
  const scoreBoth = e => {
    const x = score(e, tok.word);
    if (!reading || reading === tok.word) return x;
    const y = score(e, reading);
    return !x ? y : !y ? x : (y.s > x.s ? y : x);
  };
  const scored = entries()
    // A real name already typed out in full is not a suggestion — offering it just puts
    // a list over the panel with nothing to add. A STEM typed out in full is a different
    // matter: accepting it is what swaps in the real name, so it has to stay. Case
    // sensitive, so a lowercase attempt still gets offered the properly-cased one.
    //
    // A FUNCTION typed out in full stays for the same reason a stem does: accepting it
    // is not "insert these letters", it is "make this a call" — cos becomes cos() with
    // the caret inside. Dropping it left the list offering cosh as the best answer to
    // the whole word cos.
    // A short name typed in full ("HIPS") is not its real name typed in full: the row
    // stays, so the list says which parameter the word is.
    .filter(e => e.stem || e.kind === "fn" || e.label !== tok.word ||
                 (reading && reading !== tok.word))
    .map(scoreBoth)
    .filter(Boolean)
    // Scored first, because whether a wrong-situation parameter survives depends on
    // WHERE in the name the match landed. Not in a trig expression (inTrigExpression).
    // Off from the right-click menu ("Filter suggestions"): every match, unfiltered.
    .filter(it => !acFiltering || trig || !conflictsWithSituation(it.entry, it.hits))
    .filter(it => !acFiltering || !conflictsWithUnit(it.entry))
    .sort((a, b) => b.s - a.s);

  // The frame filter is applied LAST and only while it leaves something behind.
  //
  // It is right in principle — a dimension measured from the front face wants values
  // measured from the front, and an origin-framed one there lands the feature at
  // (face + value) — but a model whose parameters are all origin-framed has nothing
  // else to offer, and emptying the box teaches you the tool is broken rather than
  // that the frames disagree. So it narrows the list when there is a choice to be made
  // and stands aside when there is not, which means it switches itself on gradually as
  // bound-framed parameters start to exist.
  const framed = (trig || !acFiltering) ? scored : scored.filter(it => !conflictsWithFrame(it.entry));
  const kept = framed.length ? framed : scored;

  // Two entries can lead to the same parameter — the family stem and the member's own
  // name — and showing the row twice would be noise. The better-scoring one wins, which
  // is the stem when you typed the stem and the real name when you typed the name.
  const seen = new Set();
  acItems = [];
  for (const it of kept){
    if (seen.has(it.entry.target)) continue;
    seen.add(it.entry.target);
    acItems.push(it);
    if (acItems.length >= 12) break;
  }

  // The best match is highlighted the moment the list appears, so Enter (or →) takes it
  // without a trip through the arrow keys. Consequence worth knowing: while the list is
  // showing, Enter accepts the highlighted row rather than advancing to the name field,
  // and a second Enter then advances. Typing a bare number opens no list at all (see
  // tokenAtCaret), so committing a plain value is still Enter, Enter.
  //
  // EXCEPT when what you typed IS a parameter. Nothing is highlighted then, so Enter
  // advances instead of accepting. The convention deliberately produces names that are
  // prefixes of other names — Thickness and ThicknessY, PlateOffset and PlateOffsetX —
  // and without this, typing the shorter one in full and pressing the key you were going
  // to press anyway silently committed the longer one. The longer names stay in the list
  // for the arrow keys; they just no longer win by default.
  const typedIsRealName = params.some(p => p.name === tok.word);
  acIndex = (acItems.length && !typedIsRealName) ? 0 : -1;
  renderAC();
}

/* The datum words, as suggestion rows. Prefix-matched and case-insensitive — a datum is
   one word, so there is nothing for the word-anchoring rules to do. The face parameter
   each one resolves to rides along in the value column, which is the only way to see
   that "Width" picked the face you meant. */
function datumSuggestions(word){
  const q = String(word || "").toLowerCase();
  const out = [];
  for (const d of datums){
    if (q && !d.word.toLowerCase().startsWith(q)) continue;
    const hits = [];
    for (let i = 0; i < q.length; i++) hits.push(i);
    out.push({
      s: 1000,
      hits: hits,
      entry: { label: d.word, target: d.word, stem: false, datum: true,
               p: { expression: d.kind === "midline" ? "midline" : d.face } }
    });
  }
  // Left in the order they arrived, which is axis by axis — the vocabulary read out
  // rather than ranked. There is nothing here for a score to decide between: six words,
  // all equally what you might have meant.
  return out;
}

/* Existing parameters, as suggestion rows for the Name field — prefix-matched,
   case-insensitive, each showing its current expression in the value column so you
   can tell them apart before committing to one. Nothing shown for an empty field:
   there can be a great many parameters and typing nothing is not "show them all"
   the way it is for the value box's small, fixed vocabulary. Picking one is "edit
   this parameter instead" (see acceptAC / retargetParam), so the one already being
   edited is left out — suggesting itself back is not a choice. */
function nameSuggestions(word){
  const q = String(word || "").trim().toLowerCase();
  if (!q) return [];
  const out = [];
  const spoken = spokenNames(params.map(p => p.name));
  for (const p of params){
    if (p.name === origName) continue;
    let hits = [], s = 1000;
    if (p.name.toLowerCase().startsWith(q)){
      for (let i = 0; i < q.length; i++) hits.push(i);
    } else {
      // "bootD" → Boot…Door…, split at the capitals typed (see matchWords). Listed
      // after the prefix matches.
      const w = matchWords(p.name, String(word).trim(), wordStarts(p.name));
      if (w){ hits = w.hits; s = 300; } else s = 0;
    }
    // The row reads as the Browser does, and the reading matches too (see matchSpoken).
    const shown = spoken.get(p.name);
    const sp = matchSpoken(shown, String(word).trim().replace(/\s+/g, ""));
    if (!s && !sp) continue;
    out.push({ s: Math.max(s, sp ? sp.s : 0), hits: hits, shownHits: sp ? sp.hits : [],
               entry: { label: p.name, target: p.name, stem: false, existing: true, p: p,
                        shown: shown } });
  }
  return out.sort((a, b) => b.s - a.s).slice(0, 12);
}

/* Switch this session onto editing an existing parameter, picked from the Name
   field's own suggestions — the value box takes its expression, a commit from here
   renames/redefines it, and whatever this session was before (a fresh dimension
   edit, a new-parameter session) is abandoned in favour of it.
   The add-in has to be told too: commit resolves its target from the add-in's OWN
   session state, not from anything posted at commit time (see _do_commit_edit_param
   in the add-in), so picking a different parameter here would otherwise rename
   whatever the add-in still thinks this session is about. */
function retargetParam(p){
  mode = "editparam";
  origName = p.name;
  unit = p.unit || "";
  nameRequired = true;
  nameEl.placeholder = "Name (required)";
  nameEl.value = p.name;
  nameEl.setSelectionRange(nameEl.value.length, nameEl.value.length);
  // A driven-dimension link reads as the dimension does: measured, and named only.
  valueEl.value = p.linked ? stripUnit(p.measured || "", unit)
                           : tidyBrackets(stripUnit(p.expression || "", unit));
  isDriven = !!p.linked;
  $("driven").className = isDriven ? "" : "none";
  drivenExpr = isDriven ? exprText() : "";
  fName.classList.remove("bad");
  clearError();
  autosize();
  refreshChips();
  fitWidth();
  schedulePreview();
  post({ action: "retarget", name: p.name });
}

function acceptAC(){
  if (acIndex < 0 || !acItems[acIndex]) return false;
  const tok = activeToken();
  if (!tok) return false;
  // The REAL name, never the stem: Fusion only knows real names, and seeing which member
  // the situation picked is the only way to catch it having picked wrongly.
  const ent = acItems[acIndex].entry;
  const el = acField || valueEl;
  if (ent.existing){
    retargetParam(ent.p);
    acItems = []; acIndex = -1; renderAC();
    return true;
  }
  if (typeof Chips !== "undefined" && !ent.datum){
    // The chip editor owns the insertion, for one reason: a FUNCTION is chosen as a
    // call. Taking cos out of the list means cos() with the caret between the
    // brackets, waiting for the angle — never the three letters on their own, which
    // would not parse and would have to be finished by hand. It also seals what it
    // inserted, so a name chosen from the list is an item straight away rather than
    // after the next keystroke.
    Chips.accept(el, tok.a, tok.b, ent.target, ent.kind || "param");
  } else {
    // A datum takes a trailing space with it: what follows is how far in from it, and
    // there is nothing else the next keystroke could be. Its own insertion rather than
    // Chips.accept's, because that space is the one thing chips.js must not write — see
    // the guard in chipsAttach. The word still becomes a chip (it is in the vocabulary
    // now), and the caret lands past it, in the gap the offset goes in.
    const nm = ent.target + (!/^\s/.test(el.value.slice(tok.b)) ? " " : "");
    const t  = el.value;
    el.value = t.slice(0, tok.a) + nm + t.slice(tok.b);
    const caret = tok.a + nm.length;
    el.setSelectionRange(caret, caret);
  }
  acItems = []; acIndex = -1; renderAC();
  // syncSketchSituation as well as syncSketchLine: accepting sets .value DIRECTLY, and
  // assigning to .value fires no input event, so nothing else here re-reads the line.
  // Without it the box showed the datum the list had just put in and held none, and the
  // commit refused that very word.
  if (el === valueEl){
    autosize(); syncSketchLine(); syncSketchSituation(); schedulePreview();
    // …and repaint, for the same reason: the datum branch above wrote .value itself, and
    // an assignment fires no input event, so without this the word chips only on the
    // next keystroke. After syncSketchLine, which can still rewrite the line.
    if (ent.datum) refreshChips();
  } else if (el === angleEl){
    // The angle field's own input handler does exactly this; accepting from the list
    // sets .value directly and fires no input event, so it has to be repeated here.
    infoEl.textContent = sketchReadout(); schedulePlanePreview(); clearError();
  } else {
    // A panel's own text rows — Extrude's, the feature panel's (Array's quantity and
    // distance) — read their field on `input` too, so without this the preview waited
    // for the next keystroke. Not a dispatched event: chips.js's onInput would diff the
    // bubbles against a prevValue Chips.accept never updated, and shift them twice.
    for (const k in xFields) if (xFields[k] === el){ xSpec[k] = el.value; xPush(); }
    for (const k in oFields) if (oFields[k] === el){ oSpec[k] = el.value; oPush(); }
  }
  return true;
}

acEl.addEventListener("mousedown", e => {
  const row = e.target.closest(".ac-row");
  if (!row) return;
  e.preventDefault();                     // don't let the textarea lose the caret
  if (stepperOn && stepStage === "parent" && acField === nameEl){
    const it = acItems[+row.dataset.i];
    if (it) setOwner(it.entry.target);
    nameEl.focus();
    return;
  }
  if (stepperOn && stepStage === "level" && acField === nameEl){
    stepTakeSuggestion(+row.dataset.i);
    nameEl.focus();
    return;
  }
  acIndex = +row.dataset.i;
  // A tag square is a switch on the row, not a way of taking it: clicking one says
  // what this parameter is for and leaves the list open to go on choosing from.
  const pill = e.target.closest(".ac-tag");
  if (pill){ toggleAxisTag(pill.dataset.ax); return; }
  // A panel row's list keeps the keyboard in that row; everything else goes back to the
  // value box, as before.
  const panelRow = acField && (Object.values(xFields).includes(acField)
                               || Object.values(oFields).includes(acField));
  const field = panelRow ? acField : valueEl;
  acceptAC();
  field.focus();
});

/* ── How wide the dialog is ──────────────────────────────────────────────────
   The window is borderless and the page cannot size it, so the page asks: it posts a
   width and the host resizes around the panel's left edge (see setPanelWidth in
   main.swift).

   Two things ask. The GRIP on the panel's right edge, which sets the width you chose
   and remembers it — that is `baseWidth`, and it is what the dialog opens at from then
   on. And the CONTENT: an expression that no longer fits is a box that is too narrow,
   so the panel grows to hold it and shrinks back to the width you chose when it no
   longer needs to. It never shrinks below that, so growing is never something you have
   to undo.                                                                          */
const WIDTH_MAX = 1100;
let baseWidth = 400;
try {
  const saved = parseFloat(localStorage.getItem("panelWidth"));
  if (saved >= 260 && saved <= WIDTH_MAX) baseWidth = saved;
} catch (_) {}

// The command-search palette's own width — 60% of the 400 default, deliberately NOT
// baseWidth * 0.6 and not persisted, so the grip's remembered width stays the value
// box's business and the palette opens at the same width every time regardless of it.
const SEARCH_W = 240;

/* The New Component panel and its sibling — bounds added to a component that already
   exists — share every row and every rule about which panel is on screen; the only
   difference is the name field and which action a commit posts. Checked together
   wherever "is the bounds panel up" is the actual question. */
function isBoundsPanelMode(){
  return mode === "component" || mode === "addbounds" || mode === "editbounds";
}

function panelWidth(){
  const box = isBoundsPanelMode() ? cPanel
            : mode === "extrude" ? xPanel
            : mode === "mirror"  ? rPanel
            : mode === "feature" ? oPanel
            : mode === "search"  ? sPanel
            : mode === "versions" ? vPanel
            : panel;
  return box.getBoundingClientRect().width || baseWidth;
}

function askWidth(w){
  const floor = mode === "search" ? 200 : 260;
  const want = Math.round(Math.max(floor, Math.min(w, WIDTH_MAX)));
  if (Math.abs(want - panelWidth()) < 2) return;
  host("size", { width: want });
}

/* Room kept past the end of the line, always. A box that grew to exactly fit what is in
   it puts the thing you are typing hard against its own edge, which reads as full — and
   the next chip would then arrive by widening the window under your hand rather than by
   dropping into space that was already waiting for it. */
const TAIL = 80;

/* The width the text needs, measured by laying it out unwrapped in #ruler. The longest
   LINE, since the box is multi-line and it is the longest one that decides. */
let rulerEl = null;
function textWidth(el){
  // No layout, no measurement: the headless harness runs this file with a DOM that has
  // neither, and a dialog that cannot measure simply keeps the width it has.
  if (typeof getComputedStyle !== "function" || !document.body) return 0;
  if (!rulerEl){
    rulerEl = document.createElement("div");
    rulerEl.id = "ruler";
    document.body.appendChild(rulerEl);
  }
  const cs = getComputedStyle(el);
  rulerEl.style.font = cs.font;
  rulerEl.style.letterSpacing = cs.letterSpacing;
  // The gap between chips is word-spacing on the field (see theme.css), so a line
  // measured without it comes back narrower than it is drawn and the box fits nothing.
  rulerEl.style.wordSpacing = cs.wordSpacing;
  let widest = 0;
  // What the field SHOWS, which for a chip field is its short names, not the real ones.
  const shown = typeof Chips !== "undefined" ? Chips.shown(el) : el.value;
  for (const line of String(shown || "").split("\n")){
    rulerEl.textContent = line || " ";
    widest = Math.max(widest, rulerEl.getBoundingClientRect().width);
  }
  // The field's own padding — which on the value box includes the strip the Copy button
  // sits in — plus the 5px a chip paints OUTSIDE its letters at each end of the line,
  // width the ruler cannot see because the ruler holds no chips. Then the tail.
  return widest + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + 12 + TAIL;
}

/* Grow to fit what is being typed; never past WIDTH_MAX, never below the width you
   chose. Called from the input path, so it follows the text as it is written. */
function fitWidth(){
  if (!isBoundsPanelMode() && mode !== "extrude" && mode !== "mirror" && mode !== "feature"){
    askWidth(Math.max(baseWidth, textWidth(valueEl)));
    return;
  }
  // Every visible field on these panels, not just the focused one: a session opens with
  // all of them filled in and none of them focused, and an <input> that does not fit
  // cuts its text off rather than wrapping it.
  const fields = isBoundsPanelMode() ? cFields : mode === "mirror" ? {}
               : mode === "feature" ? oFields : xFields;
  let want = baseWidth;
  for (const key in fields){
    const el = fields[key];
    if (!el || !el.offsetParent) continue;             // a row that is hidden right now
    want = Math.max(want, textWidth(el));
  }
  // Extrude's two distances share a row, so that row wants both at once.
  if (mode === "extrude" && xFields.distance2.offsetParent){
    want = Math.max(want, textWidth(xFields.distance) + textWidth(xFields.distance2));
  }
  askWidth(want);
}

/* Dragging the edge. The window follows the pointer in SCREEN coordinates, because the
   window itself is moving under it: measured in page coordinates, every frame would be
   computed from a viewport that the previous frame had just resized. */
function attachGrip(box){
  const g = document.createElement("div");
  g.className = "grip";
  box.appendChild(g);
  g.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();                       // not a window drag — see startDrag
    const x0 = e.screenX, w0 = panelWidth();
    const move = ev => askWidth(w0 + (ev.screenX - x0));
    const up = () => {
      window.removeEventListener("mousemove", move, true);
      window.removeEventListener("mouseup", up, true);
      // What you dragged it to is what it opens at from now on.
      baseWidth = Math.max(260, Math.min(panelWidth(), WIDTH_MAX));
      try { localStorage.setItem("panelWidth", String(Math.round(baseWidth))); } catch (_) {}
    };
    window.addEventListener("mousemove", move, true);
    window.addEventListener("mouseup", up, true);
  });
}

/* ── Chips ───────────────────────────────────────────────────────────────────
   Every field that holds an EXPRESSION gets one: the value box, the six bounds fields
   on the New Component panel and the Extrude panel's distance and taper. Not the name
   fields — a name is being spelled, not assembled out of things.

   THE PLANE BOX'S DATUM IS A CHIP TOO. It is not a parameter, but it is exactly the
   kind of thing a chip is for: "Width" is a face you either mean or you don't, and
   there is no sense in which its fourth letter can be edited. So the datum words this
   component offers are handed to chips.js as vocabulary alongside the parameters, and
   the word seals into an item the moment the space after it is typed or the list is
   taken from — same as every other item in the box. What the guard below still keeps
   out is the ARITHMETIC: "Front 100" is a face and an offset, never a sum.

   The field stays exactly what it was. chips.js paints a layer under it holding the
   same characters, so .value, the caret, the selection and every line of this page
   that reads them are untouched — see its header. */
const spokenFields = new WeakSet();   // where a dictated paste is read (pasteSpoken)
function chipsAttach(el){
  if (typeof Chips === "undefined" || !el) return;
  spokenFields.add(el);
  try {
    Chips.attach(el, {
      names: () => (el === valueEl && mode === "sketch")
        ? params.map(p => p.name).concat(datums.map(d => d.word))
        : params.map(p => p.name),
      tags: tagsOf,
      // A parameter chip reads the way the list and the Browser read it, not as its
      // underscored Fusion name — see chipLabel.
      label: chipLabel,
      // The plane line's first word is a DATUM, so the ARITHMETIC has to leave it alone:
      // two items side by side are added ("Width Depth" is their sum), and "Front 100"
      // is a face and how far in from it, never a face plus a hundred. The guard is also
      // what makes the space after it a real space — the gap between the datum and the
      // offset is the one space in this window that is typed rather than written.
      guard: (text, a) => el === valueEl && mode === "sketch" &&
                          a === text.match(/^\s*/)[0].length,
      // The plane line's own brackets round the offset (syncSketchLine) are a bubble
      // like any other pair here — chips.js takes a matched pair on as one on the next
      // render — so they are never drawn as text, nor deleted a half at a time.
      // No unit is ever written onto a bare number typed here: "25" stays the chip "25",
      // which Fusion reads in the dimension's or parameter's own unit anyway. A unit is
      // only ever in the box because it was typed.
      unit: () => "",
      // And "mm" is never SHOWN on one either — typed, pasted or handed back by Fusion,
      // "25 mm" is the chip "25". Only where a bare number already means millimetres: a
      // mm dimension/parameter, or a new one (a bare number is inferred as length). A
      // parameter in cm, or a unitless one, keeps its mm — there it changes the value.
      bareUnits: () => (unit === "mm" || (!unit && mode !== "editparam")) ? ["mm"] : [],
      // The right-click menu. chips.js refuses WebKit's own and asks for this one.
      menu: (info, e) => openChipMenu(el, info, e),
      // Every selection change in any expression field, so "Make parameter" can come
      // and go with it.
      onRender: el === valueEl ? () => { updateMakeButton(); previewSelectionChanged(); }
                               : updateMakeButton
    });
  } catch (_) {}
}

/* Re-read the fields after the page has written to them itself — a session opening,
   a prefill, the bracket rules rewriting a plane line. Typing announces itself with
   an input event and needs none of this; assigning to .value fires nothing. */
function refreshChips(){
  if (typeof Chips === "undefined") return;
  try { Chips.refreshAll(); } catch (_) {}
}

/* A field arriving selected — every session opens this way — is a HELD chip the
   moment it holds one atom, and a held chip's rule is exactly backwards for a
   prefill: typing is meant to REPLACE it, not start a new item beside it. So a field
   is selected as plain text (Chips.editAll, or el.select() with no Chips) rather
   than left to whatever chips.js would otherwise have made of the selection. */
function selectAllAsText(el){
  if (typeof Chips !== "undefined" && Chips.editAll(el)) return;
  el.select();
}

/* A number Fusion measured, not something typed as an expression: "12.5 mm", "-4",
   "30deg". Nothing here is a parameter, so the prefill is exactly what the ordinary
   textbox convention expects — arrive selected as TEXT so the first keystroke wipes
   it, not as a chip already held (see selectAllAsText / openSession). Anything with a
   name in it — even one number combined with a parameter — is a real expression, and
   there the session opens with the caret at the end, every item a chip, nothing held. */
const BARE_LITERAL = /^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?(\s*[A-Za-z]+)?\s*$/;
function isBareLiteral(text){ return BARE_LITERAL.test(text || ""); }

/* A bare literal with more decimals than anyone types — "37.628541 mm" — is where the
   pointer happened to let go, not a value someone chose. A dimension session still shows
   it (to read or copy) but opens with it selected as text, so typing replaces it rather
   than adding beside it. Three or more decimals: nothing typed by hand has that many.
   Trailing zeros don't count ("10.000" is ten). */
const ARBITRARY_DECIMALS = 2;
function isArbitraryMeasure(text){
  if (!isBareLiteral(text)) return false;
  const m = /\.(\d+)/.exec(text || "");
  return !!m && m[1].replace(/0+$/, "").length > ARBITRARY_DECIMALS;
}

/* What a PANEL's expression field (bounds, Extrude, the feature rows) does with what is
   already in it when it arrives: the caret at its end, ready to type the next item —
   nothing held, nothing selected, a lone number included. Selecting a number as text to
   wipe is only for a value Fusion MEASURED — a dimension just placed — and that is the
   value box's business (openSession, isArbitraryMeasure); nothing in these panels is
   one, and selecting here re-ran on every focus, so a lone chip opened as highlighted
   text after each orbit. See Chips.caretAtEnd. */
function selectPrefilled(el){
  if (typeof Chips === "undefined" || !Chips.caretAtEnd(el))
    el.setSelectionRange(el.value.length, el.value.length);
}

/* ── Field plumbing ─────────────────────────────────────────────────────────── */
function autosize(){
  valueEl.style.height = "auto";
  valueEl.style.height = Math.min(valueEl.scrollHeight, 130) + "px";
  placeAC();
}

function focusField(el){
  el.focus();
  el.setSelectionRange(el.value.length, el.value.length);
  refreshFocusStyle();
}
function refreshFocusStyle(){
  fValue.classList.toggle("on", document.activeElement === valueEl);
  // The name field's row is the stepper while it is up — #pname lives inside it then.
  fName .classList.toggle("on", !stepperOn && document.activeElement === nameEl);
  fSteps.classList.toggle("on", stepperOn && document.activeElement === nameEl);
  fSName.classList.toggle("on", document.activeElement === sNameEl);
}
valueEl.addEventListener("focus", () => { attachAC(valueEl, fValue); refreshFocusStyle(); });
nameEl .addEventListener("focus", () => { attachAC(nameEl, stepperOn ? fSteps : fName); refreshFocusStyle(); });
// The sketch name completes against nothing — it is a label, not an expression, and the
// parameter list has no business being offered under it. So no attachAC: the list is
// simply put away, and acField is left pointing at whatever field owns it.
sNameEl.addEventListener("focus", () => {
  acItems = []; acIndex = -1; renderAC(); refreshFocusStyle();
});

/* ── The plane line ──────────────────────────────────────────────────────────
   "Width 100 - Membrane" is the side face, less (100 - Membrane) — everything after the
   datum is subtracted AS A WHOLE, because side - 100 - Membrane is a different number
   and never the one that was meant. So the brackets are not decoration and they are not
   left to be typed: they APPEAR the moment what follows the datum is more than one
   term, with the closing one parked after the caret so typing carries straight on, and
   they go again if it drops back to one.

   The arithmetic itself is Planes (planes.js, the twin of the add-in's planes.py). What
   is here is only where the caret ends up, which is the dialog's business.

   A DELETED HALF OF THAT PAIR PUTS IT BACK. The pair is not yours to take out — while
   there are two terms it is required — and a lone "(" left behind used to be read as
   typed, so it got a fresh pair round it: every ⌫ on the ")" grew one more "(". Now
   ⌫/⌦ on either bracket steps over it instead (e is the input event, when there is
   one; sketchLinePrev is the line as this last left it).                          */
let sketchLinePrev = "";
function syncSketchLine(e){
  if (mode !== "sketch" || typeof Planes === "undefined") return;
  const prev = sketchLinePrev;
  sketchLinePrev = "";
  if (e && e.inputType && /^delete/.test(e.inputType) && restoreSketchPair(prev)){
    sketchLinePrev = valueEl.value;
    return;
  }
  const text = valueEl.value;
  const m = text.match(/^(\s*)(\S+)([ \t]*)([\s\S]*)$/);
  if (!m) return;                        // nothing but the datum being typed
  const head = m[1] + m[2] + m[3];
  const start = head.length;
  let rest = m[4];
  let caret = valueEl.selectionStart;

  // Take off the pair we put there last time, keeping the caret on the same character,
  // so the decision below is made about what was actually typed.
  if (rest.length >= 2 && rest[0] === "(" && rest[rest.length - 1] === ")"
      && Planes.balanced(rest.slice(1, -1))){
    if (caret > start) caret--;
    if (caret > start + rest.length - 2) caret--;
    rest = rest.slice(1, -1);
  }
  if (Planes.needsBrackets(rest)){
    if (caret > start) caret++;
    rest = "(" + rest + ")";
  }

  const next = head + rest;
  sketchLinePrev = next;
  if (next === text && caret === valueEl.selectionStart) return;
  valueEl.value = next;
  caret = Math.max(0, Math.min(caret, next.length));
  valueEl.setSelectionRange(caret, caret);
}

/* The line is `prev` with one of syncSketchLine's own brackets taken out: put it back,
   the caret just inside a ")" (so the next ⌫ takes the last term) or just before a "(".
   False when that is not what happened. */
function restoreSketchPair(prev){
  const text = valueEl.value, c = valueEl.selectionStart;
  if (!prev || text.length !== prev.length - 1 || c !== valueEl.selectionEnd) return false;
  if (prev.slice(0, c) + prev.slice(c + 1) !== text) return false;
  const m = prev.match(/^(\s*)(\S+)([ \t]*)([\s\S]*)$/);
  if (!m) return false;
  const start = m[1].length + m[2].length + m[3].length, rest = m[4];
  if (rest.length < 2 || rest[0] !== "(" || rest[rest.length - 1] !== ")"
      || !Planes.balanced(rest.slice(1, -1))) return false;
  if (c !== start && c !== prev.length - 1) return false;
  valueEl.value = prev;
  valueEl.setSelectionRange(c, c);
  return true;
}

/* Which datum the first word names, and therefore the SITUATION the rest of the line is
   typed in: a plane placed in from the back face is offering values measured from the
   back, exactly as a dimension there would. That is the whole point of the offset half
   completing against parameters at all. */
function syncSketchSituation(){
  if (mode !== "sketch") return;
  const word = (typeof Planes !== "undefined")
    ? Planes.splitInput(valueEl.value)[0]
    : (valueEl.value.trim().split(/\s+/)[0] || "");
  const w = word.toLowerCase();
  curDatum = datums.find(d => d.word.toLowerCase() === w) || null;
  const nAxis = curDatum ? (curDatum.axis || "") : "";
  const nEnd  = curDatum ? (curDatum.end  || "") : "";
  if (nAxis !== axis || nEnd !== end){ axis = nAxis; end = nEnd; invalidateEntries(); }
  datum     = curDatum ? (curDatum.datum || "") : "";
  datumFaces = [];
  symmetric = curDatum ? !!curDatum.symmetric   : false;
}

/* The signed distance from the origin this line describes, as an expression Fusion can
   evaluate — "" until the datum is a word this component actually has. */
function sketchExpression(){
  if (!curDatum || typeof Planes === "undefined") return "";
  const rest = Planes.splitInput(valueEl.value)[1];
  return Planes.offsetExpression(curDatum.kind, curDatum.face, rest, curDatum.sign);
}

/* The readout under the line: the offset expression, and with an angle, how Fusion
   will read the angle and about which axis — the one place the chips' effect is
   spelled out, since the field itself never changes. */
function sketchReadout(){
  const base = sketchExpression();
  if (!base || !hasAngle() || typeof Planes === "undefined") return base;
  const a = angleSpec();
  return base + "  ∠ " + Planes.angleExpression(a.text, a.flip, a.plus90) + " about " + a.pivot;
}

/* ── The angle ───────────────────────────────────────────────────────────────
   Optional. The typed text is what the field shows and what is stored; the chips —
   reverse, quarter-turn, pivot — say how Fusion is to read it and never rewrite it. The
   pivot is one of the two axes lying in the plane (Planes.pivotAxes), the first by
   default and the other on a click. While there is an angle, the plane is PREVIEWED:
   the add-in builds it lit and moves it as this changes, because whether 30° leans the
   way you meant is a thing to see, not to read. */
let angleFlip = false, anglePlus90 = false, anglePivot = "";
// Sticky across sessions — "Planes" stays checked from one sketch to the next until
// clicked off. Independent of the angle: it's a property of "the box is open", not
// of the line being typed, so it lives alongside the angle chips without depending
// on hasAngle().
let showPlanes = false;
try { showPlanes = localStorage.getItem("sketchShowPlanes") === "1"; } catch(_){}
function pivotChoices(){
  if (!curDatum || typeof Planes === "undefined") return ["", ""];
  return Planes.pivotAxes(curDatum.axis || "");
}
function currentPivot(){
  const [d, o] = pivotChoices();
  return (anglePivot === d || anglePivot === o) ? anglePivot : d;
}
function angleSpec(){
  return { text: angleEl.value.trim(), flip: angleFlip, plus90: anglePlus90,
           pivot: currentPivot() };
}
function hasAngle(){ return mode === "sketch" && !!angleEl.value.trim(); }
function renderAngleChips(){
  $("aflip").classList.toggle("on", angleFlip);
  $("a90").classList.toggle("on", anglePlus90);
  const pv = currentPivot();
  $("apivot").textContent = pv ? "About " + pv : "";
  $("aplanes").classList.toggle("on", showPlanes);
}
let planePreviewTimer = null;
function schedulePlanePreview(){
  clearTimeout(planePreviewTimer);
  if (mode !== "sketch" || redefine) return;
  planePreviewTimer = setTimeout(() => {
    const text = valueEl.value.replace(/\s*\n\s*/g, " ").trim();
    post({ action: "previewplane", text: text, angle: angleSpec() });
  }, 250);
}
function resetAngle(spec){
  angleFlip = !!(spec && spec.flip); anglePlus90 = !!(spec && spec.plus90);
  anglePivot = (spec && spec.pivot) || "";
  angleEl.value = (spec && spec.text) || "";
  renderAngleChips();
}
angleEl.addEventListener("input", () => {
  infoEl.textContent = sketchReadout(); schedulePlanePreview(); clearError();
  updateAC();
});
for (const [id, fn] of [["aflip", () => { angleFlip = !angleFlip; }],
                        ["a90",   () => { anglePlus90 = !anglePlus90; }],
                        ["apivot", () => { const [d, o] = pivotChoices();
                                           anglePivot = currentPivot() === d ? o : d; }]]){
  // mousedown + preventDefault, like Copy: a chip must not take focus off the field.
  $(id).addEventListener("mousedown", e => {
    e.preventDefault(); e.stopPropagation();
    fn(); renderAngleChips(); infoEl.textContent = sketchReadout(); schedulePlanePreview();
  });
}
// Chips and completion, the same as the value box and the bounds fields — an angle is
// an expression too, just one whose vocabulary is filtered down to angle parameters
// (see expectedUnitFamily). attachAC hands it the suggestion list the moment it takes
// focus, exactly as valueEl/nameEl/the bounds rows do.
angleEl.addEventListener("focus", () => { attachAC(angleEl, fAngle); fAngle.classList.add("on"); });
angleEl.addEventListener("blur",  () => { fAngle.classList.remove("on"); });
angleEl.addEventListener("click", updateAC);
angleEl.addEventListener("keyup", e => {
  if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") updateAC();
});
chipsAttach(angleEl);

// "Planes" — show every construction plane the component has while this box is open,
// restored the moment it closes (cancel, create, or reopened). Add-in side: see
// _reveal_planes in Dimension Value.py.
$("aplanes").addEventListener("mousedown", e => {
  e.preventDefault(); e.stopPropagation();
  showPlanes = !showPlanes;
  try { localStorage.setItem("sketchShowPlanes", showPlanes ? "1" : "0"); } catch(_){}
  renderAngleChips();
  post({ action: "showplanes", on: showPlanes });
});

/* ── Same plane as ──────────────────────────────────────────────────────────
   "Use the plane of another sketch in this component" — copies that sketch's
   RECIPE (the typed line + its angle spec) into this box, rather than sharing the
   plane object: commit still runs create_plane_sketch and builds fresh scaffolding,
   so the two sketches end up independent, just described the same way.

   sessionSketches is the list the add-in found for THIS session (see
   _open_sketch_dialog's `others`); empty for a value-box session, which is why it is
   reset wherever a session other than the plane box opens (openSession/cancel). */
let sessionSketches = [];

function hidePick(){
  pickEl.classList.add("hide");
  pickEl.innerHTML = "";
  delete pickEl.dataset.for;
}

function placePick(){
  pickEl.style.top = (pageTop(fName) + fName.offsetHeight) + "px";
}

// "30° · reversed · +90° · about Y" — a short description of the angle a sketch's
// recipe carries, for the row beside its name. Not the composed expression
// (Planes.angleExpression): that says what Fusion will evaluate, this says what was
// chosen, which is what tells two angled sketches apart at a glance.
function sketchSpecLabel(sk){
  if (!sk.angle) return "";
  const parts = [];
  const text = (sk.angle.text || "").trim();
  if (text) parts.push(text);
  if (sk.angle.flip) parts.push("reversed");
  if (sk.angle.plus90) parts.push("+90°");
  if (sk.angle.pivot) parts.push("about " + sk.angle.pivot);
  return parts.join(" · ");
}

function renderPick(){
  if (!sessionSketches.length){ hidePick(); return; }
  const angled = sessionSketches.filter(sk => sk.angle);
  const flat   = sessionSketches.filter(sk => !sk.angle);
  const row = sk => {
    const spec = sketchSpecLabel(sk);
    const val = sk.text + (spec ? "  ∠ " + spec : "");
    return '<div class="ac-row" data-token="' + escHtml(sk.token) + '">' +
             '<span class="ac-name">' + escHtml(sk.name) + "</span>" +
             '<span class="ac-val">' + escHtml(val) + "</span>" +
           "</div>";
  };
  let html = angled.map(row).join("");
  if (angled.length && flat.length) html += '<div class="pick-sep">Flat</div>';
  html += flat.map(row).join("");
  pickEl.innerHTML = html;
  pickEl.classList.remove("hide");
  placePick();
}

function applySketchRecipe(sk){
  valueEl.value = sk.text;
  resetAngle(sk.angle || null);
  syncSketchSituation();
  autosize();
  refreshChips();
  infoEl.textContent = sketchReadout();
  schedulePreview();
  schedulePlanePreview();
  updateAC();
  hidePick();
}

// mousedown + preventDefault, like the angle chips: opening the picker must not take
// focus off the value field, which is what Enter and the arrow keys still act on.
sameEl.addEventListener("mousedown", e => {
  e.preventDefault(); e.stopPropagation();
  renderPick();
});
pickEl.addEventListener("mousedown", e => {
  e.preventDefault(); e.stopPropagation();
  const row = e.target.closest(".ac-row");
  if (!row) return;
  const sk = sessionSketches.find(s => s.token === row.dataset.token);
  if (sk) applySketchRecipe(sk);
});
// Anywhere else in the panel closes it, the same as clicking away from a menu.
panel.addEventListener("mousedown", e => {
  if (!pickEl.classList.contains("hide") &&
      !e.target.closest("#pick, #same")) hidePick();
});
valueEl.addEventListener("blur", hidePick);

/* ── Picked from the viewport ────────────────────────────────────────────────
   A face (or, moving an existing sketch, an edge or sketch line) clicked in Fusion
   while the box is up: the add-in reads its position back into datum lines, best
   first (_watch_plane_pick → fusion_ui/plane_guess.py), and they arrive as `pick` /
   `pickSeq`. The first goes straight into the line; ⌥↓ / ⌥↑ (or the arrows on
   #pickcycle) step through the rest. Typing in the line lets the list go — a
   suggestion is a starting point, not a mode. */
let lastPickSeq = null;
let lastEscSeq = null;
let pickItems = [], pickIdx = 0, pickSetting = false;
const pickCycleEl = $("pickcycle");

function dropPickCycle(){
  pickItems = []; pickIdx = 0;
  pickCycleEl.classList.add("hide");
  fName.classList.remove("picking");
  pickCycleEl.innerHTML = "";
}

function renderPickCycle(){
  if (pickItems.length < 1){ dropPickCycle(); return; }
  const it = pickItems[pickIdx] || {};
  const many = pickItems.length > 1;
  pickCycleEl.innerHTML =
    (many ? '<span class="pc-arrow" data-d="-1">‹</span>' : "") +
    '<span class="pc-count">' + (pickIdx + 1) + "/" + pickItems.length + "</span>" +
    (many ? '<span class="pc-arrow" data-d="1">›</span>' : "") +
    (it.note ? '<span class="pc-note">' + escHtml(it.note) + "</span>" : "");
  pickCycleEl.classList.remove("hide");
  fName.classList.add("picking");
}

function setPickedLine(text){
  pickSetting = true;
  try {
    valueEl.value = text;
    syncSketchLine(); syncSketchSituation();
    autosize(); fitWidth(); refreshChips();
    infoEl.textContent = sketchReadout();
    schedulePreview(); schedulePlanePreview();
    clearError();
    const n = valueEl.value.length;
    valueEl.setSelectionRange(n, n);
  } finally { pickSetting = false; }
}

function applyPick(p){
  if (p.error){ dropPickCycle(); showError(p.error); return; }
  pickItems = p.items || [];
  pickIdx = 0;
  if (!pickItems.length){ dropPickCycle(); return; }
  setPickedLine(pickItems[0].text);
  renderPickCycle();
}

function cyclePick(step){
  if (pickItems.length < 2) return;
  pickIdx = (pickIdx + step + pickItems.length) % pickItems.length;
  setPickedLine(pickItems[pickIdx].text);
  renderPickCycle();
}

pickCycleEl.addEventListener("mousedown", e => {
  e.preventDefault(); e.stopPropagation();     // keep the caret; not a window drag
  const a = e.target.closest(".pc-arrow");
  if (a) cyclePick(+a.dataset.d);
});

/* The dimension box's pick (mode 'dim'): the add-in measures the picked face/edge/point
   from the end the dimension starts at, along the axis it measures, and sends ONE
   expression that adds up to it (_watch_dim_pick → plane_guess.suggest_distance). It
   replaces the value — no cycling — with the caret at the end, which is where the host's
   reclaim (__refocus) puts the keyboard back once the click in the viewport is over. */
function applyDimPick(p){
  if (p.error){ showError(p.error); return; }
  const it = (p.items || [])[0];
  if (!it) return;
  openSelectAll = false;
  valueEl.value = it.text;
  autosize(); fitWidth(); refreshChips(); schedulePreview(); clearError();
  const n = valueEl.value.length;
  valueEl.setSelectionRange(n, n);
  guardCaret(valueEl);
  refreshFocusStyle();
}

/* ── Live evaluation ─────────────────────────────────────────────────────────
   Fusion is the only thing that can actually parse an expression (it owns the units
   and the parameter values), so the preview is a round trip. Debounced, and the reply
   carries the expression it answers so a late one can't overwrite a newer answer.   */
let previewTimer = null, previewAsked = "";
function schedulePreview(){
  if (mode === "sketch") infoEl.textContent = sketchReadout();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const e = previewExpr();
    previewAsked = e;
    if (e) post({ action: "preview", expression: e, unit: unit });
    else prevEl.textContent = "";
  }, 130);
}

/* THE READOUT FOLLOWS WHAT IS HELD. Click a parameter chip and it says what that
   parameter is worth; click a bubble (or ⇧/⌘-click a run) and it says what that part
   comes to. Nothing held — or a held operator, or a held number that would only echo
   itself — and it is the whole box again. Not the plane line: its datum is a face, and
   a piece of it is not a value. */
function heldExpr(){
  if (typeof Chips === "undefined" || mode === "sketch") return "";
  if (document.activeElement !== valueEl) return "";
  const sel = Chips.selectionOf(valueEl);
  if (sel) return (sel.whole !== false && !previewEchoes(sel.expr)) ? tidyBrackets(sel.expr) : "";
  const a = valueEl.selectionStart, b = valueEl.selectionEnd;
  if (a === b || !Chips.isItemRange(valueEl, a, b)) return "";
  const word = valueEl.value.slice(a, b).trim();
  return params.some(p => p.name === word) ? word : "";
}
function previewExpr(){ return heldExpr() || exprText(); }

/* Every repaint of the value box — a click, an arrow, a shift-click — may have changed
   what is held. Only asks again when that changes what the readout is answering. */
function previewSelectionChanged(){
  if (isOpen && previewExpr() !== previewAsked) schedulePreview();
}

/* A NUMBER SHOWS ITS DECIMALS, AND ONLY ITS DECIMALS. Fusion formats a preview to the
   document's display precision, so a round answer comes back as "502.00 mm" — two
   digits that say nothing except that the setting is 2. What is worth reading is
   whether the sum came out round at all, and ".00" is the one spelling of that which
   makes it hard to see. Trailing zeros go, and the point with them if it has nothing
   left after it; "0.55 mm" and "12.5 mm" are untouched, because those digits are the
   answer. Display only — nothing Fusion is handed ever passes through here. */
function trimZeros(text){
  return String(text == null ? "" : text)
    .replace(/(\d)\.(\d*?)0+(?=\D|$)/g, (m, whole, keep) => keep ? whole + "." + keep : whole);
}

/* The result is only worth showing when it says something the box does not: a lone
   number evaluates to itself, so "25" (or "25 mm") beside a "25" is noise. Only once
   there is a formula, a parameter or a unit conversion in play does it earn its place. */
function previewEchoes(expr){
  if (!isBareLiteral(expr)) return false;
  // A lone number in some OTHER unit ("1 in" in a mm dimension) is a conversion,
  // which is worth reading. Fusion rounds the preview to its display precision, so
  // the two are compared by unit, never by value.
  const u = (/[A-Za-z]+\s*$/.exec(expr) || [""])[0].trim();
  return !u || u === unit;
}

/* "0.55 mm" shown as "0.55" — the unit is the parameter's, not something to retype, and
   the evaluated result on the right says what it is anyway. Same rule the Params window
   uses when it opens a value for editing, and deliberately as conservative: ONLY a bare
   number carrying the unit is stripped. Touching a compound expression ("X + 150 mm")
   would mean trusting that a bare 150 reads back as millimetres, which is only true when
   the document's default length unit happens to match this parameter's.

   Editing an existing parameter or a dimension: "400 mm" on a dimension opens as the
   chip "400". A bare number reads back in the dimension's own unit, so nothing changes. */
function stripUnit(expr, u){
  if ((mode !== "editparam" && mode !== "dim") || !u) return expr;
  const m = expr.match(new RegExp("^\\s*([-+]?[0-9.]+)\\s*" + u.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*$"));
  return m ? m[1] : expr;
}

/* Brackets sit tight against what they enclose: "( a + b )" is committed and shown as
   "(a + b)", and "sqrt (2)" as "sqrt(2)". Only the space INSIDE a bracket and the one
   between a name and its opening bracket go — the spaces around operators are left
   alone, so "2 * (3 + 4)" keeps its shape rather than collapsing to "2*(3+4)".
   Whitespace is not significant to Fusion's parser, so this is purely cosmetic. */
function tidyBrackets(e){
  return String(e == null ? "" : e)
    .replace(/\(\s+/g, "(")                    // "( x"    -> "(x"
    .replace(/\s+\)/g, ")")                    // "x )"    -> "x)"
    .replace(/([A-Za-z0-9_])\s+\(/g, "$1(");   // "sqrt (" -> "sqrt("
}

/* Newlines in the value box are cosmetic — the same convention the Params window uses
   for its multi-line expressions — so they collapse to spaces on the way to Fusion. */
function exprText(){
  // A plane line is not itself an expression — the datum word is a face, not a
  // parameter. What Fusion is asked to evaluate is what the line COMPOSES to, which is
  // also what gets written into the plane.
  if (mode === "sketch") return sketchExpression();
  return tidyBrackets(valueEl.value.replace(/\s*\n\s*/g, " ").trim());
}

valueEl.addEventListener("input", (e) => {
  // Brackets first, then the situation: what completes after the datum depends on which
  // datum it is, and both have to be settled before the list is built.
  syncSketchLine(e); syncSketchSituation();
  autosize(); fitWidth(); updateAC(); schedulePreview(); clearError();
  if (mode === "sketch"){ renderAngleChips(); if (hasAngle()) schedulePlanePreview(); }
  if (!pickSetting) dropPickCycle();
});
/* Copy. mousedown rather than click, and preventDefault with it, so the press never
   takes focus off the field — pressing a button beside the box should not end what you
   were typing in it. */
$("copy").addEventListener("mousedown", (e) => {
  e.preventDefault();
  e.stopPropagation();                        // not a window drag — see startDrag
  const text = exprText();
  if (!text) return;
  // The SERVER puts it on the clipboard (pbcopy). Not the page: this window is a
  // WKWebView loaded over plain http, where navigator.clipboard needs a permission it
  // can never be granted and execCommand("copy") is a deprecated path that also steals
  // focus out of the field to do its work. The server is already there and already
  // trusted with everything else this dialog does.
  fetch(API + "/clipboard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text })
  }).then(r => r.json()).then(r => { if (r && r.ok) said("Copied"); else said("Failed"); },
           () => said("Failed"));
});
let copyTimer = null;

/* Say what happened, briefly, on the button itself — there is nowhere else for it to go,
   and a copy that silently did nothing is the worst version of this. */
function said(word){
  const el = $("copy");
  el.textContent = word;
  el.classList.toggle("done", word === "Copied");
  el.classList.toggle("bad", word === "Failed");
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    el.textContent = "Copy";
    el.classList.remove("done", "bad");
  }, 1100);
}

/* ── Make a parameter ──────────────────────────────────────────────────────────
   Two or more ⇧/⌘-clicked chips, one held bubble, one held number or one held call can
   become a parameter of their own — in ANY expression field: the value box (the plane
   box's offset included, never its datum word), the plane box's angle, and every
   expression row of the bounds, Extrude and feature panels. "Make parameter" appears
   in that field's own row (#mkparam is moved there — updateMakeButton) and on its chip
   menu; it swaps #mpanel in over the panel with that span as the expression, you name
   it, and once Fusion has made it (makeparam → set_made) the name goes back into the
   field in place of the span, held — through Chips.replaceSpan, whose input event is
   what the field's own panel acts on. The session is untouched throughout — Esc here
   only comes back to it, never cancels it.

   The span is fixed when the panel opens: the field cannot change while it is hidden,
   so [a, b) is still right when the answer comes back. */
/* WHAT IS SELECTED HAS TO BE AN EXPRESSION. A chip and the "+" beside it is a
   perfectly good selection and "Width +" is not a value — Fusion would refuse to make a
   parameter of it, and offering the button for it is offering a thing that cannot work.
   So the test is chips.js's own wholeExpr (reported as `whole` on the selection): two or
   more operands, an operator between each pair, nothing dangling at either end. */
function canMake(el){
  el = el || valueEl;
  if (!isOpen || making || typeof Chips === "undefined") return false;
  if (!Chips.attached(el)) return false;
  if (el === valueEl){
    if (mode !== "dim" && mode !== "param" && mode !== "editparam" && mode !== "sketch")
      return false;
    // A driven value is the geometry's, only named: making a parameter out of it would
    // be a frozen copy of the measurement, not a link — naming the dimension IS the link.
    if (isDriven) return false;
  } else if (el === angleEl && mode !== "sketch") return false;
  const sel = Chips.selectionOf(el);
  if (!sel || sel.whole === false) return false;
  // The plane line's first word is a face, not a value: only the offset after it.
  if (el === valueEl && mode === "sketch"){
    const m = el.value.match(/^\s*\S+[ \t]*/);
    if (!m || sel.a < m[0].length) return false;
  }
  return true;
}
/* The field the button is for: the one with the keyboard, if it is an expression field,
   else the value box. */
let makeFor = null;
function makeField(){
  const f = document.activeElement;
  return (f && typeof Chips !== "undefined" && Chips.attached(f)) ? f : valueEl;
}
function updateMakeButton(){
  const el = makeField();
  const row = canMake(el) ? el.parentNode : null;
  makeFor = row ? el : null;
  try {
    const was = mkBtn.parentNode;
    if (was && was !== row && was.classList) was.classList.remove("canmake");
    if (row && was !== row) row.appendChild(mkBtn);
    if (row) row.classList.add("canmake");
  } catch (_) {}
}

mkBtn.addEventListener("mousedown", (e) => {
  e.preventDefault();
  e.stopPropagation();                        // not a window drag — see startDrag
  openMake(makeFor);
});

function openMake(el){
  el = el || makeField();
  if (!canMake(el)) return;
  mTitleEl.textContent = dialogTitle("make");
  making = Object.assign({}, Chips.selectionOf(el), { el: el, back: panelShown });
  setMakePending("");
  acItems = []; acIndex = -1; renderAC();
  mExprEl.textContent = making.expr;
  mNameEl.value = "";
  $("fmname").classList.remove("bad");
  mErr.textContent = "";
  showPanel("make");
  makeStepperOpen();
}

/* "Edit parameter", from a parameter chip's right-click menu: the make panel again,
   nested the same way (Esc only comes back), with the parameter's own expression in an
   editable box and its name in the plain name row. Saving posts "editmade"; the add-in
   writes it (behind Extrude / a feature panel, through that command's own queue — see
   Made Params.py) and answers on the same channel as a made parameter. A rename is
   carried into every field here (editedInFields): Fusion rewrites expressions in the
   model, but not the text of a box that is still open. */
function openEditParam(el, chip){
  if (!isOpen || making) return;
  const p = params.find(x => x.name === chip.name);
  if (!p) return;
  mTitleEl.textContent = "Edit Parameter";
  making = { el: el, a: chip.a, b: chip.b, expr: p.expression || "", back: panelShown, edit: p.name };
  setMakePending("");
  acItems = []; acIndex = -1; renderAC();
  $("fmexpr").classList.add("hide");
  $("fmedit").classList.remove("hide");
  mEditEl.value = stripUnit(p.expression || "", p.unit || "") || p.expression || "";
  mNameEl.value = p.name;
  $("fmname").classList.remove("bad");
  mErr.textContent = "";
  showPanel("make");
  mNameEl.focus();
  mEditEl.focus(); mEditEl.select();
}

function commitEditMake(){
  const old = making.edit;
  const name = mNameEl.value.trim();
  const expr = mEditEl.value.trim();
  const p = params.find(x => x.name === old);
  let why = "";
  if (!expr) why = "Give the parameter a value.";
  else if (name !== old){
    if (!name) why = "Give the parameter a name.";
    else if (!NAME_RE.test(name))
      why = '"' + name + '" is not a valid parameter name — letters, digits and underscores, not starting with a digit.';
    else if (params.some(q => q.name === name)) why = 'A parameter named "' + name + '" already exists.';
  }
  if (why){ mErr.textContent = why; $("fmname").classList.add("bad"); return; }
  if (name === old && expr === (p && p.expression || "")){ closeMake(); return; }
  setMakePending(name);
  post({ action: "editmade", name: old, expression: expr, newName: name !== old ? name : "" })
    .then(r => { if (!r || !r.ok) showError("Fusion did not take that — is the add-in up to date?"); });
}

/* A parameter was renamed: its old name, as a whole word, becomes the new one in every
   expression field of the open panel. */
function editedInFields(from, to){
  if (!from || !to || from === to) return;
  const re = new RegExp("(^|[^\\w.])" + from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![\\w])", "g");
  document.querySelectorAll("input, textarea").forEach(el => {
    if (typeof Chips === "undefined" || !Chips.attached(el)) return;
    const v = el.value;
    const next = v.replace(re, (m, pre) => pre + to);
    if (next === v) return;
    el.value = next;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/* Back to the panel the make was opened from, the keyboard in the field it was for. */
function closeMake(){
  const m = making;
  making = null; setMakePending("");
  $("fmexpr").classList.remove("hide");
  $("fmedit").classList.add("hide");
  showPanel((m && m.back) || "value");
  ((m && m.el) || valueEl).focus();
  refreshChips();
}

function commitMake(){
  if (!making || makePending) return;
  if (making.edit){ commitEditMake(); return; }
  const stepped = !!makeSaved && stepperOn;
  const name = stepped ? stepperName() : ParamNaming.joinName(mNameEl.value);
  let why = name === origName && name ? 'A parameter named "' + name + '" already exists.'
                                      : nameProblem(name);
  // The stepper's own plans, as a session commit runs them: "Make name"/"Make group"
  // and "Make as group" take the parameters already at that place along.
  const pinMoves = stepped && !why ? lastPinPlan(name) : [];
  if (typeof pinMoves === "string") why = pinMoves;
  const siblingMoves = stepped && !why && stepTypeGroup ? typeGroupSiblingPlan(name) : [];
  if (siblingMoves === null) why = "Making that a group would collide with an existing parameter.";
  if (why){ mErr.textContent = why; $("fmname").classList.add("bad"); return; }
  applyRenames(pinMoves);
  applyRenames(siblingMoves);
  if (stepped) postStepShow(name, "");
  setMakePending(name);
  // A refused post would otherwise leave makePending set and every later Enter ignored.
  post({ action: "makeparam", expression: making.expr, name: name })
    .then(r => { if (!r || !r.ok) showError("Fusion did not take that — is the add-in up to date?"); });
}

/* ── The make panel's name: the name stepper, borrowed ──────────────────────────
   A parameter made from a selection is named the way every other new one is — the
   A-S-D-F stepper, Parent → groups → type → anchor/axis, to the convention. The panel
   BORROWS it: #fsteps (with #pname in it) moves into #mpanel in place of the plain
   #mname row, and the session's own stepper — a 'dim' session may be half-way through
   naming its own parameter — is set aside (makeSaved) and put back, untouched, when the
   panel goes (makeStepperClose, from showPanel). The finishing key or pick commits the
   MAKE (stepCommit), not the session.

   The parent step needs the component path. A dimension/parameter session has it; the
   others (Extrude, the plane box, the bounds and feature panels) ask the add-in
   ("makeinfo" → set_make_info → makeInfo/makeInfoSeq), and until it answers — or with
   an add-in too old to — the plain name row stands in. */
let makeSaved = null;          // the session's stepper state while the make panel has it
let makeInfoWait = false;      // a "makeinfo" is out, for the make panel now up
let lastMakeInfoSeq = null;
const fStepsHome = { parent: fSteps.parentNode, next: fSteps.nextSibling };

function stepperState(){
  return { stepperOn, stepLevels, stepStage, stepType, stepAnchors, stepAxes, stepNone,
           stepTypeGroup, stepLastPin, stepShow, stepShowHad, stepWide, sessionComponent,
           sessionComponentPath, sessionComponents, sessionSymmetry, sessionFiled,
           stepOwnerName, stepOwnerSet, ownerChain, stepAcClosed, stepTyping, stepFinding,
           stepInsert, stepEdit, stepChipEdited, stepMulti, stepMultiAnchor, stepLegacy,
           stepLegacyPrev, stepRenaming, nameValue: nameEl.value,
           nameDisplay: nameEl.style.display };
}
function setStepperState(k){
  ({ stepperOn, stepLevels, stepStage, stepType, stepAnchors, stepAxes, stepNone,
     stepTypeGroup, stepLastPin, stepShow, stepShowHad, stepWide, sessionComponent,
     sessionComponentPath, sessionComponents, sessionSymmetry, sessionFiled,
     stepOwnerName, stepOwnerSet, ownerChain, stepAcClosed, stepTyping, stepFinding,
     stepInsert, stepEdit, stepChipEdited, stepMulti, stepMultiAnchor, stepLegacy,
     stepLegacyPrev, stepRenaming } = k);
  nameEl.value = k.nameValue;
  nameEl.style.display = k.nameDisplay;
}

function makeStepperOpen(){
  makeInfoWait = false;
  const knows = sessionComponentPath.length > 0 || !!sessionComponent;
  if (!paramNamingReady || !((mode === "dim" || mode === "param" || mode === "editparam") && knows)){
    mNameEl.focus();
    if (paramNamingReady){
      makeInfoWait = true;
      post({ action: "makeinfo" });
    }
    return;
  }
  makeStepperStart(null);
}

// A fresh name, from Parent — the same start a brand-new parameter session gets.
function makeStepperStart(info){
  if (!making) return;
  if (!makeSaved) makeSaved = stepperState();
  if (info){
    sessionComponent = info.component || "";
    sessionComponentPath = Array.isArray(info.componentPath) ? info.componentPath.slice() : [];
    sessionComponents = Array.isArray(info.components) ? info.components.slice() : [];
    sessionSymmetry = info.symmetry || {};
  }
  stepperOn = true;
  stepAcClosed = false; stepTyping = false; stepFinding = false; stepInsert = null;
  stepEdit = null; stepChipEdited = false; stepRenaming = null;
  stepMulti = []; stepMultiAnchor = ""; stepLegacy = ""; stepLegacyPrev = "";
  sessionFiled = null; stepWide = false; stepTypeGroup = false; stepLastPin = null;
  stepShow = {}; stepShowHad = false;
  fillOwner(null);
  stepOwnerSet = false;
  stepLevels = [stepOwner()];
  stepType = null; stepAnchors = []; stepAxes = []; stepNone = false;
  stepStage = "parent";
  nameEl.value = "";
  // The plane box hides #pname outright (a plane has no name) — and a field that is
  // display:none cannot take the keyboard, so the menu drew but no key reached it.
  nameEl.style.display = "";
  mPanel.insertBefore(fSteps, $("fmname"));
  $("fmname").classList.add("hide");
  renderSteps();
  nameEl.focus();
}

// The make panel is going: #fsteps home, and the session's own naming back as it was.
function makeStepperClose(){
  makeInfoWait = false;
  $("fmname").classList.remove("hide");
  if (!makeSaved) return;
  fStepsHome.parent.insertBefore(fSteps, fStepsHome.next);
  setStepperState(makeSaved);
  makeSaved = null;
  acItems = []; acIndex = -1; renderAC();
  renderSteps();
}

// A pick or key that finished the name: the make's, while the panel has the stepper.
function stepCommit(){
  if (making) commitMake(); else commit();
}

/* Fusion made it. params already lists it (set_made sends the two together), so it
   lands as a chip, not a word. */
function finishMake(name, edited){
  const m = making;
  closeMake();
  if (!m) return;
  if (m.edit){
    if (edited && edited.new) editedInFields(edited.old, edited.new);
    refreshChips();
    schedulePreview();
    return;
  }
  Chips.replaceSpan(m.el, m.a, m.b, name);
  if (m.el === valueEl){ autosize(); fitWidth(); schedulePreview(); }
}

/* A parameter clicked in the Browser while a value is being edited: it goes in at the
   value box's caret, as a chip, exactly as if it had been picked off the list (a held
   chip is replaced, like typing over it; next to another item it gets its "+"). The
   caret is read off the field itself — the click went to another window, and a field
   keeps its selection while its window is not key. If the keyboard was last in another
   field (the name), the name goes on the END of the value instead. The session's own
   parameter is refused: a value can't be made of itself. */
function insertParam(name){
  if (!isOpen || !name || making || !canRefocus(valueEl)) return;
  if (mode === "editparam" && name === origName) return;
  let t = valueEl.value;
  let a = valueEl.selectionStart, b = valueEl.selectionEnd;
  if (lastFocus !== valueEl || a == null){ a = b = t.length; }
  // Two words side by side would read as one: give each the gap Chips.accept spaces.
  if (b < t.length && /[\w.]/.test(t[b])){ t = t.slice(0, b) + " " + t.slice(b); }
  if (a > 0 && /[\w.)]/.test(t[a - 1])){ t = t.slice(0, a) + " " + t.slice(a); a++; b++; }
  valueEl.value = t;
  const r = Chips.accept(valueEl, a, b, name, "param");
  valueEl.focus();
  valueEl.setSelectionRange(r.caret, r.caret);
  lastFocus = valueEl; lastSel = [r.caret, r.caret];
  autosize(); fitWidth(); schedulePreview(); refreshFocusStyle();
}

mNameEl.addEventListener("input", () => {
  $("fmname").classList.remove("bad");
  mErr.textContent = "";
});

/* The panel's two buttons — the same two things Esc and Enter already do, said where
   they can be clicked. mousedown + preventDefault, like every other control on these
   panels: pressing a button beside a field must not take the focus out of it. */
$("mback").addEventListener("mousedown", (e) => {
  e.preventDefault(); e.stopPropagation();       // not a window drag — see startDrag
  closeMake();
});
$("msave").addEventListener("mousedown", (e) => {
  e.preventDefault(); e.stopPropagation();
  commitMake();
});

valueEl.addEventListener("click", updateAC);
// What is held only counts while the box has the keyboard (see heldExpr).
valueEl.addEventListener("focus", previewSelectionChanged);
valueEl.addEventListener("blur", previewSelectionChanged);
valueEl.addEventListener("keyup", e => {
  if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") updateAC();
});

/* ── The chip menu ────────────────────────────────────────────────────────────
   Right-clicking the value box puts THIS up, not WebKit's. chips.js refuses the native
   one outright (its menu is a text field's — Cut, Paste, Look Up, spelling — and none
   of it applies to a row of objects), takes hold of the chip under the pointer, and
   calls back here with what is now selected.

   Every item is something a key already does, and says which key: this is a reminder of
   the keyboard rather than a second way of working. "Make parameter" appears on exactly
   the same test the button does (canMake), so the two can never disagree about whether
   the selection is something a parameter can be made of. */
const chipMenuEl = $("chipmenu");
let chipMenuActions = {};
let chipMenuField = null;      // the field it was opened over — see openChipMenu

function chipMenuOpen(){ return !chipMenuEl.classList.contains("hide"); }
function hideChipMenu(){
  chipMenuEl.classList.add("hide");
  chipMenuEl.innerHTML = "";
  chipMenuActions = {};
  chipMenuField = null;
}

function openChipMenu(el, info, e){
  const sel = info && info.selection;
  const chip = info && info.chip;
  const rows = [];
  const add = (id, label, key, run) => { rows.push({ id, label, key }); chipMenuActions[id] = run; };
  chipMenuActions = {};
  chipMenuField = el;

  if (chip) add("open", "Open", "F", () => Chips.openHeld(el));
  // canMake is the same test the button in the field's row uses.
  if (canMake(el)) add("make", "Make parameter", "", () => openMake(el));
  if (chip && chip.kind === "param" && !making && params.some(p => p.name === chip.name))
    add("editparam", "Edit parameter", "", () => openEditParam(el, chip));
  if (sel || chip) add("copy", "Copy", "", () => copyChipText(el, sel, chip));
  if (sel || chip) add("del", "Delete", "⌫", () => Chips.deleteHeld(el));
  // In the field itself, not on a chip: whether the list is filtered to the situation
  // (axis/end, unit, frame) at all. Off offers every match; the next session is back on.
  if (!chip){
    add("filter", "Filter suggestions", "", () => { acFiltering = !acFiltering; updateAC(); });
    rows[rows.length - 1].check = acFiltering;
  }
  showChipMenu(rows, e);
}
// `rows` [{id, label, key, check?}], their actions already in chipMenuActions, at the
// pointer. `check` (true/false) makes the row a toggle, ticked when true.
function showChipMenu(rows, e){
  if (!rows.length) return;
  chipMenuEl.innerHTML = rows.map(r =>
    `<div class="cm-row" data-id="${r.id}">` +
    (r.check === undefined ? "" : `<span class="cm-check">${r.check ? "✓" : ""}</span>`) +
    escHtml(r.label) +
    (r.key ? `<span class="cm-key">${escHtml(r.key)}</span>` : "") + `</div>`).join("");
  chipMenuEl.classList.remove("hide");
  // At the pointer, then pulled back inside the window if it would hang off an edge —
  // this window is sized to its panel, so there is no room to spare on any side.
  const w = chipMenuEl.offsetWidth, h = chipMenuEl.offsetHeight;
  const x = Math.max(2, Math.min(e.clientX, window.innerWidth  - w - 2));
  const y = Math.max(2, Math.min(e.clientY, window.innerHeight - h - 2));
  chipMenuEl.style.left = x + "px";
  chipMenuEl.style.top  = y + "px";
}

/* The selection as text on the system clipboard — the server does it (pbcopy), for the
   same reason #copy's does: navigator.clipboard needs a permission a plain-http
   WKWebView can never be granted. */
function copyChipText(el, sel, chip){
  const text = (sel && sel.expr) || (chip && chip.name) ||
               (chip ? el.value.slice(chip.a, chip.b) : "");
  if (!text) return;
  fetch(API + "/clipboard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text })
  }).catch(() => {});
}

// mousedown, like every other control here: the press must not take focus off the box.
chipMenuEl.addEventListener("mousedown", e => {
  e.preventDefault(); e.stopPropagation();
  const row = e.target.closest(".cm-row");
  if (!row) return;
  const run = chipMenuActions[row.dataset.id];
  const field = chipMenuField || valueEl;
  hideChipMenu();
  field.focus();
  if (run) run();
});
// A press anywhere else closes it, the way a menu does.
document.addEventListener("mousedown", e => {
  if (chipMenuOpen() && !e.target.closest("#chipmenu")) hideChipMenu();
}, true);

/* ── The name stepper ──────────────────────────────────────────────────────────
   The guided creation flow from fusion_ui/param_naming.js: parent, feature levels,
   then type, then anchor/axis. Runs in place of the plain Name field for 'param' and
   'dim' sessions and for 'editparam' sessions. An edited name that already parses
   opens as its chips; one that doesn't opens as ONE chip holding the whole name
   (stepLegacy) — click it (or Space) for the normal naming from Parent, X putting the
   old name back; delete it (Backspace) to start the normal naming outright. One the
   Browser has hand-filed (session `filed`) starts on the group menu instead, the
   component and groups it is filed under already chips (stepLegacyRename). The file
   missing or the session's component unknown leaves #pname a plain text field.

   ONE ELEMENT. #fsteps is a title naming the step (STEP_TITLES), the name so far (the
   crumb) and the step's keyed menu. #pname lives inside it and is the keyboard focus
   throughout, but is only SEEN while a "type your own" choice (Space) is being typed —
   then it takes the menu's place. See placeNameField.

   THE PARENT is the first step (see "The parent"): A the active component (or the
   edited name's own owner), S, D, F … the components above it up to the root; Space
   finds any component by name. Its name is the parameter's first level — never typed.

   THE GROUPS are a keyed menu, like every step after them. It opens on the owner's
   top-level groups; a key takes one and the menu becomes THAT group's children, as
   deep as you like. The path chosen so far is stepLevels (owner first) and reads out
   in the crumb.
     A S D F …   into that group
     X           back out of the last one — from the owner's own groups, back to Parent
     Space       New group: #pname takes typing (stepTyping) — "Lid, Hinge" makes a
                 group and a subgroup inside it, both under the path browsed to. The
                 shared #ac list still offers what already exists there (Enter drills
                 in, Esc closes it); Enter with none highlighted takes the text; Esc
                 with the list closed, or Backspace on an empty field, is back to the menu
     Enter       this group is the one → the type step
   A group is only a name prefix, so one the owner hasn't got yet — typed new, or kept
   across a change of owner — simply comes into being when the parameter is made.

   THE REST (type, anchor/axis) is a short keyed list: A S D F R C E W Z Q select a
   choice outright; any other letter is refused. X IS ALWAYS CANCEL and never a choice — the one key on these lists that
   means the same thing on every step, so it is held out of the pool rather than handed
   to whichever choice happened to land there. Backspace on an empty field steps back
   the same way (from the type step, to the group menu with its path intact). Enter or Space on an optional step (or a 'dim' session's own
   axis/anchor, offered first) takes the default. An Enter that finishes the name
   commits it — no second Enter. Holding Option widens branch/type suggestions from
   this owner (or this parsed name's own prefix) to the whole document / every unit.

   MORE THAN ONE DIRECTION. A type marked `multi` (Control Room ▸ Fusion ▸ Parameter
   Names) lets the anchor/axis step take several: each key or click adds its word and
   the step STAYS OPEN, pressing it again takes the word back out, and Enter (or Space)
   finishes. The chosen ones are marked in the list and read out in the crumb, so the
   step you are on always shows what it has. A one-direction type is unchanged: the
   first pick finishes the name, as it always did.

   #pname never holds the name being built — that lives in stepLevels/stepType/
   stepAnchors/stepAxes and is echoed, read aloud ("Boot Ceiling, Front - Inset -
   Back"), in #stepcrumb. The real name is compose()'s; the crumb is only for reading. */
const STEP_KEYS = "asdfrcewzq".split("");
const STEP_CANCEL_KEY = "x";   // held out of STEP_KEYS: see the note above
// "Make as group" (below): its own key, held out of STEP_KEYS the same way X is, so a
// type never lands on it by list position.
const STEP_GROUP_KEY = "g";
// "Insert new" (on a clicked parent or group chip): held out the same way.
const STEP_INSERT_KEY = "i";

let stepperOn   = false;   // this session's Name field is the stepper, not plain text
let stepLevels  = [];      // chosen feature levels; [0] is the fixed, un-typed prefix
let stepStage   = "parent"; // 'parent' | 'level' | 'type' | 'anchor' | 'axis' | 'done'
let stepType    = null;
// The chosen directions. Both are LISTS because a type can take more than one — "X
// and Y both work" (param_naming.json's `multi`) — and a one-direction type simply
// never gets a second word in its list.
let stepAnchors = [];      // anchor WORDS ('Back'…)
let stepAxes    = [];      // axis LETTERS ('X'…)
let stepNone    = false;   // the type step's "None": the name is just an exempt word
// The add-in's PROPOSAL for a new dimension's type and direction ({type, anchors, axes},
// Dimension Value.py's _proposal — from what the dimension is measured from and whether
// it runs through material). Applied ONCE, when the stepper first reaches the type step,
// so naming a dimension asks only for its feature word; going Back to the type step, or
// clicking its chip, asks as usual. `stepProposed` carries it through the direction steps.
let stepProposal = null;
let stepProposed = false;
// A name that exists already but differs from it only by axis (a "doppelganger") is not
// a collision: the add-in gives both their axes (doppel in the commit).
let doppelName = "";
// "Make as group" — off by default, a plain toggle (not a cycling one) on the type
// step: G, or a click on its own chip, flips it. On, the TYPE becomes a group level of
// its own (ParamNaming.typeGroupAt) instead of riding at the end of every sibling's
// name — Owner_Shaft_Span_Entry_Span_X, not Owner_Shaft_Entry_Span_X. Composed by
// stepResult/stepperName; commit() moves any existing sibling under the same type into
// it too (see "Make as group" near commit()).
let stepTypeGroup = false;
// "Make name" / "Make group" — the last group chip clicked, G flips what its word is:
// the parameter's own NAME or a GROUP it sits in (ParamNaming's PIN, the double
// underscore). {key, is}: `key` is the level chain it was set for (chainKey), so a
// different chain — a group changed ahead of it — no longer carries it. commit() takes
// every other parameter at that place the same way (lastPinPlan).
let stepLastPin = null;
// Shown / hidden parts of the Browser's row (⌘E, or right-click, on a name, type or
// direction chip): {name?, type?, dir?}, each true/false over ParamNaming.leafShown's
// default reading. Kept by the Browser per parameter (folders.json `show`); read back
// from its /paramshow on an editparam session, posted as BrowserParamShowCmd on commit.
let stepShow = {};
let stepShowHad = false;     // the session's parameter had an entry — commit must clear it
let stepWide    = false;   // Option held: widen scope past this component / this unit
let sessionComponent = ""; // active component's name — the owner's default
let sessionComponentPath = []; // root first, down to sessionComponent — the parent step's list
let stepOwnerName = "";    // the parent chosen (raw Fusion name); stepLevels[0] is its identifier
let stepAcClosed  = false; // Esc closed the feature step's suggestions — ↑/↓ reopen them
let stepTyping    = false; // group step: typing a New group into #pname, not browsing the menu
let stepInsert    = null;  // typing an Insert new: what to put back if it's abandoned
// A chip in the crumb was clicked: that one segment is being changed with the rest of
// the name kept. {levels, type, anchors, axes, stage, none} as they were — X puts them
// back — plus `tail`, the groups after the one being changed, set aside until a pick
// says whether they still apply (see stepEditSegment).
let stepEdit      = null;
let stepChipEdited = false;
// ⌘/Shift-clicked crumb chips, two or more, as crumb keys ("level:2", "type:") — see
// "Several chips at once". Empty otherwise: ONE chip held is still stepEdit's.
let stepMulti       = [];
let stepMultiAnchor = "";   // where a Shift-click's range starts (the last plain/⌘ click)
// An 'editparam' session whose name doesn't follow the convention: the name, whole,
// as ONE chip (seg-legacy). It stands as the name until it is clicked (or Space) —
// the normal naming from Parent, X putting it back (stepLegacyPrev) — or deleted
// (Backspace/X), which starts the normal naming with no way back to it.
let stepLegacy     = "";
let stepLegacyPrev = "";
// Where that name is hand-filed in the Browser, {owner, groups} (raw names) or null —
// giving the chip way opens the naming with those already as its owner and group chips.
let sessionFiled   = null;
let stepOwnerSet  = false; // a parent has been chosen (or came with the name) — its chip shows // a chip was clicked this session: finishing no longer commits by itself
let sessionSymmetry  = {}; // {axis: bool}, that component's
let paramNamingSpec  = null; // the raw JSON, kept for anchor axis/end lookups below

// Every new-convention parameter currently in the list, parsed once per render.
function parsedParams(){
  if (!paramNamingReady) return [];
  const out = [];
  for (const p of params){
    const parsed = ParamNaming.parse(p.name);
    if (parsed) out.push(parsed);
  }
  return out;
}

// Next-level GROUP words under `prefix` (the feature levels already typed, owner
// excluded), scoped to stepLevels[0] (the owner, or — mid-edit of an existing name —
// whatever its own top level is) unless the wide override is held. A parameter's last
// level is its NAME word (ParamNaming.nameWord), not a group, so it is never offered to
// drill into.
function levelBranches(prefix){
  const all = parsedParams();
  const top = stepLevels[0];
  const scoped = stepWide ? all : all.filter(pp => pp.levels[0] === top);
  return ParamNaming.branches(
    scoped.map(pp => ({ levels: ParamNaming.groupLevels(pp).slice(1) })), prefix || []);
}

/* ── The parent ──
   The menu's first step. A is the component the parameter belongs to by default — the
   active one, or for a name being edited, its own owner — and S, D, F … are the
   components above it, one level at a time, up to the root. Space finds ANY component
   in the design by name instead: the menu steps aside for #pname (stepFinding), the
   matches are listed in #ac as you type (stepCompSuggestions), Enter or a click takes
   one, and Esc — or Backspace on an empty field — puts the menu back. */
let sessionComponents = [];   // every component in the design, root first
let ownerChain = [];          // the parent step's choices, nearest first (raw Fusion names)
let stepFinding = false;      // parent step: typing a component name into #pname
// The chain from the session's path. `pick` is an identifier to start from (an existing
// name's own first level): on the path, the chain climbs from there; owned somewhere
// off it, that owner heads the list and the active path follows.
function fillOwner(pick){
  const path = sessionComponentPath.length ? sessionComponentPath.slice()
    : (sessionComponent ? [sessionComponent] : []);
  const up = path.slice().reverse();
  let chosen = up[0] || "";
  if (pick){
    chosen = path.find(n => ParamNaming.identifier(n) === pick) ||
             sessionComponents.find(n => ParamNaming.identifier(n) === pick) || pick;
  }
  const at = up.indexOf(chosen);
  ownerChain = at >= 0 ? up.slice(at) : [chosen].concat(up);
  stepOwnerName = chosen;
}
function stepOwner(){ return ParamNaming.identifier(stepOwnerName); }

// The parent is chosen: it becomes level 0 and the groups are next. A group is only a
// name prefix, so any path kept from before is kept — one the new owner hasn't got is
// simply made with the parameter. Its chip clicked, too, the groups step follows (not
// stepResume, which would skip to the type): more name can go in under the new parent,
// and Enter there resumes with the rest kept.
function setOwner(name){
  stepOwnerName = name;
  stepOwnerSet = true;
  if (stepLevels.length) stepLevels[0] = stepOwner();
  else stepLevels = [stepOwner()];
  enterStage("level");
}

// Starts of words first, then anywhere in the name; the design's own order otherwise.
function compRank(q){
  const s = q.trim().toLowerCase();
  const all = sessionComponents.length ? sessionComponents : ownerChain.slice();
  if (!s) return all.slice(0, 40);
  const starts = [], within = [];
  for (const n of all){
    const l = n.toLowerCase(), at = l.indexOf(s);
    if (at < 0) continue;
    (at === 0 || /[\s_\-(:]/.test(l[at - 1]) ? starts : within).push(n);
  }
  return starts.concat(within).slice(0, 40);
}

// Find's matches, as #ac rows (see renderAC).
function stepCompSuggestions(){
  return compRank(nameEl.value).map(n =>
    ({ entry: { label: n, target: n, kind: "comp" }, hits: [], s: 0 }));
}

// Space on the parent step: #pname takes a component's name.
function stepStartFinding(){
  stepFinding = true;
  nameEl.value = "";
  nameEl.placeholder = "Component name";
  stepAcClosed = false;
  clearError();
  renderSteps();
  nameEl.focus();
  updateAC();
}
function stepStopFinding(){
  stepFinding = false;
  nameEl.value = "";
  nameEl.placeholder = namePlaceholder();
  acItems = []; acIndex = -1; renderAC();
  clearError();
  renderSteps();
}

// The row under the pointer is the highlighted one — the one Enter would take — the
// way a menu behaves, rather than a faint hover tint beside a separate selection.
function markPickRow(row){
  for (const r of pickEl.querySelectorAll(".ac-row.sel")) r.classList.remove("sel");
  row.classList.add("sel");
  if (row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}
pickEl.addEventListener("mousemove", e => {
  const row = e.target.closest(".ac-row");
  if (row && !row.classList.contains("sel")) markPickRow(row);
});

/* ── The feature segment ── */
// #pname's text split at its last comma: the levels already typed, and the one being typed.
function stepLevelParts(){
  const t = nameEl.value;
  const i = t.lastIndexOf(",");
  return {
    // The head keeps the space after its last comma (see NAME_SEP) — it is what the
    // next level is written onto, and dropping it would unspace what is already there.
    head: t.slice(0, i + 1) + (/^\s/.test(t.slice(i + 1)) ? " " : ""),
    done: t.slice(0, i + 1).split(",").map(x => x.trim()).filter(Boolean),
    partial: t.slice(i + 1).trim()
  };
}

// The groups on offer at the level being typed, as #ac rows (see renderAC).
function stepLevelSuggestions(){
  const p = stepLevelParts();
  const q = p.partial.toLowerCase();
  const hits = [];
  for (let k = 0; k < q.length; k++) hits.push(k);
  return levelBranches(stepLevels.slice(1).concat(existingLevels(stepLevels.slice(1), p.done)))
    .filter(w => w.toLowerCase().startsWith(q))
    .map(w => ({ entry: { label: w, target: w, kind: "level" }, hits: hits, s: 0 }));
}

// Enter (or a click) on suggestion `i`: it replaces the level being typed, a comma and
// a space go after it, and the list moves down a level — you may want to go deeper
// still. The space is the same one typing a comma here writes for you (see NAME_SEP):
// the field is read back with each level trimmed, so it is spacing and nothing else.
function stepTakeSuggestion(i){
  const it = acItems[i];
  if (!it) return;
  nameEl.value = stepLevelParts().head + it.entry.target + ", ";
  nameEl.setSelectionRange(nameEl.value.length, nameEl.value.length);
  stepAcClosed = false;
  clearError();
  updateAC();
}

// Typed group words resolved against the groups that already exist, one depth at a
// time under `prefix`: a word that is an existing group's in any case or spacing
// ("bounds", "edit bounds" for Bounds, EditBounds) IS that group — typing it means
// "put it in there", never "make a second one beside it". Past the first word that
// matches nothing, the path is new and the rest is kept as typed.
const levelKey = w => w.toLowerCase().replace(/[^a-z0-9]/g, "");
function existingLevels(prefix, typed){
  const path = prefix.slice(), out = [];
  let fresh = false;
  for (const w of typed){
    const hit = fresh ? null : levelBranches(path).find(b => levelKey(b) === levelKey(w));
    if (!hit) fresh = true;
    out.push(hit || w); path.push(hit || w);
  }
  return out;
}

// Enter with nothing highlighted: the new group(s) as typed, under the path browsed to —
// any that already exist there are that group (existingLevels), not a new one.
// An empty field with a path already chosen is that path, as Enter on the menu is.
function stepConfirmLevels(){
  const levels = existingLevels(stepLevels.slice(1),
    nameEl.value.split(",").map(ParamNaming.joinName).filter(Boolean));
  if (!levels.length && stepInsert){ showError("Type a group to insert."); return false; }
  if (!levels.length) return stepConfirmBrowse();
  const bad = levels.find(l => !NAME_RE.test(l));
  if (bad){
    showError('"' + bad + '" is not a valid level — letters, digits and underscores, '
              + "not starting with a digit.");
    return false;
  }
  if (stepInsert){
    // Inserted after the clicked chip: the groups after it come back behind the new ones.
    stepLevels = stepLevels.concat(levels, stepEdit.tail || []);
    stepInsert = null;
    stepResume();
    return true;
  }
  stepLevels = stepLevels.concat(levels);
  if (stepEdit) stepResume(); else enterStage("type");
  return true;
}

// Enter on the menu: the group browsed to is the one. The owner alone isn't a name.
function stepConfirmBrowse(){
  // Changing a chip's group and Enter before any pick: it stays what it was.
  if (stepEdit && stepEdit.tail){ stepLevels = stepLevels.concat(stepEdit.tail); stepResume(); return true; }
  if (stepLevels.length < 2){ showError("Pick a group, or Space for a new one."); return false; }
  if (stepEdit) stepResume(); else enterStage("type");
  return true;
}

// Space on the menu (or its New group row): #pname takes the new group's name.
function stepStartTyping(){
  stepTyping = true;
  nameEl.value = "";
  nameEl.placeholder = "New group — commas for subgroups";
  stepAcClosed = false;
  clearError();
  renderSteps();
  nameEl.focus();
  updateAC();
}

const namePlaceholder = () => nameRequired ? "Name (required)" : "Name";

/* ── Insert new ──
   On a clicked PARENT or GROUP chip (anything before the type): new group(s) typed in
   straight after that chip, the rest of the name kept behind them. While it is typed,
   the chips after the insertion point sit in stepEdit.tail (drawn faded, as a group
   chip's set-aside groups are); Enter puts the new words in between, Esc/Backspace on
   the empty field puts the chip's menu back as it was. */
function stepCanInsert(){
  if (!stepEdit || stepTyping || stepFinding || stepRenaming || stepNone || stepInsert) return false;
  return stepStage === "parent" ||
         (stepStage === "level" && !!stepEdit.tail && stepEdit.tail.length > 0);
}
function stepStartInsert(){
  if (!stepCanInsert()) return;
  stepInsert = { stage: stepStage, levels: stepLevels.slice(), tail: stepEdit.tail };
  if (stepStage === "parent"){
    stepEdit.tail = stepLevels.slice(1);
    stepLevels = stepLevels.slice(0, 1);
    stepStage = "level";
  } else {
    stepLevels = stepLevels.concat(stepEdit.tail[0]);
    stepEdit.tail = stepEdit.tail.slice(1);
  }
  stepStartTyping();
  nameEl.placeholder = "Insert group — commas for several";
}
// The insert abandoned (or another chip clicked): the chip's own edit, as it was.
function stepUndoInsert(){
  const s = stepInsert;
  if (!s) return;
  stepInsert = null;
  stepStage = s.stage; stepLevels = s.levels;
  if (stepEdit) stepEdit.tail = s.tail;
}

// Back from typing to the menu, the typed text dropped.
function stepStopTyping(){
  stepUndoInsert();
  stepTyping = false;
  nameEl.value = "";
  nameEl.placeholder = namePlaceholder();
  acItems = []; acIndex = -1; renderAC();
  clearError();
  renderSteps();
}

/* ── Exempt words ──
   A feature typed as a single exempt word (paramExemptWords — Top, Width…) adds
   "None" to the end of the type step. Taking it names the parameter just that word,
   in the list's own spelling: no owner, no type, so it doesn't parse and the
   Parameters window files it at the top level rather than under a component. */
const EXEMPT_NONE = "None";
function exemptWord(){
  const levels = stepLevels.slice(1);
  if (levels.length !== 1) return null;
  const l = levels[0].toLowerCase();
  return paramExemptWords.find(w => w.toLowerCase() === l) || null;
}

function typeCandidates(){
  // A brand-new parameter (mode 'param') has no unit yet — Fusion infers it from
  // what gets typed — so nothing to filter against: every type is on offer, exactly
  // as "a completely new parameter" is described in the naming convention.
  //
  // The situation narrows it further: Offset is for a dimension measured from the
  // origin and Inset for one measured in from a bound, and offering both every time
  // makes the step longer than the decision is. The rule is each type's own
  // (param_naming.json's `suggest`) and it is the same one the value box's suggestion
  // list uses, so what gets offered and what gets suggested back can't drift apart.
  // Option held (stepWide) shows every type regardless, the same escape hatch it is
  // for the unit and for the branch list.
  const u = (stepWide || mode === "param") ? null : (unit || null);
  const types = stepWide ? ParamNaming.typesForUnit(null)
                         : ParamNaming.suggestedTypes(namingSituation(), u);
  return exemptWord() ? types.concat([EXEMPT_NONE]) : types;
}

// An anchor/axis word's own {axis, end}, read off the raw spec — the same reason the
// tag UI keeps its own copy of variables.json rather than asking the engine for it.
function anchorInfo(word){
  if (!paramNamingSpec) return null;
  const a = (paramNamingSpec.anchors || []).find(x => x.word === word);
  return a ? { axis: a.axis, end: a.end } : null;
}

// In a 'dim' session the dimension's own axis/end already IS the situation, so it is
// offered first (and so takes the first key) rather than making you re-pick it.
function defaultAnchor(){
  if (mode !== "dim" || !axis || !end) return null;
  for (const w of ParamNaming.allowedAnchors(stepType, sessionSymmetry)){
    const info = anchorInfo(w);
    if (info && info.axis === axis && info.end === end) return w;
  }
  return null;
}
function defaultAxis(){
  if (mode !== "dim" || !axis) return null;
  return ParamNaming.allowedAxes(stepType).includes(axis) ? axis : null;
}

/* How likely each word of an anchor / axis step is, {word: 0..1}, or null when the step
   has nothing to judge by (not a dimension, or no situation). A word the proposal already
   chose is certain; the dimension's own axis+end is nearly so; a datum face the line was
   measured from is likely; the right axis but the wrong end is a maybe; the rest are
   what you'd only pick on purpose. Candidates are ranked by it, and when one is
   confident the unlikely ones are dimmed (see renderSteps) — still keyed, still
   clickable, just out of the way. */
function stepScores(){
  if (stepStage !== "anchor" && stepStage !== "axis") return null;
  const out = {};
  if (stepStage === "anchor"){
    const words = ParamNaming.allowedAnchors(stepType, sessionSymmetry);
    for (const w of words){
      const info = anchorInfo(w);
      let p = 0.1;
      if (stepAnchors.includes(w) && stepProposed) p = 1;
      else if (mode === "dim" && info && axis && end && info.axis === axis && info.end === end) p = 0.9;
      else if (datumFaces.includes(w.toLowerCase())) p = 0.8;
      else if (mode === "dim" && info && axis && info.axis === axis) p = 0.4;
      out[w] = p;
    }
  } else {
    for (const w of ParamNaming.allowedAxes(stepType)){
      out[w] = (mode === "dim" && axis && w === axis) ? 0.9 : 0.1;
    }
  }
  // Nothing to tell apart: all equal → no opinion.
  const v = Object.values(out);
  return v.length && Math.max(...v) > Math.min(...v) ? out : null;
}
const STEP_DIM_AT = 0.8;     // the best word must be at least this likely to dim the rest
const STEP_DIM_BELOW = 0.5;  // …and a word below this is the one dimmed

// The current step's candidate words, in the order they take keys — the likeliest
// first (stepScores), so it lands on the same first key every time; equally likely
// words keep their natural order.
function stepCandidates(){
  if (stepStage === "parent") return ownerChain.slice();
  if (stepStage === "level") return levelBranches(stepLevels.slice(1));
  if (stepStage === "type")  return typeCandidates();
  if (stepStage === "anchor" || stepStage === "axis"){
    const cands = (stepStage === "anchor"
      ? ParamNaming.allowedAnchors(stepType, sessionSymmetry)
      : ParamNaming.allowedAxes(stepType)).slice();
    const sc = stepScores();
    if (sc){
      const idx = new Map(cands.map((w, i) => [w, i]));
      cands.sort((a, b) => (sc[b] - sc[a]) || (idx.get(a) - idx.get(b)));
    }
    return cands;
  }
  return [];
}

function stepIsOptional(){
  if (stepStage !== "axis") return false;
  const td = ParamNaming.typeDef(stepType);
  return (td && td.axis) === "optional";
}

// This step takes more than one word, and is therefore finished by hand (Enter/Space)
// rather than by the first pick.
function stepIsMulti(){
  return (stepStage === "anchor" || stepStage === "axis") &&
         ParamNaming.takesMany(stepType);
}

// What the current step has chosen so far — the list the picks go in and out of.
function stepChosen(){
  return stepStage === "anchor" ? stepAnchors : stepStage === "axis" ? stepAxes : [];
}

// Advance past any stage that has nothing to ask: a type with only one candidate for
// this unit, an anchor/axis a type doesn't take at all, or one with only one choice.
function settleStage(){
  for (;;){
    if (stepStage === "type"){
      const cands = typeCandidates();
      if (stepProposal && !stepEdit && cands.includes(stepProposal.type)){
        const p = stepProposal;
        stepProposal = null;
        stepType = p.type;
        const okA = ParamNaming.allowedAnchors(p.type, sessionSymmetry) || [];
        const okX = ParamNaming.allowedAxes(p.type) || [];
        stepAnchors = (p.anchors || []).filter(w => okA.includes(w));
        stepAxes = stepAnchors.length ? [] : (p.axes || []).filter(w => okX.includes(w));
        stepProposed = true;
        stepStage = "anchor";
        continue;
      }
      if (cands.length === 1){ stepType = cands[0]; stepStage = "anchor"; continue; }
      break;
    }
    if (stepStage === "anchor"){
      const td = ParamNaming.typeDef(stepType) || {};
      if ((td.anchor || "none") !== "required"){ stepStage = "axis"; continue; }
      if (stepProposed && stepAnchors.length){ stepStage = "axis"; continue; }
      const cands = ParamNaming.allowedAnchors(stepType, sessionSymmetry);
      if (cands.length === 1 && !stepAnchors.length){
        stepAnchors = [cands[0]]; stepStage = "done"; continue;
      }
      break;
    }
    if (stepStage === "axis"){
      const td = ParamNaming.typeDef(stepType) || {};
      if ((td.axis || "none") === "none"){ stepStage = "done"; continue; }
      // A proposal decided the axis too — one, or none for an optional one (Thickness).
      if (stepProposed && (stepAxes.length || (td.axis || "none") === "optional" || stepAnchors.length)){
        stepProposed = false; stepStage = "done"; continue;
      }
      if ((td.axis || "none") === "required"){
        const cands = ParamNaming.allowedAxes(stepType);
        if (cands.length === 1 && !stepAxes.length){
          stepAxes = [cands[0]]; stepStage = "done"; continue;
        }
      }
      break; // optional, or more than one choice: wait for a pick or a skip
    }
    break; // 'level' never auto-advances (depth is open-ended); 'done' has nothing left
  }
  if (stepStage === "done"){ stepEdit = null; stepProposed = false; }
  renderSteps();
}

// The name so far, READ rather than spelled: each CamelCase word spaced out, a space
// after every comma and one either side of each dash — "Boot Ceiling, Front - Inset -
// Back". Display only; the real name is compose()'s (stepperName).
function stepCrumbText(){
  if (stepNone) return exemptWord() || "";
  const W = ParamNaming.words;
  let out = stepLevels.map(W).join(", ");
  if (stepType) out += " - " + W(stepType);
  // Mid-step too: on a multi step the crumb is where the words chosen so far show.
  const tail = stepAnchors.length ? stepAnchors : stepAxes;
  if (tail.length) out += " - " + tail.map(W).join(", ");
  return out;
}

/* ── The crumb as chips ──
   The name so far, one chip per segment, coloured by what the segment IS — parent,
   group, type, anchor/axis — in the value box's own chip look. A chip is a way in:
   clicking one opens its step (stepEditSegment). The segment being chosen right now
   carries the held ring; groups set aside while an earlier one is changed are shown
   faded after it, since a different pick drops them. */
function crumbChip(seg, text, i, cls){
  return `<span class="stepchip seg-${seg}${cls || ""}" data-seg="${seg}"` +
         (i !== undefined ? ` data-i="${i}"` : "") + `>${escHtml(text)}</span>`;
}
/* ── Name or group: the last level ──
   What the last level IS — the parameter's name or a group — the way the Browser will
   draw it: pinned here (stepLastPin), else pinned by the parameters already at that
   place, else ParamNaming.nameWord's own rules (two or more entries past the owner and
   nobody else at exactly that chain). */
const chainKey = lv => lv.join("\u0000");
// The name's levels, the set-aside tail included.
function stepChain(){ return stepLevels.concat((stepEdit && stepEdit.tail) || []); }
// A parsed parameter's levels without its own "Make as group" type level — where it sits.
function baseLevels(pp){
  const t = ParamNaming.typeGroupAt(pp);
  return t < 0 ? pp.levels : pp.levels.slice(0, t).concat(pp.levels.slice(t + 1));
}
// The OTHER parameters at exactly `chain` (this session's own left out).
function paramsAtChain(chain){
  if (!paramNamingReady) return [];
  const key = chainKey(chain), out = [];
  for (const p of params){
    if (mode === "editparam" && p.name === origName) continue;
    const pp = ParamNaming.parse(p.name);
    if (pp && chainKey(baseLevels(pp)) === key) out.push({ p, pp });
  }
  return out;
}
function stepPinned(){
  const chain = stepChain();
  if (chain.length < 2) return null;
  if (stepLastPin && stepLastPin.key === chainKey(chain)) return stepLastPin.is;
  const at = paramsAtChain(chain).find(x => x.pp.lastIs);
  return at ? at.pp.lastIs : null;
}
// "name" | "group", or null with nothing past the owner.
function stepLastIs(){
  const chain = stepChain();
  if (chain.length < 2) return null;
  return stepPinned() || (chain.length >= 3 && !paramsAtChain(chain).length ? "name" : "group");
}
// G is offered only on the LAST group chip, clicked.
function stepCanFlipLast(){
  return stepStage === "level" && !stepTyping && !stepRenaming && !!stepEdit &&
         !!stepEdit.tail && stepEdit.tail.length === 1;
}
// Flip it, and the name is whole again — like picking the same group back.
function stepFlipLast(){
  if (!stepCanFlipLast()) return;
  const is = stepLastIs() === "name" ? "group" : "name";
  stepLevels = stepLevels.concat(stepEdit.tail);
  stepLastPin = { key: chainKey(stepLevels), is };
  stepResume();
}

// The index into stepLevels + the set-aside tail of the parameter's NAME word — the last
// level, once the groups step is behind it (while it is still open, the last word typed
// may yet be a group with more under it) or a clicked group has set the rest aside.
// -1 for none: an owner and nothing else has no name word (ParamNaming.nameWord).
function crumbNameIndex(){
  const tail = (stepEdit && stepEdit.tail) || [];
  const n = stepLevels.length + tail.length;
  // Matches ParamNaming.nameWord()'s structural rule: one entry past the owner is
  // always a group (n === 2), never a name — it takes two or more (n >= 3). "Make as
  // group" (stepTypeGroup) doesn't change this: it inserts a level BETWEEN the ones
  // already here (or, with only one, appends and immediately disqualifies itself as
  // the type-group's own trailing level — see stepFinalLevels), so the entry this
  // index points at is the same one either way.
  if (stepLastIs() !== "name") return -1;
  if (!tail.length && (stepStage === "level" || stepStage === "parent")) return -1;
  return n - 1;
}
// The crumb's chips in order, {seg, text, i, cls, key} — what renderCrumb draws, and
// what a Shift-click's range and the several-chip actions walk. `key` is the chip's
// identity ("seg:i"), stable across a held chip's groups being set aside and put back.
function crumbParts(){
  const W = ParamNaming.words;
  const cur = (seg, i) => {
    if (seg === "parent") return stepStage === "parent";
    if (seg === "level") return stepStage === "level" && !!stepEdit && !!stepEdit.tail &&
                                i === stepLevels.length;
    if (seg === "type") return stepStage === "type";
    return stepStage === "anchor" || stepStage === "axis";
  };
  // The ring marks the chip that was clicked, and only that: building a name forward,
  // no chip is "selected" — the page is for a chip that does not exist yet.
  const ring = (seg, i) => stepEdit && cur(seg, i) ? " cur" : "";
  const out = [];
  const add = (seg, text, i, cls) =>
    out.push({ seg, text, i, cls: cls || "", key: seg + ":" + (i === undefined ? "" : i) });
  if (stepLevels.length && stepOwnerSet) add("parent", W(stepLevels[0]), undefined, ring("parent"));
  // The name word is still a level chip (clicking it opens the same step), with
  // seg-name on top for its own colour.
  const nameAt = crumbNameIndex();
  const nm = i => i === nameAt ? " seg-name" : "";
  for (let i = 1; i < stepLevels.length; i++)
    add("level", W(stepLevels[i]), i, ring("level", i) + nm(i));
  const tail = stepEdit && stepEdit.tail;
  if (tail) tail.forEach((w, k) => add("level", W(w), stepLevels.length + k,
                                       (k === 0 ? " cur" : " aside") + nm(stepLevels.length + k)));
  if (stepType) add("type", W(stepType), undefined, ring("type"));
  (stepAnchors.length ? stepAnchors : stepAxes).forEach((w, k) => add("dir", W(w), k, ring("dir")));
  return out;
}
function renderCrumb(){
  if (stepLegacy){ stepCrumbEl.innerHTML = crumbChip("legacy", stepLegacy); return; }
  if (stepNone){
    const ring = stepEdit && stepStage === "level" && stepEdit.tail && stepLevels.length === 1
                 ? " cur" : "";
    stepCrumbEl.innerHTML = crumbChip("level", exemptWord() || "", 1, ring);
    return;
  }
  let parts = crumbParts();
  // Several chips selected: the ring is on each of them, and on nothing else. A chip
  // that has gone (deleted, the type changed) leaves the selection.
  if (stepMulti.length){
    const keys = parts.map(p => p.key);
    stepMulti = stepMulti.filter(k => keys.includes(k));
    if (stepMulti.length < 2) stepMulti = [];
    else parts = parts.map(p => Object.assign({}, p, {
      cls: p.cls.replace(/ (cur|aside)\b/g, "") + (stepMulti.includes(p.key) ? " cur" : "") }));
  }
  stepCrumbEl.innerHTML = parts.map(p => crumbChip(p.seg, p.text, p.i, p.cls)).join("");
  // Crossed out: every part the Browser's row will NOT say, and every parent/group
  // word hidden from typing.
  const shown = stepShown();
  for (const chip of stepCrumbEl.querySelectorAll(".stepchip")){
    const part = chipPart(chip);
    if (part === "lv" ? stepLvHidden(chipWord(chip)) : part && shown[part] === false)
      chip.classList.add("unsaid");
  }
}

/* ── Shown or hidden in the Browser ──
   Not every chip is said on the Browser's row: groups and the parent are the tree it
   sits in, and of the rest ParamNaming.leafShown decides — the type unless it is a
   group (or a Span saying its axis words), the direction only when a twin (same chain
   and type) needs it. ⌘E, or right-click, on a name, type or direction chip flips it
   (stepShow); a chip the Browser won't say is crossed out, and uncrossed again the
   moment a rule starts saying it. One flag for all the directions of a multi name.
   The PARENT and GROUP chips flip too, but not on the row — they are the tree, and stay
   it: hiding one leaves its word out of how the parameter is TYPED in the value box
   (stepShow.lv, a list of level words; see typingHidden). */
const HIDE_CHORD = "cmd+e";
// "name" | "type" | "dir", "lv" for a parent/group chip, or null.
function chipPart(chip){
  const seg = chip.dataset.seg;
  if (seg === "type" || seg === "dir") return seg;
  if (seg === "level" && chip.classList.contains("seg-name")) return "name";
  if (seg === "parent" || seg === "level") return "lv";
  return null;
}
// The level word a parent/group chip stands for (a clicked chip's groups may be set
// aside in stepEdit.tail).
function stepWordAt(i){
  if (i < stepLevels.length) return stepLevels[i];
  const tail = (stepEdit && stepEdit.tail) || [];
  return tail[i - stepLevels.length];
}
function chipWord(chip){
  return chip.dataset.seg === "parent" ? stepLevels[0] : stepWordAt(+chip.dataset.i);
}
function stepLvHidden(word){
  return !!word && Array.isArray(stepShow.lv) && stepShow.lv.includes(word);
}
// Another parameter at exactly this chain and type — the Browser's twinKey.
function stepHasTwin(){
  const levels = stepFinalLevels(), key = chainKey(levels);
  return params.some(p => {
    if (mode === "editparam" && p.name === origName) return false;
    const pp = ParamNaming.parse(p.name);
    return !!pp && pp.type === stepType && chainKey(pp.levels) === key;
  });
}
// {name, type, dir}: true said, false not, null no such part (yet).
function stepShown(show){
  if (!stepType) return { name: null, type: null, dir: null };
  const parts = { levels: stepFinalLevels(), type: stepType, lastIs: stepComposePin(),
                  anchors: stepAnchors.slice(), axes: stepAxes.slice() };
  const out = ParamNaming.leafShown(parts, { direction: stepHasTwin(),
                                             show: show === undefined ? stepShow : show });
  // The name word as the crumb already reads it (sharing included), not nameWord's
  // index-less guess.
  const s = show === undefined ? stepShow : show;
  out.name = crumbNameIndex() < 0 ? null : (typeof s.name === "boolean" ? s.name : true);
  return out;
}
// The part the CLICKED chip is, or null.
function stepHeldPart(){
  if (!stepperOn || !stepEdit || stepLegacy || stepNone) return null;
  if (stepStage === "parent" && stepOwnerSet && stepLevels.length) return "lv";
  if (stepStage === "type") return "type";
  if (stepStage === "anchor" || stepStage === "axis")
    return (stepAnchors.length || stepAxes.length) ? "dir" : null;
  if (stepStage === "level" && stepEdit.tail && stepEdit.tail.length === 1 &&
      crumbNameIndex() === stepLevels.length) return "name";
  if (stepStage === "level" && stepEdit.tail && stepEdit.tail.length) return "lv";
  return null;
}
// Flip the clicked chip between said and not. An override equal to the default is
// dropped, so the chip follows the rules again from there.
function stepToggleHide(){
  const part = stepHeldPart();
  if (!part){
    if (stepEdit) stepTitleError("Click a chip first.");
    return false;
  }
  if (part === "lv"){
    const word = stepStage === "parent" ? stepLevels[0] : stepEdit.tail[0];
    const lv = (stepShow.lv || []).filter(w => w !== word);
    if (!stepLvHidden(word)) lv.push(word);
    const next = Object.assign({}, stepShow);
    if (lv.length) next.lv = lv; else delete next.lv;
    stepShow = next;
    if (stepStage === "level" && stepEdit.tail){ stepLevels = stepLevels.concat(stepEdit.tail); }
    stepResume();
    return true;
  }
  if (!stepType){ stepTitleError("Pick a type first."); return false; }
  const auto = stepShown({})[part];
  if (auto === null) return false;
  const want = !stepShown()[part];
  const next = Object.assign({}, stepShow);
  if (want === auto) delete next[part]; else next[part] = want;
  stepShow = next;
  // Done with the chip, the way a flip is: the groups it set aside go back.
  if (stepStage === "level" && stepEdit.tail){ stepLevels = stepLevels.concat(stepEdit.tail); }
  stepResume();
  return true;
}
async function loadStepShow(name){
  try {
    const all = await (await fetch(BROWSER_API + "/paramshow", { cache: "no-store" })).json();
    const s = all && all[name];
    if (!s || typeof s !== "object" || name !== origName) return;
    stepShow = Object.assign({}, s); stepShowHad = true;
    renderSteps();
  } catch (_) {}
}
// On commit: the Browser keeps it under the parameter's final name.
function postStepShow(name, fromName){
  if (!stepperOn || stepLegacy || stepNone || !name) return;
  if (!Object.keys(stepShow).length && !stepShowHad) return;
  const from = fromName !== undefined ? fromName : mode === "editparam" ? origName : "";
  // Only the level words the final name still has — a group since deleted or renamed
  // takes its hide with it.
  const show = Object.assign({}, stepShow);
  if (show.lv){
    const parsed = paramNamingReady ? ParamNaming.parse(name) : null;
    show.lv = parsed ? show.lv.filter(w => parsed.levels.includes(w)) : [];
    if (!show.lv.length) delete show.lv;
  }
  fetch(BROWSER_API + "/uiaction", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: "BrowserParamShowCmd", arg: { name, from, show } }) }).catch(() => {});
}

// A chip clicked: open its step, keeping the rest of the name. Parent and type keep the
// groups; a GROUP's step shows what sits beside it, the groups after it set aside —
// the same one picked again puts them back, anything else (or a New group) is another
// branch of the tree and they are dropped. The type and its anchor/axis stay either way.
function stepEditSegment(seg, i){
  if (!stepperOn) return;
  stepMulti = [];
  stepLeaveText();
  // Mid-way through another chip's edit: the groups it set aside go back first.
  if (stepEdit && stepEdit.tail){ stepLevels = stepLevels.concat(stepEdit.tail); stepEdit.tail = null; }
  if (!stepEdit) stepEdit = { levels: stepLevels.slice(), type: stepType, anchors: stepAnchors.slice(),
                              axes: stepAxes.slice(), stage: stepStage, none: stepNone, tail: null,
                              typeGroup: stepTypeGroup, lastPin: stepLastPin,
                              show: Object.assign({}, stepShow) };
  stepChipEdited = true;
  clearError();
  if (seg === "parent") stepStage = "parent";
  else if (seg === "level"){
    if (stepNone){ stepNone = false; stepStage = "level"; }
    else {
      const at = Math.max(1, Math.min(i, stepLevels.length));
      stepEdit.tail = stepLevels.slice(at);
      stepLevels = stepLevels.slice(0, at);
      stepStage = "level";
    }
  }
  else if (seg === "type") stepStage = "type";
  else {
    stepEdit.dirAt = i === undefined ? -1 : i;
    const td = ParamNaming.typeDef(stepType) || {};
    stepStage = (td.anchor || "none") === "required" ? "anchor" : "axis";
  }
  renderSteps();
  nameEl.focus();
}

// Whatever #pname was taking text for (a rename, Insert new, New group, Find) is dropped.
function stepLeaveText(){
  stepRenaming = null;
  stepUndoInsert();
  if (stepTyping || stepFinding){
    stepTyping = false; stepFinding = false;
    nameEl.value = ""; nameEl.placeholder = namePlaceholder();
    acItems = []; acIndex = -1; renderAC();
  }
}

/* ── Several chips at once ──
   ⌘-click adds a chip to the selection or takes it out; Shift-click selects every chip
   from the last one clicked to this one — the way a list anywhere else does. The chip
   held by a plain click is where it starts: that chip's edit is settled (its groups
   put back, the name as it stands) and the selection takes over. With two or more
   selected, the step's menu is not a menu of choices for any one of them, so it goes:
   in its place only what applies to all of them — Hide/Show (⌘E), Delete, Cancel (X or
   Esc, which just drops the selection). A plain click on any chip is back to one. */
// The crumb key of the chip a plain click is holding, or "".
function stepHeldKey(){
  if (!stepEdit) return "";
  if (stepStage === "parent") return stepOwnerSet && stepLevels.length ? "parent:" : "";
  if (stepStage === "type") return stepType ? "type:" : "";
  if (stepStage === "anchor" || stepStage === "axis")
    return (stepAnchors.length || stepAxes.length) ? "dir:" + Math.max(0, stepEdit.dirAt || 0) : "";
  if (stepStage === "level" && stepEdit.tail && stepEdit.tail.length) return "level:" + stepLevels.length;
  return "";
}
function stepMultiClick(key, range){
  if (!stepperOn || stepLegacy || stepNone) return;
  let sel = stepMulti.slice();
  const held = stepHeldKey();
  if (!sel.length && held){ sel = [held]; stepMultiAnchor = held; }
  // Settle the held chip: as it stands, its set-aside groups back in place.
  stepLeaveText();
  if (stepEdit){
    if (stepEdit.tail) stepLevels = stepLevels.concat(stepEdit.tail);
    stepEdit.tail = null;
    if (held) stepResume();
  }
  const order = crumbParts().map(p => p.key);
  if (!order.includes(key)) return;
  if (range){
    const from = order.includes(stepMultiAnchor) ? stepMultiAnchor : (sel[sel.length - 1] || key);
    const a = order.indexOf(from), b = order.indexOf(key);
    sel = order.slice(Math.min(a, b), Math.max(a, b) + 1);
  } else {
    sel = sel.includes(key) ? sel.filter(k => k !== key) : sel.concat([key]);
    stepMultiAnchor = key;
  }
  stepChipEdited = true;
  clearError();
  if (sel.length === 1){
    // One left is simply that chip, clicked.
    const [seg, i] = sel[0].split(":");
    stepEditSegment(seg, i === "" ? undefined : +i);
    return;
  }
  stepMulti = sel;
  renderSteps();
  nameEl.focus();
}
function stepMultiClear(){
  stepMulti = [];
  renderSteps();
}
// The selected chips, in crumb order.
function stepMultiParts(){
  return crumbParts().filter(p => stepMulti.includes(p.key));
}
// "name" | "type" | "dir" | "lv" for a crumbParts entry (chipPart, without the DOM).
function crumbPartOf(p){
  if (p.seg === "type" || p.seg === "dir") return p.seg;
  if (p.seg === "level" && / seg-name\b/.test(p.cls)) return "name";
  if (p.seg === "parent" || p.seg === "level") return "lv";
  return null;
}
const crumbWordOf = p => p.seg === "parent" ? stepLevels[0] : stepWordAt(p.i);
// Said (true), not said (false), or nothing to flip (null) — the chip's ⌘E state.
function crumbSaid(p){
  const part = crumbPartOf(p);
  if (part === "lv") return !stepLvHidden(crumbWordOf(p));
  if (!part || !stepType) return null;
  const s = stepShown()[part];
  return s === undefined ? null : s;
}
// ⌘E on the selection: any of them said → hide them all; none → show them all.
function stepMultiHide(){
  const ps = stepMultiParts().filter(p => crumbSaid(p) !== null);
  if (!ps.length){ stepTitleError("Nothing selected can be hidden."); return false; }
  const hide = ps.some(p => crumbSaid(p));
  const next = Object.assign({}, stepShow);
  let lv = (next.lv || []).slice();
  const auto = stepShown({});
  for (const p of ps){
    const part = crumbPartOf(p);
    if (part === "lv"){
      const w = crumbWordOf(p);
      lv = lv.filter(x => x !== w);
      if (hide) lv.push(w);
    } else if (auto[part] !== null){
      if (!hide === auto[part]) delete next[part]; else next[part] = !hide;
    }
  }
  if (lv.length) next.lv = lv; else delete next.lv;
  stepShow = next;
  stepChipEdited = true;
  renderSteps();
  return true;
}
// Delete on the selection: every group/name word and direction in it goes, or — if any
// one of them can't (the parent, the type, the last group, a required last direction)
// — none do, and the title line says why.
function stepMultiDelete(){
  const ps = stepMultiParts();
  if (ps.some(p => p.seg === "parent")){
    stepTitleError("The parent component can't be deleted — every name starts with one."); return true;
  }
  if (ps.some(p => p.seg === "type")){
    stepTitleError("The measurement type can't be deleted — every name needs one."); return true;
  }
  const lvAt = new Set(ps.filter(p => p.seg === "level").map(p => p.i));
  const dirAt = new Set(ps.filter(p => p.seg === "dir").map(p => p.i));
  const levels = stepLevels.filter((_, k) => !lvAt.has(k));
  if (levels.length < 2){
    stepTitleError("That's every group — a name needs at least one after its parent."); return true;
  }
  const anchored = stepAnchors.length > 0;
  const list = anchored ? stepAnchors : stepAxes;
  const left = list.filter((_, k) => !dirAt.has(k));
  if (list.length && !left.length && stepType){
    const td = ParamNaming.typeDef(stepType) || {};
    if ((anchored ? td.anchor : td.axis) === "required"){
      stepTitleError(`${ParamNaming.words(stepType)} needs ${anchored ? "an anchor" : "an axis"} — they can't all be deleted.`);
      return true;
    }
  }
  stepLevels = levels;
  if (anchored) stepAnchors = left; else stepAxes = left;
  stepMulti = [];
  stepChipEdited = true;
  if (stepStage === "done") stepResume(); else renderSteps();
  return true;
}
// The keys while several chips are selected (#pname out of sight, no menu of choices).
// Enter drops the selection and goes on to mean what it always does.
function stepMultiKey(e, stop){
  const plainBack = e.key === "Backspace" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
  if (physChord(e) === HIDE_CHORD){ stop(); stepMultiHide(); return true; }
  if (plainBack || deleteChords.includes(physChord(e))){ stop(); stepMultiDelete(); return true; }
  if (e.key === "Enter"){ stepMulti = []; renderSteps(); return false; }
  if (e.key === "Escape" && !e.shiftKey){ stop(); stepMultiClear(); return true; }
  if (e.metaKey || e.ctrlKey || e.altKey || e.key === "Tab") return true;
  if (e.key.length === 1){
    stop();
    if (e.key.toLowerCase() === STEP_CANCEL_KEY) stepMultiClear();
  }
  return true;
}
// In place of the step's menu: what the selection can do.
function renderMultiMenu(){
  const ps = stepMultiParts();
  const flip = ps.filter(p => crumbSaid(p) !== null);
  stepLabelEl.textContent = ps.length + " chips";
  const rows = [];
  if (flip.length) rows.push(`<span class="stepopt" data-mhide="1"><span class="key">⌘E</span>` +
                             (flip.some(p => crumbSaid(p)) ? "Hide" : "Show") + `</span>`);
  rows.push(`<span class="stepopt" data-mdel="1"><span class="key">⌫</span>Delete</span>`);
  rows.push(`<span class="stepopt back" data-back="1">` +
            `<span class="key">${STEP_CANCEL_KEY}</span>Cancel</span>`);
  stepChoicesEl.innerHTML = rows.join("");
}

// X during a chip's edit: everything back as it was before the chip was clicked.
function stepCancelEdit(){
  const e = stepEdit;
  stepEdit = null;
  stepRenaming = null;
  stepInsert = null;
  stepTyping = false; stepFinding = false;
  nameEl.value = ""; nameEl.placeholder = namePlaceholder();
  acItems = []; acIndex = -1; renderAC();
  stepLevels = e.levels; stepType = e.type; stepAnchors = e.anchors; stepAxes = e.axes;
  stepNone = e.none; stepStage = e.stage; stepTypeGroup = e.typeGroup; stepLastPin = e.lastPin;
  stepShow = e.show;
  stepOwnerName = ownerChain.find(n => ParamNaming.identifier(n) === stepLevels[0]) || stepOwnerName;
  clearError();
  renderSteps();
}

// The old-convention name's chip gives way to the normal naming, from Parent. `keep`
// (a click, or Space) lets X / Backspace on Parent put it back; deleting it doesn't.
// Filed in the Browser, it starts on the group menu instead, the component and groups
// it is filed under already chips — each changeable or deletable like any other, Space
// for a new group inside them, Enter on to the type.
function stepLegacyRename(keep){
  stepLegacyPrev = keep ? stepLegacy : "";
  stepLegacy = "";
  stepType = null; stepAnchors = []; stepAxes = []; stepNone = false;
  clearError();
  const f = sessionFiled;
  if (f && f.owner){
    fillOwner(ParamNaming.identifier(f.owner));
    stepOwnerSet = true;
    stepLevels = [stepOwner()].concat((f.groups || []).map(g => ParamNaming.identifier(g))
                                                     .filter(g => NAME_RE.test(g)));
    enterStage("level");
  } else {
    fillOwner(null);
    stepOwnerSet = false;
    stepLevels = [stepOwner()];
    enterStage("parent");
  }
  nameEl.focus();
}
function stepLegacyRestore(){
  stepLegacy = stepLegacyPrev; stepLegacyPrev = "";
  stepLevels = []; stepType = null; stepAnchors = []; stepAxes = []; stepNone = false;
  stepOwnerSet = false;
  stepStage = "done";
  nameEl.value = ""; acItems = []; acIndex = -1; renderAC();
  clearError();
  renderSteps();
}

// A chip's segment has been chosen: on to whatever the name still lacks — nothing, if
// the rest was kept — rather than onward through every step after it.
function stepResume(){
  stepTyping = false; stepFinding = false;
  nameEl.value = ""; nameEl.placeholder = namePlaceholder();
  acItems = []; acIndex = -1; renderAC();
  if (stepEdit) stepEdit.tail = null;
  if (stepNone){ stepStage = "done"; settleStage(); return; }
  if (!stepType){ enterStage("type"); return; }
  const td = ParamNaming.typeDef(stepType) || {};
  if ((td.anchor || "none") === "required"){
    if (!stepAnchors.length){ stepStage = "anchor"; settleStage(); return; }
  } else if ((td.axis || "none") === "required" && !stepAxes.length){
    stepStage = "axis"; settleStage(); return;
  }
  stepStage = "done"; settleStage();
}

// A new type in place of the old: its directions stay where the new type takes the same
// kind and still allows them, and go where it doesn't.
function stepKeepDirections(type){
  const td = ParamNaming.typeDef(type) || {};
  const many = ParamNaming.takesMany(type);
  if ((td.anchor || "none") === "required"){
    const ok = ParamNaming.allowedAnchors(type, sessionSymmetry);
    stepAnchors = stepAnchors.filter(w => ok.includes(w));
    if (!many) stepAnchors = stepAnchors.slice(0, 1);
    stepAxes = [];
  } else {
    stepAnchors = [];
    const ok = (td.axis || "none") === "none" ? [] : ParamNaming.allowedAxes(type);
    stepAxes = stepAxes.filter(w => ok.includes(w));
    if (!many) stepAxes = stepAxes.slice(0, 1);
  }
}

/* ── Deleting a chip ──
   The Delete chord on a CLICKED crumb chip takes that piece out of the name — when what
   is left is still a name. The parent and the measurement type are what every name is
   built on, so those refuse on the title line; so does the only level past the owner
   (ParamNaming reads a name with no feature as nothing), and a direction its type
   requires when it is the last one. A group or name word, or an optional direction,
   simply goes, and the stepper resumes as any chip edit does (Enter commits, X puts
   the name back as it was before the click). Returns false with no chip clicked, so
   the key falls through to whatever else it means. */
function stepDeleteChip(){
  if (!stepperOn || !stepEdit || stepLegacy || stepNone) return false;
  const refuse = msg => { stepTitleError(msg); return true; };
  if (stepStage === "parent") return refuse("The parent component can't be deleted — every name starts with one.");
  if (stepStage === "type")   return refuse("The measurement type can't be deleted — every name needs one.");
  if (stepStage === "level"){
    const tail = stepEdit.tail || [];
    if (!tail.length) return false;
    if (stepLevels.length - 1 + tail.length - 1 < 1)
      return refuse("That's the only group — a name needs at least one after its parent.");
    const at = stepLevels.length;
    stepLevels = stepLevels.concat(tail.slice(1));
    stepEdit.tail = null;
    stepResume();
    // The next group takes the hold (the one before, when it was the last), so the
    // same key keeps deleting.
    if (stepStage === "done") stepEditSegment("level", Math.min(at, stepLevels.length - 1));
    return true;
  }
  if (stepStage === "anchor" || stepStage === "axis"){
    const td = ParamNaming.typeDef(stepType) || {};
    const anchored = stepAnchors.length > 0;
    const list = anchored ? stepAnchors : stepAxes;
    const need = anchored ? (td.anchor || "none") === "required" : (td.axis || "none") === "required";
    if (!list.length) return false;
    if (need && list.length === 1)
      return refuse(`${ParamNaming.words(stepType)} needs ${anchored ? "an anchor" : "an axis"} — it can't be deleted.`);
    const at = stepEdit.dirAt >= 0 && stepEdit.dirAt < list.length ? stepEdit.dirAt : list.length - 1;
    const left = list.filter((_, k) => k !== at);
    if (anchored) stepAnchors = left; else stepAxes = left;
    stepResume();
    if (stepStage === "done" && left.length) stepEditSegment("dir", Math.min(at, left.length - 1));
    return true;
  }
  return false;
}

/* ── Renaming a group ──
   The master Rename chord (fusion_ui/general.json's "rename", read from /general and
   never spelled here) on a clicked GROUP chip: #pname takes the new word, and Enter
   renames the group everywhere — every parameter filed under it, at that one level —
   through the Parameters window's /set, the same post the Browser's own group rename
   makes, so the Browser redraws the group under its new name on its next poll. The
   whole plan is checked first: a rename that would leave two parameters sharing a name
   goes nowhere. */
let renameChord = "";        // normalized "cmd+r", from /general
let stepRenaming = null;     // {depth, old} while the group's new word is being typed
let pendingRetarget = "";    // this session's own parameter, renamed: retarget once it lands

function normChord(s){
  const order = ["cmd", "ctrl", "alt", "shift"];
  const mods = []; let k = "";
  for (let p of String(s || "").toLowerCase().split("+").map(x => x.trim()).filter(Boolean)){
    if (p === "meta" || p === "command") p = "cmd";
    if (p === "option") p = "alt";
    if (p === "control") p = "ctrl";
    if (order.includes(p)) mods.push(p); else k = p;
  }
  mods.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  return mods.concat([k]).join("+");
}
function eventChord(e){
  const m = [];
  if (e.metaKey) m.push("cmd"); if (e.ctrlKey) m.push("ctrl");
  if (e.altKey) m.push("alt");  if (e.shiftKey) m.push("shift");
  return m.concat([(e.key || "").toLowerCase()]).join("+");
}
// eventChord by the PHYSICAL key: with ⌥ held e.key is the composed character (⌥⇧A
// arrives as "Å"), so a letter or digit is read off e.code instead. Normalized.
function physChord(e){
  const c = e.code || "";
  const k = /^Key[A-Z]$/.test(c) ? c.slice(3).toLowerCase()
          : /^Digit\d$/.test(c) ? c.slice(5) : (e.key || "").toLowerCase();
  const m = [];
  if (e.metaKey) m.push("cmd"); if (e.ctrlKey) m.push("ctrl");
  if (e.altKey) m.push("alt");  if (e.shiftKey) m.push("shift");
  return normChord(m.concat([k]).join("+"));
}
/* Close Whole Dialog: the master chord (general.json "close-dialog", "space+escape" by
   default) cancels the session past whatever is open inside it. In a text field Space
   never reaches this page — remap's space layer swallows it — so there the binding
   linked to the master (fusionwin.space.escape) sends ⇧Esc instead, and ⇧Esc is
   always this. Elsewhere (a panel with no field) Space arrives, and is tracked here. */
let closeDialogChord = "space+escape";
let spaceDown = false;
document.addEventListener("keydown", e => { if (e.code === "Space") spaceDown = true; }, true);
document.addEventListener("keyup", e => { if (e.code === "Space") spaceDown = false; }, true);
window.addEventListener("blur", () => { spaceDown = false; });
// Fusion's Super Escape key (fusion_ui/bindings.json) means the same here.
let superEscChord = "";
function isCloseDialog(e){
  if (e.key === "Escape" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) return true;
  if (superEscChord && physChord(e) === superEscChord) return true;
  const parts = closeDialogChord.split("+");
  const spaced = parts.length > 1 && parts.slice(0, -1).includes("space");
  if (spaced !== spaceDown) return false;
  // Caps Lock reaches a page only as the Escape Karabiner makes it (caps.escape).
  const want = parts.filter((p, i) => p !== "space" || i === parts.length - 1)
                    .map(p => p === "caps_lock" ? "escape" : p).join("+");
  return physChord(e) === normChord(want);
}
// Delete: the master Delete chord (general.json "delete") AND whatever Fusion's own
// Delete Selection is on (fusion_ui/bindings.json — Space+A arrives as it), since both
// mean "delete what is held" and a clicked chip is what is held here.
let deleteChords = [];
(async () => {
  try {
    const g = await (await fetch(API + "/general", { cache: "no-store" })).json();
    const a = ((g && g.actions) || []).find(x => x.id === "rename");
    renameChord = a && a.chord ? normChord(a.chord) : "";
    const d = ((g && g.actions) || []).find(x => x.id === "delete");
    if (d && d.chord) deleteChords.push(normChord(d.chord));
    const c = ((g && g.actions) || []).find(x => x.id === "close-dialog");
    if (c && c.chord) closeDialogChord = String(c.chord).toLowerCase();
  } catch (_) {}
  try {
    const b = await (await fetch(API + "/fusion_bindings.json", { cache: "no-store" })).json();
    if (b && b.DeleteSelectionCmd) deleteChords.push(normChord(b.DeleteSelectionCmd));
    if (b && b.SuperEscapeCmd) superEscChord = normChord(b.SuperEscapeCmd);
  } catch (_) {}
})();

// Every parameter under the group `old` sitting after `prefix` (owner first), renamed to
// `word` at that level: [{from, to}], or a string saying why it can't be done.
function groupRenamePlan(prefix, old, word){
  const moves = [];
  for (const p of params){
    const pp = ParamNaming.parse(p.name);
    if (!pp || pp.levels.length <= prefix.length) continue;
    if (pp.levels[prefix.length] !== old || !prefix.every((w, k) => pp.levels[k] === w)) continue;
    const levels = pp.levels.slice();
    levels[prefix.length] = word;
    moves.push({ from: p.name, to: ParamNaming.compose({ levels, type: pp.type,
                                                         anchors: pp.anchors, axes: pp.axes }) });
  }
  const taken = new Set(params.map(p => p.name)), seen = new Set();
  for (const m of moves){
    if (!m.to) return '"' + m.from + '" has no valid name under the new group.';
    if (seen.has(m.to) || taken.has(m.to))
      return '"' + m.from + '" would become "' + m.to + '", and that name is taken.';
    seen.add(m.to);
  }
  return moves;
}

// The chord on a clicked group chip: its word, ready to be typed over.
function stepStartRename(){
  if (!(stepEdit && !stepInsert && stepStage === "level" && stepEdit.tail && stepEdit.tail.length)){
    showError("Click a group's chip to rename it.");
    return;
  }
  stepTyping = false; stepFinding = false;
  stepRenaming = { depth: stepLevels.length, old: stepEdit.tail[0] };
  acItems = []; acIndex = -1; renderAC();
  nameEl.value = stepRenaming.old;
  nameEl.placeholder = "New name for the group";
  clearError();
  renderSteps();
  nameEl.focus();
  nameEl.setSelectionRange(0, nameEl.value.length);
}
function stepStopRename(){
  stepRenaming = null;
  nameEl.value = ""; nameEl.placeholder = namePlaceholder();
  clearError();
  renderSteps();
}
// Post every {from, to} in `moves` — the Parameters window's own write path
// (PARAMS_API `/set`) plus the Browser's `BrowserParamEditedCmd` (so filing follows),
// same posts groupRenamePlan's caller always made, now shared with "Make as group"'s
// own sibling moves. Optimistic like the Browser: params/`row.name` update now, the
// next poll is the truth. If the session's OWN parameter (origName, an 'editparam') is
// among them, the add-in is told once the rename has landed (see poll()'s pendingRetarget).
function applyRenames(moves){
  for (const m of moves){
    fetch(PARAMS_API + "/set", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: m.from, newName: m.to }) }).catch(() => {});
    fetch(BROWSER_API + "/uiaction", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: "BrowserParamEditedCmd", arg: { name: m.from, newName: m.to } }) }).catch(() => {});
    const row = params.find(p => p.name === m.from);
    if (row) row.name = m.to;
    if (mode === "editparam" && m.from === origName){ origName = m.to; pendingRetarget = m.to; }
  }
  if (moves.length) invalidateEntries();
}

function stepApplyRename(){
  const { depth, old } = stepRenaming;
  const typed = nameEl.value.trim();
  const word = typed ? ParamNaming.identifier(ParamNaming.joinName(typed)) : "";
  if (!word || !NAME_RE.test(word)){ showError("Letters, digits and underscores, not starting with a digit."); return; }
  if (word === old){ stepStopRename(); return; }
  const prefix = stepLevels.slice(0, depth);
  const moves = groupRenamePlan(prefix, old, word);
  if (typeof moves === "string"){ showError(moves); return; }
  applyRenames(moves);
  // The name being built carries the new word too — and so does what X would restore.
  stepEdit.tail[0] = word;
  if (stepEdit.levels[depth] === old && prefix.every((w, k) => stepEdit.levels[k] === w))
    stepEdit.levels[depth] = word;
  stepRenaming = null;
  nameEl.placeholder = namePlaceholder();
  stepLevels = stepLevels.concat(stepEdit.tail);
  stepResume();
}

stepCrumbEl.addEventListener("mousedown", e => {
  const chip = e.target.closest(".stepchip");
  if (!chip) return;
  e.preventDefault();                       // keep the keyboard in #pname
  if (e.button !== 0) return;               // a right-click is the menu's (below)
  if (chip.dataset.seg === "legacy"){ stepLegacyRename(true); return; }
  const key = chip.dataset.seg + ":" + (chip.dataset.i !== undefined ? chip.dataset.i : "");
  // ⌘ or Shift: several chips at once (stepMultiClick).
  if (e.metaKey || e.shiftKey){ stepMultiClick(key, e.shiftKey && !e.metaKey); return; }
  stepMultiAnchor = key;
  stepEditSegment(chip.dataset.seg, chip.dataset.i !== undefined ? +chip.dataset.i : undefined);
});

// Right-click on a crumb chip: the LAST group chip's menu — "Make group" / "Make name",
// the same flip G makes after a click (stepEditSegment, then stepFlipLast), so it is
// committed the same way. Nothing on any other chip, and never WebKit's own menu.
stepCrumbEl.addEventListener("contextmenu", e => {
  e.preventDefault();
  const chip = e.target.closest(".stepchip");
  if (!chip || !stepperOn || stepNone || stepLegacy) return;
  const seg = chip.dataset.seg;
  const i = chip.dataset.i !== undefined ? +chip.dataset.i : -1;
  const rows = [];
  hideChipMenu();
  chipMenuField = nameEl;
  chipMenuActions = {};
  // On one of several selected chips: what the selection can do, for all of them.
  if (stepMulti.includes(seg + ":" + (i >= 0 ? i : ""))){
    const flip = stepMultiParts().filter(p => crumbSaid(p) !== null);
    if (flip.length){
      chipMenuActions.hide = stepMultiHide;
      rows.push({ id: "hide", label: flip.some(p => crumbSaid(p)) ? "Hide" : "Show", key: "⌘E" });
    }
    chipMenuActions.del = stepMultiDelete;
    rows.push({ id: "del", label: "Delete", key: "⌫" });
    showChipMenu(rows, e);
    return;
  }
  if (seg === "level" && i >= 1 && i === stepChain().length - 1){
    chipMenuActions.flip = () => { stepEditSegment("level", i); stepFlipLast(); };
    rows.push({ id: "flip", label: stepLastIs() === "name" ? "Make group" : "Make name",
                key: STEP_GROUP_KEY.toUpperCase() });
  }
  // Hide / Show on the row: the same flip ⌘E makes on the chip once clicked.
  const part = chipPart(chip);
  const said = part === "lv" ? !stepLvHidden(chipWord(chip)) : part ? stepShown()[part] : null;
  if (said !== null && said !== undefined){
    chipMenuActions.hide = () => { stepEditSegment(seg, i >= 0 ? i : undefined); stepToggleHide(); };
    // A parent/group stays in the Browser's tree: its flip is only about typing.
    rows.push({ id: "hide", label: (said ? "Hide" : "Show") + (part === "lv" ? " when typing" : ""),
                key: "⌘E" });
  }
  showChipMenu(rows, e);
});

// stepLevels with the "Make as group" type level spliced in, right before whatever is
// currently the trailing NAME entry (so "Entry" stays readable as the row and "Span"
// becomes the group it sits in — Owner_Shaft_Span_Entry_Span_X) — or simply appended
// when there's nothing past the owner to protect (Owner_Width_Span_Span_X: Width was
// already a bare group, Span nests inside it). Unchanged, and equal to stepLevels
// itself, while the toggle is off or no type is picked yet.
// A PINNED last level decides too: a name is protected, a group gets the type after it.
function stepTypeGroupAt(){
  const n = stepLevels.length, pin = stepPinned();
  return pin === "name" ? n - 1 : pin === "group" ? n : (n >= 3 ? n - 1 : n);
}
function stepFinalLevels(){
  if (!stepTypeGroup || !stepType) return stepLevels.slice();
  const at = stepTypeGroupAt();
  return stepLevels.slice(0, at).concat([stepType], stepLevels.slice(at));
}
// The pin the composed name carries. A pinned group with the type level appended after
// it needs none: not being last, it is a group already.
function stepComposePin(){
  const pin = stepPinned();
  if (pin === "group" && stepTypeGroup && stepType) return undefined;
  return pin || undefined;
}

// {levels, type, anchors, axes} once the stepper has reached 'done' — what commit()
// hands to ParamNaming.compose() for the real Fusion name.
function stepResult(){
  if (stepStage !== "done") return null;
  return { levels: stepFinalLevels(), type: stepType, lastIs: stepComposePin(),
           anchors: stepAnchors.slice(), axes: stepAxes.slice() };
}

// The real Fusion name the stepper has built so far, or "" while it's incomplete.
function stepperName(){
  if (stepLegacy) return stepLegacy;
  if (stepNone) return stepStage === "done" ? (exemptWord() || "") : "";
  const r = stepResult();
  return r ? ParamNaming.compose(r) : "";
}

// What each step is called, in #steptitle — the one line saying which part of the
// name the menu under it is choosing.
const STEP_TITLES = { parent: "Parent", level: "Parameter groups", type: "Measurement type",
                      anchor: "Anchor", axis: "Axis", done: "Name" };

// The Space key, drawn: a bar with its two ends turned up — the key's own shape.
const SPACE_KEY = `<span class="key wide space" title="Space"><svg viewBox="0 0 16 8" ` +
  `width="16" height="8" fill="none" stroke="currentColor" stroke-width="1.5" ` +
  `stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 2v4h13V2"/></svg></span>`;

// #pname's home. While the stepper is up it lives inside #fsteps, under the crumb, and
// the Name row it normally sits in goes (the value's evaluated readout moving up beside
// the step's title); any other session puts both back where index.html has them.
function placeNameField(){
  if (stepperOn){
    if (nameEl.parentNode !== fSteps) fSteps.insertBefore(nameEl, stepChoicesEl);
    if (prevEl.parentNode !== stepTitleEl) stepTitleEl.appendChild(prevEl);
    fName.classList.add("hide");
  } else {
    if (nameEl.parentNode !== fName) fName.insertBefore(nameEl, fName.firstChild);
    if (prevEl.parentNode !== fName) fName.insertBefore(prevEl, sameEl);
    fName.classList.remove("hide");
    nameEl.classList.remove("stepfield-hidden");
  }
}

function renderSteps(){
  placeNameField();
  // Find component searches component names, which are sentences with spaces in them —
  // so the space is a space there, not capitalise.js's word-join (data-joinwords).
  nameEl.dataset.joinwords = stepperOn && stepStage === "parent" && stepFinding ? "0" : "1";
  if (!stepperOn){ fSteps.classList.add("hide"); return; }
  fSteps.classList.remove("hide");
  // One element: the menu, or — only while a "type your own" choice is being typed —
  // the text box in its place. #pname keeps the keyboard either way (collapsed, not
  // display:none, which would drop focus), so every key still arrives in one handler.
  const typing = stepTyping || stepFinding || !!stepRenaming;
  nameEl.classList.toggle("stepfield-hidden", !typing);
  stepChoicesEl.classList.toggle("hide", typing);
  stepLabelEl.textContent = stepRenaming ? "Rename group" : (STEP_TITLES[stepStage] || "");
  renderCrumb();

  if (stepMulti.length){ renderMultiMenu(); return; }
  if (stepStage === "parent"){ renderParentMenu(); return; }
  if (stepStage === "level"){ renderLevelMenu(); return; }
  if (stepLegacy){
    stepChoicesEl.innerHTML = `<span class="stepopt new" data-legacy="1">${SPACE_KEY}New name</span>`;
    return;
  }
  if (stepStage === "done"){
    // Nothing left to choose; the one thing to do from here is go back into it.
    stepChoicesEl.innerHTML = `<span class="stepopt back" data-back="1">` +
      `<span class="key">${STEP_CANCEL_KEY}</span>Back</span>`;
    return;
  }

  const rows = [];
  const chosen = stepChosen();
  const sc = stepScores();
  const sure = sc && Math.max(...Object.values(sc)) >= STEP_DIM_AT;
  stepCandidates().forEach((w, i) => {
    const key = STEP_KEYS[i];
    const on = (chosen.includes(w) ? " on" : "") +
               (sure && sc[w] < STEP_DIM_BELOW ? " dim" : "");
    rows.push(key
      ? `<span class="stepopt${on}" data-w="${w}"><span class="key">${key}</span>${w}</span>`
      : `<span class="stepopt${on}" data-w="${w}">${w}</span>`);
  });
  // What Space does here, said in the words of the step it is on: finish a multi
  // step that has something in it, skip an optional one that hasn't.
  if (stepIsMulti() && chosen.length){
    rows.push(`<span class="stepopt new" data-space="1">${SPACE_KEY}Finish</span>`);
  } else if (stepIsOptional()){
    rows.push(`<span class="stepopt new" data-space="1">${SPACE_KEY}Skip</span>`);
  }
  // "Make as group" — a plain on/off toggle, not a candidate: G or a click flips it,
  // and it stays wherever it was set as the rest of the step is picked (see
  // enterStage/stepEditSegment).
  if (stepStage === "type"){
    rows.push(`<span class="stepopt toggle${stepTypeGroup ? " on" : ""}" data-toggle="1">` +
              `<span class="key">${STEP_GROUP_KEY.toUpperCase()}</span>Make as group</span>`);
  }
  // THE WAY BACK, LAST AND KEYED LIKE THE REST. Backspace on an empty field has
  // always done this; a step that lists every other answer on screen should not make
  // you know one of them by heart. X, ALWAYS — it used to take whatever key came
  // after the choices, which moved from step to step, and the one row that means the
  // same thing everywhere should be under the same finger everywhere.
  // "Cancel", not "Back": what it undoes is the choice you just made, and every
  // other undo-a-decision word on these panels is this one.
  rows.push(`<span class="stepopt back" data-back="1">` +
            `<span class="key">${STEP_CANCEL_KEY}</span>Cancel</span>`);
  stepChoicesEl.innerHTML = rows.join("");
}

// The parent menu: the owner chain, nearest first, then Find (Space). Nothing to go
// back to from here — it is the first step.
function renderParentMenu(){
  if (stepFinding){ stepChoicesEl.innerHTML = ""; return; }
  const rows = ownerChain.map((n, i) => {
    const key = STEP_KEYS[i];
    return `<span class="stepopt" data-w="${escHtml(n)}">` +
           (key ? `<span class="key">${key}</span>` : "") + escHtml(n) + `</span>`;
  });
  rows.push(`<span class="stepopt new" data-new="1">${SPACE_KEY}Find component</span>`);
  if (stepCanInsert()) rows.push(insertRow());
  if (stepEdit || stepLegacyPrev) rows.push(`<span class="stepopt back" data-back="1">` +
                          `<span class="key">${STEP_CANCEL_KEY}</span>Cancel</span>`);
  stepChoicesEl.innerHTML = rows.join("");
}

const insertRow = () => `<span class="stepopt" data-insert="1">` +
  `<span class="key">${STEP_INSERT_KEY.toUpperCase()}</span>Insert new</span>`;

// The group menu: the groups inside the path browsed to, keyed, then New group (Space)
// and Back (X). While a new group is being typed the menu steps aside for #pname.
function renderLevelMenu(){
  if (stepTyping){ stepChoicesEl.innerHTML = ""; return; }
  const rows = stepCandidates().map((w, i) => {
    const key = STEP_KEYS[i];
    const label = escHtml(ParamNaming.displayWord(w));
    return `<span class="stepopt" data-w="${escHtml(w)}">` +
           (key ? `<span class="key">${key}</span>` : "") + label + `</span>`;
  });
  rows.push(`<span class="stepopt new" data-new="1">${SPACE_KEY}New group</span>`);
  if (stepCanInsert()) rows.push(insertRow());
  if (stepCanFlipLast())
    rows.push(`<span class="stepopt" data-flip="1"><span class="key">${STEP_GROUP_KEY.toUpperCase()}</span>` +
              (stepLastIs() === "name" ? "Make group" : "Make name") + `</span>`);
  rows.push(`<span class="stepopt back" data-back="1">` +
            `<span class="key">${STEP_CANCEL_KEY}</span>${stepEdit ? "Cancel" : "Back"}</span>`);
  stepChoicesEl.innerHTML = rows.join("");
}

// A step is entered fresh — clears #pname (last step's typed text was spent taking
// it) and lets settleStage() skip past anything with nothing to ask.
function enterStage(stage){
  stepStage = stage;
  stepNone = false;
  if (stepTyping || stepFinding){
    stepTyping = false; stepFinding = false; nameEl.placeholder = namePlaceholder();
  }
  // Entering the type step, or the direction step straight after one was chosen, is
  // starting that decision over: whatever directions were in hand belonged to the
  // last type. (Reopening a multi step from 'done' keeps them — see stepBack.) A fresh
  // type step also starts "Make as group" off — a CHIP edit of the type (stepStage set
  // to 'type' directly by stepEditSegment, bypassing enterStage) keeps whatever it was.
  if (stage === "type" || stage === "anchor"){ stepAnchors = []; stepAxes = []; }
  if (stage === "type") stepTypeGroup = false;
  nameEl.value = "";
  acItems = []; acIndex = -1; renderAC();
  settleStage();
}

// Commit the choice at index `i` of the CURRENT (untyped-filter) candidate list.
function pickStepIndex(i){
  const cands = stepCandidates();
  // Cancel is X and only X now (STEP_CANCEL_KEY), so a key past the end of the choices
  // is a key with nothing under it — it used to fall through to the Cancel row, which
  // meant the letter that did so changed with the length of the list.
  const w = cands[i];
  if (w === undefined) return false;
  return pickStepWord(w);
}
function pickStepWord(w){
  // The parent menu: that component owns the name; the groups are next.
  if (stepStage === "parent"){ setOwner(w); return true; }
  // The group menu: into that group — the menu now lists what is inside it.
  if (stepStage === "level"){
    // Changing a chip's group: the same one again puts the groups after it back and the
    // name is whole again; any other is a different branch, and they are dropped.
    const tail = stepEdit && stepEdit.tail;
    if (tail && tail[0] === w){ stepLevels = stepLevels.concat(tail); stepResume(); return true; }
    if (tail) stepEdit.tail = null;
    stepLevels.push(w); nameEl.value = ""; clearError(); renderSteps();
    return true;
  }
  if (stepStage === "type" && w === EXEMPT_NONE && exemptWord()){
    stepType = null; stepAnchors = []; stepAxes = [];
    stepNone = true; stepStage = "done"; stepEdit = null; nameEl.value = ""; renderSteps();
    return true;
  }
  if (stepStage === "type" && stepEdit){
    // A chip's type changed: its directions stay where they still fit (stepKeepDirections).
    stepType = w; stepKeepDirections(w); stepResume(); return true;
  }
  if (stepStage === "type"){ stepType = w; enterStage("anchor"); return true; }
  if (stepStage !== "anchor" && stepStage !== "axis") return false;

  // A step that takes several stays open and toggles: the same key puts a direction
  // in and takes it back out again, and Enter/Space is what finishes (stepFinish).
  if (stepIsMulti()){
    const chosen = stepChosen();
    const at = chosen.indexOf(w);
    if (at >= 0) chosen.splice(at, 1); else chosen.push(w);
    nameEl.value = "";
    renderSteps();
    return true;
  }

  if (stepStage === "anchor") stepAnchors = [w]; else stepAxes = [w];
  stepStage = "done"; nameEl.value = ""; settleStage();
  return true;
}

// "Make as group": G or a click on its own row, only on the type step. A plain flip,
// not a pick — it never advances the stage or finishes the name by itself.
function stepToggleGroup(){
  if (stepStage !== "type") return;
  stepTypeGroup = !stepTypeGroup;
  renderSteps();
}

// A multi step is done being added to. Refused while it holds nothing — a required
// step with no answer is not an answer.
function stepFinish(){
  if (!stepChosen().length) return false;
  stepStage = "done"; nameEl.value = ""; settleStage();
  return true;
}

// A step Enter couldn't finish — a required anchor/axis/type with nothing chosen and
// no default — says so briefly on the step's own title line (#steperr), rather than
// the panel's shared #err: the field is out of sight on these steps, so an error at
// the bottom of the panel would appear nowhere near what caused it. Cleared by the
// very next key (see the stepper's own keydown handler, top).
let stepErrTimer = 0;
function stepTitleError(msg){
  stepErrEl.textContent = msg;
  clearTimeout(stepErrTimer);
  stepErrTimer = setTimeout(() => { stepErrEl.textContent = ""; }, 1800);
}
function clearStepTitleError(){
  clearTimeout(stepErrTimer);
  stepErrEl.textContent = "";
}

// Enter with #pname empty: end the feature segment, or take whatever default the
// current step has (a 'dim' session's own axis/anchor, or an optional step's "none").
function stepConfirmEmpty(){
  if (stepStage === "level") return;   // the feature step's Enter is stepConfirmLevels
  // A multi step with something already in it: this is the key that ends it. Empty,
  // it falls through to the same default/skip an ordinary step gets.
  if (stepIsMulti() && stepChosen().length){ stepFinish(); return; }
  if (stepStage === "anchor"){
    const def = defaultAnchor();
    if (def){ pickStepWord(def); if (stepIsMulti()) stepFinish(); return; }
    stepTitleError("Pick an anchor.");
    return; // required, no default: nothing to confirm empty-handed
  }
  if (stepStage === "axis"){
    const def = defaultAxis();
    if (def){ pickStepWord(def); if (stepIsMulti()) stepFinish(); return; }
    if (stepIsOptional()){ stepAxes = []; stepStage = "done"; nameEl.value = ""; settleStage(); return; }
    stepTitleError("Pick an axis.");
    return;
  }
  if (stepStage === "type"){ stepTitleError("Pick a measurement type."); return; }
}

// Backspace on an empty field: undo the last decision, one stage at a time.
function stepBack(){
  if (stepMulti.length){ stepMultiClear(); return; }
  // Changing a chip: X (or Backspace) is "leave it as it was", not a step back through
  // the rest of the name.
  if (stepEdit){ stepCancelEdit(); return; }
  if (stepLegacy){ stepLegacyRename(false); return; }
  if (stepStage === "parent" && stepLegacyPrev && !stepFinding){ stepLegacyRestore(); return; }
  if (stepStage === "done" && stepNone){ enterStage("type"); return; }
  if (stepStage === "done"){
    const td = ParamNaming.typeDef(stepType) || {};
    const hasAnchorStage = (td.anchor || "none") === "required";
    const hasAxisStage = (td.axis || "none") !== "none";
    if (!hasAnchorStage && !hasAxisStage){
      // Radius, Multiplier, Angle: nothing was ever asked here — the only thing to
      // undo is the type itself.
      stepType = null;
      enterStage("type");
      return;
    }
    // Undo the anchor/axis decision (or, if it was skipped rather than picked,
    // return to the stage that offered the skip) and land back on whichever of the
    // two this type actually takes. BACK ON A PAGE, ITS CHIP GOES: whatever is picked
    // there now makes it again (see "The crumb as chips").
    stepStage = hasAnchorStage ? "anchor" : "axis";
    stepAnchors = []; stepAxes = [];
    nameEl.value = "";
    renderSteps();
    return;
  }
  if (stepStage === "axis" || stepStage === "anchor"){
    // Mid-step, the thing to undo is the last direction put in — the type is only
    // reached once the step is empty again.
    const chosen = stepChosen();
    if (chosen.length){ chosen.pop(); renderSteps(); return; }
    stepType = null;
    enterStage("type");
    return;
  }
  if (stepStage === "type"){
    // Back to the group menu, its chip gone: the menu shows the groups beside the last
    // one, and picking sets it again.
    stepType = null;
    if (stepLevels.length > 1) stepLevels.pop();
    enterStage("level");
    return;
  }
  if (stepStage === "level"){
    // Typing a new group: back to the menu. Browsing: out of the last group, and from
    // the owner's own groups, back to Parent.
    if (stepTyping){ stepStopTyping(); return; }
    if (stepLevels.length > 1){ stepLevels.pop(); clearError(); renderSteps(); return; }
    stepOwnerSet = false;                   // back on Parent: its chip goes too
    enterStage("parent");
    return;
  }
  if (stepStage === "parent" && stepFinding) stepStopFinding();
}

document.addEventListener("keydown", e => {
  if (e.key === "Alt" && stepperOn){ stepWide = true; renderSteps(); }
});
document.addEventListener("keyup", e => {
  if (e.key === "Alt" && stepperOn){ stepWide = false; renderSteps(); }
});

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
nameEl.addEventListener("input", () => {
  if (stepperOn){ renderSteps(); if (stepTextMode()){ clearError(); updateAC(); } return; }
  const v = ParamNaming.joinName(nameEl.value);
  // Empty reads as "not filled in yet", not as an error, even when one is required —
  // the refusal on commit is what says that, and colouring an untouched field red the
  // moment the dialog opens would be shouting before anything went wrong.
  const bad = !!v && !!nameProblem(v);
  fName.classList.toggle("bad", bad);
  clearError();
  updateAC();
});

/* ── The name's separators ──
   A comma between feature levels is punctuation, and punctuation that runs straight
   into the next word ("Boot,Ceiling") is harder to read than punctuation that doesn't.
   So the comma WRITES ITS OWN SPACE, here and when a suggestion is taken from the list
   (stepTakeSuggestion) — one rule, so the two ways of adding a level look the same.

   AND NEVER TWO SPACES. A space typed where one already stands is refused: a name has
   no use for a double space, and the one you were about to type is the one the comma
   already wrote. (A single space inside a level is still capitalise.js's word-join key
   — "boot lid" becomes "BootLid" — which is why it is a refusal and not a deletion.)

   Both name fields take it: #pname and the make-parameter panel's #mname, which is the
   same kind of field. data-joinwords is the test, since that is exactly the mark of "a
   parameter name is being spelled here".

   On the DOCUMENT, so a field built later is covered too, and after the field's own
   handlers have had the key — a space the stepper spent on "skip this step" arrives
   here already answered (defaultPrevented) and is left alone. */
const NAME_SEP = ", ";
document.addEventListener("keydown", e => {
  const el = e.target;
  if (!el || !el.dataset || el.dataset.joinwords !== "1") return;
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  const v = el.value, a = el.selectionStart, b = el.selectionEnd;
  if (a === null || a === undefined) return;
  if (e.key === ","){
    e.preventDefault();
    // Not a second space when the text already has one where the comma's would go.
    const sep = /^[ \t]/.test(v.slice(b)) ? "," : NAME_SEP;
    el.value = v.slice(0, a) + sep + v.slice(b);
    el.setSelectionRange(a + sep.length, a + sep.length);
    // Assigning to .value fires nothing, and everything downstream of this field —
    // the steps, the suggestions, the error — listens for `input`.
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  if (e.key === " " && a === b && (v[a - 1] === " " || v[a] === " ")) e.preventDefault();
});

// Everything the stepper owns while #pname has focus: the A-S-D-F keys, Backspace to
// go back, Enter/Space to confirm. Attached to the FIELD, not the document, so it runs
// before the shared keydown handler below and can stopPropagation to keep that handler
// from also treating the same key as an accept/commit. Escape and Tab are left alone —
// they fall through to the shared handler exactly as they do outside a stepper session.
// Is #pname taking typed text right now (New group, Find component), rather than
// standing in, unseen, for the menu?
const stepTextMode = () => !!stepRenaming || (stepStage === "level" && stepTyping) ||
                           (stepStage === "parent" && stepFinding);
nameEl.addEventListener("focus", () => { if (stepperOn && stepTextMode()) updateAC(); });
// A click is arriving fresh, even into a field that already has focus (no focus event
// then): suggestions Esc closed come back, top row preselected, as on first entry.
// LEAVING THE FIELD CLOSES ITS LIST. A suggestion list belongs to the field it is
// completing: once the keyboard is somewhere else — a click on the value box — it is a
// panel of answers to a question nobody is asking, sitting over whatever you moved to.
// Clicking a row of it doesn't blur the field (the list's own mousedown refuses the
// focus — see acEl's handler), so this never eats a choice.
nameEl.addEventListener("blur", () => {
  if (!acItems.length) return;
  acItems = []; acIndex = -1;
  renderAC();
});
nameEl.addEventListener("click", () => {
  if (!stepperOn || !stepTextMode()) return;
  stepAcClosed = false;
  updateAC();
});

nameEl.addEventListener("keydown", e => {
  if (!stepperOn) return;
  if (isCloseDialog(e)) return;   // the whole dialog — the document handler's
  clearStepTitleError();
  const stop = () => { e.preventDefault(); e.stopPropagation(); };
  if (stepMulti.length && stepMultiKey(e, stop)) return;
  if (stepRenaming){ stepTextKey(e, stop, stepStopRename, stepApplyRename); return; }
  if (renameChord && eventChord(e) === renameChord){ stop(); stepStartRename(); return; }
  // Backspace too: in this window Space+A (and the other A layers) arrives AS Backspace
  // (remap's mirror.a), which would otherwise be stepBack's "leave the chip as it was".
  const plainBack = e.key === "Backspace" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
  if ((plainBack || deleteChords.includes(physChord(e))) && !stepTextMode() && stepDeleteChip()){
    stop(); return;
  }
  if (physChord(e) === HIDE_CHORD && !stepTextMode() && stepEdit){
    stop(); stepToggleHide(); return;
  }
  if (stepStage === "parent"){
    // No leading space, and never two in a row (the joinwords guard below is off here).
    if (stepFinding && e.key === " "){
      const v = nameEl.value, a = nameEl.selectionStart, b = nameEl.selectionEnd;
      if (!v.slice(0, a).trim() || (a === b && (v[a - 1] === " " || v[a] === " "))){ stop(); return; }
    }
    if (stepFinding) stepTextKey(e, stop, stepStopFinding, () => {
      const it = acItems[acIndex >= 0 ? acIndex : 0];
      if (it) setOwner(it.entry.target);
      else showError("No component by that name.");
    });
    else stepParentKey(e, stop);
    return;
  }
  if (stepStage === "level"){
    if (stepTyping) stepTextKey(e, stop, stepStopTyping, () => {
      if (acIndex >= 0 && acItems[acIndex]) stepTakeSuggestion(acIndex);
      else stepConfirmLevels();
    });
    else stepMenuKey(e, stop);
    return;
  }
  if (e.key === "Escape" || e.key === "Tab") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === "Backspace"){ stop(); stepBack(); return; }
  if (e.key === "Enter"){
    // 'done' means there is nothing left for the stepper to confirm — Enter here is
    // an ordinary commit and belongs to the shared handler below.
    if (stepStage === "done") return;
    stop();
    stepConfirmEmpty();
    // The Enter that finished the name (Thickness, Radius, an anchor) is the commit too —
    // unless a chip was clicked: then the name is being revised, and a second Enter says so.
    if (stepStage === "done" && !stepChipEdited) stepCommit();
    return;
  }
  if (e.key === " "){ stop(); if (stepLegacy) stepLegacyRename(true);
                      else if (stepStage !== "done") stepConfirmEmpty(); return; }
  if (e.key.length !== 1) return;              // arrows etc.
  // The field is out of sight on these steps, so nothing may be typed into it: a key
  // takes its choice, X is the Cancel row's (STEP_CANCEL_KEY — answered here rather
  // than by its position in the list), and any other key is refused.
  stop();
  const k = e.key.toLowerCase();
  if (k === STEP_CANCEL_KEY){ if (!stepLegacy) stepBack(); return; }   // no Cancel row under the old name's chip
  if (k === STEP_GROUP_KEY && stepStage === "type"){ stepToggleGroup(); return; }
  if (stepStage === "done") return;
  const idx = STEP_KEYS.indexOf(k);
  if (idx >= 0 && pickStepIndex(idx) && stepStage === "done" && !stepChipEdited) stepCommit();   // the pick that finished the name commits it
});

// A click on a choice takes it, like its key — and, when that finishes the name,
// commits the parameter there and then, as the finishing Enter does.
stepChoicesEl.addEventListener("mousedown", e => {
  if (!stepperOn) return;
  clearStepTitleError();
  if (e.target.closest(".stepopt[data-mhide]")){ e.preventDefault(); stepMultiHide(); nameEl.focus(); return; }
  if (e.target.closest(".stepopt[data-mdel]")){ e.preventDefault(); stepMultiDelete(); nameEl.focus(); return; }
  const back = e.target.closest(".stepopt[data-back]");
  if (back){ e.preventDefault(); stepBack(); nameEl.focus(); return; }
  if (e.target.closest(".stepopt[data-legacy]")){ e.preventDefault(); stepLegacyRename(true); nameEl.focus(); return; }
  if (e.target.closest(".stepopt[data-new]")){
    e.preventDefault();
    if (stepStage === "parent") stepStartFinding(); else stepStartTyping();
    return;
  }
  if (e.target.closest(".stepopt[data-space]")){
    e.preventDefault(); stepConfirmEmpty(); nameEl.focus();
    if (stepStage === "done" && !stepChipEdited) stepCommit();
    return;
  }
  if (e.target.closest(".stepopt[data-insert]")){
    e.preventDefault(); stepStartInsert(); return;
  }
  if (e.target.closest(".stepopt[data-flip]")){
    e.preventDefault(); stepFlipLast(); nameEl.focus(); return;
  }
  if (e.target.closest(".stepopt[data-toggle]")){
    e.preventDefault(); stepToggleGroup(); nameEl.focus(); return;
  }
  const opt = e.target.closest(".stepopt[data-w]");
  if (!opt) return;
  e.preventDefault();                       // keep the keyboard in #pname
  if (!pickStepWord(opt.dataset.w)) return;
  nameEl.focus();
  if (stepStage === "done" && !stepChipEdited) stepCommit();
});

// The keys while #pname is taking text (New group, Find component) — the #ac list's,
// owned here rather than by the shared handler below so Esc can close it before it
// would cancel the dialog. `leave` puts the menu back; `enter` takes what was typed.
function stepTextKey(e, stop, leave, enter){
  if (e.key === "Backspace" && !nameEl.value){ stop(); leave(); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp"){
    stop();
    if (stepAcClosed || !acItems.length){ stepAcClosed = false; updateAC(); return; }
    const n = acItems.length;
    if (acIndex < 0) acIndex = 0;
    else acIndex = e.key === "ArrowDown" ? (acIndex + 1) % n : (acIndex <= 0 ? n - 1 : acIndex - 1);
    renderAC();
    return;
  }
  if (e.key === "Escape"){
    stop();
    // One press closes the list (a click brings it back); with it closed, back to the menu.
    if (!acItems.length){ leave(); return; }
    stepAcClosed = true;
    acItems = []; renderAC();
    return;
  }
  if (e.key === "Enter"){ stop(); enter(); }
}

// The parent menu's keys: a letter takes that component, Enter the first (the default),
// Space finds one by name. X and Backspace have nothing to go back to. Any other key is
// refused — the field is out of sight.
function stepParentKey(e, stop){
  if (e.key === "Escape" || e.key === "Tab") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Enter"){ stop(); if (ownerChain.length) setOwner(ownerChain[0]); return; }
  if (e.key === " "){ stop(); stepStartFinding(); return; }
  if (e.key === "Backspace"){ stop(); if (stepEdit || stepLegacyPrev) stepBack(); return; }
  if (e.key.length !== 1) return;              // arrows etc.
  stop();
  if ((stepEdit || stepLegacyPrev) && e.key.toLowerCase() === STEP_CANCEL_KEY){ stepBack(); return; }
  if (e.key.toLowerCase() === STEP_INSERT_KEY && stepCanInsert()){ stepStartInsert(); return; }
  const idx = STEP_KEYS.indexOf(e.key.toLowerCase());
  if (idx >= 0) pickStepIndex(idx);
}

// The group menu's keys (#pname out of sight): a letter goes into that group, X backs
// out, Space starts a New group, Enter takes the group browsed to. Any other letter is
// refused — it would otherwise type into a field that isn't listening. Esc and Tab fall
// through to the shared handler, as on every step.
function stepMenuKey(e, stop){
  if (e.key === "Escape" || e.key === "Tab") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Enter"){ stop(); stepConfirmBrowse(); return; }
  if (e.key === " "){ stop(); stepStartTyping(); return; }
  if (e.key === "Backspace"){ stop(); stepBack(); return; }
  if (e.key.length !== 1) return;              // arrows etc.
  stop();
  const k = e.key.toLowerCase();
  if (k === STEP_CANCEL_KEY){ stepBack(); return; }
  if (k === STEP_GROUP_KEY && stepCanFlipLast()){ stepFlipLast(); return; }
  if (k === STEP_INSERT_KEY && stepCanInsert()){ stepStartInsert(); return; }
  const idx = STEP_KEYS.indexOf(k);
  if (idx >= 0) pickStepIndex(idx);
}

function clearError(){ errEl.textContent = ""; }

/* ── Commit / cancel ─────────────────────────────────────────────────────────
   The window hides on CANCEL immediately (nothing can fail), but waits for Fusion on
   COMMIT: the add-in is the only thing that knows whether the expression parses and
   whether the name is free, and a dialog that vanished before it knew would lose
   whatever you typed.

   While it waits the panel DIMS (`body.busy`, theme.css): a build can take Fusion
   seconds, and a panel that just sat there looked like the Enter had missed. An error
   (showError) or the session ending lifts it. Every commit in flight — a panel's, or
   the make panel's name waiting for Fusion to say it exists — goes through here. */
function setCommitting(on){ committing = on; syncBusy(); }
function setMakePending(name){ makePending = name; syncBusy(); }
function syncBusy(){ document.body.classList.toggle("busy", committing || !!makePending); }

function commit(){
  if (committing) return;
  const expr = exprText();
  // A driven dimension commits a NAME only: the add-in links it (a user parameter that
  // tracks the measurement — Driven Link.py) and never reads the value. nameRequired
  // is on for it, so the name is checked below like a new parameter's.
  if (isDriven && expr !== drivenExpr){
    showError("This dimension is driven — its value comes from the sketch's constraints. "
              + "Put it back and give it a name instead.");
    return;
  }
  if (!expr && !isDriven && !flexGap()){ showError("Enter a value first."); focusField(valueEl); return; }
  if (pendingRetarget){ showError("Still renaming the group — a moment."); return; }
  // While the stepper is running, #pname never holds the real name — it's the
  // composed levels/type/anchor from stepResult(), or "" until it reaches 'done'.
  const name = stepperOn ? stepperName() : ParamNaming.joinName(nameEl.value);
  // A parameter session has nothing to fall back on: without a name there is no
  // parameter to make, so the commit is refused HERE rather than round-tripping through
  // Fusion to be refused there. Fusion still checks (it owns the name space), but this
  // way the key simply doesn't take and the caret lands where the problem is.
  if (nameRequired){
    const why = nameProblem(name);
    if (why){ showError(why); focusField(nameEl); return; }
  }
  // "Make as group": every existing sibling this parameter's own type/group would
  // otherwise repeat moves into the group with it — same posts groupRenamePlan's
  // caller makes, refused whole (nothing committed either) on any collision.
  // "Make name" / "Make group": every other parameter at that place goes the same way.
  const pinMoves = stepperOn ? lastPinPlan(name) : [];
  if (typeof pinMoves === "string"){ showError(pinMoves); return; }
  const siblingMoves = stepperOn && stepTypeGroup ? typeGroupSiblingPlan(name) : [];
  if (siblingMoves === null){
    showError("Making that a group would collide with an existing parameter.");
    return;
  }
  applyRenames(pinMoves);
  applyRenames(siblingMoves);
  setCommitting(true);
  // The mode rides along so a commit still means the right thing if the add-in reloaded
  // while this dialog was open: it forgets the session, but a parameter needs no target
  // to create, so it can still be made from what the window says it is.
  post({ action: "commit", expression: expr, name: name, mode: mode,
         doppel: !!doppelName && doppelName === name, axis: namingSituation().axis || "",
         flex: flexState() });
  postStepShow(name);
}

// "Make name" / "Make group" (stepLastPin) at commit: every other parameter at the same
// place — the same chain, owner first — is taken the same way, so the tree reads one way.
// A word made a GROUP: the parameters naming themselves with it pin it as a group and go
// into it; those already under it are in it. A group made a NAME: the parameters directly
// in it take it as their name, and a row inside it that has a name of its own takes both,
// joined ("Curve" + "Inset" → CurveInset) — the group is gone. Groups (or a "Make as
// group" type level) inside it refuse the whole thing. [{from, to}], or why not.
function lastPinPlan(newName){
  if (!stepLastPin || stepLastPin.key !== chainKey(stepLevels) || stepNone || stepLegacy) return [];
  const is = stepLastPin.is, d = stepLevels.length - 1, W = stepLevels[d];
  const prefix = stepLevels.slice(0, d);
  const idx = ParamNaming.nameIndex(parsedParams());
  const moves = [];
  for (const p of params){
    if (mode === "editparam" && p.name === origName) continue;
    const pp = ParamNaming.parse(p.name);
    if (!pp) continue;
    const lv = pp.levels;
    if (lv.length <= d || lv[d] !== W || !prefix.every((w, k) => lv[k] === w)) continue;
    const grouped = ParamNaming.typeGroupAt(pp) >= 0;
    let to;
    if (lv.length === d + 1){
      if (grouped) continue;              // it sits in its type's group, not here
      to = ParamNaming.compose(Object.assign({}, pp, { lastIs: is }));
    } else if (is === "name"){
      if (grouped || lv.length !== d + 2 || ParamNaming.nameWord(pp, idx) !== lv[d + 1])
        return '"' + ParamNaming.words(W) + '" has groups inside it — move them out first.';
      to = ParamNaming.compose(Object.assign({}, pp,
             { levels: prefix.concat([W + lv[d + 1]]), lastIs: "name" }));
    } else continue;                      // under a word becoming a group: in it already
    if (to !== p.name) moves.push({ from: p.name, to });
  }
  // A pin is only an extra underscore, so two names that differ by nothing else are the
  // same parameter to the reader — compared with the seams collapsed.
  const norm = n => n.replace(/_+/g, "_");
  const moving = new Set(moves.map(m => m.from));
  const taken = new Set(params.filter(p => !moving.has(p.name) &&
                                         !(mode === "editparam" && p.name === origName))
                              .map(p => norm(p.name)));
  const seen = new Set([norm(newName)]);
  if (taken.has(norm(newName))) return '"' + newName + '" is already a parameter, bar the pin.';
  for (const m of moves){
    if (seen.has(norm(m.to)) || taken.has(norm(m.to)))
      return '"' + m.from + '" would become "' + m.to + '", and that name is taken.';
    seen.add(norm(m.to));
  }
  return moves;
}

// Every existing sibling under the SAME base path and type — not already grouped by it
// — that would collide with the parameter this session is about to commit as the group
// gathers them in: [{from, to}], or null on any collision (nothing is committed then,
// same as groupRenamePlan's own refusal). `newName` is the parameter's own composed
// name, checked as an occupied spot too, since it is about to take it.
function typeGroupSiblingPlan(newName){
  if (!stepTypeGroup || !stepType || !paramNamingReady) return [];
  const at = stepTypeGroupAt();
  const baseKey = stepLevels.slice(0, at).join("\u0000");
  const moves = [];
  for (const p of params){
    if (p.name === origName) continue;   // this session's own parameter, if editing one
    const parts = ParamNaming.parse(p.name);
    if (!parts || parts.type !== stepType) continue;
    if (ParamNaming.typeGroupAt(parts) >= 0) continue;             // already grouped
    if (ParamNaming.groupLevels(parts).join("\u0000") !== baseKey) continue;
    const to = ParamNaming.compose({
      levels: parts.levels.slice(0, at).concat([stepType], parts.levels.slice(at)),
      type: parts.type, anchors: parts.anchors, axes: parts.axes });
    if (to && to !== p.name) moves.push({ from: p.name, to });
  }
  const taken = new Set(params.map(p => p.name).concat([newName]));
  const seen = new Set([newName]);
  for (const m of moves){
    if (seen.has(m.to) || (taken.has(m.to) && m.to !== m.from)) return null;
    seen.add(m.to);
  }
  return moves;
}

/* Make the plane and the sketch on it, or move an existing sketch's plane. The line is
   sent as TYPED, not as the expression it composes to: the add-in composes its own from
   the same rules, and the typed line is what gets stored on the sketch so this box can
   be opened on it again. Like commit(), the window stays up until Fusion says it worked
   — a datum that resolves to nothing has to be able to say so without losing the line. */
function commitSketch(){
  if (committing) return;
  const text = valueEl.value.replace(/\s*\n\s*/g, " ").trim();
  if (!text){ showError("Name a datum first."); focusField(valueEl); return; }
  if (!curDatum){
    showError('"' + Planes.splitInput(text)[0] + '" is not a datum here.');
    focusField(valueEl);
    return;
  }
  setCommitting(true);
  clearTimeout(planePreviewTimer);
  // The name is optional: left empty, Fusion's own "Sketch7" stands and nothing is
  // renamed. Sent as typed — a sketch name is a sentence, spaces and all.
  post({ action: "createsketch", text: text, sketch: sketchToken,
         name: sNameEl.value.trim(),
         angle: hasAngle() ? angleSpec() : null });
}

/* ── Flexible ────────────────────────────────────────────────────────────────
   Whether Section: Move may change this value (on by default, as the notes ask) and,
   optionally, the least and most it may become — expressions, so "Gap" or "8 mm" both
   do. Rides along in the commit as `flex`; the add-in stores it on the parameter
   (Section.write_flex). Flexible with a limit and NO value, on a dimension between two
   section references, is a gap limit: the add-in keeps the limit and drops the
   dimension (it measured nothing anyone means to drive). */
let flexOn = true;

function setFlex(f){
  flexOn = !f || f.flexible !== false;
  flexMinEl.value = (f && f.min) || "";
  flexMaxEl.value = (f && f.max) || "";
  renderFlex();
}

function renderFlex(){
  flexOnEl.classList.toggle("on", flexOn);
  fFlex.classList.toggle("off", !flexOn);
}

function flexState(){
  if (fFlex.classList.contains("hide")) return null;
  return { flexible: flexOn, min: flexMinEl.value.trim(), max: flexMaxEl.value.trim() };
}

// Flexible, a limit, and nothing in the value box: the gap-limit case above.
function flexGap(){
  const f = flexState();
  return !!(f && f.flexible && (f.min || f.max)) && !exprText();
}

flexOnEl.addEventListener("mousedown", e => { e.preventDefault(); flexOn = !flexOn; renderFlex(); });
for (const el of [flexMinEl, flexMaxEl]){
  el.addEventListener("keydown", e => {
    if (e.key === "Enter"){ e.preventDefault(); e.stopPropagation(); commit(); }
    else if (e.key === "Escape"){ e.preventDefault(); e.stopPropagation(); cancel(); }
  });
}

/* Why `name` cannot be used, or "" if it can. Duplicates the add-in's own rule on
   purpose — see commit() — and matches the live red-text check on the name field. */
function nameProblem(name){
  if (!name) return "Give the parameter a name.";
  // Editing a parameter and leaving its name alone is not a duplicate.
  if (name === origName) return "";
  if (!NAME_RE.test(name))
    return '"' + name + '" is not a valid parameter name — letters, digits and '
           + "underscores, not starting with a digit.";
  if (params.some(p => p.name === name)){
    if (isDoppelganger(name)){ doppelName = name; return ""; }
    return 'A parameter named "' + name + '" already exists.';
  }
  return "";
}

/* The same measurement made again, with no axis in its name — the "doppelganger" the
   notes describe: two of them are evidently for two axes, so the add-in gives each its
   axis (the existing one renamed, this one composed with its own) and nobody is asked.
   Only for a type that CAN carry an axis, and only when this dimension has one. */
function isDoppelganger(name){
  const parts = ParamNaming.parse(name);
  if (!parts || (parts.anchors || []).length || (parts.axes || []).length) return false;
  const td = ParamNaming.typeDef(parts.type) || {};
  if ((td.axis || "none") === "none") return false;
  return !!namingSituation().axis;
}

function cancel(){
  if (!isOpen) return;
  isOpen = false; setCommitting(false);
  sessionSketches = []; hidePick(); dropPickCycle();
  hideWindow();
  post({ action: "cancel" });
}

function showError(msg){
  // Each panel carries its own error line, and the others are hidden — writing to the
  // value box's while another panel is up put the message somewhere nobody could see
  // it, which reads exactly like the commit having done nothing at all.
  if (making){ setMakePending(""); mErr.textContent = msg; setCommitting(false); return; }
  const el = mode === "extrude" ? xErr : mode === "mirror" ? rErr
           : mode === "feature" ? oErr
           : (isBoundsPanelMode() ? cErr : errEl);
  el.textContent = msg;
  setCommitting(false);
}

function hideWindow(){
  acItems = []; acIndex = -1; renderAC();
  host("session", { open: false });
}

/* ── Keys ────────────────────────────────────────────────────────────────────── */
// The session itself, whichever panel holds it — the last Escape, and Close Whole
// Dialog at once.
function cancelSession(){
  if (isBoundsPanelMode()){
    isOpen = false; setCommitting(false);
    hideWindow();
    showPanel("value");
    post({ action: "cancel" });
    return;
  }
  if (mode === "extrude"){ cancelExtrude(); return; }
  if (mode === "mirror"){ cancelMirror(); return; }
  if (mode === "feature"){ cancelFeature(); return; }
  if (mode === "search"){ cancelSearch(); return; }
  if (mode === "versions"){ cancelVersions(); return; }
  cancel();
}

// Close Whole Dialog — the key below, or Fusion's Super Escape (escSeq on /state).
function closeWholeDialog(){
  if (!isOpen) return;
  if (chipMenuOpen()) hideChipMenu();
  acItems = []; acIndex = -1; renderAC();
  if (making){ making = null; setMakePending(""); }
  closeRowSheet();
  cancelSession();
}

document.addEventListener("keydown", e => {
  /* CLOSE WHOLE DIALOG (isCloseDialog — the master chord, Space+Esc by default), whatever
     is open inside it: the hand that holds Space means "out", not "put the suggestion
     list away first". Everything inner is put away quietly, then the same session
     cancel the last plain Escape does. */
  if (isCloseDialog(e)){
    e.preventDefault();
    closeWholeDialog();
    return;
  }
  // The versions panel has no field and owns every key, Escape included (an open
  // question takes it first) — see "The versions panel".
  if (mode === "versions" && isOpen){ versionsKey(e); return; }
  if (e.key === "Escape"){
    e.preventDefault();
    if (chipMenuOpen()){ hideChipMenu(); return; }
    if (!pickEl.classList.contains("hide")){ hidePick(); return; }
    /* A SUGGESTION LIST IS A LITTLE DIALOG OF ITS OWN, and Escape closes the
       innermost thing that is open. With one up over the value box, the first press
       put it away and the second cancels the session — the same two presses the
       stepper's own feature list already took (stepLevelKey, which answers Escape
       before this handler ever sees it and is why #pname is not tested here). */
    if (acItems.length){
      acItems = []; acIndex = -1;
      renderAC();
      return;
    }
    if (making){ closeMake(); return; }
    if (isSheetOpen()){ closeRowSheet(); return; }
    cancelSession();
    return;
  }

  // No field on this panel at all, so it owns every key: Enter is OK, and any chord
  // that means something in Fusion right now goes back to Fusion — see "The Mirror
  // panel". Nothing else can reach a key here, so nothing below needs to see it. A
  // feature panel with no text row is the same panel.
  if (mode === "mirror" || (mode === "feature" && !oHasText())){
    if (e.key === "Enter"){
      e.preventDefault();
      if (mode === "mirror") commitMirror(); else commitFeature();
      return;
    }
    const chord = e.repeat ? null : xChordFrom(e);
    const keys = mode === "mirror" ? rKeys : oKeys;
    if (chord && keys.has(chord)){
      e.preventDefault();
      // A cycle steps the row here and now (xCycle); anything else goes to Fusion.
      const kind = (mode === "mirror" ? rCycleKeys : (oSpec.cycleKeys || {}))[chord];
      if (kind && (mode === "mirror" ? rCycle(kind) : oCycle(kind))) return;
      rPressKey(chord);
    }
    return;
  }

  // One field, no list: Enter asks Fusion for the parameter, Tab has nowhere to go.
  if (making){
    if (e.key === "Enter"){ e.preventDefault(); commitMake(); }
    else if (e.key === "Tab") e.preventDefault();
    return;
  }

  // One field and a list under it, so there is nothing else to Tab to. ↑/↓ walk the
  // list (wrapping, same as .ac-row elsewhere) and Enter runs the highlighted row —
  // checked ahead of everything below because this mode owns every key while it is up.
  if (mode === "search"){
    // With the sheet up the list underneath is hidden and not this key's to navigate
    // or run — the kit's handleKey answers that, too.
    sKit.handleKey(e);
    return;
  }

  /* ⌘1 ⌘2 ⌘3 — tag the parameter in front of you with an axis: the highlighted
     suggestion if the list is open, otherwise the chip the caret is on. The digits
     follow the vocabulary's own order, so a fourth axis word (there will not be one)
     would be ⌘4 without this learning anything.

     A WINDOW key, not a Fusion one. It never reaches Fusion — this window has the
     keyboard while it is up — so it is not in the keymap and does not go through
     hotkeys.py, exactly like the Browser window's ⌘X / ⌘V on parameter rows. */
  if ((e.metaKey || e.ctrlKey) && /^[1-9]$/.test(e.key)){
    const word = tagWords("axis")[+e.key - 1];
    if (word && toggleAxisTag(word)){ e.preventDefault(); return; }
  }

  // ⌥↓ / ⌥↑ step through what a viewport pick suggested (see "Picked from the
  // viewport"). Ahead of the plain arrows, which belong to the autocomplete.
  if (e.altKey && !e.metaKey && !e.ctrlKey && isOpen && mode === "sketch" &&
      (e.key === "ArrowDown" || e.key === "ArrowUp") && pickItems.length > 1){
    e.preventDefault();
    cyclePick(e.key === "ArrowDown" ? 1 : -1);
    return;
  }

  /* ↑ / ↓ walk the suggestion list, wherever the list is. It used to be handled below,
     inside the value box's own section, which meant the bounds fields and the extrude
     fields — both of which complete against the same list — had arrow keys that did
     nothing but move the caret through a one-line field. The list belongs to whichever
     field has it (acField), so the key does too.

     With no list open the arrows are left alone: ↓ opens one if there is anything to
     show, and in the multi-line value box ↑/↓ are still line movement. */
  if (e.key === "ArrowDown" || e.key === "ArrowUp"){
    const acEl2 = acField || valueEl;
    if (document.activeElement === acEl2){
      if (!acItems.length){
        if (e.key === "ArrowDown"){
          updateAC();
          if (acItems.length){ e.preventDefault(); acIndex = 0; acTouched = true; renderAC(); }
        }
        return;
      }
      e.preventDefault();
      const n = acItems.length;
      // THE FIRST ARROW INTO AN UNMARKED LIST MARKS WHERE THE DEFAULT ALREADY IS. The
      // top row is what Enter would have taken all along (see acTouched); it simply was
      // not lit, so there is nothing under you to step off, and stepping to the second
      // row would silently skip the one you were about to take.
      if (!acTouched && acIndex >= 0){ acTouched = true; renderAC(); return; }
      acTouched = true;
      acIndex = e.key === "ArrowDown"
        ? (acIndex + 1) % n
        : (acIndex <= 0 ? n - 1 : acIndex - 1);
      renderAC();
      return;
    }
  }

  // F TAKES A SUGGESTION THE ARROWS STEPPED TO — the same as Enter's acceptAC. Only once
  // an arrow has marked a row (acTouched): before that the list is only a default, and F
  // is a letter being typed like any other. One press, one take (no auto-repeat).
  if ((e.key === "f" || e.key === "F") && !e.metaKey && !e.ctrlKey && !e.altKey &&
      acTouched && acIndex >= 0 && acItems.length &&
      document.activeElement === (acField || valueEl)){
    e.preventDefault();
    if (!e.repeat) acceptAC();
    return;
  }

  // The component panel has many fields, so Tab is the browser's to handle — walking
  // them in order is exactly right, and the two-field cycle below would trap it.
  if (isBoundsPanelMode()){
    if (e.key === "Enter"){
      e.preventDefault();
      if (acceptAC()) return;          // a highlighted suggestion wins the key first
      commitComponent();
    }
    return;
  }

  // Several fields, so Tab is the browser's to handle — walking them in order is
  // exactly right, and the two-field cycle below would trap it. Same as the component
  // panel, and for the same reason.
  // A feature panel with a text row is the Extrude panel again.
  if (mode === "extrude" || mode === "feature"){
    // Active While Typing: a chord one of this shortcut's rows owns fires it instead
    // of typing, even though the caret is sitting in Distance. Checked before anything
    // else in this mode — Enter included — because it is a claim on the KEY, not on
    // what the field would otherwise do with it.
    const keys = mode === "extrude" ? xKeys : oKeys;
    if (keys.size){
      const chord = xChordFrom(e);
      if (chord && keys.has(chord)){
        e.preventDefault();
        // A cycle steps the row here and now (xCycle); anything else goes to Fusion.
        const kind = (mode === "extrude" ? xCycleKeys : (oSpec.cycleKeys || {}))[chord];
        if (kind && (e.repeat || (mode === "extrude" ? xCycle(kind) : oCycle(kind)))) return;
        xPressKey(chord);
        return;
      }
    }
    if (e.key === "Enter"){
      e.preventDefault();
      if (acceptAC()) return;        // a highlighted suggestion wins the key first
      if (mode === "extrude") commitExtrude(); else commitFeature();
    }
    return;
  }

  // Enter takes a highlighted suggestion, or commits — from any of the box's fields,
  // since none of them is the one the commit is waiting on. Everything else — the arrow
  // keys, the → accept, the list itself — is the value box's ordinary behaviour and is
  // left below.
  if (mode === "sketch"){
    // The line, the name, and the angle when there is one (Project Bounds hides it) —
    // Tab cycles them, wrapping, the same shape as the value box's own cycle below.
    if (e.key === "Tab"){
      e.preventDefault();
      const stops = flat ? [valueEl, sNameEl] : [valueEl, sNameEl, angleEl];
      const at = stops.indexOf(document.activeElement);
      const next = stops[(at < 0 ? 0 : at + (e.shiftKey ? stops.length - 1 : 1)) % stops.length];
      next.focus();
      next.setSelectionRange(next.value.length, next.value.length);
      updateAC();
      refreshFocusStyle();
      return;
    }
    // → accepts the highlighted suggestion from EITHER field now that the angle
    // completes too — the value box's own copy of this rule is below, for the modes
    // that never reach here. Only with the caret at the END of the token it would
    // complete; anywhere else → is still just cursor movement.
    if (e.key === "ArrowRight" && acIndex >= 0 && acItems.length){
      const fld = document.activeElement === angleEl ? angleEl : valueEl;
      const tok = activeToken();
      if (tok && fld.selectionStart === tok.b){
        e.preventDefault();
        acceptAC();
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      if (acceptAC()) return;      // a highlighted suggestion wins the key first, either field
      commitSketch();
      return;
    }
    // Plain typing in the angle or the name: neither is the value box, so none of the
    // chip/suggestion handling below is theirs.
    if (document.activeElement === angleEl ||
        document.activeElement === sNameEl) return;
  }

  if (e.key === "Tab"){
    e.preventDefault();
    // A cycle, not a walk off the end into whatever the browser thinks is next: the
    // value and the name. The stepper's parent is a step of the name, not a stop of its own.
    const stops = [valueEl, nameEl];
    const at = stops.indexOf(document.activeElement);
    const next = stops[(at < 0 ? 0 : at + (e.shiftKey ? stops.length - 1 : 1)) % stops.length];
    hidePick();
    focusField(next);
    if (next === valueEl) updateAC();
    return;
  }

  const inValue = document.activeElement === valueEl;

  // → accepts the highlighted suggestion, but only with the caret at the END of the
  // token it would complete. Anywhere else → is still just cursor movement, so you can
  // walk back into an expression without the list swallowing the key.
  if (inValue && e.key === "ArrowRight" && acIndex >= 0 && acItems.length){
    const tok = activeToken();
    if (tok && valueEl.selectionStart === tok.b){
      e.preventDefault();
      acceptAC();
      return;
    }
  }

  if (e.key === "Enter"){
    if (e.shiftKey && inValue) return;            // ⇧Enter = newline, the multi-line escape
    e.preventDefault();
    // A highlighted suggestion wins the key first, from EITHER field — the Name field
    // completes against existing parameters exactly as the value box completes against
    // its own vocabulary, and Enter has to take it the same way there too.
    if (acceptAC()) return;
    if (!inValue){ commit(); return; }
    // Enter from the value field COMMITS. Naming the parameter is the rarer half of this
    // dialog and Tab is the way to it, so making every commit pass through the name field
    // charged the common case for the uncommon one.
    //
    // Unless the name still needs something: then Enter advances to it, saying why on
    // the name's own line. The test is the name as it stands — with the stepper up that
    // is stepperName(), NOT #pname, which is always empty there; reading #pname sent an
    // edited parameter's perfectly good name back through the stepper on every Enter.
    // An edited name that doesn't follow the convention (stepLegacy) counts as needing
    // naming too — Enter from the name step keeps it as it is.
    if (nameRequired){
      const name = stepperOn ? stepperName() : ParamNaming.joinName(nameEl.value);
      const why = stepLegacy ? "This name doesn't follow the naming convention."
                             : nameProblem(name);
      if (why){
        focusField(nameEl);
        if (stepperOn) stepTitleError(why); else showError(why);
        return;
      }
    }
    commit();
  }
});

/* ── The New Component panel ─────────────────────────────────────────────────
   Built once from a spec: three near-identical axis blocks, each a symmetry checkbox
   plus either one span field or a pair of face fields. A symmetric axis wants ONE
   distance, because both faces are the same distance away and the value means midline
   to side — so the span field is shown and the pair hidden, and toggling carries the
   value across rather than losing what was typed to a checkbox.

   Every bounds field completes against the parameter list, which is why this panel is
   here rather than in a window of its own.                                          */
// `primary` is the face a SYMMETRIC axis is given by: the other one is its mirror, so
// ticking the axis greys out and empties the secondary field (see applySymmetry). Y's
// primary is Front — the LOW end — which Bounds.symmetric_side follows on the Fusion side.
const AXIS_ROWS = [
  { axis: "X", span: "Width",  high: "right", low: "left",   primary: "right" },
  { axis: "Y", span: "Depth",  high: "back",  low: "front",  primary: "front" },
  { axis: "Z", span: "Height", high: "top",   low: "bottom", primary: "top"   }
];
const secondaryOf = spec => spec.primary === spec.high ? spec.low : spec.high;
// Width, Depth and Height are never typed here. They are DERIVED from the two faces —
// high + low, halved when the axis is symmetric — so asking for them as well would be
// asking the same question twice and inviting the two answers to disagree. Symmetry
// still has a checkbox, because it decides that halving and the vocabulary, not the
// number of fields.

// Each header is otherwise just somewhere to drag from — the title span (dialogTitle,
// set on every open) is the one thing in it.
const cPanel = $("cpanel"), cRows = $("crows"), cErr = $("cerr"), cTitleEl = $("cdtitle");
const xPanel = $("xpanel"), xRows = $("xrows"), xErr = $("xerr"),
      xAdv = $("xadv"), xAdvHead = $("xadvhead"), xTitleEl = $("xdtitle");
const rPanel = $("rpanel"), rRows = $("rrows"), rErr = $("rerr"),
      rAdv = $("radv"), rAdvHead = $("radvhead"), rTitleEl = $("rdtitle");
const oPanel = $("opanel"), oRows = $("orows"), oErr = $("oerr"),
      oAdv = $("oadv"), oAdvHead = $("oadvhead"), oTitleEl = $("odtitle");
let cName = null;
let cNameRow = null;         // hidden when the panel targets an existing component
const cFields = {};          // direction -> input
const cSyms   = {};          // axis -> checkbox
let cSheetRow = null;        // "Sheet metal" row — hidden outright in 'addbounds'
let cSheet = null;           // "Sheet metal" checkbox
let cSheetRuleRow = null;    // hidden until the checkbox is on, and hidden outright
let cSheetRule = null;       // for 'addbounds' — an existing component isn't converted here

/* Which panel is on screen. Set display directly rather than toggling a class: the
   class route depends on specificity against #panel's own rules, and when it silently
   lost, BOTH panels rendered — the value box stacked above the component one, and the
   component panel's last rows pushed off the bottom of a window sized for one of them.
   Two symptoms, one cause. */
let panelShown = "value";
function showPanel(which){
  if (which !== "make") panelShown = which;
  panel.style.display  = which === "value" ? "" : "none";
  cPanel.style.display = which === "component" ? "" : "none";
  xPanel.style.display = which === "extrude" ? "" : "none";
  rPanel.style.display = which === "mirror" ? "" : "none";
  oPanel.style.display = which === "feature" ? "" : "none";
  sPanel.style.display = which === "search" ? "" : "none";
  vPanel.style.display = which === "versions" ? "" : "none";
  mPanel.style.display = which === "make" ? "" : "none";
  // Any other panel coming up — a new session over this one included — ends a make.
  if (which !== "make"){ making = null; setMakePending(""); makeStepperClose(); }
}

/* `into` is what lets the Extrude panel reuse these rows: the row primitives are about
   the LOOK of a row, not about which panel it belongs to, and the New Component panel
   was simply the only caller when they were written. */
function cRow(cls, into){
  const d = document.createElement("div");
  d.className = "crow" + (cls ? " " + cls : "");
  (into || cRows).appendChild(d);
  return d;
}

/* The label lives in the PLACEHOLDER, so it is faint until you type and what you type
   reads in --text — the same trick, and the same two tones, as the value box. */
/* `isExpression` marks a field that holds a parameter expression, which is the one
   distinction the panel needs: those fields complete against the parameter list AND
   select their contents on entry, because they arrive prefilled and are meant to be
   overwritten. The name is neither — it starts empty, and selecting nothing while
   offering to complete a component name against parameters would be two kinds of
   wrong. */
function cTextRow(placeholder, isExpression, into){
  const row = cRow("", into);
  const i = document.createElement("input");
  i.type = "text"; i.spellcheck = false; i.autocomplete = "off";
  i.placeholder = placeholder;
  row.appendChild(i);
  i.addEventListener("focus", () => {
    attachAC(isExpression ? i : null, isExpression ? row : null);
    row.classList.add("on");
    if (isExpression) selectPrefilled(i);
  });
  i.addEventListener("blur", () => row.classList.remove("on"));
  if (isExpression) i.addEventListener("input", () => { updateAC(); fitWidth(); });
  // The bounds and extrude fields hold expressions, so they get items too — the whole
  // reason those panels live in this window is that they want what the value box has.
  if (isExpression) chipsAttach(i);

  /* Arriving at a field puts the caret after its last item (see selectPrefilled), ready
     for Backspace, an arrow walk, or a keystroke starting the next item beside it.

     A CLICK is the exception, and used to be caught on its way out to re-apply the
     selection the caret wiped. Not any more: a click into a field of items is a click AT
     something — the chip you aimed at, or the gap you want the caret in — and answering
     it by selecting everything is answering a question that was not asked. */
  return { row, input: i };
}

function cCheckRow(label){
  const row = cRow("check");
  const l = document.createElement("span");
  l.className = "clabel";
  l.textContent = label;
  row.appendChild(l);
  return row;
}

function cCheckbox(row, label, checked){
  const wrap = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox"; box.checked = !!checked;
  const txt = document.createElement("span");
  txt.textContent = label;
  wrap.appendChild(box); wrap.appendChild(txt);
  row.appendChild(wrap);
  return box;
}

// Same metrics as cCheckRow (a .crow.check), but for a choice with more options than a
// checkbox row can hold — the sheet metal rule list, which can run to a dozen-plus
// named rules and so wants a native <select> rather than the Extrude panel's segmented
// words (those are for two or three options, not a whole library).
function cSelectRow(label){
  const row = cRow("check");
  const l = document.createElement("span");
  l.className = "clabel";
  l.textContent = label;
  row.appendChild(l);
  const sel = document.createElement("select");
  row.appendChild(sel);
  return { row, select: sel };
}

function buildComponentPanel(){
  const nameField = cTextRow("Name", false);
  cName = nameField.input;
  cNameRow = nameField.row;

  for (const spec of AXIS_ROWS){
    // Left/Right and Front/Back read low-to-high; Top sits above Bottom, as it does
    // in the model.
    const dirs = spec.axis === "Z" ? [spec.high, spec.low] : [spec.low, spec.high];
    for (const dir of dirs){
      cFields[dir] = cTextRow(dir[0].toUpperCase() + dir.slice(1), true).input;
    }
  }

  // Last, and below the faces it qualifies: one word with a switch per axis. It is a
  // single setting with three options rather than three separate questions, and it
  // describes the six values above rather than asking for a seventh.
  const symRow = cCheckRow("Symmetry");
  for (const spec of AXIS_ROWS){
    cSyms[spec.axis] = cCheckbox(symRow, spec.axis, spec.axis === "X");
    cSyms[spec.axis].addEventListener("change", () => {
      applySymmetry(spec, false);
      refreshChips();
      fitWidth();
    });
  }

  // Sheet metal, native's own two-step: a checkbox, then which rule — hidden until
  // the box is ticked, since there is nothing to choose while it reads "no". Hidden
  // outright in 'addbounds' mode (openAddBoundsSession clears sheetRules): converting
  // an existing component to sheet metal is not this panel's job.
  cSheetRow = cCheckRow("Sheet metal");
  cSheet = cCheckbox(cSheetRow, "", false);
  const ruleField = cSelectRow("Rule");
  cSheetRuleRow = ruleField.row;
  cSheetRule = ruleField.select;
  cSheetRuleRow.style.display = "none";
  cSheet.addEventListener("change", () => {
    cSheetRuleRow.style.display = (cSheet.checked && cSheetRule.options.length) ? "" : "none";
    fitWidth();
    sizeWindow("keep");
  });
}

/* Which panel a session opens. The two share a window, a theme and an autocomplete,
   and nothing else — so they are two functions and one door. */
function beginSession(s){
  openSelectAll = false;              // openSession sets it again when it applies
  // Every panel types parameters — the plane box, Extrude, the feature rows — so every
  // session reads what each one hides from typing, not only openSession's.
  loadBrowserShow();
  const m = s.mode || "";
  if (m === "component") openComponentSession(s);
  else if (m === "addbounds" || m === "editbounds") openAddBoundsSession(s);
  else if (m === "sketch") openSketchSession(s);
  else if (m === "extrude") openExtrudeSession(s);
  else if (m === "mirror") openMirrorSession(s);
  else if (m === "feature") openFeatureSession(s);
  else if (m === "search") openSearchSession(s);
  else if (m === "versions") openVersionsSession(s);
  else openSession(s);
}

function openComponentSession(s){
  applyNamingSituation(null);
  const showing = isOpen && mode === "component";
  isOpen = true; setCommitting(false);
  mode = "component";
  cTitleEl.textContent = dialogTitle(mode, s);
  cErr.textContent = "";
  cNameRow.style.display = "";

  const values = s.bounds || {}, sym = s.axisSymmetry || {};
  cName.value = "";
  fillBounds(values, sym, false);

  cSheetRow.style.display = "";
  cSheet.checked = false;         // unticked every time the panel opens fresh
  cSheetRule.innerHTML = "";
  for (const rule of (s.sheetRules || [])){
    const opt = document.createElement("option");
    opt.value = rule.name; opt.textContent = rule.name;
    cSheetRule.appendChild(opt);
  }
  if (s.sheetRule) cSheetRule.value = s.sheetRule;
  cSheetRuleRow.style.display = "none";   // stays hidden until the box is ticked

  showPanel("component");
  // Filled by assignment, so nothing has told the chip layers what is in them.
  refreshChips();
  fitWidth();
  sizeWindow(showing ? "keep" : null);
  if (!showing){ cName.focus(); cName.select(); }
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* A BLANK face is a face left out — no parameter, no points — so a blank arriving
   stays blank (the placeholder shows which face it is) rather than becoming "0".
   Bottom arrives blank by default; see Bounds.inherited_values. Symmetry is locked
   while editing: changing it would move points between the midline and the low face,
   which the edit does not do. */
function fillBounds(values, sym, lockSymmetry){
  for (const spec of AXIS_ROWS){
    cSyms[spec.axis].checked = !!sym[spec.axis];
    cSyms[spec.axis].disabled = !!lockSymmetry;
    cFields[spec.high].value = values[spec.high] || "";
    cFields[spec.low].value  = values[spec.low]  || "";
    cStash[secondaryOf(spec)] = "";
    applySymmetry(spec, !!lockSymmetry);
  }
}

/* A symmetric axis is given by its PRIMARY face alone (Right, Front, Top), so its
   secondary field is greyed out and takes no typing. What it held is set aside rather
   than lost — untick the axis and it comes back — and it goes out blank, which makes it
   a face left out.

   `keep` is 'editbounds': symmetry is locked there, and a component that already has
   both faces keeps its secondary as it is — greyed, but sent back unchanged, since a
   blank would REMOVE that face and rebase whatever was built on it. */
const cStash = {};           // secondary direction -> the value ticking the axis set aside
function applySymmetry(spec, keep){
  const sec = secondaryOf(spec), f = cFields[sec];
  const on = cSyms[spec.axis].checked;
  if (on && !keep && f.value){ cStash[sec] = f.value; f.value = ""; }
  if (!on && !f.value && cStash[sec]){ f.value = cStash[sec]; cStash[sec] = ""; }
  f.disabled = on;
  f.closest(".crow").classList.toggle("off", on);
}

/* The same rows, aimed at a component that already exists instead of one about to be
   made. No name to type — the component keeps its own — so that row is hidden and a
   commit posts "addboundstocomponent"; the target is the panel's session on the add-in
   side, not anything typed here. A component that already HAS bounds opens as
   'editbounds': the rows hold its current expressions, and blanking one removes that
   face (the add-in rebases whatever was built on it onto the other face). */
function openAddBoundsSession(s){
  const m = s.mode === "editbounds" ? "editbounds" : "addbounds";
  const showing = isOpen && mode === m;
  isOpen = true; setCommitting(false);
  mode = m;
  cTitleEl.textContent = dialogTitle(mode, s);
  cErr.textContent = "";
  cNameRow.style.display = "none";

  fillBounds(s.bounds || {}, s.axisSymmetry || {}, m === "editbounds");

  // Not offered here — converting an existing component to sheet metal is a
  // different act than giving it bounds, and not this panel's job.
  cSheetRow.style.display = "none";
  cSheet.checked = false;
  cSheetRuleRow.style.display = "none";

  showPanel("component");
  refreshChips();
  fitWidth();
  sizeWindow(showing ? "keep" : null);
  if (!showing){
    // focus() alone is enough — cTextRow's own focus listener is what selects the
    // field (see selectPrefilled); doing it again here would be a second, identical
    // wiring, the same as the extrude panel's distance field.
    cFields[AXIS_ROWS[0].high].focus();
  }
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* The header's title, one line, in the drag-handle header every panel already has.
   Set on every open rather than left to whichever session opened last — a `mode` that
   doesn't say (search: it's a palette, not a dialog) gets none. `s` is only read for
   the feature panel, whose title (Array/Combine/Scale…) comes from the add-in rather
   than being one of a fixed few strings. */
function dialogTitle(mode, s){
  switch (mode){
    case "dim":        return s && s.isDriven ? "Driven Dimension" : "Dimension";
    case "param":      return "Parameter";
    case "editparam":  return s && s.isDriven ? "Driven Parameter" : "Edit Parameter";
    case "sketch":     return "Sketch Plane";
    case "component":  return "Component";
    case "addbounds":
    case "editbounds": return "Bounds";
    case "extrude":    return "Extrude";
    case "mirror":     return "Mirror";
    case "feature":    return (s && s.feature && s.feature.title) || "";
    case "make":       return "Make Parameter";
    default:           return "";
  }
}

/* Where the window goes. `keep` is the important one: the extrude session is RE-ARMED
   every time you pick something in the viewport — a pick is the one input the panel does
   not own, so the add-in sends the whole session again — and placing the window on each
   of those threw away wherever you had dragged it to. A session that is already on
   screen is never re-placed; only a new one is.

   The height needs no window change either way: the window is a fixed rectangle with the
   panel drawn at its top and transparent space below (that space is where the suggestion
   list drops), so a panel that grows grows into room that is already there. */
function sizeWindow(place){
  const box = isBoundsPanelMode() ? cPanel
            : mode === "extrude" ? xPanel
            : mode === "mirror"  ? rPanel
            : mode === "feature" ? oPanel
            : mode === "search"  ? sPanel
            : mode === "versions" ? vPanel
            : panel;
  const top = isBoundsPanelMode() || mode === "extrude" || mode === "mirror"
              || mode === "feature";
  const defaultPlace = mode === "search" ? "corner" : mode === "versions" ? "center"
                     : top ? "top" : "cursor";
  host("session", { open: true, place: place || defaultPlace,
                    height: Math.ceil(box.getBoundingClientRect().height) });
}

function commitComponent(){
  if (committing) return;
  const editing = mode === "editbounds";
  const existing = mode === "addbounds" || editing;
  let name = "";
  if (!existing){
    name = (cName.value || "").trim();
    if (!name){ cErr.textContent = "Give it a name."; cName.focus(); return; }
  }

  const symmetry = {}, values = {};
  for (const spec of AXIS_ROWS){
    symmetry[spec.axis] = cSyms[spec.axis].checked;
    values[spec.high] = (cFields[spec.high].value || "").trim();
    values[spec.low]  = (cFields[spec.low].value  || "").trim();
    // A face may be left out; an axis may not — something has to say where the
    // component is along it.
    if (!values[spec.high] && !values[spec.low]){
      cErr.textContent = "Each axis needs at least one face.";
      cFields[spec.primary].focus();
      return;
    }
  }

  const sheetMetal = !existing && cSheet.checked;
  const sheetRule = sheetMetal ? cSheetRule.value : "";

  setCommitting(true);
  isOpen = false;
  hideWindow();
  showPanel("value");
  post(editing ? { action: "editbounds", values: values }
    : existing
    ? { action: "addboundstocomponent", values: values, symmetry: symmetry }
    : { action: "createcomponent", name: name, values: values, symmetry: symmetry,
        sheetMetal: sheetMetal, sheetRule: sheetRule });
}

buildComponentPanel();

/* ── The Extrude panel ───────────────────────────────────────────────────────
   Different from the other two in one way that shapes all of it: a Fusion command is
   RUNNING while this is open, drawing the real preview in the viewport. So nothing here
   is stored up for a commit — every edit posts a "setextrude" and the command redraws,
   and the operation posts back the other way because Fusion's rules decide it from what
   the extrusion runs into, not from anything on this panel.

   Extent type is deliberately absent. Distance is the only extent, which is what makes
   this panel four rows instead of eleven; the three that are here but rarely wanted —
   type, start and taper — are behind Advanced.                                       */

// What the distance field means: a length, or a datum line the extrude runs TO
// ("Top 5", "Top - BootCeiling_Inset_Top") — the add-in works the length out
// (Extrude.py _to_distance), parametrically, from the profile to that plane.
const X_REACH = [["distance", "Distance"], ["to", "To plane"]];
const X_DIRECTIONS = [["one", "One side"], ["symmetric", "Symmetric"],
                      ["two", "Two sides"]];
// No "Auto" button. The command still STARTS on auto — Fusion-style, join, cut or new
// body from what the extrusion meets (Extrude.py _auto_operation; a component with no
// bodies yet is always a new body) — but that shows as the operation it landed on,
// selected like any other, and the first click replaces it.
const X_OPERATIONS = [["cut", "Cut"], ["intersect", "Intersect"], ["join", "Join"],
                      ["newbody", "New body"]];
// Every operation row wears its operation's colour on the ONE cell that is what will
// happen — theme.css --op-*. The Extrude, Mirror and feature panels all use this.
const OP_COLOURED = new Set(["cut", "intersect", "join", "newbody"]);
function opMark(cells, effective){
  for (const { cell, value } of cells){
    const on = value === effective && OP_COLOURED.has(value);
    cell.classList.toggle("opsel", on);
    if (on) cell.dataset.op = value; else delete cell.dataset.op;
  }
}
const X_TYPES = [[true, "Solid"], [false, "Surface"]];
const X_STARTS = [["profile", "Profile"], ["offset", "Offset"]];
// Off: nothing computes while you work — an edit takes a clicked choice straight into
// the feature, typed values on Enter. The add-in opens an edit of a heavy feature with
// it already off (Extrude.py _too_heavy).
const X_PREVIEW = [[true, "On"], [false, "Off"]];
// What a click on a body takes: that body, or its whole Browser group — or, with no
// click at all, every body of the component. A group or the component the feature then
// keeps following (Extrude.py "Following a group"). Mirror's scope row is the same.
const X_BODY_SCOPES = [["bodies", "Bodies"], ["group", "Group"], ["component", "Component"]];

// What the panel currently says. Sent whole on every change: it is nine short fields,
// and a diff would be more code than the thing it saves.
let xSpec = {};
const xSegs = {};      // key -> {row, set(value)}
const xFields = {};    // key -> input

// The chords Active While Typing owns right now (fusion_ui/hotkeys.py::typing_keys,
// canonical ctrl/alt/shift/cmd order — see the keydown hook below). Fusion's own
// keymap never sees a key while this panel has focus (the real Extrude dialog it
// stands in for is hidden), so this panel hands a matching chord over itself. Arrives
// inside the extrude spec (`keys`) from both _open_panel and _push_state, so it is
// current on every poll, same as everything else the running command tells the panel.
let xKeys = new Set();
// Those of them that are the shared cycles: {chord: 'operation' | 'direction'} — the
// panel steps the row itself (xCycle) rather than hand the key over.
let xCycleKeys = {};

// event.code (the PHYSICAL key) -> hotkeys.py's spelling — mirrors remap/index.js's
// own CODE table, which the Shortcuts editor captures chords with, so a key typed here
// and the same key bound there agree on what they are called.
const X_CODE = { Space:"space", Enter:"return", Tab:"tab", Backspace:"delete",
  Escape:"escape", ArrowLeft:"left", ArrowRight:"right", ArrowUp:"up", ArrowDown:"down",
  Minus:"-", Equal:"=", BracketLeft:"[", BracketRight:"]", Backslash:"\\",
  Semicolon:";", Quote:"'", Comma:",", Period:".", Slash:"/", Backquote:"`" };

// The chord this keydown would be, in fusion.py::join order (ctrl, alt, shift, cmd,
// then the key) — the same order hotkeys.py::typing_keys() spells xKeys in. null for a
// bare modifier or a physical key with no chord spelling (mirrors chordFrom there).
function xChordFrom(e){
  let k = null;
  if (X_CODE[e.code]) k = X_CODE[e.code];
  else if (/^Key[A-Z]$/.test(e.code)) k = e.code.slice(3).toLowerCase();
  else if (/^Digit[0-9]$/.test(e.code)) k = e.code.slice(5);
  if (!k) return null;
  const mods = [];
  if (e.ctrlKey) mods.push("ctrl");
  if (e.altKey) mods.push("alt");
  if (e.shiftKey) mods.push("shift");
  if (e.metaKey) mods.push("cmd");
  return mods.concat([k]).join("+");
}

// A chord Active While Typing owns, captured by this panel because the real Extrude
// dialog it stands in for is hidden and Fusion's keymap never sees the key at all —
// see fusion_ui/hotkeys.py::press. Fire-and-forget, same as post(): a dropped reply
// here is a missed cycle, not a lost edit.
function xPressKey(chord){
  fetch(SHORTCUTS_API + "/press", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ seq: chord })
  }).catch(() => {});
}

function xSegRow(key, label, options, into){
  const row = document.createElement("div");
  row.className = "xseg";
  const l = document.createElement("span");
  l.className = "xlabel";
  l.textContent = label;
  row.appendChild(l);
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(opts);
  (into || xRows).appendChild(row);

  const cells = options.map(([value, text]) => {
    const c = document.createElement("span");
    c.className = "xopt";
    c.textContent = text;
    c.addEventListener("mousedown", (e) => {
      e.preventDefault();          // never take focus off the field being typed in
      xClaim(key, value);
      xSpec[key] = value;
      xRender();
      xPush();
    });
    opts.appendChild(c);
    return { cell: c, value: value };
  });

  xSegs[key] = {
    row: row,
    render: () => {
      for (const { cell, value } of cells) cell.classList.toggle("on", xSpec[key] === value);
    },
    cells: cells
  };
  return row;
}

/* The selection row: "Selected  3  Clear". Not a choice and not a field, so it is
   built by hand rather than through xSegRow — but it wears the same row so the panel
   reads as one list of things down its left edge. */
function xSelectionRow(){
  const row = document.createElement("div");
  row.className = "xseg xsel";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = "Selected";
  const count = document.createElement("span");
  count.className = "xcount";
  const clear = document.createElement("span");
  clear.className = "xopt xclear";
  clear.textContent = "Clear";
  clear.addEventListener("mousedown", (e) => {
    e.preventDefault();                    // never take focus off the distance field
    // Optimistic, like every other row in these windows: the count drops NOW and the
    // command's own answer follows on the next push. Waiting for the round trip made a
    // click on Clear look like nothing had happened.
    xClaim("selectionCount", 0);
    xClaim("hasSelection", false);
    xSpec.selectionCount = 0;
    xSpec.hasSelection = false;
    xRender();
    post({ action: "clearextrude", rev: panelRev });
  });
  row.appendChild(label); row.appendChild(count); row.appendChild(clear);
  xRows.appendChild(row);

  xSegs.selection = {
    row: row,
    cells: [],
    render: () => {
      const n = xSpec.selectionCount || 0;
      // "No" rather than "None" or "0": the row is read as a sentence with its label —
      // "Selected: no" — and a zero reads as a measurement that happens to be zero.
      count.textContent = n > 0 ? String(n) : "No";
      row.classList.toggle("empty", n === 0);
    }
  };
}

/* The bodies row: which of the component's own bodies a join, cut or intersect works
   on. One chip per body, lit while it takes part; a click leaves it out or lets it back
   in, and so does clicking the body itself in the viewport or the Browser window (the
   add-in pushes the new `exclude` back). The list is the add-in's (`bodyNames`, from the
   last preview) and `exclude` is what the click changes, so the chips are rebuilt on
   render rather than once like xSegRow's. Until something is clicked the add-in keeps
   the list on "the bodies it runs into"; the first click makes it yours. Clear, at the
   end, leaves every body out — click the ones you want back in.

   Only shown for a join, cut or intersect — a new body touches none. */
function xSetExclude(list){
  xSpec.exclude = list;
  xClaim("exclude", xSpec.exclude);
  xRender();
  xPush(true);
}
// "Bodies are: Group": the chips are the component's groups (`groupChips`, [id, name]),
// lit while in `groups`; a click takes a whole group in or out.
function xSetGroups(list){
  xSpec.groups = list;
  xClaim("groups", xSpec.groups);
  xRender();
  xPush(false, true);
}
// Out of Advanced and up from the moment there is a pick — the scope can be set before
// a distance is typed, when nothing has been run into yet. The chips come from the
// pick's component, not the last build (Extrude.py::_panel_group_chips).
function xGroupsRow(into){
  const row = document.createElement("div");
  row.className = "xseg xbodies";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = "Groups";
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(label); row.appendChild(opts);
  (into || xRows).appendChild(row);

  let drawn = "";
  xSegs.groups = {
    row: row,
    cells: [],
    render: () => {
      if (xSegs.bodiesScope)
        xSegs.bodiesScope.row.style.display = xSpec.hasSelection ? "" : "none";
      row.style.display = xSpec.hasSelection && xSpec.bodiesScope === "group" ? "" : "none";
      const chips = xSpec.groupChips || [], chosen = new Set(xSpec.groups || []);
      const key = JSON.stringify([chips, [...chosen]]);
      if (key === drawn) return;
      drawn = key;
      opts.textContent = "";
      if (!chips.length){
        const none = document.createElement("span");
        none.className = "xopt xclear";
        none.textContent = "None in this component";
        opts.appendChild(none);
        return;
      }
      for (const [id, name] of chips){
        const c = document.createElement("span");
        c.className = "xopt";
        c.textContent = name;
        c.classList.toggle("on", chosen.has(id));
        c.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const next = new Set(xSpec.groups || []);
          if (next.has(id)) next.delete(id); else next.add(id);
          xSetGroups(chips.map(ch => ch[0]).filter(g => next.has(g)));
        });
        opts.appendChild(c);
      }
      const clear = document.createElement("span");
      clear.className = "xopt xclear";
      clear.textContent = "Clear";
      clear.addEventListener("mousedown", (e) => { e.preventDefault(); xSetGroups([]); });
      opts.appendChild(clear);
    }
  };
}
function xBodiesRow(into){
  const row = document.createElement("div");
  row.className = "xseg xbodies";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = "Bodies";
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(label); row.appendChild(opts);
  (into || xRows).appendChild(row);

  let drawn = "";
  xSegs.bodies = {
    row: row,
    cells: [],
    render: () => {
      const names = xSpec.bodyNames || [];
      const op = xSpec.operation === "auto" ? xSpec.operationResolved : xSpec.operation;
      row.style.display = (xSpec.bodiesScope || "bodies") === "bodies" &&
        (op === "join" || op === "cut" || op === "intersect") && names.length ? "" : "none";
      const out = new Set(xSpec.exclude || []);
      const key = JSON.stringify([names, [...out]]);
      if (key === drawn) return;
      drawn = key;
      opts.textContent = "";
      for (const name of names){
        const c = document.createElement("span");
        c.className = "xopt";
        c.textContent = name;
        c.classList.toggle("on", !out.has(name));
        c.addEventListener("mousedown", (e) => {
          e.preventDefault();          // never take focus off the field being typed in
          const next = new Set(xSpec.exclude || []);
          if (next.has(name)) next.delete(name); else next.add(name);
          xSetExclude(names.filter((n) => next.has(n)));
        });
        opts.appendChild(c);
      }
      const clear = document.createElement("span");
      clear.className = "xopt xclear";
      clear.textContent = "Clear";
      clear.addEventListener("mousedown", (e) => {
        e.preventDefault();
        xSetExclude(names.slice());
      });
      opts.appendChild(clear);
    }
  };
}

function xTextRow(key, placeholder, into){
  const { input } = cTextRow(placeholder, true, into || xRows);
  input.addEventListener("input", () => { xSpec[key] = input.value; xFlexRender(); xPush(); });
  xFields[key] = input;
  return input;
}

function buildExtrudePanel(){
  // The far side's distance sits BESIDE the first, not under it: only a two-sided
  // extrude has one, and on its own row it made the panel jump in height every time
  // the direction changed. Two .crow halves of one .crowpair.
  const pair = document.createElement("div");
  pair.className = "crowpair";
  xRows.appendChild(pair);
  xTextRow("distance", "Distance", pair);
  xTextRow("distance2", "Other side", pair);
  xSegRow("reach", "Reach", X_REACH);
  // Operation above direction, on every panel that has both.
  xSegRow("operation", "Operation", X_OPERATIONS);
  xSegRow("direction", "Direction", X_DIRECTIONS);
  // Under the operation, because that is the question it answers a part of — what this
  // is being done TO. At the top it was the first thing read on a panel whose first
  // business is the distance, and it is the one row you never type into.
  xSelectionRow();
  // Which bodies a join/cut/intersect works on, by Browser group: out here so it can be
  // set the moment there is a pick, before a distance or any body has been run into.
  xSegRow("bodiesScope", "Bodies are", X_BODY_SCOPES);
  xGroupsRow();
  xSegRow("preview", "Preview", X_PREVIEW);

  // Which bodies a join/cut/intersect works on: behind Advanced — the automatic list
  // (what the extrusion runs into) is right nearly every time, and the row can be a
  // hundred chips long. Clicking a body in the viewport or Browser still toggles it
  // with Advanced shut.
  xBodiesRow(xAdv);
  xSegRow("isSolid", "Type", X_TYPES, xAdv);
  xSegRow("start", "Start", X_STARTS, xAdv);
  xTextRow("startOffset", "Start offset", xAdv);
  xTextRow("taper", "Taper angle", xAdv);

  xAdvHead.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const shut = xAdv.classList.toggle("hide");
    xAdvHead.querySelector(".twirl").classList.toggle("collapsed", shut);
    // NO sizeWindow(). The window is a fixed 400x400 with the panel drawn at its top and
    // transparent space below (that space is where the suggestion list drops), so an
    // expanding panel already grows DOWNWARD into room that is there. Re-sizing would
    // re-run frameCentered, which keeps the panel's CENTRE on the screen's middle — so
    // opening Advanced shifted the whole panel up under the pointer, and every row you
    // had just been looking at moved. It also threw away a dragged position.
  });
}
buildExtrudePanel();

/* Everything the panel shows about itself, from xSpec. Called after any change from
   either end — a click here, or an operation pushed back from Fusion. */
function xRender(){
  for (const key in xSegs) xSegs[key].render();
  xFlexRender();

  // "To plane": the field names a plane, and there is one side only — a length along
  // the profile's normal, signed by where the plane is.
  const toPlane = xSpec.reach === "to";
  xFields.distance.placeholder = toPlane ? "Plane — Top, or Top 5" : "Distance";
  if (xSegs.direction) xSegs.direction.row.style.display = toPlane ? "none" : "";
  // The far-side distance is meaningless unless there are two sides.
  const two = xSpec.direction === "two" && !toPlane;
  xFields.distance2.parentNode.style.display = two ? "" : "none";
  // Likewise the start offset.
  xFields.startOffset.parentNode.style.display =
    xSpec.start === "offset" ? "" : "none";

  // Still on auto (nothing chosen yet): the operation the rule landed on is shown
  // SELECTED, exactly as if it had been clicked — there is no Auto button to light.
  const op = xSpec.operation === "auto" ? (xSpec.operationResolved || "") : xSpec.operation;
  for (const { cell, value } of (xSegs.operation.cells || [])){
    cell.classList.toggle("on", value === op);
  }
  opMark(xSegs.operation.cells || [], op);
  // Two sides puts a second field on the distance row: wide enough for both.
  if (isOpen && mode === "extrude") fitWidth();

  // The header carries no text. Which component the feature lands in is decided by what
  // you picked, not by anything on this panel, so saying it back was a line of prose that
  // could never be acted on. The strip stays because it is what you drag the panel by —
  // same as the value box's header.
}

/* ── What we have asked for and Fusion has not confirmed ─────────────────────
   Every choice on this panel is optimistic: the row moves on the press, because waiting
   for the viewport to answer makes a click look like it missed. But the answer is a
   round trip through the command, and the pushes that arrive DURING it still carry the
   word you have just changed away from — so the row flicked back to the old option for a
   moment and then forward again.

   A claim holds our answer against those pushes until Fusion says the same thing (it
   landed, and the claim is done) or Fusion has HEARD it and says otherwise (its rules
   won, and its word beats ours). Heard is by `rev`: every post from these panels carries
   `panelRev`, a claim takes the next one, and the add-in echoes the newest rev it has
   acted on in every push. A timer used to stand in for "heard" — and a preview longer
   than the timer (a big model) let the push that left at the END of the previous
   preview, still carrying the old word, flick the row back until the next one landed.
   The timer is left only for a push with no rev at all (an add-in not reloaded yet),
   and as a backstop for a rev that never comes back.

   Clock-seeded, like the window's seqs: the window outlives an add-in reload, and a rev
   that restarted at 0 would be "older" than anything the add-in had already heard. */
let panelRev = Date.now();
const CLAIM_NOREV_MS = 1500;
const CLAIM_MAX_MS = 30000;

function makeClaims(){
  const claims = {};
  const api = {
    claim(key, value){ claims[key] = { value: value, rev: ++panelRev, at: Date.now() }; },
    /* Is what arrived for `key` news, or an echo of a state this panel has already left?
       `rev` is the push's own (undefined from an add-in that does not send one). */
    stale(key, value, rev){
      const c = claims[key];
      if (!c) return false;
      // By content, so a list (the bodies left out) lands the same way a word does.
      if (JSON.stringify(value) === JSON.stringify(c.value)){        // it landed
        delete claims[key]; return false;
      }
      const age = Date.now() - c.at;
      const heard = typeof rev === "number" ? rev >= c.rev : age >= CLAIM_NOREV_MS;
      if (!heard && age < CLAIM_MAX_MS) return true;                 // still in flight
      delete claims[key];                                            // overruled
      return false;
    },
    /* A re-arm carries the whole spec: our claims still in flight win over it. */
    overlay(spec){
      for (const key in claims){
        if (api.stale(key, spec[key], spec.rev)) spec[key] = claims[key].value;
      }
    },
    clear(){ for (const key in claims) delete claims[key]; }
  };
  return api;
}

const xClaims = makeClaims();
function xClaim(key, value){ xClaims.claim(key, value); }

/* Step a row on one, as the shared Cycle Operation / Cycle Direction key would — here,
   the instant the key goes down, instead of handing the key to Fusion and waiting for
   the add-in's tick (queued behind whatever preview is computing) to push the row back.
   Posted as an ordinary choice, so the add-in takes it exactly as it takes a click.
   `cycleKeys` (hotkeys.py::cycle_keys) says which chords are the cycles. The order is
   the add-ins' own (Extrude.py _step, Mirror.py _cycle_plane, Feature Panel.py
   cycle_row): the row's, from what it is showing. */
function stepRing(ring, now){
  const at = ring.indexOf(now);
  return ring[at < 0 ? 0 : (at + 1) % ring.length];
}

function xCycle(kind){
  const key = kind === "operation" || kind === "direction" ? kind : null;
  if (!key) return false;
  const ring = (key === "operation" ? X_OPERATIONS : X_DIRECTIONS).map(o => o[0]);
  let now = xSpec[key];
  // Still on auto: step from the operation it is DOING (Extrude.py _cycle_operation).
  if (key === "operation" && now === "auto") now = xSpec.operationResolved || "newbody";
  const nxt = stepRing(ring, now);
  xClaim(key, nxt);
  xSpec[key] = nxt;
  xRender();
  xPush();
  return true;
}

/* Send the whole spec. The command redraws on receipt, so this is the thing that makes
   the viewport follow what is typed. */
function xPush(withBodies, withGroups){
  const out = { action: "setextrude" };
  for (const key of ["distance", "distance2", "reach", "direction", "operation",
                     "isSolid", "start", "startOffset", "taper", "preview", "bodiesScope"]){
    if (key in xSpec) out[key] = xSpec[key];
  }
  // Same rule as the bodies list: only when a group chip (or its Clear) made the push.
  if (withGroups) out.groups = xSpec.groups || [];
  // The bodies list only rides the push a chip (or Clear) made. The add-in owns it
  // otherwise — it follows the distance while automatic, an edit seeds it from the
  // feature, a body clicked in the viewport or Browser changes it — and a keystroke
  // carrying the panel's copy would race all of those and put the old list back.
  if (withBodies) out.exclude = xSpec.exclude || [];
  out.flex = xFlexState();
  out.rev = panelRev;
  post(out);
}

function openExtrudeSession(s){
  applyNamingSituation(s.extrude && s.extrude.situation);
  // Re-armed rather than opened: a pick in the viewport sends the whole session again.
  // Keep where it is, and keep the caret where it was — the distance may be half typed.
  const showing = isOpen && mode === "extrude";
  isOpen = true; setCommitting(false);
  mode = "extrude";
  xTitleEl.textContent = dialogTitle(mode, s);
  xErr.textContent = "";
  // The expression fields survive a re-arm for the same reason their text does: they may
  // be half typed at this instant, and what is in the panel is the truth for them.
  const typed = showing ? { distance: xSpec.distance, distance2: xSpec.distance2,
                            startOffset: xSpec.startOffset, taper: xSpec.taper } : null;
  xSpec = Object.assign({}, s.extrude || {});
  xFieldsSeq = (s.extrude && s.extrude.fieldsSeq) || xFieldsSeq;
  if (typed) Object.assign(xSpec, typed);
  xKeys = new Set((s.extrude && s.extrude.keys) || []);
  xCycleKeys = (s.extrude && s.extrude.cycleKeys) || {};
  // And a choice made a moment ago is still ours: a re-arm that arrived in between
  // carries the word we have already moved on from, exactly as a push does. A new
  // command starts with none — its add-in has heard nothing yet.
  if (showing) xClaims.overlay(xSpec); else xClaims.clear();
  unit = s.unit || "";
  nameRequired = false; origName = ""; isDriven = false;

  // Not while it is already up: the distance may be half typed, and the panel's own
  // text is the truth for the expression fields (the same rule applyExtrudeUpdate obeys).
  if (!showing) for (const key in xFields) xFields[key].value = xSpec[key] || "";
  xRender();
  // Filled by assignment, so nothing has told the chip layers what is in them.
  refreshChips();
  showPanel("extrude");
  // The distance arrives already filled in, and a single-line field cuts off rather than
  // wrapping — so the panel is fitted to it before it is shown, not on the first
  // keystroke after.
  fitWidth();
  sizeWindow(showing ? "keep" : null);
  if (!showing){
    // focus() fires the focus listener cTextRow installed, which is what attaches the
    // autocomplete AND selects the field (see selectPrefilled) — doing either again
    // here would be a second, identical wiring.
    xFields.distance.focus();
  }
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* A push back FROM Fusion: the operation its rules chose, and whether the direction rule
   found a bound. Never touches the fields — you may be typing in one — so only the parts
   of xSpec that Fusion owns are taken. */
/* Flexible, on the Extrude panel. Only when the distance is exactly one user parameter
   and the reach is a length: the range lives on that parameter (Section Move reads it
   there). A reference ("To plane") follows its reference and is never flexible. */
const xFlexEl = $("xflex"), xFlexOnEl = $("xflexon"),
      xFlexMinEl = $("xflexmin"), xFlexMaxEl = $("xflexmax");
let xFlexName = "", xFlexOn = true;
function xFlexParam(){
  if (xSpec.reach === "to") return "";
  const t = (xSpec.distance || "").trim();
  return params.some(p => p.name === t) ? t : "";
}
function xFlexRender(){
  const name = xFlexParam();
  if (name !== xFlexName){
    xFlexName = name;
    const f = name && (xSpec.flexes || {})[name];
    xFlexOn = !f || f.flexible !== false;
    xFlexMinEl.value = (f && f.min) || "";
    xFlexMaxEl.value = (f && f.max) || "";
  }
  xFlexEl.classList.toggle("hide", !name);
  xFlexEl.classList.toggle("off", !xFlexOn);
  xFlexOnEl.classList.toggle("on", xFlexOn);
}
function xFlexState(){
  const name = xFlexParam();
  if (!name) return null;
  return { name: name, flexible: xFlexOn, min: xFlexMinEl.value.trim(), max: xFlexMaxEl.value.trim() };
}
xFlexOnEl.addEventListener("mousedown", e => { e.preventDefault(); xFlexOn = !xFlexOn; xFlexRender(); xPush(); });
for (const el of [xFlexMinEl, xFlexMaxEl]) el.addEventListener("input", () => xPush());

let xFieldsSeq = 0;
function applyExtrudeUpdate(next){
  if (!next) return;
  // Fields the ADD-IN set (a reference clicked in the viewport: Reach + the datum line).
  // Each push carries its own seq and is applied once; the panel's text is otherwise
  // never overwritten from outside.
  if (next.fields && (next.fieldsSeq || 0) !== xFieldsSeq){
    xFieldsSeq = next.fieldsSeq || 0;
    for (const k in next.fields){
      xSpec[k] = next.fields[k];
      if (xFields[k]) xFields[k].value = next.fields[k];
    }
    refreshChips(); autosize && autosize(); fitWidth(); xRender();
  }
  if (next.keys) xKeys = new Set(next.keys);
  if (next.cycleKeys) xCycleKeys = next.cycleKeys;
  let changed = false;
  // operation and direction are in here because a KEY can change them from outside this
  // panel — see the Extrude add-in's cycle actions. They are choices, not fields, so
  // taking them can never overwrite something half-typed.
  for (const key of ["operationResolved", "component", "inward", "flipped",
                     "operation", "direction", "preview", "hasSelection", "selectionCount",
                     "exclude", "bodyNames", "noTargets",
                     "bodiesScope", "groups", "groupChips"]){
    if (!(key in next)) continue;
    if (xClaims.stale(key, next[key], next.rev)) continue;   // ours, still in flight
    // By content: exclude and bodyNames are lists, a fresh one on every push.
    if (JSON.stringify(xSpec[key]) !== JSON.stringify(next[key])){
      xSpec[key] = next[key]; changed = true;
    }
  }
  if (changed) xRender();
}

function commitExtrude(){
  if (committing) return;
  if (!xSpec.hasSelection){
    xErr.textContent = "Pick something to extrude first.";
    return;
  }
  if (!(xSpec.distance || "").trim()){
    xErr.textContent = "Give it a distance.";
    xFields.distance.focus();
    return;
  }
  // The window stays up until Fusion says the feature built — same rule as the value
  // box's commit. A commit CAN be refused (an expression that will not build), and a
  // panel that had already vanished would take the reason with it. _Destroy posts the
  // cancel that closes the session on the way out, so a success still hides it.
  setCommitting(true);
  xErr.textContent = "";
  post({ action: "commitextrude" });
}

function cancelExtrude(){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  post({ action: "cancelextrude" });
}

/* ── The Mirror panel ────────────────────────────────────────────────────────
   The Extrude panel's arrangement for Mirror: a live command is running behind it, every
   choice posts a "setmirror" and the command redraws the real feature in the viewport.

   What is different is that there is nothing to TYPE — every row is a choice — so the
   panel holds the keyboard with no field in it. Enter commits and Escape cancels, and
   every other chord that means something in Fusion right now is handed back to Fusion
   (rKeys, from hotkeys.py::panel_keys). That is what keeps the plane-cycling key — or
   any other — working while this panel has the keyboard: without it, a key bound in
   Control Room would reach nothing at all.

   The plane is one of the component's own origin planes, X by default — the component
   being the one the picked bodies are IN, not the active one. X means across X: the YZ
   plane. "Pick" waits for a face or construction plane of your own; a construction
   plane clicked at any time is taken as the plane without asking. */

const R_PLANES = [["x", "X"], ["y", "Y"], ["z", "Z"], ["picked", "Pick"]];
// Join first: it is the default. A copy clear of the original cannot join, and the
// add-in builds it as a new body instead — lit in the accent (operationResolved).
const R_OPERATIONS = [["join", "Join"], ["newbody", "New body"]];
const R_COMPUTE = [["optimized", "Optimized"], ["identical", "Identical"],
                   ["adjust", "Adjust"]];
// What a selection of each kind is called on the Selected row.
const R_KINDS = { bodies: ["body", "bodies"], features: ["feature", "features"] };

let rSpec = {};
const rSegs = {};      // key -> {row, cells, render()}
let rKeys = new Set();
let rCycleKeys = {};   // xCycleKeys, for this panel

// The Extrude panel's claims, for the same reason — see "What we have asked for".
const rClaims = makeClaims();
function rClaim(key, value){ rClaims.claim(key, value); }

/* xCycle for this panel (Mirror.py _cycle_plane / _cycle_operation): the plane walks
   X, Y, Z and then the picked plane, if there is one; the operation is join ↔ new body,
   bodies only — a mirror of features has none, and the key does nothing. */
function rCycle(kind){
  if (kind === "direction"){
    const ring = ["x", "y", "z"].concat(rSpec.pickedLabel ? ["picked"] : []);
    rChoose("plane", stepRing(ring, rSpec.plane));
    return true;
  }
  if (kind === "operation"){
    if (rSpec.kind === "bodies") rChoose("operation", rSpec.operation === "join" ? "newbody" : "join");
    return true;
  }
  return false;
}

// xPressKey with `any`: this panel has no field, so the chord runs whatever wins it in
// Fusion right now, not only an Active While Typing row (hotkeys.py::press).
function rPressKey(chord){
  fetch(SHORTCUTS_API + "/press", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ seq: chord, any: true })
  }).catch(() => {});
}

function rSegRow(key, label, options, into){
  const row = document.createElement("div");
  row.className = "xseg";
  const l = document.createElement("span");
  l.className = "xlabel";
  l.textContent = label;
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(l); row.appendChild(opts);
  (into || rRows).appendChild(row);
  const cells = options.map(([value, text]) => {
    const c = document.createElement("span");
    c.className = "xopt";
    c.textContent = text;
    c.addEventListener("mousedown", (e) => {
      e.preventDefault();
      rChoose(key, value);
    });
    opts.appendChild(c);
    return { cell: c, value: value, text: text };
  });
  rSegs[key] = {
    row: row, cells: cells,
    render: () => {
      for (const { cell, value } of cells) cell.classList.toggle("on", rSpec[key] === value);
    }
  };
  return row;
}

/* A click on a choice. Pick is the one that is not simply "set it": pressed while it
   is already the plane, it lets go of the picked plane and waits for another. */
function rChoose(key, value){
  const out = { action: "setmirror" };
  if (key === "plane" && value === "picked" && rSpec.plane === "picked" && rSpec.pickedLabel){
    out.repick = true;
    rSpec.pickedLabel = "";
  }
  rClaim(key, value);
  rSpec[key] = value;
  out[key] = value;
  out.rev = panelRev;
  rRender();
  post(out);
}

/* "Selected  2 bodies  Clear" — the Extrude panel's row, saying what kind of thing
   too: a click on a face mirrors its BODY, and this is where that is said. */
function rSelectionRow(){
  const row = document.createElement("div");
  row.className = "xseg xsel";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = "Selected";
  const count = document.createElement("span");
  count.className = "xcount";
  const clear = document.createElement("span");
  clear.className = "xopt xclear";
  clear.textContent = "Clear";
  clear.addEventListener("mousedown", (e) => {
    e.preventDefault();
    rClaim("selectionCount", 0);
    rSpec.selectionCount = 0;
    rRender();
    post({ action: "clearmirror", rev: panelRev });
  });
  row.appendChild(label); row.appendChild(count); row.appendChild(clear);
  rRows.appendChild(row);
  rSegs.selection = {
    row: row, cells: [],
    render: () => {
      const n = rSpec.selectionCount || 0;
      const words = R_KINDS[rSpec.kind] || ["", ""];
      count.textContent = n > 0 ? (n + " " + words[n === 1 ? 0 : 1]).trim() : "No";
      row.classList.toggle("empty", n === 0);
    }
  };
}

/* "Bodies are: Group" — xGroupsRow for this panel: the component's groups
   (`groupChips`, [id, name]), lit while in `groups`; a click takes one in or out. */
function rGroupsRow(){
  const row = document.createElement("div");
  row.className = "xseg xbodies";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = "Groups";
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(label); row.appendChild(opts);
  rRows.appendChild(row);

  const set = (list) => {
    rClaim("groups", list);
    rSpec.groups = list;
    rRender();
    post({ action: "setmirror", groups: list, rev: panelRev });
  };
  let drawn = "";
  rSegs.groups = {
    row: row, cells: [],
    render: () => {
      row.style.display = rSpec.scope === "group" && rSpec.pickedKind !== "features"
        ? "" : "none";
      const chips = rSpec.groupChips || [], chosen = new Set(rSpec.groups || []);
      const key = JSON.stringify([chips, [...chosen]]);
      if (key === drawn) return;
      drawn = key;
      opts.textContent = "";
      if (!chips.length){
        const none = document.createElement("span");
        none.className = "xopt xclear";
        none.textContent = "Click a body in a group";
        opts.appendChild(none);
        return;
      }
      for (const [id, name] of chips){
        const c = document.createElement("span");
        c.className = "xopt";
        c.textContent = name;
        c.classList.toggle("on", chosen.has(id));
        c.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const next = new Set(rSpec.groups || []);
          if (next.has(id)) next.delete(id); else next.add(id);
          set(chips.map(ch => ch[0]).filter(g => next.has(g)));
        });
        opts.appendChild(c);
      }
    }
  };
}

function buildMirrorPanel(){
  rSegRow("plane", "Plane", R_PLANES);
  rSegRow("operation", "Operation", R_OPERATIONS);
  // A group or the whole component, followed once made — X_BODY_SCOPES, as Extrude's.
  rSegRow("scope", "Bodies are", X_BODY_SCOPES);
  rGroupsRow();
  rSelectionRow();
  rSegRow("compute", "Compute", R_COMPUTE, rAdv);
  rAdvHead.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const shut = rAdv.classList.toggle("hide");
    rAdvHead.querySelector(".twirl").classList.toggle("collapsed", shut);
    // No sizeWindow(), for the reason the Extrude panel's Advanced gives.
  });
}
buildMirrorPanel();

function rRender(){
  for (const key in rSegs) rSegs[key].render();
  // The Pick cell says what it is doing: waiting for a pick (in the accent, the
  // "this is what will happen" colour), or the name of what was picked.
  for (const { cell, value, text } of rSegs.plane.cells){
    if (value !== "picked") continue;
    const waiting = rSpec.plane === "picked" && !rSpec.pickedLabel;
    cell.textContent = rSpec.plane === "picked" && rSpec.pickedLabel ? rSpec.pickedLabel
                     : waiting ? "Pick…" : text;
    cell.classList.toggle("auto", waiting);
    cell.title = waiting ? "Click a face or a construction plane" : "";
  }
  // Coloured: what the mirror WILL do. A copy clear of the original cannot join and is
  // built as a new body (operationResolved), so New body wears the colour while Join
  // stays plainly chosen.
  opMark(rSegs.operation.cells, rSpec.operationResolved || rSpec.operation);
  // Join means "into the body it came from", which only bodies have.
  rSegs.operation.row.style.display = rSpec.kind === "features" ? "none" : "";
  // A mirror of features is always the features picked.
  rSegs.scope.row.style.display = rSpec.pickedKind === "features" ? "none" : "";
}

function openMirrorSession(s){
  applyNamingSituation(null);
  const showing = isOpen && mode === "mirror";
  isOpen = true; setCommitting(false);
  mode = "mirror";
  rTitleEl.textContent = dialogTitle(mode, s);
  rErr.textContent = "";
  rSpec = Object.assign({}, s.mirror || {});
  rKeys = new Set((s.mirror && s.mirror.keys) || []);
  rCycleKeys = (s.mirror && s.mirror.cycleKeys) || {};
  if (showing) rClaims.overlay(rSpec); else rClaims.clear();
  unit = "";
  nameRequired = false; origName = ""; isDriven = false;
  rRender();
  showPanel("mirror");
  // Nothing here to type into, and a field left focused in a panel that has just
  // been hidden would still be the one the keys went to.
  if (document.activeElement && document.activeElement !== document.body){
    document.activeElement.blur();
  }
  fitWidth();
  sizeWindow(showing ? "keep" : null);
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* A push back FROM Fusion. Everything on this panel is a choice, so all of it is
   taken — a key can change the plane from out there. */
function applyMirrorUpdate(next){
  if (!next) return;
  if (next.keys) rKeys = new Set(next.keys);
  if (next.cycleKeys) rCycleKeys = next.cycleKeys;
  let changed = false;
  for (const key of ["plane", "operation", "operationResolved", "compute", "hasSelection",
                     "selectionCount", "kind", "component", "pickedLabel", "scope",
                     "groups", "groupChips", "pickedKind"]){
    if (!(key in next)) continue;
    if (rClaims.stale(key, next[key], next.rev)) continue;
    if (JSON.stringify(rSpec[key]) !== JSON.stringify(next[key])){
      rSpec[key] = next[key]; changed = true;
    }
  }
  if (changed) rRender();
}

function commitMirror(){
  if (committing) return;
  if (!rSpec.selectionCount){
    rErr.textContent = "Pick something to mirror first.";
    return;
  }
  if (rSpec.plane === "picked" && !rSpec.pickedLabel){
    rErr.textContent = "Pick a face or plane to mirror across, or choose X, Y or Z.";
    return;
  }
  // Up until Fusion says it built — see commitExtrude.
  setCommitting(true);
  rErr.textContent = "";
  post({ action: "commitmirror" });
}

function cancelMirror(){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  post({ action: "cancelmirror" });
}

/* ── The feature panel ───────────────────────────────────────────────────────
   The Mirror panel's arrangement for every add-in on AddIns/Feature Panel.py —
   Rectangular Pattern, Combine, Scale — with the rows arriving as DATA with the session
   (that file's ROWS says what they can be) instead of being written out here once per
   feature. A live command is running behind it: every change posts a "setfeature"
   carrying the add-in's `feature` kind, which is what picks its queue on the server,
   and the command redraws the real feature.

   Keys follow the panel's rows. With a text row, the Extrude panel's rules: Enter takes
   a highlighted suggestion or commits, and the Active While Typing chords go to Fusion
   (`keys` is typing_keys). With none, the Mirror panel's: every chord that means
   something in Fusion right now goes back to it (`keys` is panel_keys).

   A row may name a SLOT — a thing the next click is taken as (a direction, a point, the
   target body). A slot waiting for its click reads "Pick…" in the accent. */

let oKind = "";              // the add-in the panel is built for
let oRowsJSON = "";          // …and the rows it was built from
let oRowDefs = [];
let oSpec = {};
let oRowEls = [];            // [{def, row, render()}]
const oFields = {};          // key -> input
let oUnit = "";              // the design's length unit, for a text row that has one
let oKeys = new Set();

// The Extrude panel's claims, for the same reason — see "What we have asked for".
const oClaims = makeClaims();
function oClaim(key, value){ oClaims.claim(key, value); }

/* xCycle for this panel (Feature Panel.py cycle_row): the add-in's `cycles` names the
   row each shared cycle steps. Its ring is the options on offer — in the order the
   add-in offers them when it narrows them (`<key>Only`, Rectangular Pattern's ranked
   directions) — then Pick, only while the slot holds a pick. Another row of the same
   choices that held the new value takes the old one: the two swap rather than coincide
   (Rectangular Pattern's second direction). */
function oCycle(kind){
  const key = (oSpec.cycles || {})[kind];
  const def = key && oRowDefs.find(d => d.type === "seg" && d.key === key);
  if (!def) return false;
  const values = (def.options || []).map(o => o[0]);
  const only = oSpec[key + "Only"];
  let ring = Array.isArray(only) && only.length
    ? only.filter(v => v !== "picked" && values.indexOf(v) >= 0)
          .concat(values.indexOf("picked") >= 0 ? ["picked"] : [])
    : values.slice();
  if (def.slot && !(oSpec.picked || {})[def.slot]) ring = ring.filter(v => v !== "picked");
  if (!ring.length) return true;
  const now = oSpec[key];
  const nxt = stepRing(ring, now);
  const same = JSON.stringify(def.options);
  for (const other of oRowDefs){
    if (other === def || other.type !== "seg" || JSON.stringify(other.options) !== same) continue;
    if (oShown(other) && oSpec[other.key] === nxt){
      oClaim(other.key, now);
      oSpec[other.key] = now;
    }
  }
  oChoose(def, nxt);
  return true;
}

function oHasText(){ return oRowDefs.some(d => d.type === "text"); }
function oShown(def){
  for (const key in (def.when || {})) if (def.when[key].indexOf(oSpec[key]) < 0) return false;
  return true;
}

/* The whole of what the panel says, every time — a handful of short fields, as xPush. */
function oPush(extra){
  const out = Object.assign({ action: "setfeature", feature: oKind }, extra || {});
  for (const def of oRowDefs){
    if (def.type === "seg" || def.type === "text" || def.type === "chips")
      out[def.key] = oSpec[def.key];
  }
  out.rev = panelRev;
  post(out);
}

function oSegRow(def, into){
  const row = document.createElement("div");
  row.className = "xseg";
  const l = document.createElement("span");
  l.className = "xlabel";
  l.textContent = def.label || "";
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(l); row.appendChild(opts);
  into.appendChild(row);
  const cells = (def.options || []).map(([value, text]) => {
    const c = document.createElement("span");
    c.className = "xopt";
    c.textContent = text;
    c.addEventListener("mousedown", (e) => {
      e.preventDefault();          // never take focus off a field being typed in
      oChoose(def, value);
    });
    opts.appendChild(c);
    return { cell: c, value: value, text: text };
  });
  return { def, row, render: () => {
    const picked = def.slot ? (oSpec.picked || {})[def.slot] : "";
    // The choices on offer right now, when the add-in says (Feature Panel.py, ROWS).
    const only = oSpec[def.key + "Only"];
    for (const { cell, value, text } of cells){
      cell.style.display = !Array.isArray(only) || only.indexOf(value) >= 0
                           || (def.slot && value === "picked") ? "" : "none";
      cell.classList.toggle("on", oSpec[def.key] === value);
      if (!def.slot || value !== "picked") continue;
      // Waiting for a click (in the accent), or the name of what was clicked.
      const waiting = oSpec[def.key] === "picked" && !picked;
      cell.textContent = oSpec[def.key] === "picked" && picked ? picked
                       : waiting ? "Pick…" : text;
      cell.classList.toggle("auto", waiting);
    }
    if (def.key === "operation") opMark(cells, oSpec.operation);
  }};
}

/* A click on a choice. On a slot's row, Pick pressed while it already holds a pick lets
   go of it and waits for another — Mirror's Pick. */
function oChoose(def, value){
  const extra = {};
  if (def.slot && value === "picked" && oSpec[def.key] === "picked"
      && (oSpec.picked || {})[def.slot]){
    extra.repick = def.slot;
    oSpec.picked = Object.assign({}, oSpec.picked);
    delete oSpec.picked[def.slot];
  }
  oClaim(def.key, value);
  oSpec[def.key] = value;
  oRender();
  oPush(extra);
}

function oTextRow(def, into){
  const { row, input } = cTextRow(def.placeholder || "", true, into);
  // A row says its own unit ("" for a count or a factor), so the suggestions offered in
  // it are the parameters that fit it; without one it is a length.
  // Capture: ahead of cTextRow's own focus listener, which offers the first suggestions.
  input.addEventListener("focus", () => { unit = def.unit != null ? def.unit : oUnit; }, true);
  input.addEventListener("input", () => { oSpec[def.key] = input.value; oPush(); });
  oFields[def.key] = input;
  return { def, row, render: () => {} };
}

/* "Selected  3 bodies  Clear" — the Mirror panel's row, for the objects ('selection')
   or a slot (any other key: "Target  Body1  Pick"). */
function oSelRow(def, into){
  const row = document.createElement("div");
  row.className = "xseg xsel";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = def.label || "";
  const count = document.createElement("span");
  count.className = "xcount";
  const btn = document.createElement("span");
  btn.className = "xopt xclear";
  btn.textContent = def.button || "Clear";
  const isObjects = def.key === "selection";
  btn.addEventListener("mousedown", (e) => {
    e.preventDefault();
    // Optimistic, as every row here: the read-out changes now, the add-in's answer
    // follows on the next push.
    if (isObjects){
      oClaim("selectionText", ""); oSpec.selectionText = "";
    } else {
      oClaim("armed", def.key); oSpec.armed = def.key;
      oSpec.picked = Object.assign({}, oSpec.picked); delete oSpec.picked[def.key];
    }
    oRender();
    post({ action: "clearfeature", feature: oKind, what: def.key, rev: panelRev });
  });
  row.appendChild(label); row.appendChild(count); row.appendChild(btn);
  into.appendChild(row);
  return { def, row, render: () => {
    const text = isObjects ? oSpec.selectionText : (oSpec.picked || {})[def.key];
    // The objects wait whenever no slot does: that is where the next click goes (Combine's
    // Tools, the moment its target is in).
    const waiting = isObjects ? !oSpec.armed : oSpec.armed === def.key;
    count.textContent = text || (waiting ? "Pick…" : "No");
    count.classList.toggle("auto", waiting && !text);
    row.classList.toggle("empty", !text);
  }};
}

/* A 'chips' row (Feature Panel.py, ROWS): xGroupsRow's chips, from the add-in's
   `<key>Chips` ([value, text]) — Combine's groups in the component. With `single`, one
   is lit (spec[key] a value); otherwise each click toggles one in the list, and Clear
   empties it. */
function oChipsRow(def, into){
  const row = document.createElement("div");
  row.className = "xseg xbodies";
  const label = document.createElement("span");
  label.className = "xlabel";
  label.textContent = def.label || "";
  const opts = document.createElement("div");
  opts.className = "xopts";
  row.appendChild(label); row.appendChild(opts);
  into.appendChild(row);
  const set = (value) => {
    oClaim(def.key, value);
    oSpec[def.key] = value;
    oRender();
    oPush();
  };
  let drawn = "";
  return { def, row, render: () => {
    const chips = oSpec[def.key + "Chips"] || [];
    const now = oSpec[def.key];
    const chosen = new Set(def.single ? (now ? [now] : []) : (now || []));
    const key = JSON.stringify([chips, [...chosen]]);
    if (key === drawn) return;
    drawn = key;
    opts.textContent = "";
    if (!chips.length){
      const none = document.createElement("span");
      none.className = "xopt xclear";
      none.textContent = "None in this component";
      opts.appendChild(none);
      return;
    }
    for (const [value, text] of chips){
      const c = document.createElement("span");
      c.className = "xopt";
      c.textContent = text;
      c.classList.toggle("on", chosen.has(value));
      c.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (def.single){ if (!chosen.has(value)) set(value); return; }
        const next = new Set(chosen);
        if (next.has(value)) next.delete(value); else next.add(value);
        set(chips.map(ch => ch[0]).filter(v => next.has(v)));
      });
      opts.appendChild(c);
    }
    if (def.single) return;
    const clear = document.createElement("span");
    clear.className = "xopt xclear";
    clear.textContent = "Clear";
    clear.addEventListener("mousedown", (e) => { e.preventDefault(); set([]); });
    opts.appendChild(clear);
  }};
}

function buildFeaturePanel(rows){
  oRows.textContent = ""; oAdv.textContent = "";
  for (const key in oFields) delete oFields[key];
  oRowEls = [];
  for (const def of rows){
    const into = def.adv ? oAdv : oRows;
    if (def.type === "seg") oRowEls.push(oSegRow(def, into));
    else if (def.type === "text") oRowEls.push(oTextRow(def, into));
    else if (def.type === "sel") oRowEls.push(oSelRow(def, into));
    else if (def.type === "chips") oRowEls.push(oChipsRow(def, into));
  }
  oAdvHead.classList.toggle("none", !rows.some(d => d.adv));
}
oAdvHead.addEventListener("mousedown", (e) => {
  e.preventDefault();
  const shut = oAdv.classList.toggle("hide");
  oAdvHead.querySelector(".twirl").classList.toggle("collapsed", shut);
  // No sizeWindow(), for the reason the Extrude panel's Advanced gives.
});

function oRender(){
  for (const el of oRowEls){
    el.row.style.display = oShown(el.def) ? "" : "none";
    el.render();
  }
}

function openFeatureSession(s){
  applyNamingSituation(null);
  const f = s.feature || {};
  const kind = f.kind || "";
  // Re-armed rather than opened, when it is this same feature already up: keep its
  // place and whatever is half typed.
  const showing = isOpen && mode === "feature" && oKind === kind;
  isOpen = true; setCommitting(false);
  mode = "feature";
  oTitleEl.textContent = dialogTitle(mode, s);
  oErr.textContent = "";
  const rowsJSON = JSON.stringify(f.rows || []);
  if (kind !== oKind || rowsJSON !== oRowsJSON){
    oKind = kind; oRowsJSON = rowsJSON; oRowDefs = f.rows || [];
    buildFeaturePanel(oRowDefs);
  }
  const typed = {};
  if (showing) for (const key in oFields) typed[key] = oSpec[key];
  oSpec = Object.assign({}, f.spec || {}, typed);
  oKeys = new Set(oSpec.keys || []);
  // A new command starts with no claims — its add-in has heard nothing yet.
  if (showing) oClaims.overlay(oSpec); else oClaims.clear();
  oUnit = s.unit || "";
  unit = oUnit;
  nameRequired = false; origName = ""; isDriven = false;
  if (!showing) for (const key in oFields) oFields[key].value = oSpec[key] || "";
  oRender();
  refreshChips();
  showPanel("feature");
  fitWidth();
  sizeWindow(showing ? "keep" : null);
  if (!showing){
    // The first field that is showing, as the Extrude panel focuses its distance; with
    // none, nothing — a field left focused in a panel just hidden still takes the keys.
    const first = oRowDefs.find(d => d.type === "text" && oShown(d));
    if (first) oFields[first.key].focus();
    else if (document.activeElement && document.activeElement !== document.body){
      document.activeElement.blur();
    }
  }
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* A push back FROM Fusion. Every choice and read-out is taken — a key can change a choice
   from out there — but never a field: the panel's own text is the truth for those. */
function applyFeatureUpdate(f){
  if (!f || (f.kind || "") !== oKind) return;
  const next = f.spec || {};
  if (next.keys) oKeys = new Set(next.keys);
  let changed = false;
  for (const key in next){
    if (key in oFields || key === "keys") continue;
    if (oClaims.stale(key, next[key], next.rev)) continue;
    if (JSON.stringify(oSpec[key]) !== JSON.stringify(next[key])){
      oSpec[key] = next[key]; changed = true;
    }
  }
  if (changed) oRender();
}

function commitFeature(){
  if (committing) return;
  // Up until Fusion says it built — see commitExtrude. What is missing is the add-in's
  // to say (Feature.why_not): only it knows what this feature needs.
  setCommitting(true);
  oErr.textContent = "";
  post({ action: "commitfeature", feature: oKind });
}

function cancelFeature(){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  post({ action: "cancelfeature", feature: oKind });
}

/* The plane box. The SAME panel as the value box, with #pname's row given over to saying
   what the line resolved to — the line is an expression after its first word, so it
   wants that box, that autocomplete and that live preview, and a second panel would be a
   copy of all three.

   Four rows, then: the line, what to call the sketch (#fsname, this box's own field —
   the parameter name field is busy being the readout), the readout, and the angle. */
function openSketchSession(s){
  applyNamingSituation(null);
  isOpen = true; setCommitting(false);
  mode = "sketch";
  dTitleEl.textContent = dialogTitle(mode, s);
  showPanel("value");
  datums = s.datums || [];
  redefine = !!s.redefine;
  flat = !!s.flat;
  sketchToken = s.sketch || "";
  unit = ""; nameRequired = false; origName = ""; isDriven = false;
  $("driven").className = "none";

  // No name to give a plane, so the parameter field goes and its row becomes the readout
  // — which puts #pname and the readout back in that row if a stepper had them.
  stepperOn = false; renderSteps();
  nameEl.style.display = "none";
  fName.classList.add("readout");
  fName.classList.remove("bad");
  valueEl.placeholder = "Plane";

  // The SKETCH's name, though, is worth asking for — Fusion's own "Sketch7" says nothing
  // about where the sketch is. Prefilled with the name it already has when the box was
  // opened on an existing sketch (redefining its plane, or Project Bounds), so leaving it
  // alone renames nothing; empty for a new one, where empty means "Fusion's default".
  fSName.classList.remove("hide");
  sNameEl.value = s.name || "";

  // Other plane-box sketches in this component, for "Same as..." — see
  // _open_sketch_dialog's `others`. Empty unless the add-in found at least one.
  sessionSketches = s.sketches || [];
  fName.classList.toggle("hassame", sessionSketches.length > 0);
  hidePick();

  valueEl.value = s.expression || "";
  clearError();
  prevEl.textContent = ""; infoEl.textContent = "";
  syncSketchSituation();
  fAngle.classList.toggle("hide", flat);
  resetAngle(s.angle || null);
  if (showPlanes) post({ action: "showplanes", on: true });
  autosize();
  schedulePreview();
  refreshChips();
  valueEl.focus();
  valueEl.setSelectionRange(0, valueEl.value.length);
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  refreshFocusStyle();
  // With the box empty the list shows the whole vocabulary, which is the answer to
  // "what can I say here" without having to guess a first letter.
  updateAC();

  host("session", { open: true, place: s.place || "top",
                    height: Math.ceil(panel.getBoundingClientRect().height) });
}

/* ── Command search ────────────────────────────────────────────────────────────
   Fusion's own "find a command" box, opened by bare S, only ever lists Fusion's own
   commands and only matches the start of a name. This panel replaces it: the
   Shortcuts add-in has taken S over from Fusion's native search command (see
   Shortcuts.py, _adopt_native_search_key) and opens this instead. The catalog is not
   kept here — it is fetched straight from fusion_shortcuts' own server (port 8733),
   which already builds it (native commands AND our own add-in buttons/actions) for its
   own window; see fusion_shortcuts/CLAUDE.md. Running the highlighted row posts back
   to THAT server's /run, not to anything in this add-in — this window never executes a
   command itself.

   The palette itself — ranking, list, keys, the right-click sheet — is the shared
   fusion_ui/search.js (SearchKit; the Photoshop palette is the same code). What stays
   here is Fusion's ADAPTER: its environments (sketch / viewport / extrude), the
   SOLID / SHEET METAL / SURFACE trio dedup and dot, sketch-first ranking, how a row
   runs, and how Control Room is asked for a key. */

/* SOLID / SHEET METAL / SURFACE each carry their own copy of some commands (three
   "Extrude"s, etc. — Fusion declares one CommandDefinition per tab, all under the same
   display name). `group` is fusion_shortcuts' tab label (_build_catalog), all-caps the
   way Fusion names them. Order is the one asked for: solid, sheet metal, surface. */
const ENV_GROUPS = { SOLID: "solid", "SHEET METAL": "sheetmetal", SURFACE: "surface" };
const ENV_GROUP_ORDER = { SOLID: 0, "SHEET METAL": 1, SURFACE: 2 };

/* Same-named candidates that collapse to one row (kept out of `pool` after this),
   except a same-named SOLID/SHEET METAL/SURFACE trio — those name a genuinely
   different command per environment, so all of them stay (see ENV_GROUPS above; they
   are told apart by the dot the adapter's decorate() adds, not by hiding two of the
   three). "Ours" (an add-in's own action, or one that got a button under My add-ins)
   wins an ordinary collision — a native command sharing a name with one of ours is
   always the one to drop. Ties within "ours" or within native keep catalog order,
   which is already stable. */
function _sDedup(commands){
  const byName = new Map();
  for (const c of commands){
    const key = (c.name || c.id || "").trim().toLowerCase();
    if (!byName.has(key)) byName.set(key, []);
    byName.get(key).push(c);
  }
  const out = [];
  for (const group of byName.values()){
    if (group.length === 1){ out.push(group[0]); continue; }
    const envGroups = group.map(c => ENV_GROUPS[c.group]).filter(Boolean);
    if (envGroups.length === group.length && new Set(envGroups).size === group.length){
      out.push(...group);
      continue;
    }
    out.push(group.find(c => c.kind === "action" || c.kind === "addin") || group[0]);
  }
  return out;
}

function sStoreHides(c, env, show){ return SearchKit.storeHides(c, env, show); }

/* What Fusion adds to SearchKit.rank (see its docstring for opts). `kind: 'missing' (a
   binding pointing at a command this session never loaded) and CommandSearchCmd itself
   are never offered — running either could only reopen this same palette. `env` is the
   environment the search key was pressed in ('sketch' / 'viewport' / 'extrude' / '' for
   unknown); it also drops what plainly cannot apply — a SKETCH-group command outside a
   sketch, a SOLID/SHEET METAL/SURFACE one inside it — and in a sketch the sketch
   toolbar's own commands come first ("circle" otherwise listed every stray
   "Circle…"-named id ahead of "3-Point Circle"). An unknown env ('') fails open. */
const FUSION_RANK = {
  skip: (c, env) => c.kind === "missing" || c.id === "CommandSearchCmd" ||
                    (env === "sketch" && !!ENV_GROUPS[c.group]) ||
                    (!!env && env !== "sketch" && c.group === "SKETCH"),
  dedup: _sDedup,
  boost: (c, env) => env === "sketch" && c.group === "SKETCH" ? 0 : 1,
  tie: (a, b) => (ENV_GROUP_ORDER[a.group] ?? 9) - (ENV_GROUP_ORDER[b.group] ?? 9),
};

/* Pure and DOM-free on purpose — fusion_value/test/cases.js calls this directly, the
   same way it drives updateAC(). Same arguments as SearchKit.rank. */
function searchRank(query, commands, recentIds, opts){
  return SearchKit.rank(query, commands, recentIds, Object.assign({}, FUSION_RANK, opts));
}

// A SOLID/SHEET METAL/SURFACE row only gets its dot when another listed row shares its
// name — dedup already dropped an ordinary duplicate down to one, so what's left
// sharing a name is exactly the trio the dot exists to tell apart.
let sDotItems = null, sDotCounts = null;
function sDecorate(c, items){
  if (!ENV_GROUPS[c.group]) return null;
  if (sDotItems !== items){
    sDotItems = items; sDotCounts = new Map();
    items.forEach(x => {
      if (!ENV_GROUPS[x.group]) return;
      const key = (x.name || x.id || "").trim().toLowerCase();
      sDotCounts.set(key, (sDotCounts.get(key) || 0) + 1);
    });
  }
  if (!(sDotCounts.get((c.name || c.id || "").trim().toLowerCase()) > 1)) return null;
  const dot = document.createElement("span");
  dot.className = "ac-envdot";
  return { cls: "ac-env-" + ENV_GROUPS[c.group], node: dot };
}

/* Each row's binding, as Control Room shows it: the REACH (Space C, 3 fingers + A, J
   held), not the anchor chord Fusion's keymap holds for a held binding. Control Room's
   /fusion/keys is ~0.5 s, so it is fetched alongside the catalog and the rows redrawn
   when it lands; the last answer is kept between opens. Until one has ever arrived (Control
   Room not up yet), Fusion's own keymap chord stands in. */
let sKeys = null;
function sLoadKeys(){
  fetch(FUSION_API + "/fusion/keys", { cache: "no-store" })
    .then(r => r.json())
    .then(d => {
      if (!d || d.up === false || !d.keys) return;
      sKeys = d.keys;
      if (mode === "search") sKit.render();
    })
    .catch(() => {});
}
const S_MOD_SYMBOL = { ctrl: "⌃", control: "⌃", alt: "⌥", opt: "⌥",
  option: "⌥", shift: "⇧", cmd: "⌘", command: "⌘", meta: "⌘" };
const S_MOD_ORDER = ["⌃", "⌥", "⇧", "⌘"];
function sKeyWord(k){
  k = String(k || "");
  return k.length === 1 ? k.toUpperCase() : k.charAt(0).toUpperCase() + k.slice(1);
}
function sChordText(seq){
  const parts = String(seq || "").split("+").filter(Boolean);
  const key = parts.pop();
  if (!key) return "";
  const syms = new Set(parts.map(p => S_MOD_SYMBOL[p.toLowerCase()] || ""));
  return S_MOD_ORDER.filter(s => syms.has(s)).join("") + sKeyWord(key);
}
function sBinding(c, keymap){
  const t = sKeys ? sKeys[c.id] : (keymap[c.id] ? { kind: "chord", seq: keymap[c.id] } : null);
  if (!t) return null;
  const glyph = (t.kind === "space" && "saturn") || (t.kind === "fingers" && "hand");
  if (glyph && typeof Glyphs !== "undefined" && t.key){
    const span = document.createElement("span");
    span.innerHTML = glyph === "hand" ? Glyphs.hand(t.fingers || 0) : Glyphs.saturn();
    span.appendChild(document.createTextNode(sKeyWord(t.key)));
    span.title = (glyph === "hand" ? (t.fingers || 0) + " fingers + " : "Space + ") + sKeyWord(t.key);
    return span;
  }
  if (t.kind === "space")   return "Space " + sKeyWord(t.key);
  if (t.kind === "fingers") return (t.fingers || 0) + "F " + sKeyWord(t.key);
  if (t.kind === "held")    return String(t.held || "").split("-").map(sKeyWord).join("+")
                                   + " " + sKeyWord(t.key);
  return sChordText(t.seq) || null;
}

const sKit = SearchKit.create({
  query: sQueryEl, results: sResultsEl, sheet: sSheetEl,
  adapter: {
    stores: { base: SHORTCUTS_API, show: "/search_show", name: "/search_name", pin: "/search_pin" },
    envs: [["sketch", "Sketch"], ["viewport", "Viewport"], ["extrude", "Extrude"]],
    storageKey: "searchShowHidden",
    rankOpts: () => FUSION_RANK,
    decorate: sDecorate,
    binding: sBinding,
    isActive: () => mode === "search",
    // "keep": placement is under the CURSOR (main.swift's "corner"), not centred —
    // recomputing on every keystroke would chase a mouse that has since moved. Nothing
    // else changes size: the window is a fixed rectangle (see WIN_H) with room already
    // held below the panel for this list.
    onRendered: () => sizeWindow("keep"),
    onSheetClose: () => { if (mode === "search"){ askWidth(SEARCH_W); sizeWindow("keep"); } },
    async loadCatalog(){
      sLoadKeys();
      const d = await (await fetch(SHORTCUTS_API + "/data", { cache: "no-store" })).json();
      const keymap = {};
      for (const entry of (d.hotkeys || [])){
        for (const c of (entry.commands || [])) keymap[c.id] = entry.seq;
      }
      return { commands: d.commands, recents: (d.recents || []).map(r => r.id),
               disabled: d.disabled, keymap, show: d.search_show, names: d.search_names,
               pins: d.search_pins };
    },
    run: runSearchSelection,
    lookupRow: sLookupRow,
    setKey: sSetKey,
    fitSheet: sFitSheet,
  },
});

/* Drop the session. A plain POST /cmd {action:"cancel"} — what every other mode's
   cancel() sends — only ENQUEUES the close; the add-in's tick does the actual
   _state["open"] = False some milliseconds later. That is fine for a cancel, but not
   for running a command (see runSearchSelection): /search/close closes it inline, in
   the HTTP thread, because closing needs no Fusion API call — same reasoning as
   /extrude/update and /tags being served rather than queued. */
function closeSearch(){
  return fetch(API + "/search/close", { method: "POST" }).catch(() => {});
}

/* Run the chosen command. The window hides and /search/close is AWAITED before
   the command is asked for — not after — because several of our own commands
   (Extrude, New Component…) reopen THIS SAME WINDOW in another mode the instant they
   start. Without that ordering, this session's belated close could land after the new
   one already opened and stomp _state["open"] back to false, closing what the key we
   just ran opened. */
function runSearchSelection(item){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  // The extrude/component sessions ask sizeWindow() for their own frame with no width
  // of their own (the value session re-sends its own at openSession) — without this
  // they would inherit whatever the palette last asked for (SEARCH_W, 240).
  askWidth(baseWidth);
  const closed = closeSearch();
  if (!item) return;
  closed.then(() => {
    fetch(SHORTCUTS_API + "/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd_id: item.id })
    }).catch(() => {});
  });
}

function cancelSearch(){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  askWidth(baseWidth);
  closeSearch();
}

function openSearchSession(s){
  isOpen = true; setCommitting(false);
  mode = "search";
  showPanel("search");
  sKit.open({ env: (s && s.searchEnv) || "" });   // clears the field, fetches the catalog
  askWidth(SEARCH_W);
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  sizeWindow();
  sQueryEl.focus();
}

sQueryEl.addEventListener("input", () => sKit.rerun());

/* ── The versions panel ───────────────────────────────────────────────────────
   AddIns/Versions' face, in place of Fusion's Data Panel / version dialogs. Two lists:
   the OPEN DOCUMENTS (click one to switch to it, × to close it — a modified one asks
   Save / Don't Save / Cancel first) and the active document's last 20 VERSIONS (click,
   or ↑/↓ + Enter, to open that one: the add-in saves and closes the document that is
   open, then opens it). The add-in pushes everything (`s.versions`, no new seq — see
   server.update_versions); this page only renders and posts picks, every one carrying
   the file it was made against so a pick from a stale list is refused there.

   `confirm` is a question over the lists — or the whole panel when `compact` (the Save
   command on an old version asks "save as latest?" here, no lists). Its keys are on
   its buttons; Escape answers its last (No / Cancel). */
const VERSIONS_W = 360;
let vData = {}, vIndex = -1;

function openVersionsSession(s){
  isOpen = true; setCommitting(false);
  mode = "versions";
  showPanel("versions");
  vData = {}; vIndex = -1;
  askWidth(VERSIONS_W);
  applyVersionsUpdate(s.versions || {}, true);
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  sizeWindow();
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}

function applyVersionsUpdate(v, force){
  v = v || {};
  if (!force && JSON.stringify(v) === JSON.stringify(vData)) return;
  const fileChanged = (v.file || {}).id !== (vData.file || {}).id;
  vData = v;
  if (fileChanged) vIndex = -1;
  renderVersions();
  sizeWindow("keep");
}

function vWhen(secs){
  if (!secs) return "";
  const d = new Date(secs * 1000), now = new Date();
  const ago = (now - d) / 1000;
  if (ago < 60) return "just now";
  if (ago < 3600) return Math.floor(ago / 60) + " min ago";
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return hm;
  const md = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return d.getFullYear() === now.getFullYear() ? md + " " + hm : md + " " + d.getFullYear();
}

function vEl(tag, cls, text){
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}

function renderVersions(){
  const v = vData, file = v.file || null;
  vTitleEl.textContent = v.compact ? (v.title || "Save")
                       : file ? "Versions — " + file.name : "Versions";
  vErr.textContent = v.error || "";
  renderVersionsConfirm(v.confirm || null);
  vBodyEl.innerHTML = "";
  if (v.compact) return;

  vBodyEl.appendChild(vEl("div", "vsec", "Open"));
  const docs = vEl("div"); docs.id = "vdocs";
  for (const d of v.docs || []){
    const row = vEl("div", "ac-row vrow" + (d.active ? " cur" : "") + (d.modified ? " mod" : ""));
    row.title = d.modified ? "Unsaved changes" : "";
    row.appendChild(vEl("span", "vmod"));
    row.appendChild(vEl("span", "ac-name", d.name));
    if (d.version) row.appendChild(vEl("span", "ac-res",
      "v" + d.version + (d.latest && d.version < d.latest ? " of " + d.latest : "")));
    const x = vEl("span", "vx", "×");
    x.title = "Close";
    x.addEventListener("mousedown", e => { e.preventDefault(); e.stopPropagation();
      vPost({ action: "versionsclose", doc: d.id }); });
    row.appendChild(x);
    if (!d.active) row.addEventListener("mousedown", e => { e.preventDefault();
      vPost({ action: "versionsactivate", doc: d.id }); });
    docs.appendChild(row);
  }
  if (!(v.docs || []).length) docs.appendChild(vEl("div", "vempty", "No documents open."));
  vBodyEl.appendChild(docs);

  if (!file) return;
  vBodyEl.appendChild(vEl("div", "vsec", "Versions"));
  const list = vEl("div"); list.id = "vlist";
  const rows = v.list || [];
  rows.forEach((r, i) => {
    const cur = r.n === file.version;
    const row = vEl("div", "ac-row vrow" + (cur ? " cur" : "") + (i === vIndex ? " sel" : ""));
    row.appendChild(vEl("span", "vnum", "v" + r.n));
    row.appendChild(vEl("span", "ac-name", r.desc || ""));
    row.appendChild(vEl("span", "ac-val", cur ? "open" : vWhen(r.date)));
    row.appendChild(vEl("span", "ac-mark"));
    if (!cur) row.addEventListener("mousedown", e => { e.preventDefault(); vOpen(r.n); });
    list.appendChild(row);
  });
  if (!rows.length)
    list.appendChild(vEl("div", "vempty", v.loading ? "Loading versions…"
                                       : file.saved === false ? "Never saved." : "No versions."));
  vBodyEl.appendChild(list);
  const sel = list.querySelector(".sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

// The question, as [label, key, answer] buttons — the add-in says which kind.
function vConfirmButtons(c){
  return c.kind === "close"
    ? (c.unsaved ? [["Don't Save", "D", "discard"], ["Cancel", "Esc", "cancel"]]
                 : [["Save", "↩", "save"], ["Don't Save", "D", "discard"], ["Cancel", "Esc", "cancel"]])
    : [["Save as Latest", "↩", "yes"], ["No", "Esc", "no"]];
}

function renderVersionsConfirm(c){
  vConfirmEl.innerHTML = "";
  vConfirmEl.classList.toggle("hide", !c);
  if (!c) return;
  vConfirmEl.appendChild(vEl("div", "vq", c.text || ""));
  if (c.sub) vConfirmEl.appendChild(vEl("div", "vqsub", c.sub));
  const btns = vEl("div", "vbtns");
  vConfirmButtons(c).forEach(([label, key, answer], i) => {
    const b = vEl("button", i === 0 ? "def" : "");
    b.appendChild(document.createTextNode(label));
    b.appendChild(vEl("span", "vkey", key));
    b.addEventListener("mousedown", e => { e.preventDefault(); vAnswer(answer); });
    btns.appendChild(b);
  });
  vConfirmEl.appendChild(btns);
}

function vPost(body){
  body.file = (vData.file || {}).id || "";
  return post(body);
}

// The add-in closes the panel itself before it saves/closes/opens documents, but the
// page goes at once: nothing on it means anything once the document is changing.
function vLeave(){
  isOpen = false; setCommitting(false);
  hideWindow();
  showPanel("value");
  askWidth(baseWidth);
  fetch(API + "/versions/close", { method: "POST" }).catch(() => {});
}

function vOpen(n){
  vPost({ action: "versionsopen", version: n });
  vLeave();
}

function vAnswer(answer){
  const c = vData.confirm || {};
  vPost({ action: "versionsanswer", kind: c.kind || "", doc: c.doc || "", answer });
  // A compact question is the whole panel: any answer ends it. One over the lists
  // goes away and the lists stay (the add-in pushes them again after a close).
  if (vData.compact) vLeave();
  else { vData = Object.assign({}, vData, { confirm: null }); renderVersions(); sizeWindow("keep"); }
}

function cancelVersions(){
  vLeave();
  post({ action: "versionscancel" });
}

function versionsKey(e){
  const c = vData.confirm;
  if (c){
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const btns = vConfirmButtons(c);
    let answer = null;
    if (k === "Enter") answer = btns[0][2];
    else if (k === "Escape") answer = btns[btns.length - 1][2];
    else for (const [, key, a] of btns) if (key.toLowerCase() === k) answer = a;
    if (c.kind !== "close" && k === "y") answer = "yes";
    if (c.kind !== "close" && k === "n") answer = "no";
    if (c.kind === "close" && k === "s" && !c.unsaved) answer = "save";
    if (answer){ e.preventDefault(); vAnswer(answer); }
    return;
  }
  if (e.key === "Escape"){ e.preventDefault(); cancelVersions(); return; }
  const rows = vData.list || [];
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && rows.length){
    e.preventDefault();
    vIndex = e.key === "ArrowDown" ? (vIndex + 1) % rows.length
                                   : (vIndex <= 0 ? rows.length - 1 : vIndex - 1);
    renderVersions();
    return;
  }
  if (e.key === "Enter"){
    e.preventDefault();
    const r = rows[vIndex], file = vData.file || {};
    if (r && r.n !== file.version) vOpen(r.n);
  }
}

/* ── The per-row sheet ────────────────────────────────────────────────────────
   Right-click a row for its shortcut and where it shows in search — drawn by the
   shared fusion_ui/search.js, which REPLACES the list in the same panel rather than
   floating (this window is a fixed rectangle, see WIN_H in main.swift, so a floating
   menu would need clamping a panel swap does not). What is Fusion's here: where the
   current chord comes from, and how a new one is set.

   Setting the chord goes through Control Room's OWN write path — POST 8735/fusion/set,
   the same endpoint and body shape fusionSave() sends (remap/index.js) — not a second
   implementation of "how a Fusion shortcut gets set". The sheet only captures the chord
   and hands it off; remapctl, the anchor handoff and Fusion's own modal prompt are
   exactly what Control Room already uses. */

function isSheetOpen(){ return sKit.isSheetOpen(); }
function closeRowSheet(){ sKit.closeSheet(); }

async function sPostFusion(path, body){
  return SearchKit.postJSON(FUSION_API + path, body);
}

// This command's row in Control Room's Fusion list (null if it isn't running).
async function sLookupRow(cmd){
  const d = await (await fetch(FUSION_API + "/fusion?custom=1&native=1",
                               { cache: "no-store" })).json();
  return (d.rows || []).find(r => r.id === cmd.id) || null;
}

/* The sheet asks for the width its widest row needs — laid out once at max-content to
   measure — and onSheetClose puts the palette back to SEARCH_W. Height needs nothing:
   the window is a fixed rectangle with room below the panel (see sizeWindow), but the
   new height is still reported so a sheet near the screen's bottom edge gets lifted. */
function sFitSheet(sheetEl, isOpen){
  if (mode !== "search" || typeof getComputedStyle !== "function") return;
  const prev = sheetEl.style.width;
  sheetEl.style.width = "max-content";
  const need = Math.ceil(sheetEl.getBoundingClientRect().width);
  sheetEl.style.width = prev;
  const cs = getComputedStyle(sPanel);
  const chrome = parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) || 0;
  askWidth(Math.max(SEARCH_W, need + chrome));
  requestAnimationFrame(() => { if (isOpen()) sizeWindow("keep"); });
}

/* Set or Clear (`chord === ""`). Mirrors remap/index.js's fusionSave exactly — same
   endpoint, same body shape, same anchor handoff — because this is Control Room's
   write path reused, not a second one. `held` is always empty: the palette never
   offers a space/finger reach, so there is none to send; a row that HAD one is what
   the failure branch restores. */
async function sSetKey(cmd, chord, row){
  const sc = SearchKit.splitChord(chord);
  const body = {
    cmd_id: cmd.id, key: sc.key, mods: sc.mods, held: [],
    space: false, fingers: 0,
    when: row.when || {}, typing: !!row.typing, master: null,
  };
  let res;
  try { res = await sPostFusion("/fusion/set", body); }
  catch (e){ res = { ok: false, error: String(e) }; }

  if (res && res.needs_anchor){
    // Fusion's own modal prompt needs Fusion frontmost to take a keystroke — hand
    // over exactly the way hideWindow always does (hideDialog -> activateFusion in
    // main.swift), wait for it to land, then drive the plan. No new Swift handler:
    // the session is never closed server-side by this, only hidden.
    hideWindow();
    await new Promise(r => setTimeout(r, 150));
    try { res = await sPostFusion("/fusion/anchor", { plan: res.needs_anchor }); }
    catch (e){ res = { ok: false, error: String(e) }; }
    if (!(res && res.ok)){
      // Fusion would not take it, so the claim /fusion/set already wrote names a key
      // that fires nothing — put back what was there before, same as fusionSave does.
      const t = row.trigger || {};
      try {
        await sPostFusion("/fusion/set", {
          cmd_id: cmd.id, key: t.key || "", space: t.kind === "space",
          fingers: t.fingers || 0, mods: t.mods || [],
          held: t.kind === "space" ? ["space"] : (t.fingers ? ["finger" + t.fingers] : []),
          when: row.when || {},
        });
      } catch (e2) { /* best effort — the row still shows what /fusion actually holds */ }
    }
    // Reopen at the cursor's current spot — this session was never closed, only
    // hidden for the prompt, so isOpen/mode and the open sheet are exactly as they were.
    if (sKit.sheetCmd === cmd){ sizeWindow(); sQueryEl.focus(); }
  }
  return res;
}

/* ── Session lifecycle ───────────────────────────────────────────────────────── */
/* The situation parameter naming reads (ParamNaming.fits): axis/end, every axis within
   45° and which end, the datum and the bound faces sitting at the origin, symmetry.
   Applied by every session that opens — a panel that sends none RESETS it, so the last
   dimension's situation can never leak into an Extrude (it hid the very names that
   fit: "Boot Ceiling Top Inset" dropped by a stale axis). `s` may be a session or a
   bare situation object. */
function applyNamingSituation(s){
  s = s || {};
  axis = s.axis || "";
  end   = s.end   || "";
  // Every world axis this dimension is within 45° of (45° included — see
  // namingSituation()), and which end on each. Falls back to the single axis/end
  // above when the add-in doesn't send them, same as ParamNaming.fits() does.
  situAxes = Array.isArray(s.axes) ? s.axes.slice() : (axis ? [axis] : []);
  situEnds = (s.ends && typeof s.ends === "object") ? Object.assign({}, s.ends)
           : (axis ? { [axis]: end } : {});
  datum     = s.datum || "";
  datumFaces = Array.isArray(s.datumFaces) ? s.datumFaces.map(f => String(f).toLowerCase()) : [];
  symmetric = !!s.symmetric;
  invalidateEntries();
}
function openSession(s){
  isOpen = true; setCommitting(false);
  acFiltering = true;
  showPanel("value");
  // Undo whatever the plane box did to this panel — it is the same one.
  nameEl.style.display = "";
  fName.classList.remove("readout", "hassame");
  fAngle.classList.add("hide");
  fSName.classList.add("hide");
  sNameEl.value = "";
  infoEl.textContent = "";
  valueEl.placeholder = "Value";
  datums = []; curDatum = null; redefine = false; flat = false; sketchToken = "";
  sessionSketches = []; hidePick(); dropPickCycle();
  // Both are edited in Control Room ▸ Fusion while this window is closed, so both are
  // re-read whenever a session opens — an edit there shows up in the steps you are
  // about to take, rather than after a relaunch. Unreachable keeps the last good one.
  loadParamExempt();
  loadParamNaming();
  unit = s.unit || "";
  applyNamingSituation(s);
  invalidateEntries();
  mode = s.mode || "dim";
  dTitleEl.textContent = dialogTitle(mode, s);
  nameRequired = !!s.nameRequired;
  origName = s.name || "";
  // The name carries the session in a parameter session, so say so in the field rather
  // than letting an empty box look optional the way it is for a dimension.
  nameEl.placeholder = nameRequired ? "Name (required)" : "Name";

  isDriven = !!s.isDriven;
  $("driven").className = isDriven ? "" : "none";
  fFlex.classList.toggle("hide", !(mode === "dim" || mode === "param" || mode === "editparam"));
  setFlex(s.flex || null);

  valueEl.value = tidyBrackets(stripUnit(s.expression || "", unit));
  // A dimension placed just now, or one reading to more decimals than anyone types,
  // holds only where the pointer let go: shown, but selected so typing replaces it.
  const arbitraryMeasure = mode === "dim" && !!valueEl.value &&
                           (!!s.created || isArbitraryMeasure(valueEl.value));
  nameEl.value  = s.name || "";
  fName.classList.remove("bad");
  clearError();
  acItems = []; acIndex = -1; renderAC();
  prevEl.textContent = "";

  // The name stepper. A brand-new parameter ('param'/'dim') gets it whenever the
  // add-in could say which component it belongs to; an 'editparam' session gets it
  // only when the name it already has parses under the convention — an old-convention
  // name is left as the plain field it always was, editable exactly as before.
  sessionComponent = s.component || "";
  sessionComponentPath = Array.isArray(s.componentPath) ? s.componentPath.slice() : [];
  sessionComponents = Array.isArray(s.components) ? s.components.slice() : [];
  sessionSymmetry  = s.symmetry  || {};
  stepAcClosed = false;
  stepTyping = false;
  stepFinding = false;
  stepInsert = null;
  stepEdit = null; stepChipEdited = false; stepRenaming = null; pendingRetarget = "";
  stepMulti = []; stepMultiAnchor = "";
  stepLegacy = ""; stepLegacyPrev = "";
  sessionFiled = (s.filed && typeof s.filed === "object") ? s.filed : null;
  stepWide = false;
  stepTypeGroup = false;
  stepLastPin = null;
  stepShow = {}; stepShowHad = false;
  if (mode === "editparam" && s.name) loadStepShow(s.name);
  clearStepTitleError();
  const existing = (mode === "editparam" && paramNamingReady)
    ? ParamNaming.parse(s.name || "") : null;
  // One that doesn't parse gets it too, its name standing whole as one chip
  // (stepLegacy) until that chip is clicked or deleted.
  const knowsOwner = !!sessionComponent || sessionComponentPath.length > 0;
  const legacy = mode === "editparam" && paramNamingReady && existing === null &&
                 !!(s.name || "").trim() && knowsOwner;
  stepperOn = paramNamingReady &&
    (existing !== null || legacy || ((mode === "param" || mode === "dim") && knowsOwner));
  if (stepperOn){
    if (existing){
      fillOwner(existing.levels[0]);
      stepOwnerSet = true;
      // A level equal to the type word (typeGroupAt) is "Make as group"'s own doing —
      // pull it back out of the levels and let the toggle stand for it again, the same
      // way stepFinalLevels puts it back in on compose.
      const tgAt = ParamNaming.typeGroupAt(existing);
      stepTypeGroup = tgAt >= 0;
      stepLevels = tgAt >= 0
        ? existing.levels.slice(0, tgAt).concat(existing.levels.slice(tgAt + 1))
        : existing.levels.slice();
      // Its pin stands as this session's own. A type level appended after the last
      // (Make as group on a pinned group) was the pin, the name carrying none.
      if (existing.lastIs) stepLastPin = { key: chainKey(stepLevels), is: existing.lastIs };
      else if (tgAt >= 0 && tgAt === existing.levels.length - 1 && stepLevels.length >= 3)
        stepLastPin = { key: chainKey(stepLevels), is: "group" };
      stepType = existing.type;
      stepAnchors = (existing.anchors || []).slice();
      stepAxes = existing.anchors && existing.anchors.length ? [] : (existing.axes || []).slice();
      stepNone = false;
      stepStage = "done";
    } else if (legacy){
      fillOwner(null);
      stepOwnerSet = false;
      stepLevels = []; stepType = null; stepAnchors = []; stepAxes = []; stepNone = false;
      stepTypeGroup = false;
      stepLegacy = s.name.trim();
      stepStage = "done";
    } else {
      // The owner is a raw Fusion component name — "Front Panel", spaces and all — so
      // level 0 goes through the same sanitiser a typed level is gated by (NAME_RE
      // below), or a component name with a space commits a name Fusion rejects.
      fillOwner(null);
      stepOwnerSet = false;
      stepLevels = [stepOwner()];
      stepType = null; stepAnchors = []; stepAxes = []; stepNone = false;
      stepTypeGroup = false;
      stepStage = "parent";
    }
    nameEl.value = "";
  }
  // Only a brand-new name takes the add-in's proposal (see stepProposal).
  stepProposal = (stepperOn && !existing && !legacy && s.proposal && s.proposal.type)
    ? s.proposal : null;
  stepProposed = false;
  doppelName = "";
  renderSteps();

  autosize();
  // The box was filled by assignment, which fires no input event, so the items in the
  // prefilled expression have to be painted explicitly.
  refreshChips();
  // A new session is a new box: ⌘Z must not walk back into the LAST dimension's
  // expression. The only place this is thrown away — every other refreshChips() above
  // is the page rewriting the box within a session, which stays undoable.
  if (typeof Chips !== "undefined"){ try { Chips.resetHistory(valueEl); } catch (_) {} }
  fitWidth();
  // Evaluate what is already in the box. Without this the result stayed blank until the
  // first keystroke, even though there was a perfectly good expression sitting there:
  // schedulePreview only ever ran off the input event.
  schedulePreview();
  // Whatever is already there — a measured "400" as much as a real expression — opens
  // as chips with the caret at its end and nothing held, ready for a keystroke starting
  // the next item beside the last. (The bounds and extrude fields still arrive with a
  // bare literal selected as text — see selectPrefilled.) The exception is a dimension
  // showing only where the pointer let go (isArbitraryMeasure): it is still there to
  // read or copy, but selected as text so the first keystroke replaces it.
  // A DRIVEN dimension is the exception: its value is the geometry's, shown to read, and
  // naming it is the whole session — so the keyboard starts in the name stepper.
  drivenExpr = isDriven ? exprText() : "";
  openSelectAll = arbitraryMeasure && !isDriven;
  if (isDriven) focusField(nameEl);
  else {
    valueEl.focus();
    if (arbitraryMeasure) selectAllAsText(valueEl);
    else if (typeof Chips === "undefined" || !Chips.caretAtEnd(valueEl))
      valueEl.setSelectionRange(valueEl.value.length, valueEl.value.length);
  }
  // Focusing a field inside a page whose content overflows can scroll it; the panel
  // must stay pinned to the top of the window.
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  refreshFocusStyle();

  host("session", { open: true, place: s.place || "cursor",
                    width: Math.round(Math.max(baseWidth,
                                               Math.min(textWidth(valueEl), WIDTH_MAX))),
                    height: Math.ceil(panel.getBoundingClientRect().height) });
}

async function poll(){
  try {
    const s = await (await fetch(API + "/state", { cache: "no-store" })).json();
    // This window reads index.html once, at launch, so an edit to the page leaves a
    // running window serving the old one indefinitely — and reloading the add-in does
    // not help. Rather than remembering to restart the app after every change, notice
    // it: the server reports how old the page's sources are, and a change means reload.
    // Only while NO session is open, so this can never interrupt an edit; the window is
    // idle almost all of the time, so the refresh lands long before it is next needed.
    if (uiStamp === null){
      uiStamp = s.uiStamp || 0;
    } else if (s.uiStamp && s.uiStamp !== uiStamp && !isOpen){
      location.reload();
      return;
    }

    // A changed parameter list invalidates every family derived from the old one.
    if (s.params && s.params.length !== params.length) invalidateEntries();
    params = s.params || [];
    // A group rename took this session's own parameter with it: now it exists under the
    // new name, the add-in is told to commit to that one (see "Renaming a group").
    if (pendingRetarget && params.some(p => p.name === pendingRetarget)){
      post({ action: "retarget", name: pendingRetarget });
      pendingRetarget = "";
    }

    // Tags are filed per document, so a different document is a different set of them.
    // Fetched on the change rather than polled: it is a small file that only this
    // window writes.
    if (s.doc !== undefined && s.doc !== tagDoc) loadTags(s.doc || "");

    // A counter BELOW ours is a new server under this long-lived window (Fusion or the
    // add-in restarted). The server seeds from the clock so this should not happen
    // (webwin.seq_base), but if it does, re-adopt as on a first poll rather than
    // waiting for the new count to overtake the old one — every session armed until
    // then would go unrendered while this window, still polling, looked healthy.
    if (lastSeq !== null && (s.seq || 0) < lastSeq){
      lastSeq = null;
      if (isOpen && !s.open){ isOpen = false; setCommitting(false); hideWindow(); }
    }
    if (lastSeq === null){
      // First poll adopts the current session number without acting on it: a session
      // armed while the window was still starting up belongs to that keypress, but
      // replaying an old one every time the app launches would pop the dialog for no
      // reason. Only an already-open session is honoured.
      lastSeq = s.seq || 0;
      lastErrSeq = s.errSeq || 0;
      lastMadeSeq = s.madeSeq || 0;
      lastInsertSeq = s.insertSeq || 0;
      lastPickSeq = s.pickSeq || 0;
      if (s.open) beginSession(s);
    } else if ((s.seq || 0) > lastSeq){
      lastSeq = s.seq;
      lastErrSeq = s.errSeq || 0;
      lastPickSeq = s.pickSeq || 0;       // a pick from before this session is not for it
      if (s.open) beginSession(s);
      // A newer session that is already over — or a restarted server that never had
      // ours — leaves nothing for the panel on screen to belong to.
      else if (isOpen){ isOpen = false; setCommitting(false); hideWindow(); }
    } else if (isOpen && !s.open){
      // Fusion closed the session (a successful commit, or the target went away).
      isOpen = false; setCommitting(false);
      hideWindow();
    }

    if (isOpen && (s.errSeq || 0) > lastErrSeq){
      lastErrSeq = s.errSeq;
      if (s.error) showError(s.error);
    }

    // "Make a parameter": Fusion made the one this panel asked for. Checked after the
    // params line above, which already took the fresh list set_made sent with it.
    // The component path a make panel asked for (makeStepperOpen).
    if (lastMakeInfoSeq === null || (s.makeInfoSeq || 0) > lastMakeInfoSeq){
      const fresh = lastMakeInfoSeq !== null;
      lastMakeInfoSeq = s.makeInfoSeq || 0;
      if (fresh && makeInfoWait && making){ makeInfoWait = false; makeStepperStart(s.makeInfo || {}); }
    }

    if ((s.madeSeq || 0) > lastMadeSeq){
      lastMadeSeq = s.madeSeq;
      if (making && makePending && s.made === makePending) finishMake(s.made, s.madeEdit);
    }

    if (lastInsertSeq !== null && (s.insertSeq || 0) > lastInsertSeq){
      lastInsertSeq = s.insertSeq;
      insertParam(s.insert);
    }

    if (lastPickSeq !== null && (s.pickSeq || 0) > lastPickSeq){
      lastPickSeq = s.pickSeq;
      if (isOpen && mode === "sketch") applyPick(s.pick || {});
      else if (isOpen && mode === "dim" && !making) applyDimPick(s.pick || {});
    }

    // Fusion's Super Escape: Close Whole Dialog. The first poll only adopts the count.
    if (lastEscSeq !== null && (s.escSeq || 0) > lastEscSeq) closeWholeDialog();
    lastEscSeq = s.escSeq || 0;

    if (isOpen && s.previewFor && s.previewFor === previewExpr()){
      prevEl.textContent = previewEchoes(s.previewFor) ? "" : trimZeros(s.preview || "");
    }

    // The operation Fusion's rules landed on, and whether the direction rule found a
    // bound. Pushed WITHOUT a new seq — see server.update_extrude — precisely so that
    // it lands here rather than re-arming the session under whatever is being typed.
    if (isOpen && mode === "extrude") applyExtrudeUpdate(s.extrude);
    if (isOpen && mode === "mirror") applyMirrorUpdate(s.mirror);
    if (isOpen && mode === "feature") applyFeatureUpdate(s.feature);
    if (isOpen && mode === "versions") applyVersionsUpdate(s.versions);
  } catch (_) { /* server not up yet — keep polling */ }
}
/* ── Restoring focus after a trip to the viewport ────────────────────────────
   Orbiting or panning activates Fusion and takes the keyboard with it. The host watches
   for the end of that (main.swift's reclaimIfLost) and calls __refocus,
   which puts the caret back exactly where it was rather than in whichever field happens
   to be first.

   Tracked CONTINUOUSLY rather than captured on blur. Deactivating an app does not
   reliably fire `blur` on the focused element — WebKit just stops the document being
   focused — so a blur-based capture never ran in the one case it exists for, and the
   caret always came back at position 0 of the value field.

   ANY editable text field counts, not a fixed list — the bounds panel's name and face
   fields, the extrude distance, +Param's name, the component finder and the search
   sheet's rename all have to come back too. A read-only input (the chord-capture box)
   is not typing and is left out, as are checkboxes and buttons. */
let lastFocus = null, lastSel = [0, 0];
const NON_TEXT = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"]);
function isTextField(el){
  if (!el) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === "TEXTAREA") return !el.readOnly && !el.disabled;
  return el.tagName === "INPUT" && !el.readOnly && !el.disabled && !NON_TEXT.has(el.type);
}
// …except the activation reset itself: a collapse to 0 arriving while the window is
// away or just back, before the user has touched anything, is WebKit's, not a place
// anyone put the caret — recording it made __refocus faithfully restore the reset.
let userSinceFocus = true;
window.addEventListener("focus", () => { userSinceFocus = false; });
window.addEventListener("blur",  () => { userSinceFocus = false; });
for (const ev of ["keydown", "mousedown"])
  document.addEventListener(ev, () => { userSinceFocus = true; }, true);
function trackFocus(){
  const el = document.activeElement;
  if (!isTextField(el)) return;
  let a, b;
  try { a = el.selectionStart; b = el.selectionEnd; } catch (_) {}
  if (!userSinceFocus && a === 0 && b === 0 && el.value && lastFocus === el) return;
  lastFocus = el;
  lastSel = [a, b];
}
document.addEventListener("selectionchange", trackFocus);
for (const ev of ["focusin", "input", "keyup", "click"]) document.addEventListener(ev, trackFocus, true);
// Still on screen and still usable — a field from a panel that has since been swapped
// out (or a session since closed) must not pull the caret into nowhere.
function canRefocus(el){
  return isTextField(el) && el.isConnected && el.getClientRects().length > 0;
}

/* Coming back from an orbit or a pan. The field goes live again so typing can carry on
   — but a whole-field selection is NOT what to come back to: it would mean the next
   keystroke wiped an expression you had only stepped away from. So the caret comes back
   on the chip that was HELD, and otherwise at the end, which is where the next item goes
   either way.

   The search palette owns the keyboard differently — one field, no chips — so it gets
   its own branch rather than running through refreshFocusStyle(), which only knows the
   value box's two fields. A click in the viewport while the palette is up must land
   back in #squery, not in whichever of the value box's fields happened to focus last.

   Whatever it lands on is then GUARDED (guardCaret, top of the file): the activation
   reset can arrive after this has run, and would otherwise win. lastSel is copied
   first — focus() fires focusin, and trackFocus would overwrite it with the very
   selection being corrected. */
window.__refocus = () => {
  if (!isOpen) return;
  // The exception: a value the session opened selected (openSelectAll) and nobody has
  // touched yet is not an expression stepped away from — it comes back selected.
  if (openSelectAll && canRefocus(valueEl)){
    valueEl.focus();
    selectAllAsText(valueEl);
    guardCaret(valueEl);
    refreshFocusStyle();
    return;
  }
  // A guard still armed knows better than lastSel — see caretGuard, top of the file.
  const g = caretGuard;
  if (g && g.el.isConnected && canRefocus(g.el)){ lastFocus = g.el; lastSel = [g.a, g.b]; }
  const sel = lastSel.slice();
  refocusNow(sel);
  if (isOpen) guardCaret(document.activeElement);
};
/* A click the page never got. WKWebView drops a mousedown that arrives while this app is
   inactive, and the window is inactive whenever the last thing you touched was the
   viewport — so clicking straight from a pick into "Distance, other side" landed nowhere,
   and the reclaim that followed put the caret back in whichever field it had been guarding
   (Distance). The host sees that click natively (main.swift's lost-click monitor) and hands
   its point in here: the text field under it gets the focus the click was for, and becomes
   the field every later __refocus returns to. Only text fields — focusing one twice is
   harmless, whereas replaying a click on a toggle could undo the one that did land. */
window.__clickAt = (x, y) => {
  if (!isOpen) return;
  let el = document.elementFromPoint(x, y);
  // The field itself, or the row it sits in (a chip layer is drawn over the input).
  for (let i = 0; el && i < 4 && !isTextField(el); i++){
    const inside = el.querySelectorAll ? [...el.querySelectorAll("input, textarea")].filter(isTextField) : [];
    if (inside.length === 1){ el = inside[0]; break; }
    el = el.parentElement;
  }
  if (!isTextField(el) || !canRefocus(el)) return;
  caretGuard = null;
  userSinceFocus = true;
  if (document.activeElement !== el) el.focus();
  trackFocus();
  refreshFocusStyle();
};
function refocusNow(lastSel){
  // Any field other than the value box's own: back to it, caret where it was.
  if (lastFocus && lastFocus !== valueEl && lastFocus !== nameEl && lastFocus !== angleEl &&
      canRefocus(lastFocus)){
    lastFocus.focus();
    try { lastFocus.setSelectionRange(lastSel[0], lastSel[1]); } catch (_) {}
    return;
  }
  if (mode === "search"){
    sQueryEl.focus();
    sQueryEl.setSelectionRange(sQueryEl.value.length, sQueryEl.value.length);
    return;
  }
  if (lastFocus === angleEl && canRefocus(angleEl)){
    angleEl.focus();
    try { angleEl.setSelectionRange(lastSel[0], lastSel[1]); } catch (_) {}
    return;
  }
  // A panel with no value box on screen (bounds, extrude): its first field.
  if (!canRefocus(valueEl)){
    const box = isBoundsPanelMode() ? cPanel : mode === "extrude" ? xPanel
              : mode === "mirror" ? rPanel : mode === "feature" ? oPanel : panel;
    const first = [...box.querySelectorAll("input, textarea")].find(canRefocus);
    if (first) first.focus();
    return;
  }
  const el = (lastFocus === nameEl) ? nameEl : (acField || valueEl);
  el.focus();
  try {
    const oneChip = typeof Chips !== "undefined" && el !== nameEl &&
                    lastSel[0] !== lastSel[1] && Chips.isItemRange(el, lastSel[0], lastSel[1]);
    // A plain caret (nothing held) comes back where it was — a Browser click that
    // just put a name in mid-expression (insertParam) left it there on purpose.
    const caret = el !== nameEl && lastFocus === el && lastSel[0] === lastSel[1] &&
                  lastSel[0] != null && lastSel[0] <= el.value.length;
    if (oneChip) el.setSelectionRange(lastSel[0], lastSel[1]);
    else if (caret) el.setSelectionRange(lastSel[0], lastSel[0]);
    else el.setSelectionRange(el.value.length, el.value.length);
  } catch (_) {}
  refreshFocusStyle();
};

/* ── Dragging ────────────────────────────────────────────────────────────────
   The window is borderless, so there is no title bar to grab and the web content
   swallows the events that would otherwise let AppKit move it by its background.
   Instead the page reports press and release, and the host slides the window to
   follow the cursor for as long as the button is down.

   Anywhere on the panel works except the parts you interact with — the two fields and
   the suggestion rows — so there is no small strip to aim for. preventDefault keeps
   the press from stealing the caret out of whichever field has it. */
let dragging = false;
function startDrag(e){
  if (e.button !== 0) return;
  // .xopt and #xadvhead are the extrude panel's own clickable bits. They preventDefault
  // but do not stop propagation, so without them here every choice you clicked also
  // grabbed the window and dragged it out from under the pointer. #same/#pick are the
  // "Same as..." trigger and its list — already stopPropagation of their own, but a
  // press that misses a row and lands on #pick's own padding still shouldn't drag.
  // #ssheet is the search palette's per-row sheet — its checkboxes are already
  // <input>, but a press on the plain text/labels around them shouldn't drag either.
  // select: the sheet metal rule picker — a preventDefault here stops its menu opening.
  // .fbtn is a panel's own Back/Save pair — same story as .xopt: they preventDefault
  // but do not stop propagation, and a button that also dragged the window out from
  // under the pointer is not a button.
  if (e.target.closest("input, textarea, select, .ac-row, .xopt, #xadvhead, #radvhead, #oadvhead, .grip, #same, #pick, #ssheet, .fbtn")) return;
  dragging = true;
  host("drag", true);
  e.preventDefault();
}
panel.addEventListener("mousedown", startDrag);
cPanel.addEventListener("mousedown", startDrag);
xPanel.addEventListener("mousedown", startDrag);
// The make-a-parameter panel too — it has a header of its own to grab by (empty now,
// and sized like a field for exactly that reason), and it was never wired up.
mPanel.addEventListener("mousedown", startDrag);
// Mirror, the shared feature panel (Array/Combine/Scale) and the search palette —
// same story, just never wired up.
rPanel.addEventListener("mousedown", startDrag);
oPanel.addEventListener("mousedown", startDrag);
sPanel.addEventListener("mousedown", startDrag);
window.addEventListener("mouseup", () => {
  if (!dragging) return;
  dragging = false;
  host("drag", false);
});

/* The host calls this when the window is dismissed from outside the page. */
window.__cancel = cancel;

/* The host calls this on a left/right press outside the panel (main.swift's mouse-down
   monitors). Only the command-search palette closes on it — every other mode is an edit
   in progress, and a click into the viewport there is part of the work. */
window.__clickOutside = () => { if (isOpen && mode === "search") cancelSearch(); };

/* The variable vocabulary, fetched once. Polling starts regardless and does not wait
   for it: a session that opens in the first few milliseconds gets plain name completion
   and picks up families on the next keystroke, which is a far better failure than a
   dialog that will not appear because a static file was slow. */
loadNaming();
loadParamNaming();
loadParamExempt();
attachAC(valueEl, fValue);          // the value box owns it until something else asks
chipsAttach(valueEl);

// A paste whose ENTIRE text is exactly one real parameter's name (see fusion_browser's
// ⌘C, the paired half of this) becomes a proper chip — a REFERENCE, via the same
// Chips.accept() the autocomplete list uses (see acceptAC) — rather than plain text
// that merely happens to spell a name right. This is what "insert the parameter
// itself, preserving the dependency" means: a chip IS that reference. Anything else
// pastes exactly as it always did — capitalise.js already leaves paste alone, for the
// same reason. Split out from the event listener so a test can call it directly with
// a bare string, without needing a real ClipboardEvent.
function pasteAsChip(text){
  if (typeof Chips === "undefined") return false;
  const name = (text || "").trim();
  if (!name || !params.some(p => p.name === name)) return false;
  Chips.accept(valueEl, valueEl.selectionStart, valueEl.selectionEnd, name, "param");
  // Chips.accept() sets .value directly, which fires no input event — same follow-up
  // acceptAC() does after it, for the same reason.
  autosize(); schedulePreview();
  return true;
}
valueEl.addEventListener("paste", e => {
  const cd = e.clipboardData || window.clipboardData;
  if (pasteAsChip(cd ? cd.getData("text/plain") : "")) e.preventDefault();
});

/* A DICTATED expression (Wispr Flow pastes what was said as one paste): "half the depth
   plus ten" goes in as Depth / 2 + 10, the words matched to real parameters by
   fusion_ui/spoken_expr.js against the same readings the suggestion list shows (owner
   first, a bounds face as "Nose - Right"), the session's own component winning a tie.
   Every chip expression field (chipsAttach), after pasteAsChip has had its turn. A paste
   that is already an expression (only real names, digits, symbols) is left alone; words
   that match nothing stay in as one word, for the live evaluation to flag. */
function spokenCandidates(){
  return entries().filter(e => !e.stem).map(e => {
    const parts = paramNamingReady ? ParamNaming.parse(e.target) : null;
    return { name: e.target, reading: e.shown || "", readingOnly: e.readingOnly,
             owner: parts ? parts.levels[0] : "" };
  });
}
function spokenOwners(){
  // Nearest first: the session's component, then up its path to the root.
  return [sessionComponent].concat(sessionComponentPath.slice().reverse()).filter(Boolean);
}
function pasteSpoken(el, text){
  if (typeof SpokenExpr === "undefined" || typeof Chips === "undefined") return false;
  const r = SpokenExpr.convert(text || "", spokenCandidates(), { owners: spokenOwners() });
  if (!r.spoken || !r.expr) return false;
  Chips.replaceSpan(el, el.selectionStart, el.selectionEnd, r.expr);
  // Its input event is what a panel row acts on; the value box also re-sizes and previews.
  if (el === valueEl){ autosize(); fitWidth(); schedulePreview(); }
  return true;
}
document.addEventListener("paste", e => {
  if (e.defaultPrevented || !spokenFields.has(e.target)) return;
  const cd = e.clipboardData || window.clipboardData;
  try { if (pasteSpoken(e.target, cd ? cd.getData("text/plain") : "")) e.preventDefault(); }
  catch (_) {}          // a matcher fault must never cost the plain paste
});

for (const box of [panel, cPanel, xPanel]) attachGrip(box);

setInterval(poll, 60);
poll();

/* The fast path for OPENING. /wait is held by the server until `seq` passes ours, so a
   session armed by a keypress (the search palette above all — you start typing straight
   after S) is picked up the instant it lands rather than on the next 60ms tick. It only
   wakes poll(); the interval above stays the source of truth for everything else, and
   keeps working alone against a server too old to know /wait (a 404 backs off). */
async function waitLoop(){
  for (;;){
    try {
      const r = await fetch(API + "/wait?seq=" + (lastSeq || 0) +
                            "&ins=" + (lastInsertSeq || 0) +
                            "&pk=" + (lastPickSeq || 0), { cache: "no-store" });
      if (!r.ok){ await new Promise(res => setTimeout(res, 5000)); continue; }
      const d = await r.json();
      if ((d.seq || 0) > (lastSeq || 0) || (d.insertSeq || 0) > (lastInsertSeq || 0) ||
          (d.pickSeq || 0) > (lastPickSeq || 0)) await poll();
    } catch (_) {
      await new Promise(res => setTimeout(res, 1000));   // server down — poll() copes
    }
  }
}
waitLoop();
