// fusion_timeline/index.js — the vertical timeline (server.py's header says what it is).
//
// Drawn by the list kit (fusion_ui/list.js — UI.md is its rulebook): this file holds
// only what the timeline is ABOUT — the tree model, what a row shows, what its verbs
// post. The model is pure functions over (rows, comps, activeRef, data), so
// test/cases.js drives it headlessly.
//
// Grep the section banners: grep -n '────' fusion_timeline/index.js

const API = "";                                   // this window's own server (8736)
// Fusion's keymap as the Browser reads it (with remapd's translations folded in) —
// fetched from there so the two windows never spell a binding twice.
const BROWSER_API = "http://127.0.0.1:8732";
const { seqOf, normSeq, seqLabel } = Keys;

const OCC_TYPE = "adsk::fusion::Occurrence";
const SKETCH_TYPE = "adsk::fusion::Sketch";

// ── the tree model (pure — test/cases.js drives these directly) ───────────────
//
// rows       the timeline, in order, as the add-in sends it:
//            {key, tok, name, type, icon, compRef, compPath, health, msg, suppressed,
//             past, occTok}. key = "<compRef>|<name>", unique (the add-in suffixes #2…).
// comps      {ref: {name, path, occTok}} — every component in the active subtree;
//            `path` runs from just below the active component down to this one.
// activeRef  the active component — the list's top level.
// data       this window's own groups for the document (groups.json):
//              groups        {id: {name, parent, collapsed}} — parent is "c:<compRef>"
//                            (that component's level) or "g:<id>" (inside a group)
//              member        {rowKey: groupId}
//              compCollapsed {compRef: true} — the derived component groups' state
//
// Component groups ("cg:<ref>") are DERIVED, never stored: a sub-component's features
// sit in a group named after it, nested the way the Browser nests components. User
// groups ("ug:<id>") nest inside the active level, a component group or each other.
// A feature files only into groups in its own component's chain; anything else draws
// loose (the Browser's moveKind rule). Rows keep timeline order, and a group sits
// where its earliest member does, so it needs no contiguous span.

const ROOT = "", CG = "cg:", UG = "ug:";
const HEALTH_RANK = { "": 0, warning: 1, error: 2 };
function worse(a, b){ return (HEALTH_RANK[b] || 0) > (HEALTH_RANK[a] || 0) ? b : a; }

// Shared with the Browser's feature rows: displayName and autoGroupName / AUTO_NAME
// (what a row / an unnamed group is CALLED) in fusion_ui/tl_names.js; kindCls and the
// glyphs in fusion_ui/tl_icons.js; the group data itself (emptyData, normData,
// groupComp, newGroupId, explodeGroup, carryGroupKey) in fusion_ui/tl_groups.js.

function rowCls(r){
  return [r.health ? "h-" + r.health : "", r.suppressed ? "supp" : "", r.past ? "past" : "",
          r.type === OCC_TYPE && !isCutPaste(r) ? "k-comp" : "", kindCls(r.type)].filter(Boolean).join(" ");
}

// A cut-paste is an Occurrence row too; Fusion names its timeline object "CutPaste
// <occurrence>" (a creation's is just the occurrence, " Fuselage:1"). It MOVES a
// component, so it draws Move's glyph and isn't bold like a component coming in.
function isCutPaste(r){ return r.type === OCC_TYPE && /^\s*CutPaste\b/.test(r.name || ""); }
// The type a row's glyph is drawn for.
function glyphType(r){ return isCutPaste(r) ? "adsk::fusion::MoveFeature" : glyphKey(r.type, r.op); }

function buildModel(rows, comps, activeRef, data){
  data = normData(data);
  comps = comps || {};
  rows = rows || [];
  const nodes = {};
  const make = (id, extra) => (nodes[id] = Object.assign(
    { id, children: [], pos: Infinity, health: "", order: 0 }, extra));
  // The node a component's own rows sit in: the top level for the active component, its
  // group for one below it, null for one outside the subtree (not drawn).
  const compNode = ref => (!ref || ref === activeRef) ? ROOT : (comps[ref] ? CG + ref : null);

  make(ROOT, { kind: "root" });
  for (const ref of Object.keys(comps)){
    if (ref === activeRef) continue;
    const path = comps[ref].path || [];
    const up = path.length > 1 ? path[path.length - 2] : activeRef;
    make(CG + ref, { kind: "cg", ref, label: displayName(comps[ref].name) || ref,
                     parentId: compNode(up) == null ? ROOT : compNode(up) });
  }
  Object.keys(data.groups).forEach((gid, i) => {
    const ref = groupComp(data, gid);
    if (ref == null || compNode(ref) == null) return;      // its component is out of view
    const p = String(data.groups[gid].parent || "");
    make(UG + gid, { kind: "ug", gid, ref, order: i, label: String(data.groups[gid].name || "").trim(),
                     parentId: p.startsWith("g:") ? UG + p.slice(2) : compNode(ref) });
  });
  for (const n of Object.values(nodes))
    if (n.kind !== "root") (nodes[n.parentId] || nodes[ROOT]).children.push(n);

  rows.forEach((r, idx) => {
    // The first row of each PHASE (AddIns/Phases.py) — the add-in keeps the real
    // timeline in phase order, so a change of phase down the list is a phase line.
    const prev = rows[idx - 1];
    r.phaseEdge = typeof r.phase === "number" && !!prev && typeof prev.phase === "number"
                  && prev.phase !== r.phase;
    const home = compNode(r.compRef);
    if (home == null) return;
    const g = nodes[UG + (data.member[r.key] || "")];
    const at = g && g.ref === r.compRef ? g : nodes[home];   // wrong-side filing: loose
    at.children.push({ kind: "row", row: r, pos: idx, health: r.health || "" });
  });

  // Position = earliest member; health = worst member, like a collapsed native group.
  // Empty groups go last, component groups before user groups, then in creation order.
  const rank = n => n.kind === "row" ? 0 : n.kind === "cg" ? 1 : 2;
  const settle = n => {
    for (const c of n.children){
      if (c.kind !== "row") settle(c);
      if (c.pos < n.pos) n.pos = c.pos;
      n.health = worse(n.health, c.health);
    }
    n.children.sort((a, b) => (a.pos - b.pos) || (rank(a) - rank(b)) || (a.order - b.order));
  };
  settle(nodes[ROOT]);

  // A user group wears the icon most of its features (nested ones included) have;
  // a tie goes to the one reached first in timeline order. "" when it has none.
  const rowsIn = n => n.children.flatMap(c => c.kind === "row" ? [c.row] : rowsIn(c));
  // Counted by type (so our glyphs, which follow the type, agree with it); the pick
  // carries its row's Fusion icon for types we don't draw.
  const iconOf = n => {
    const count = new Map();
    for (const r of rowsIn(n)) if (r.icon || glyphOf(glyphType(r))){
      const t = glyphType(r), e = count.get(t) || { r, k: 0 };
      e.k++; count.set(t, e);
    }
    let best = null, most = 0;
    for (const e of count.values()) if (e.k > most){ best = e.r; most = e.k; }
    return best ? { icon: best.icon || "", type: glyphType(best) } : { icon: "", type: "" };
  };
  // A user group of one kind wears that kind's colour (nested rows counted); a mixed
  // one, and every component group, stays white.
  const kindOf = n => {
    const ks = new Set(rowsIn(n).map(r => kindCls(r.type)));
    return ks.size === 1 ? [...ks][0] : "";
  };
  // An unnamed user group is called after its features, nested ones included.
  const labelOf = c => c.kind !== "ug" || !AUTO_NAME.test(c.label) ? c.label
    : autoGroupName(rowsIn(c).map(r => r.type));

  const items = [], groups = [], sequence = [];
  const walk = (n, depth, gid) => {
    for (const c of n.children){
      if (c.kind === "row"){
        const r = c.row;
        const it = { id: r.key, label: displayName(r.name), depth, gid, row: r, title: r.msg || "",
                     cls: rowCls(r) + (r.phaseEdge ? " phase-edge" : "") };
        items.push(it); sequence.push({ item: it });
      } else {
        // Only a user group drags (its features move as one, like a native group); a
        // component's features never move as a block in the native bar either.
        const g = { id: c.id, label: labelOf(c), named: c.kind !== "ug" || !AUTO_NAME.test(c.label), depth, parent: gid, kind: c.kind, ref: c.ref,
                    gid: c.gid, health: c.health,
                    cls: [c.health ? "h-" + c.health : "", c.kind === "ug" ? kindOf(c) : ""].filter(Boolean).join(" "),
                    draggable: c.kind === "ug",
                    ...(c.kind === "ug" ? (o => ({ icon: o.icon, iconType: o.type }))(iconOf(c))
                                        : { icon: "", iconType: "" }),
                    collapsed: c.kind === "cg" ? !!data.compCollapsed[c.ref]
                                               : !!data.groups[c.gid].collapsed };
        groups.push(g); sequence.push({ group: g });
        walk(c, depth + 1, c.id);
      }
    }
  };
  walk(nodes[ROOT], 0, "");
  return { items, groups, sequence, health: nodes[ROOT].health };
}

// ── group edits (pure, on `data`) ─────────────────────────────────────────────
// The stored parent for a model node id: "" → the active level, cg:ref, ug:id.
function parentFor(nodeId, activeRef){
  if (!nodeId) return "c:" + activeRef;
  if (nodeId.startsWith(CG)) return "c:" + nodeId.slice(CG.length);
  if (nodeId.startsWith(UG)) return "g:" + nodeId.slice(UG.length);
  return null;
}

// An empty group inside a node (the kit's New Group Inside; ⌘G on an open group).
function addGroupInside(data, nodeId, activeRef){
  const parent = parentFor(nodeId, activeRef);
  if (!parent) return null;
  const id = newGroupId(data);
  data.groups[id] = { name: "", parent };          // unnamed: called after its contents
  return id;
}

// ⌘G around rows and/or user groups, all of one component. The new group sits in the
// one container they share, or at their component's level when they came from several.
// Returns {id} or {error}.
function groupAround(model, data, rowKeys, groupIds, activeRef){
  const its = rowKeys.map(k => model.items.find(i => i.id === k)).filter(Boolean);
  const gs = groupIds.map(id => model.groups.find(g => g.id === id)).filter(Boolean);
  if (gs.some(g => g.kind !== "ug")) return { error: "A component's group can't be put in a group." };
  if (!its.length && !gs.length) return { error: "Nothing to group." };
  const refs = new Set(its.map(i => i.row.compRef || activeRef).concat(gs.map(g => g.ref)));
  if (refs.size !== 1) return { error: "Features from different components can't share a group." };
  const ref = [...refs][0];
  const homes = new Set(its.map(i => i.gid).concat(gs.map(g => g.parent)));
  const home = homes.size === 1 ? [...homes][0] : "";
  const parent = home.startsWith(UG) ? "g:" + home.slice(UG.length) : "c:" + ref;
  const id = newGroupId(data);
  data.groups[id] = { name: "", parent };          // unnamed: called after its contents
  for (const i of its) data.member[i.id] = id;
  for (const g of gs) data.groups[g.gid].parent = "g:" + id;
  return { id };
}

// ── dragging (pure) ───────────────────────────────────────────────────────────
// A drag is {items: [rowKey], groups: [model group id]} (the kit's). What it MOVES in
// Fusion is rows: a user group stands for every row under it. Where it lands is a
// row to go before ("" = the end of the timeline) — Fusion's canReorder decides if
// that's allowed (the add-in's probe), never this file. Filing is the window's own
// and follows the drop spot, as dropping inside a native group puts a feature in it.

// Every group id at or under these, and the rows filed there, in timeline order.
function under(model, groupIds){
  const ids = new Set(groupIds);
  for (let grew = true; grew; ){
    grew = false;
    for (const g of model.groups)
      if (!ids.has(g.id) && ids.has(g.parent)){ ids.add(g.id); grew = true; }
  }
  return { ids, items: model.items.filter(i => ids.has(i.gid)) };
}

// The rows a drag moves, each once, in timeline order (model.items is that order).
function dragRows(model, d){
  const keys = new Set(d.items || []);
  for (const i of under(model, d.groups || []).items) keys.add(i.id);
  return model.items.filter(i => keys.has(i.id)).map(i => i.row);
}

// Where a kit drop target lands: {before: tok | "", home: model node id} — `home` is
// the group the dropped things are filed into — or {into: node id} for a heading's
// lower half (filing only, no move). null: nowhere (an empty group has no row to go
// before).
function dropSpot(model, got){
  if (!got) return null;
  if (got.end) return { before: "", home: "" };
  if (got.into !== undefined) return { into: got.into };
  const b = got.before || {};
  if (b.item !== undefined){
    const it = model.items.find(i => i.id === String(b.item));
    return it ? { before: it.row.tok, home: it.gid } : null;
  }
  if (b.group !== undefined){
    const g = model.groups.find(x => x.id === String(b.group));
    const first = g && under(model, [g.id]).items[0];
    return first ? { before: first.row.tok, home: g.parent } : null;
  }
  return null;
}

// Is filing into node `home` legal for this drag? Every row and group must be of the
// home's component, and no group may go inside itself.
function canFileInto(model, d, home, activeRef){
  const g = model.groups.find(x => x.id === home);
  const ref = !home ? activeRef : g ? g.ref : null;
  if (ref == null) return false;
  if (under(model, d.groups || []).ids.has(home)) return false;
  const its = (d.items || []).map(k => model.items.find(i => i.id === k)).filter(Boolean);
  const gs = (d.groups || []).map(id => model.groups.find(x => x.id === id)).filter(Boolean);
  return its.every(i => (i.row.compRef || activeRef) === ref) && gs.every(x => x.ref === ref);
}

// File the dragged rows/groups at `home` (a model node id). Where that isn't legal for
// one of them, it goes loose in its own component — the spot it was dropped at still
// shows it there, since a row is drawn where it happened.
function fileAt(model, data, d, home, activeRef){
  const hg = model.groups.find(x => x.id === home);
  const inside = under(model, d.groups || []).ids;
  for (const k of d.items || []){
    const it = model.items.find(i => i.id === k);
    if (!it) continue;
    if (hg && hg.kind === "ug" && hg.ref === (it.row.compRef || activeRef)) data.member[k] = hg.gid;
    else delete data.member[k];
  }
  for (const id of d.groups || []){
    const g = model.groups.find(x => x.id === id);
    if (!g || g.kind !== "ug" || !data.groups[g.gid]) continue;
    data.groups[g.gid].parent = hg && hg.kind === "ug" && hg.ref === g.ref && !inside.has(home)
      ? "g:" + hg.gid : "c:" + g.ref;
  }
}

// A rename made ELSEWHERE (Fusion's own timeline, the Browser) arrives as the same
// entity token under a new key — the Browser's rebuildKeyIndex idea.
function carryByToken(data, prevRows, rows){
  const byTok = {};
  for (const r of prevRows || []) if (r.tok) byTok[r.tok] = r.key;
  const now = new Set((rows || []).map(r => r.key));
  let changed = false;
  for (const r of rows || []){
    const old = r.tok && byTok[r.tok];
    if (old && old !== r.key && !now.has(old)) changed = carryGroupKey(data, old, r.key) || changed;
  }
  return changed;
}

// ── state + networking ────────────────────────────────────────────────────────
let rows = [], comps = {}, activeRef = "", docId = "", compIcon = "";
let phaseNames = [];            // Phases.PHASES, from /state
let data = emptyData();
let groupsDoc = null;           // the doc whose groups are loaded (null: none yet)
let groupsRev = null;           // the server's groups revision our copy matches
let model = buildModel([], {}, "", data);
let lastJSON = "";
let connected = false;
const msgOpen = new Set();
let drag = null;                // the drag under way: {toks, probe, stale}
let probeId = 0;
let lastNoticeAt = null;        // the add-in's last refusal shown (null: none seen yet)      // row keys whose error/warning message is unfolded
const LIST = ListKit.state("fusion/timeline");

function post(body){
  fetch(API + "/cmd", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).catch(() => {});
}

async function loadGroups(doc, rev){
  groupsDoc = null;
  groupsRev = rev == null ? null : rev;
  try {
    const all = await (await fetch(API + "/groups", { cache: "no-store" })).json();
    data = normData(((all && all.byDoc) || {})[doc]);
  } catch (_) {
    data = emptyData();
  }
  groupsDoc = doc;
  refresh();
}
function saveGroups(){
  // Never before this doc's groups have loaded: saving then would write the empty
  // overlay over the real one (the Browser lost its folders exactly that way once).
  if (groupsDoc === null || groupsDoc !== docId || !docId) return;
  fetch(API + "/groups", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ docId, data }) })
    .then(r => r.json()).then(r => { if (r && typeof r.rev === "number") groupsRev = r.rev; })
    .catch(() => {});
}

// Actions the add-in forwarded (a Browser hotkey pressed in the viewport while this
// panel shows — HotkeyStartingHandler in Browser.py). Same contract as the Browser's.
let lastUiSeq = null;
let forwardedDepth = 0;
function runForwardedActions(s){
  if (lastUiSeq === null){ lastUiSeq = s.uiSeq || 0; return; }
  if (typeof s.uiSeq === "number" && s.uiSeq < lastUiSeq) lastUiSeq = 0;   // server restarted
  for (const a of (s.uiActions || [])){
    if (!a || typeof a.seq !== "number" || a.seq <= lastUiSeq) continue;
    lastUiSeq = a.seq;
    const fn = ACTION_IMPL[a.id];
    if (!fn) continue;
    forwardedDepth++;
    try { fn(null, a.arg); } finally { forwardedDepth--; }
  }
  if (typeof s.uiSeq === "number" && s.uiSeq > lastUiSeq) lastUiSeq = s.uiSeq;
}

// Which panel the dock shows. Both pages report it to the shell (FusionDockApp), which
// swaps the web views; whichever page is visible sees the change first.
let lastMode = null;
function noteDockMode(m){
  if (!m || m === lastMode) return;
  lastMode = m;
  try { window.webkit.messageHandlers.dockMode.postMessage(m); } catch (_) {}
}
function toggleMode(){ post({ action: "toggle" }); }

async function pollState(){
  let s;
  try { s = await (await fetch(API + "/state", { cache: "no-store" })).json(); }
  catch (_) { return; }
  runForwardedActions(s);
  noteDockMode(s.dockMode);
  if (s.probe && s.probe.id === probeId && drag && !drag.probe){
    drag.probe = s.probe;
    if (s.probe.why) ListKit.flash(s.probe.why, "warn");
  }
  if (s.notice && s.notice.at !== lastNoticeAt){
    if (lastNoticeAt !== null){ ListKit.flash(s.notice.text, "warn"); lastJSON = ""; }
    lastNoticeAt = s.notice.at;
  }
  if (lastNoticeAt === null) lastNoticeAt = 0;
  // The Browser files feature rows into these same groups: a revision we didn't write
  // means our copy is stale — reload it before anything here saves over it.
  if (groupsDoc && groupsDoc === s.doc_id && typeof s.groupsRev === "number"
      && groupsRev !== null && s.groupsRev !== groupsRev) loadGroups(groupsDoc, s.groupsRev);
  const j = JSON.stringify([s.rows, s.comps, s.activeRef, s.doc_id, s.compIcon]);
  if (j === lastJSON) return;
  lastJSON = j;
  connected = !!s.doc_id;
  const prev = rows;
  rows = s.rows || []; comps = s.comps || {}; activeRef = s.activeRef || "";
  compIcon = s.compIcon || "";
  phaseNames = s.phases || [];
  const doc = s.doc_id || "";
  if (doc !== docId){ docId = doc; if (doc) loadGroups(doc, s.groupsRev); else groupsDoc = null; }
  else if (groupsDoc === docId && carryByToken(data, prev, rows)) saveGroups();
  refresh();
}

// ── drawing ───────────────────────────────────────────────────────────────────
const el = (t, c, txt) => {
  const e = document.createElement(t);
  if (c) e.className = c;
  if (txt != null) e.textContent = txt;
  return e;
};

// Our glyph (fusion_ui/tl_icons.js), else the operation's own Fusion icon (health
// is the marker after the name, not a backing). An unmapped type gets a generic glyph.
function iconNode(rel, generic, type){
  const w = el("span", "tico");
  const g = glyphSVG(type);
  if (g){
    w.innerHTML = g;
  } else if (rel){
    const img = el("img");
    img.src = "/icon/" + rel.split("/").map(encodeURIComponent).join("/");
    img.draggable = false;
    img.onerror = () => { img.remove(); w.classList.add(generic || "g-feature"); };
    w.appendChild(img);
  } else w.classList.add(generic || "g-feature");
  return w;
}

function refresh(){
  // Not mid-drag: redrawing would take the dragged row out from under the drag and
  // WebKit would drop it. The drag's end redraws.
  if (drag){ drag.stale = true; return; }
  model = buildModel(rows, comps, activeRef, data);
  render();
}

function render(){
  const list = document.getElementById("list");
  if (!list) return;
  list.innerHTML = "";
  if (!connected){
    const w = el("div", "", "waiting for Fusion…");
    w.id = "waiting";
    list.appendChild(w);
    return;
  }
  ListKit.draw(list, LIST, {
    items: model.items, groups: model.groups, sequence: model.sequence, nest: true,
    name: it => it.label,
    lead: it => [iconNode(it.row.icon, "", glyphType(it.row))],
    groupLead: g => [g.kind === "cg" ? iconNode(compIcon, "g-comp", OCC_TYPE)
                                     : iconNode(g.icon, "g-folder", g.iconType)],
    row: it => phaseNodes(it).concat(msgNodes(it)),
    groupHead: g => g.health ? [healthMark(g.health)] : [],
    drag: { order: true, into: true, mixed: true, start: dragStart, end: dragEnd, can: canDrop },
    onDrop: dropped,
    renameItem: (it, v) => renameRow(it.row, v),
    renameGroup: (g, v) => renameGroup(g, v),
    newGroup: parentId => {
      const id = addGroupInside(data, parentId, activeRef);
      if (!id) return null;
      saveGroups(); refresh();
      return { id: UG + id };
    },
    onCollapse: (g, c) => {
      if (g.kind === "cg"){ if (c) data.compCollapsed[g.ref] = true; else delete data.compCollapsed[g.ref]; }
      else if (data.groups[g.gid]) data.groups[g.gid].collapsed = !!c;
      saveGroups();
    },
    menu: (it, sel) => rowMenu(it, sel),
    groupMenu: (g, sel) => groupMenu(g, sel),
    ungroup: gs => explodeGroups(gs.map(g => g.id)),
    empty: "No features here yet.",
  });
  publishLiveBottom();
}

// ── verbs ─────────────────────────────────────────────────────────────────────
function itemByKey(k){ return model.items.find(i => i.id === k) || null; }
function groupById(id){ return model.groups.find(g => g.id === id) || null; }

function renameRow(r, v){
  v = String(v || "").trim();
  // The field opens on the CLEANED name — confirming it untouched is not a rename.
  if (!v || v === r.name || v === displayName(r.name)) return;
  post({ action: "rename", tok: r.tok, name: v });
  const to = (r.compRef || "") + "|" + v;
  if (carryGroupKey(data, r.key, to)) saveGroups();
  if (LIST.cur === "i:" + r.key) LIST.cur = "i:" + to;
  r.name = v; r.key = to;              // optimistic; the next poll brings the real row
  refresh();
}

function renameGroup(g, v){
  v = String(v || "").trim();
  if (g.kind === "ug"){
    if (!data.groups[g.gid]) return;
    // Confirming the content-derived name untouched keeps it following the contents;
    // renaming to "Group" hands the group back to it (AUTO_NAME).
    if (!g.named && v === g.label) return;
    data.groups[g.gid].name = v;
    saveGroups();
  } else if (g.kind === "cg"){
    if (!v || comps[g.ref] && v === displayName(comps[g.ref].name)) return;
    // A component group IS the component: renaming it renames the component.
    post({ action: "renamecomp", ref: g.ref, name: v });
    if (comps[g.ref]) comps[g.ref].name = v;
  }
  refresh();
}

function editRow(r){
  if (!r || !r.tok) return;
  post({ action: "edit", tok: r.tok, sketch: r.type === SKETCH_TYPE, occTok: r.occTok || "" });
}

function setSuppressed(rs, on){
  rs = rs.filter(r => r && r.tok);
  if (!rs.length) return;
  post({ action: "suppress", toks: rs.map(r => r.tok), on: !!on });
  for (const r of rs) r.suppressed = !!on;
  refresh();
}

// A real Fusion delete (undoable with ⌘Z). Rows go at once; the next poll confirms.
function deleteRows(rs){
  rs = rs.filter(r => r && r.tok);
  if (!rs.length) return;
  post({ action: "delete", toks: rs.map(r => r.tok) });
  const gone = new Set(rs.map(r => r.key));
  rows = rows.filter(r => !gone.has(r.key));
  for (const k of gone) LIST.pick.delete("i:" + k);
  if (LIST.cur && gone.has(LIST.cur.slice(2))) LIST.cur = null;
  refresh();
}

// ⌘D, as in the Browser: groups picked or under the cursor dissolve first (window-only,
// so one keystroke can't sweep features away alongside a group); otherwise the rows go.
function deleteSelection(){
  const keys = LIST.pick.size ? [...LIST.pick] : (LIST.cur ? [LIST.cur] : []);
  const gids = keys.filter(k => k.startsWith("g:")).map(k => k.slice(2));
  if (gids.length) return explodeGroups(gids);
  deleteRows(keys.filter(k => k.startsWith("i:")).map(k => itemByKey(k.slice(2)))
                 .filter(Boolean).map(i => i.row));
}

// An error or warning is a MARKER at the end of the row (or group — its worst row's);
// on a row with a message a click unfolds it under the row, a second folds it away.
function healthMark(health){
  const m = el("span", "msg-mark h-" + health, "!");
  m.title = health === "warning" ? "Warning" : "Error";
  m.draggable = false;
  return m;
}
// The phase a row is in, said where it changes (a line over the row, and the phase's
// name after it) — and on every dependent / post-dependent row, the Browser's mark.
const PHASE_MARKS = { 5: "⇄", 6: "↳" };
function phaseNodes(it){
  const r = it.row, out = [];
  const names = phaseNames || [];
  if (PHASE_MARKS[r.phase]){
    const m = el("span", "phase-mark", PHASE_MARKS[r.phase]);
    m.title = names[r.phase - 1] || "";
    out.push(m);
  }
  if (r.phaseEdge && names[r.phase - 1]) out.push(el("span", "phase-tag", names[r.phase - 1]));
  return out;
}
function msgNodes(it){
  const r = it.row;
  if (!r.health && !r.msg) return [];
  const m = healthMark(r.health || "warning");
  if (!r.msg) return [m];
  const open = msgOpen.has(r.key);
  m.classList.add("has-msg");
  if (open) m.classList.add("open");
  m.title = open ? "Hide the message" : m.title + " — click to read";
  // ⌥-click (a held Space) falls through to the row, which selects without opening.
  m.onclick = e => { if (e.altKey) return; e.stopPropagation(); toggleMsg(r); };
  const out = [m];
  if (open) out.push(el("span", "sub", r.msg));
  return out;
}
function toggleMsg(r){
  if (!r || !r.msg) return;
  if (msgOpen.has(r.key)) msgOpen.delete(r.key); else msgOpen.add(r.key);
  refresh();
}

// ── dragging ──────────────────────────────────────────────────────────────────
// A drag asks Fusion (the add-in's probe: canReorder for every spot) where the rows
// may go the moment it starts; until the answer is in, and wherever it is no, the kit
// draws no drop mark. Filing into a heading is window-only and needs no answer.
function dragStart(d){
  const rs = dragRows(model, d);
  const toks = rs.map(r => r.tok);
  drag = { toks, probe: null, stale: false };
  if (!rs.length || toks.some(t => !t)) return;
  const mine = new Set(toks);
  post({ action: "probe", id: ++probeId, toks,
         targets: model.items.map(i => i.row.tok).filter(t => t && !mine.has(t)) });
}
function dragEnd(){
  const stale = drag && drag.stale;
  drag = null;
  if (stale) refresh();
}
function canDrop(d, got){
  const spot = dropSpot(model, got);
  if (!spot) return false;
  if (spot.into !== undefined) return canFileInto(model, d, spot.into, activeRef);
  const p = drag && drag.probe;
  if (!p || p.why) return false;
  return spot.before ? p.ok.includes(spot.before) : !!p.end;
}
function dropped({ items, groups, into, before, end }){
  const d = { items, groups };
  const spot = dropSpot(model, { into, before, end });
  if (!spot) return;
  if (spot.into !== undefined){
    if (!canFileInto(model, d, spot.into, activeRef)) return;
    fileAt(model, data, d, spot.into, activeRef);
  } else {
    const rs = dragRows(model, d);
    if (!rs.length) return;
    post({ action: "reorder", toks: rs.map(r => r.tok), before: spot.before });
    // Shown moved at once; the next walk brings Fusion's answer (and puts it back if
    // it refused).
    const moving = new Set(rs.map(r => r.key));
    const rest = rows.filter(r => !moving.has(r.key));
    let at = spot.before ? rest.findIndex(r => r.tok === spot.before) : rest.length;
    if (at < 0) at = rest.length;
    rows = rest.slice(0, at).concat(rows.filter(r => moving.has(r.key)), rest.slice(at));
    fileAt(model, data, d, spot.home, activeRef);
    // A refusal changes nothing Fusion reports, so the unchanged rows would never
    // redraw over the optimistic ones: let the next poll count as new either way.
    setTimeout(() => { lastJSON = ""; }, 1500);
  }
  saveGroups();
  refresh();
}

// Grouping from the keyboard or the menu. The pick if there is one; otherwise the
// cursor — an OPEN group gets a new group inside it, a shut one (or a row) gets a
// group around it; nothing at all makes one at the top level.
function groupSelection(){
  const pickI = [...LIST.pick].filter(k => k.startsWith("i:")).map(k => k.slice(2));
  const pickG = [...LIST.pick].filter(k => k.startsWith("g:")).map(k => k.slice(2));
  let made = null;
  if (pickI.length || pickG.length){
    made = groupAround(model, data, pickI, pickG, activeRef);
  } else if (!LIST.cur){
    const id = addGroupInside(data, ROOT, activeRef);
    made = id ? { id } : null;
  } else if (LIST.cur.startsWith("g:")){
    const g = groupById(LIST.cur.slice(2));
    if (g && (g.kind === "cg" || !g.collapsed)){
      const id = addGroupInside(data, g.id, activeRef);
      if (id && g.kind === "cg") delete data.compCollapsed[g.ref];   // open it to show the new one
      made = id ? { id } : null;
    } else if (g) made = groupAround(model, data, [], [g.id], activeRef);
  } else {
    made = groupAround(model, data, [LIST.cur.slice(2)], [], activeRef);
  }
  if (!made) return;
  if (made.error){ ListKit.flash(made.error, "warn"); return; }
  LIST.pick.clear();
  LIST.cur = "g:" + UG + made.id;
  LIST.renaming = LIST.cur;
  saveGroups();
  wantFocus();
  refresh();
}

function explodeGroups(ids){
  let any = false;
  for (const id of ids){
    const g = groupById(id);
    if (g && g.kind === "ug") any = explodeGroup(data, g.gid) || any;
  }
  if (!any) return;
  if (LIST.cur && ids.includes(LIST.cur.slice(2))) LIST.cur = null;
  saveGroups();
  refresh();
}
function explodeCursor(){
  const picked = [...LIST.pick].filter(k => k.startsWith("g:")).map(k => k.slice(2));
  if (picked.length) return explodeGroups(picked);
  if (LIST.cur && LIST.cur.startsWith("g:")) explodeGroups([LIST.cur.slice(2)]);
}

// ⏎: edit the feature under the cursor; on a group, open or shut it.
function activateCursor(){
  const k = LIST.cur;
  if (!k) return;
  if (k.startsWith("i:")){
    const it = itemByKey(k.slice(2));
    if (it && it.row.type !== OCC_TYPE) editRow(it.row);
    return;
  }
  nav(groupById(k.slice(2)) && groupById(k.slice(2)).collapsed ? "nav-in" : "nav-out");
}

function nav(id){ ListKit.command(id); }

// L, as in the Browser: bring the cursor's row back into view.
function jumpToCursor(){
  const n = LIST.cur && document.querySelector('[data-key="' + CSS.escape(LIST.cur) + '"]');
  if (n) n.scrollIntoView({ block: "center" });
}

function beginRename(){
  if (ListKit.command("rename")) wantFocus();
}

// A rename or new group started by a key pressed in the VIEWPORT opens its field in a
// window that isn't key — ask the shell for the keyboard (same as the Browser).
function wantFocus(){
  if (!forwardedDepth) return;
  try { window.webkit.messageHandlers.focus.postMessage(true); } catch (_) {}
}

// ── menus ─────────────────────────────────────────────────────────────────────
// The tail every menu carries: what the DOCK shows, not what the row is.
function tailItems(){
  return [{ sep: true }, { label: "Show Browser", run: toggleMode }];
}

function rowMenu(it, sel){
  const r = it.row;
  const rs = sel.map(itemByKey).filter(Boolean).map(i => i.row);
  const out = [];
  // A component coming in has nothing to edit — its group is where its features are.
  if (sel.length <= 1 && r.type !== OCC_TYPE)
    out.push({ label: r.type === SKETCH_TYPE ? "Edit Sketch" : "Edit Feature", run: () => editRow(r) });
  const allOff = rs.length && rs.every(x => x.suppressed);
  out.push({ label: allOff ? "Unsuppress" : "Suppress", run: () => setSuppressed(rs, !allOff) });
  out.push({ label: "Group", run: () => {
    const made = groupAround(model, data, sel, [], activeRef);
    if (made.error) return ListKit.flash(made.error, "warn");
    LIST.pick.clear(); LIST.cur = LIST.renaming = "g:" + UG + made.id;
    saveGroups(); refresh();
  } });
  if (r.msg && sel.length <= 1)
    out.push({ label: (msgOpen.has(r.key) ? "Hide " : "Show ") + (r.health === "warning" ? "Warning" : "Error"),
               run: () => toggleMsg(r) });
  out.push({ sep: true },
           { label: rs.length > 1 ? "Delete " + rs.length + " Selected" : "Delete",
             run: () => deleteRows(rs) });
  return out.concat(tailItems());
}

function groupMenu(g, sel){
  const out = [];
  if (g.kind === "ug"){
    out.push({ label: "Explode Group", run: () => explodeGroups(sel) });
  }
  return out.concat(tailItems());
}

// Right-click on empty space: just the tail.
document.addEventListener("contextmenu", e => {
  if (!e.target || !e.target.closest || e.target.closest(".row, .group-head, .ctxmenu")) return;
  ListKit.showMenu(e, tailItems().slice(1));
});
document.addEventListener("mousedown", e => {
  if (ListKit.menuOpen() && !(e.target && e.target.closest && e.target.closest(".ctxmenu")))
    ListKit.closeMenu();
});

// ── the empty band below the list belongs to the viewport ─────────────────────
// Same contract as the Browser's publishLiveBottom: the shell lets clicks and gestures
// below the last row through to Fusion. -1 = the whole window is live.
let lastLiveBottom = null;
function publishLiveBottom(){
  const list = document.getElementById("list");
  let px = -1;
  if (list && connected && !ListKit.menuOpen() && !ListKit.inlineOpen()
      && !document.getElementById("settings-bg")
      && list.scrollHeight <= list.clientHeight + 1){
    const all = list.querySelectorAll(".row, .group-head, .empty");
    const last = all.length ? all[all.length - 1] : null;
    if (last) px = Math.ceil(last.getBoundingClientRect().bottom) + 6;
  }
  if (px === lastLiveBottom) return;
  lastLiveBottom = px;
  try { window.webkit.messageHandlers.liveBottom.postMessage(px); } catch (_) {}
}

// ── keyboard ──────────────────────────────────────────────────────────────────
// id -> what that action does HERE. The ids are the Browser's (fusion_ui/actions.json):
// the same keys drive whichever panel the dock shows. An id with no entry is Fusion's
// and is forwarded to it.
const ACTION_IMPL = {
  'BrowserNavDownCmd':         () => nav("nav-down"),
  'BrowserNavUpCmd':           () => nav("nav-up"),
  'BrowserNavBottomCmd':       () => nav("nav-bottom"),
  'BrowserNavTopCmd':          () => nav("nav-top"),
  'BrowserGoInDeepCmd':        () => nav("nav-in"),
  'BrowserGoInCmd':            () => nav("nav-in"),
  'BrowserGoOutCmd':           () => nav("nav-out"),
  // Edit edits a feature; on a group it does nothing (Enter still opens / shuts one).
  'BrowserEditCmd':            () => { if (LIST.cur && LIST.cur.startsWith("i:")) activateCursor(); },
  // Go Down / Go Up would leave the locked component; here they open and shut groups.
  'ActivateAndIsolateCmd':     () => nav("nav-in"),
  'GoToParentComponentCmd':    () => nav("nav-out"),
  // A — clears BOTH sides, as in the Browser. From the viewport it arrives vetoed, so
  // Fusion's own selection is only cleared if this asks for it.
  'deselectAllCmd':            () => { nav("deselect"); post({ action: "deselect" }); },
  'BrowserJumpToCursorCmd':    () => jumpToCursor(),
  'BrowserRenameSelectionCmd': () => beginRename(),
  'DeleteSelectionCmd':        () => deleteSelection(),
  'BrowserNewFolderCmd':       () => groupSelection(),
  'BrowserNewParamGroupCmd':   () => groupSelection(),
  'BrowserSettingsCmd':        () => openSettings(),
  'DockTimelineToggleCmd':     () => toggleMode(),
  // Super Escape: the rename, the menu, the settings panel all at once, then Fusion's own
  // (its command, the Value window) unless this came FROM Fusion.
  'SuperEscapeCmd':            () => {
    if (ListKit.inlineOpen()) ListKit.escape();
    if (ListKit.menuOpen()) ListKit.closeMenu();
    if (document.getElementById("settings-bg")) closeSettings();
    if (!forwardedDepth) post({ action: "command", cmd_id: "SuperEscapeCmd" });
  },
};

let fusionKeys = {};
async function pollHotkeys(){
  try {
    const raw = await (await fetch(BROWSER_API + "/fusionkeys", { cache: "no-store" })).json();
    const nk = {};
    for (const k in raw) nk[normSeq(k)] = raw[k];
    fusionKeys = nk;
  } catch (_) {}
}

document.addEventListener("keydown", e => {
  if (ListKit.inlineOpen()) return;                  // the rename field has its keys
  if (document.getElementById("settings-bg")){
    if (e.key === "Escape" && !hotkeyPanel.capturing()){ e.preventDefault(); closeSettings(); }
    return;
  }
  if (ListKit.menuOpen()){
    if (e.key === "Escape"){ e.preventDefault(); ListKit.closeMenu(); }
    return;
  }
  const k = (e.key || "").toLowerCase();
  // One of our own actions bound to Return / Space (Browser: Edit rebound to Space)
  // runs; a Fusion command on them never crosses. Return is Edit only while Browser:
  // Edit has no key of its own in the keymap.
  if ((k === "enter" || k === " ") && !e.metaKey && !e.ctrlKey && !e.altKey){
    e.preventDefault();
    const cid = Keys.lookup(fusionKeys, e);
    if (cid && ACTION_IMPL[cid]) ACTION_IMPL[cid](e);
    else if (k === "enter" && !e.shiftKey &&
             !Object.values(fusionKeys).includes("BrowserEditCmd")) activateCursor();
    return;
  }
  if (!e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey){
    const local = { arrowdown: () => nav("nav-down"), arrowup: () => nav("nav-up"),
                    arrowright: () => nav("nav-in"), arrowleft: () => nav("nav-out"),
                    enter: activateCursor, backspace: explodeCursor, delete: explodeCursor,
                    escape: () => ListKit.escape() }[k];
    if (local){ e.preventDefault(); local(); return; }
  }
  // Space is a held layer here as in the viewport (Keys' HELD SPACE), not a scroll.
  if (e.key === " "){ e.preventDefault(); return; }
  const cid = Keys.lookup(fusionKeys, e);
  if (cid){
    e.preventDefault();
    if (ACTION_IMPL[cid]) ACTION_IMPL[cid](e);
    else post({ action: "command", cmd_id: cid });
  }
});

// WebKit eats ⌘R / ⌘E before the page sees them; the dock shell hands the CHORD here.
window.__key = function(chord){
  const cid = fusionKeys[normSeq(chord || "")];
  if (!cid) return;
  if (ACTION_IMPL[cid]) ACTION_IMPL[cid](null);
  else post({ action: "command", cmd_id: cid });
};
window.__rename = beginRename;

// ── settings / hotkeys (⌘,) ───────────────────────────────────────────────────
// Mirrors the Browser's table: the shared ids share custom bindings with it (one
// settings file), the rest are this window's own.
const ACTIONS = {
  'nav-down': { label:'Move selection down', scope:'shared', def:['↓'], run:() => nav("nav-down") },
  'nav-up':   { label:'Move selection up',   scope:'shared', def:['↑'], run:() => nav("nav-up") },
  'expand':   { label:'Expand / into',       scope:'shared', def:['→'], run:() => nav("nav-in") },
  'collapse': { label:'Collapse / out',      scope:'shared', def:['←'], run:() => nav("nav-out") },
  'rename':   { label:'Rename',              scope:'shared', def:[], general:'rename', run:() => beginRename() },
  'settings': { label:'Open settings',       scope:'shared', def:['⌘,'], run:() => openSettings() },
  // The Browser's own ids where the verb is the same, so a key added to one in the
  // Browser's panel works here too (custom bindings are keyed by id, one file).
  'browser:activate':   { label:'Edit feature / sketch', scope:'timeline', def:['↵'], run:() => activateCursor() },
  'browser:new-folder': { label:'Group',                 scope:'timeline', def:['⌘G'], run:() => groupSelection() },
  'browser:new-param-group': { label:'Group (parameter-group key)', scope:'timeline', def:[], run:() => groupSelection() },
  'browser:del-folder': { label:'Explode group',         scope:'timeline', def:['⌫'], run:() => explodeCursor() },
  // The master shortcut; the list kit keeps it to selected groups only.
  'ungroup':  { label:'Ungroup',             scope:'shared', def:[], general:'ungroup', dispatch:true, run:() => nav("ungroup") },
  'timeline:toggle':  { label:'Show the Browser',      scope:'timeline', def:[], run:() => toggleMode() },
};
const hotkeyPanel = Keys.panel({
  api: API,
  actions: ACTIONS,
  sections: [['Shared (Browser and Timeline)', 'shared'], ['Timeline window', 'timeline']],
  blocked: () => !!document.getElementById('settings-bg') || ListKit.inlineOpen(),
  buttonClass: 'sbtn',
});
function openSettings(){ hotkeyPanel.open(); }
function closeSettings(){ hotkeyPanel.close(); }

// ── boot ──────────────────────────────────────────────────────────────────────
ListKit.config({ onChange: () => render() });
hotkeyPanel.load();
document.addEventListener('keydown', hotkeyPanel.onCaptureKey, true);
document.addEventListener('keydown', hotkeyPanel.onCustomKey, true);
setInterval(hotkeyPanel.loadGeneral, 5000);
pollHotkeys(); setInterval(pollHotkeys, 3000);
setInterval(publishLiveBottom, 250);
pollState(); setInterval(pollState, 80);
