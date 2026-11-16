'use strict';

// ── State ────────────────────────────────────────────────────────────────────
//
// The layout is a tree of nodes persisted in groups.json as { tree: [...] }:
//   group: { id, type:'group', name, collapsed, children:[node...] }
//   param: { type:'param', name }            (identified by its Fusion name)
// Params that exist in Fusion but aren't placed in the tree are appended at the
// root automatically (reconcileTree). Selection is by id: group.id or param name.

const { seqOf, normSeq } = Keys;   // fusion_ui/keys.js — the chord spelling

let rawParams = [];    // last raw snapshot from Fusion (before optimistic overlay)
let params  = [];      // [{name, expression, unit, comment, deps}] shown (raw + optimistic)
// selected: Set of row keys ("param:Name" / "group:id"); anchor: last single pick
// (for Shift-range). A row's key is in its data-key attribute. state.tree is the
// active document's tree (a reference into allGroups[currentDocId]).
let state   = { tree: [], selected: new Set(), anchor: null };
let allGroups  = {};      // { docId: { tree:[...] } } — one organization per file
let currentDocId = null;
let legacyTree = null;    // pre-per-doc single tree, assigned to the first doc seen
let lastTs  = -1;
let snapshotHash = '';
let dirty = false;
let editing = {};      // "name<sep>field" → {orig, strippedUnit}
let pending = {};      // {paramName: expression} optimistic
let pendingMeta = {};  // {paramName: {was, ts}} — Fusion's pre-commit expression + when we sent it
let pendingAdds = {};  // {name: {param:{name,expression,unit,comment}, ts}} shown until Fusion confirms
let pendingDeletes = {}; // {name: ts} hidden until Fusion confirms removal
let pendingRenames = {}; // {newName: {old, ts}} — show row under its new name until Fusion confirms
let connected = false;
let saveGroupsTimer = null;
// Line breaks for multi-line expressions, per parameter, for the active document —
// a reference into allGroups[currentDocId].layouts. Fusion stores expressions flat
// (it strips newlines itself), so the layout only lives here. See layoutFor().
let docLayouts = {};
let bigEdit = null;        // {name, orig} while the full-window expression editor is open
let lastEditOpenAt = 0;    // when an inline expression field last opened (double-Enter window)
const DOUBLE_ENTER_MS = 500;

const INDENT  = 17;   // per-nesting-level indent (px); matches Browser's indent
const ARROW_W = 4;    // arrows removed; tuned so params align one level in under their group

// ── Boot ─────────────────────────────────────────────────────────────────────

async function init() {
  try {
    const g = await fetch('/groups').then(r => r.json());
    if (g && g.byDoc) {                          // current per-document format
      allGroups = g.byDoc;
    } else if (g && g.tree) {                    // old single tree → first doc gets it
      legacyTree = normalizeTree(g.tree);
    } else if (g && g.groups) {                  // ancient flat format → first doc gets it
      legacyTree = g.groups.map(grp => ({
        id: newId(), type: 'group', name: grp.name, collapsed: false,
        children: (grp.params || []).map(n => ({ type: 'param', name: n })),
      }));
    }
  } catch (_) {}
  loadParamNaming();
  initColResize();
  loadCloseHotkey();
  hotkeyPanel.load();
  document.addEventListener('keydown', hotkeyPanel.onCaptureKey, true);   // rebind capture (runs first)
  document.addEventListener('keydown', hotkeyPanel.onCustomKey, true);    // user custom bindings
  document.addEventListener('keydown', onGlobalKey);
  // Right-click on any row selects it; no context menu.
  document.getElementById('groups-root').addEventListener('contextmenu', e => {
    e.preventDefault();
    const row = e.target.closest('.node-row');
    if (row) handleRowClick(e, row.dataset.key);
  });
  // Select param rows on mousedown so the first click into the window works.
  // Exception: pressing a row that's already part of a multi-selection (no
  // modifier) keeps the whole selection so a drag can move all of it — the
  // collapse-to-single happens on the following click only if no drag occurred.
  document.getElementById('groups-root').addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    if (e.target.closest('.group-del')) return;
    if (e.target.classList.contains('group-name-input')) return;
    const row = e.target.closest('.param-row');
    if (!row) return;
    const key = row.dataset.key;
    if (!e.metaKey && !e.shiftKey && state.selected.has(key) && state.selected.size > 1) {
      _pendingSingle = key;            // defer; resolved by the click handler below
    } else if (!e.metaKey && !e.shiftKey && state.selected.size === 1 && state.selected.has(key)) {
      // Already the whole selection: a click (plain or Space) deselects it — on the
      // click, so pressing it to drag still carries it.
      _pendingSingle = null; _pendingDeselect = key;
    } else {
      _pendingSingle = null;
      handleRowClick(e, key);
    }
  });
  // Plain click (mouseup without an intervening drag) on a row that was part of a
  // multi-selection collapses to just that row. A drag suppresses the click, so
  // the multi-selection survives for onListDrop.
  document.getElementById('groups-root').addEventListener('click', e => {
    if (_pendingDeselect != null) {
      const row = e.target.closest('.param-row'), key = _pendingDeselect;
      _pendingDeselect = null;
      // Not the second click of a double-click: that one edits the row, selected.
      if (row && row.dataset.key === key && !e.metaKey && !e.shiftKey && e.detail < 2) clearSelection();
      return;
    }
    if (_pendingSingle == null) return;
    const row = e.target.closest('.param-row');
    const key = _pendingSingle; _pendingSingle = null;
    if (row && row.dataset.key === key && !e.metaKey && !e.shiftKey) selectSingle(key);
  });
  startPolling();
}

// The keyboard shortcut that opens this window (assigned via the AnyShortcut
// add-in, stored in Fusion's own shortcut table). Pressing it again while the
// window is focused should close it — Fusion never sees the key because the
// window has focus, so we handle the close here. Re-read on every focus so a
// changed binding is picked up without restarting.
let closeHotkey = null;   // { key, shift, meta, alt, ctrl } | null
let fusionKeys = {};      // normalized seq -> Fusion command_id (full keymap, forwarded)

async function loadCloseHotkey() {
  try {
    const r = await fetch('/hotkey').then(x => x.json());
    closeHotkey = (r && r.hotkey) ? parseHotkey(r.hotkey) : null;
  } catch (_) { /* leave whatever we had */ }
}
// Full Fusion keymap, re-read so changes are picked up live. Normalize each sequence
// to match what seqOf() produces for a real keypress.
async function loadFusionKeys() {
  try {
    const raw = await fetch('/fusionkeys').then(x => x.json());
    const nk = {}; for (const k in raw) nk[normSeq(k)] = raw[k]; fusionKeys = nk;
  } catch (_) { /* leave whatever we had */ }
}
window.addEventListener('focus', () => { loadCloseHotkey(); loadFusionKeys(); });
loadFusionKeys(); setInterval(loadFusionKeys, 3000);

function parseHotkey(str) {
  const toks = String(str).split('+').map(s => s.trim().toLowerCase()).filter(Boolean);
  const hk = { key: '', shift: false, meta: false, alt: false, ctrl: false };
  for (const t of toks) {
    if (t === 'shift') hk.shift = true;
    else if (t === 'cmd' || t === 'command' || t === 'meta' || t === 'win') hk.meta = true;
    else if (t === 'alt' || t === 'option' || t === 'opt') hk.alt = true;
    else if (t === 'ctrl' || t === 'control') hk.ctrl = true;
    else hk.key = t;
  }
  return hk.key ? hk : null;
}

function matchesCloseHotkey(e) {
  const hk = closeHotkey;
  if (!hk) return false;
  return (e.key || '').toLowerCase() === hk.key &&
         e.shiftKey === hk.shift && e.metaKey === hk.meta &&
         e.altKey === hk.alt && e.ctrlKey === hk.ctrl;
}

// Close the window. main.swift installs a "close" script-message handler; fall
// back to window.close() if running in a plain browser.
function closeWindow() {
  try {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.close) {
      window.webkit.messageHandlers.close.postMessage('close');
      return;
    }
  } catch (_) {}
  window.close();
}

// Switch the active document's tree in/out of `state` (called when the document
// changes). state.tree is kept as a reference into allGroups so saves persist it.
function switchDoc(docKey) {
  // A big-box edit belongs to the document it was opened from; don't let it commit
  // into the one that just became active.
  if (bigEdit) closeBigEdit(false);
  currentDocId = docKey;
  if (!allGroups[docKey]) {
    allGroups[docKey] = { tree: legacyTree || [] };   // first file inherits the legacy tree
    legacyTree = null;
  }
  state.tree = normalizeTree(allGroups[docKey].tree || []);
  allGroups[docKey].tree = state.tree;
  if (!allGroups[docKey].layouts) allGroups[docKey].layouts = {};
  docLayouts = allGroups[docKey].layouts;
  state.selected.clear();
  state.anchor = null;
}

function onGlobalKey(e) {
  // Cmd-S saves the Fusion document — let it through even while editing / over a
  // modal so the usual save shortcut keeps working from this window.
  if (e.metaKey && !e.shiftKey && !e.altKey && !e.ctrlKey && (e.key === 's' || e.key === 'S')) {
    e.preventDefault(); saveFusion(); return;
  }
  if (document.getElementById('settings-bg')) {        // settings open: only Esc closes it
    if (e.key === 'Escape' && !hotkeyPanel.capturing()) { e.preventDefault(); closeSettings(); }
    return;
  }
  if (document.getElementById('modal-bg')) return;     // modal handles its own keys
  if (isEditing()) return;                             // let text editing keep the keys

  // The window's own open shortcut, pressed while focused → close (toggle off).
  if (matchesCloseHotkey(e)) { e.preventDefault(); closeWindow(); return; }

  // Key repeat must never reach the pick logic: holding Enter would arm and then
  // immediately toggle back off, which reads as "it won't arm".
  if (e.repeat) return;

  // Escape gets out of a pending viewport pick before anything else looks at the key.
  if (e.key === 'Escape' && relinkName) { e.preventDefault(); cancelRelink('escape'); return; }

  // Any other key drops a pending pick too — the mode should never outlive your
  // attention. Modifiers alone don't count (resting a finger on Cmd shouldn't cancel),
  // and Enter falls through to the toggle just below. This doesn't consume the key:
  // whatever it normally does still happens.
  if (relinkName && e.key !== 'Enter' &&
      !['Shift', 'Meta', 'Alt', 'Control', 'CapsLock'].includes(e.key)) {
    cancelRelink('key:' + e.key);
  }

  // Enter on a single selected parameter opens its expression for editing — except on a
  // driven row, whose value is measured and has nothing to type. There it arms a pick:
  // click a driven dimension in the viewport and this parameter re-points at it.
  if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey && !e.altKey) {
    // Derive from state.selected rather than the DOM (selectedNodes), so a poll-driven
    // re-render that briefly rebuilds the rows can't make this a no-op — the same
    // reason deleteSelectedParam reads the selection this way. Reading the DOM meant a
    // freshly-clicked row was sometimes invisible to Enter until it was clicked again.
    const names = [...state.selected].filter(k => k.startsWith('param:')).map(k => k.slice(6));
    if (names.length !== 1) return;
    const name = names[0];
    e.preventDefault();
    if (isDriven(name)) {
      // Enter again on the row that's already waiting cancels it — but not a second
      // press that lands right on top of the first. A double-tap is how you ask for
      // something that felt unresponsive, and cancelling on it makes the mode look
      // broken; only a deliberate, later press should undo the arm.
      if (relinkName === name) {
        if (Date.now() - relinkArmedAt > 700) cancelRelink('toggle');
      } else startRelink(name);
    } else enterExpression(name);
    return;
  }

  // Cmd shortcuts: group / delete / rename / add. Anything that isn't one of the
  // window's own shortcuts falls through to be forwarded to Fusion below.
  if (e.metaKey && !e.ctrlKey && !e.altKey) {
    if (!e.shiftKey && e.key === ',') { e.preventDefault(); openSettings(); return; }
    if (!e.shiftKey && (e.key === 'g' || e.key === 'G')) { e.preventDefault(); addGroup(); return; }
    if (!e.shiftKey && (e.key === 'd' || e.key === 'D')) { e.preventDefault(); deleteSelectedParam(); return; }
    if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); deleteSelection(); return; }
    if (e.shiftKey && (e.key === 'n' || e.key === 'N')) { e.preventDefault(); openAddDialog(); return; }
  }

  // Any other Fusion modifier shortcut → forward to Fusion (full keymap, refreshed
  // live), so every Fusion modifier shortcut keeps working while this window is focused.
  if (e.metaKey || e.ctrlKey || e.altKey) {
    const cid = Keys.lookup(fusionKeys, e);
    if (cid) {
      e.preventDefault();
      fetch('/command', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd_id: cid }) }).catch(() => {});
    }
    return;
  }

  // Plain keys (no modifier): expand/collapse + navigate the visible list. The
  // arrow keys mirror the letter keys (Right/F expand, Left/D collapse, Down/C
  // next, Up/R prev) so either set works when not editing a text field.
  if (!e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    if (e.key === 'f' || e.key === 'F' || e.key === 'ArrowRight') { e.preventDefault(); setCollapsedSelected(false); }  // expand
    else if (e.key === 'd' || e.key === 'D' || e.key === 'ArrowLeft') { e.preventDefault(); setCollapsedSelected(true); }  // collapse
    else if (e.key === 'c' || e.key === 'C' || e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1); }   // down
    else if (e.key === 'r' || e.key === 'R' || e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1); }    // up
  }
}

// Expand (false) or collapse (true) the selected group(s).
function setCollapsedSelected(collapsed) {
  const gs = selectedNodes().filter(n => n.type === 'group');
  if (!gs.length) return;
  for (const g of gs) setGroupCollapsed(g, collapsed, true);
  saveGroups();
  render();
}

// Collapse/expand a group, with a 5-second memory of its inner state: if a
// collapsed group isn't re-expanded within 5s, its descendant groups are then
// collapsed too, so re-opening it later shows a tidy single level. Re-expanding
// within the window keeps the inner layout. Pass defer=true to skip the immediate
// save/render (the caller batches them).
const collapseTimers = {};   // group id -> pending "collapse descendants" timeout
function setGroupCollapsed(node, collapsed, defer) {
  node.collapsed = collapsed;
  if (collapseTimers[node.id]) { clearTimeout(collapseTimers[node.id]); delete collapseTimers[node.id]; }
  if (collapsed) {
    collapseTimers[node.id] = setTimeout(() => {
      delete collapseTimers[node.id];
      if (node.collapsed) { collapseDescendantGroups(node); saveGroups(); render(); }
    }, 5000);
  }
  if (!defer) { saveGroups(); render(); }
}

function collapseDescendantGroups(node) {
  (function w(arr) {
    for (const n of arr || []) {
      if (n.type === 'group') { n.collapsed = true; w(n.children); }
    }
  })(node.children);
}

// Move the selection to the adjacent row in the visible list (no group opening).
function moveSelection(dir) {
  const order = visibleKeys();
  if (!order.length) return;
  let idx = order.findIndex(k => state.selected.has(k));
  if (idx === -1) idx = dir > 0 ? -1 : 0;
  const next = (idx + dir + order.length) % order.length;   // wrap around
  const key = order[next];
  state.selected = new Set([key]); state.anchor = key;
  applySelection();
  const el = document.querySelector('.node-row[data-key="' + cssAttr(key) + '"]');
  if (el) el.scrollIntoView({ block: 'nearest' });
}

// Jump to section bottom / top (Master Shortcuts): to the edge of the selection's
// run of siblings, then up a level (Keys.sectionJump — the Browser's rule; this
// list has no categories).
function jumpSelection(dir) {
  const rows = [...document.querySelectorAll('#groups-root .node-row')];
  if (!rows.length) return;
  const at = rows.findIndex(r => state.selected.has(r.dataset.key));
  const i = Keys.sectionJump(rows.map(r => ({ depth: +r.dataset.depth || 0 })), at, dir);
  if (i < 0) return;
  const key = rows[i].dataset.key;
  state.selected = new Set([key]); state.anchor = key;
  applySelection();
  rows[i].scrollIntoView({ block: 'nearest' });
}

function saveFusion() { fetch('/save', { method: 'POST' }).catch(() => {}); }

function newId() {
  return 'g' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// Ensure every node has the fields we rely on (ids, collapsed, children arrays).
function normalizeTree(arr) {
  if (!Array.isArray(arr)) return [];
  const out = [];
  for (const n of arr) {
    if (!n || typeof n !== 'object') continue;
    if (n.type === 'group') {
      out.push({ id: n.id || newId(), type: 'group', name: n.name || 'Group',
                 collapsed: !!n.collapsed, children: normalizeTree(n.children || []) });
    } else if (n.type === 'param' && n.name) {
      out.push({ type: 'param', name: n.name });
    } else if (n.type === 'draft') {
      // A not-yet-created parameter (empty name and/or expression). Lives only in
      // the tree until both fields are valid, then it's promoted to a real param.
      out.push({ type: 'draft', id: n.id || newId(), name: n.name || '',
                 expression: n.expression || '', unit: n.unit == null ? 'mm' : n.unit,
                 comment: n.comment || '' });
    }
  }
  return out;
}

// ── Polling ───────────────────────────────────────────────────────────────────

function startPolling() { poll(); setInterval(poll, 500); }

async function poll() {
  try {
    const data = await fetch('/params').then(r => r.json());
    if (!connected) { connected = true; document.getElementById('offline').classList.remove('show'); }
    applyPendingInsert(data.pending_insert);
    if (data.relink) applyRelink(data.relink.name || '');

    const docKey = data.doc_id || '';     // '' = no active doc / add-in not synced yet
    const switching = docKey && docKey !== currentDocId;
    if (data.ts !== lastTs || switching) {
      lastTs = data.ts;
      rawParams = data.params || [];
      if (switching) { switchDoc(docKey); pending = {}; pendingMeta = {}; pendingAdds = {}; pendingDeletes = {}; pendingRenames = {}; }
      for (const name of Object.keys(pending)) {
        const p = rawParams.find(x => x.name === name);
        if (p && normExpr(p.expression) === normExpr(pending[name])) {
          delete pending[name]; delete pendingMeta[name];
        }
      }
    }
    // Always re-derive: expires optimistic adds/deletes that Fusion never confirmed
    // even when the raw snapshot itself didn't change.
    const h = (docKey || '') + '|' + relinkName + '|' + JSON.stringify(rawParams) +
              '|' + Object.keys(pendingAdds).join(',') + '|' + Object.keys(pendingDeletes).join(',') +
              '|' + Object.keys(pendingRenames).join(',');
    if (deriveParams() || h !== snapshotHash) {
      snapshotHash = h;
      if (isEditing()) dirty = true; else render();
    }
  } catch (_) {
    if (connected) {
      connected = false;
      document.getElementById('offline').classList.add('show');
    }
  }
}

// How long to keep showing an optimistic add (or keep hiding an optimistic delete)
// before assuming Fusion silently rejected it (e.g. a bad expression / a parameter
// still in use) and reverting to the real snapshot.
const OPTIMISTIC_TTL = 3000;   // ms

// Derive the displayed `params` from the last raw snapshot plus any optimistic
// add/delete still in flight. Confirmed changes drop their optimistic entry; ones
// Fusion never confirms expire after OPTIMISTIC_TTL and revert. Returns true if
// the derived list changed (so the caller can re-render).
function deriveParams() {
  const now = Date.now();
  let expired = false;
  // An expression Fusion rejects (bad syntax, unknown name, circular reference) is
  // never reported back, so the confirm-and-clear pass in poll() would leave the
  // typed text sitting in the row for good. Expire it like the other optimistic
  // states and let the row fall back to what Fusion actually holds.
  for (const name of Object.keys(pending)) {
    const rec = pendingMeta[name];
    if (rec && now - rec.ts <= OPTIMISTIC_TTL) continue;
    // Don't yank the text out from under a field the user is still in (or the big
    // box) — the expiry can wait for the commit that blur will send.
    if (editing[fkey(name, 'expression')] || (bigEdit && bigEdit.name === name)) continue;
    const p = rawParams.find(x => x.name === name);
    const sent = pending[name];
    // Fusion took it after all (the confirm pass in poll() only runs when the
    // snapshot's ts moves, which a value-preserving edit may not do).
    const confirmed = p && normExpr(p.expression) === normExpr(sent);
    const rejected = !confirmed && rec && p && normExpr(p.expression) === normExpr(rec.was);
    delete pending[name]; delete pendingMeta[name];
    expired = true;
    // Fusion still reports exactly what it held before the commit, so it took
    // nothing. (Any other value means it accepted the edit and merely spells it
    // differently — revert to Fusion's wording without complaining.)
    if (rejected) alert('Could not set "' + name + '" to "' + sent + '".');
  }
  for (const name of Object.keys(pendingAdds)) {
    if (rawParams.find(x => x.name === name)) delete pendingAdds[name];        // confirmed
    else if (now - pendingAdds[name].ts > OPTIMISTIC_TTL) {                    // rejected
      delete pendingAdds[name]; pendingInsert = null;
      alert('Could not add parameter "' + name + '".');
    }
  }
  for (const name of Object.keys(pendingDeletes)) {
    if (!rawParams.find(x => x.name === name)) delete pendingDeletes[name];    // confirmed gone
    else if (now - pendingDeletes[name] > OPTIMISTIC_TTL) delete pendingDeletes[name]; // failed → un-hide
  }
  for (const nu of Object.keys(pendingRenames)) {
    const rec = pendingRenames[nu];
    if (rawParams.find(x => x.name === nu)) delete pendingRenames[nu];          // confirmed renamed
    else if (now - rec.ts > OPTIMISTIC_TTL) {                                   // Fusion never applied it
      revertRename(nu, rec.old); delete pendingRenames[nu];
    }
  }

  let next = rawParams.slice();
  // Show the row under its new name (carrying the old row's values) until Fusion
  // reports the rename — otherwise reconcileTree treats the renamed tree node as
  // an orphan and re-homes it at the root, popping it out of its group.
  for (const nu of Object.keys(pendingRenames)) {
    const rec = pendingRenames[nu];
    const idx = next.findIndex(x => x.name === rec.old);
    if (idx >= 0 && !next.find(x => x.name === nu)) {
      next[idx] = Object.assign({}, next[idx], { name: nu });
    }
  }
  for (const name of Object.keys(pendingAdds)) {
    if (!next.find(x => x.name === name)) next.push(pendingAdds[name].param);
  }
  if (Object.keys(pendingDeletes).length) next = next.filter(x => !pendingDeletes[x.name]);

  const changed = JSON.stringify(next) !== JSON.stringify(params);
  params = next;
  return changed || expired;
}

// Undo an optimistic rename Fusion never confirmed: rename the tree node back to
// its old name and restore the selection/anchor.
function revertRename(newName, oldName) {
  renameLayout(newName, oldName);
  (function upd(arr) {
    for (const n of arr) {
      if (n.type === 'param' && n.name === newName) n.name = oldName;
      else if (n.children) upd(n.children);
    }
  })(state.tree);
  if (state.selected.has('param:' + newName)) {
    state.selected.delete('param:' + newName);
    state.selected.add('param:' + oldName);
    if (state.anchor === 'param:' + newName) state.anchor = 'param:' + oldName;
  }
  saveGroups();
}

// ── Derived groups ───────────────────────────────────────────────────────────
// A parameter named under the structured feature/type/anchor convention
// (fusion_ui/param_naming.js — a separate, newer convention from anything naming.js
// covers) organizes ITSELF: each feature level becomes, or reuses, a nested group by
// that name, and a newly-placed parameter files into the innermost one. This only
// ever runs at PLACEMENT time (see reconcileTree) — a parameter already in the tree,
// however it got there, is never moved again on its own; dragging one out of its
// derived group is exactly as durable as dragging any other row. A name that doesn't
// parse (the old convention, or anything else) is placed exactly as it always was.
let paramNamingReady = false;
async function loadParamNaming() {
  try {
    if (typeof ParamNaming === 'undefined') return;
    const spec = await (await fetch('/param_naming.json', { cache: 'no-store' })).json();
    ParamNaming.setVocabulary(spec);
    paramNamingReady = true;
  } catch (_) { paramNamingReady = false; }
}

// Find-or-create the nested group path named by `levels` (root to leaf), and return
// the innermost one's children array — where the parameter itself belongs. A group
// already there under that name at that position is reused, whether it was created
// by an earlier parameter's arrival or the user made it by hand: a component's own
// hierarchy is one thing however it was assembled.
function derivedGroupChildren(levels) {
  let arr = state.tree;
  for (const level of levels) {
    let g = arr.find(n => n.type === 'group' && n.name === level);
    if (!g) {
      g = { id: newId(), type: 'group', name: level, collapsed: false, children: [] };
      arr.push(g);
    }
    arr = g.children;
  }
  return arr;
}

// ── Tree helpers ───────────────────────────────────────────────────────────────

function nodeByPath(path) {
  let arr = state.tree, node = null;
  for (const i of path) { node = arr[i]; if (!node) return null; arr = node.children || []; }
  return node;
}

function findParent(node, arr = state.tree) {
  for (let i = 0; i < arr.length; i++) {
    if (arr[i] === node) return { arr, idx: i };
    if (arr[i].children) { const r = findParent(node, arr[i].children); if (r) return r; }
  }
  return null;
}

function removeNode(node) { const p = findParent(node); if (p) p.arr.splice(p.idx, 1); }

function isDescendant(group, node) {
  if (!group.children) return false;
  for (const c of group.children) {
    if (c === node) return true;
    if (c.children && isDescendant(c, node)) return true;
  }
  return false;
}

function depthOf(node) {
  function rec(arr, d) {
    for (const n of arr) {
      if (n === node) return d;
      if (n.children) { const r = rec(n.children, d + 1); if (r >= 0) return r; }
    }
    return -1;
  }
  return rec(state.tree, 0);
}

// Remove param nodes for deleted params; place new Fusion params (under the
// selection if one was just added via the dialog, else at the root).
function reconcileTree() {
  // Guard: if params is empty, Fusion hasn't synced yet (or briefly dropped the
  // connection). Don't wipe param nodes from groups — that destroys the tree org.
  const names = new Set(params.map(p => p.name));
  if (!names.size) return;
  const placed = new Set();
  let changed = false;
  (function walk(arr) {
    for (let i = arr.length - 1; i >= 0; i--) {
      const n = arr[i];
      if (n.type === 'param') {
        if (!names.has(n.name)) { arr.splice(i, 1); changed = true; }
        else placed.add(n.name);
      } else if (n.type === 'group') { walk(n.children); }
      // 'draft' nodes are local-only and have no children — leave them untouched.
    }
  })(state.tree);

  for (const p of params) {
    if (placed.has(p.name)) continue;
    const node = { type: 'param', name: p.name };
    const reveal = pendingInsert && pendingInsert.name === p.name && pendingInsert.select;
    if (pendingInsert && pendingInsert.name === p.name && placeNewParam(node)) {
      pendingInsert = null;
    } else {
      // A structured name (fusion_ui/param_naming.js) organizes itself into nested
      // groups by its own feature levels; anything else lands at the root exactly
      // as it always has. The last level is the parameter's own name word, not a group
      // (ParamNaming.groupLevels leaves it off) — UNLESS it is the only level past the
      // owner, which is always a group (never a bare name at the component's own top).
      // Deliberately NOT fed a nameIndex: that half of the rule (two parameters
      // sharing one chain promote it to a group between them) is derived fresh every
      // render in the Browser, but this tree is placed once at insertion and never
      // re-sorted — retroactively promoting an already-placed sibling when a second
      // one arrives would mean moving a row out from under a user who has since
      // dragged it elsewhere, which is exactly what "never re-sorts" rules out.
      const parsed = paramNamingReady ? ParamNaming.parse(p.name) : null;
      if (parsed) derivedGroupChildren(ParamNaming.groupLevels(parsed)).push(node);
      else state.tree.push(node);
    }
    if (reveal) {
      expandTo(p.name);
      state.selected = new Set(['param:' + p.name]);
      state.anchor = 'param:' + p.name;
    }
    changed = true;
  }
  if (enforceOrder(state.tree)) changed = true;   // params before groups everywhere
  if (changed) saveGroups();
}

// A parameter created on Fusion's side (the Link Driven Dimension hotkey) should
// land under THIS window's current selection, exactly as one added from the dialog
// does. The add-in can't see that selection, so it only announces the name and we
// resolve the destination here. The first token after load is adopted without
// acting, so a stale announcement from an earlier session can't re-file an old row.
let lastInsertToken = null;

// Parameter currently waiting for a viewport pick. The add-in is the authority — this
// is set optimistically on Enter and then reconciled from /params every poll, so a pick
// that lands, times out, or fails clears the row without the window having to track it.
let relinkName = '';
// What we last ASKED the add-in for, until it confirms. The add-in only drains its
// command queue on a 500 ms tick, so for up to half a second /params still reports the
// old state — without this guard the very next poll wipes the optimistic arm before it
// is ever visible, and the next Enter toggles instead of arming. Same shape as
// pendingAdds / pendingRenames.
let relinkPending = null;   // {name, ts}
let relinkArmedAt = 0;      // when the pick was armed, for the double-tap guard below

function applyRelink(fromServer) {
  if (relinkPending) {
    if (fromServer === relinkPending.name) relinkPending = null;              // confirmed
    else if (Date.now() - relinkPending.ts < OPTIMISTIC_TTL) return;          // in flight
    else relinkPending = null;                                                // never landed
  }
  relinkName = fromServer;
}

function applyPendingInsert(pi) {
  if (!pi) return;
  const tok = pi.token || 0;
  if (lastInsertToken === null) { lastInsertToken = tok; return; }  // adopt on load, don't act
  if (tok === lastInsertToken) return;
  lastInsertToken = tok;
  if (!pi.name) return;
  pendingInsert = { name: pi.name, select: true };
  const sel = selectedNodes();
  if (sel.length === 1) {
    if (sel[0].type === 'group') pendingInsert.insideId = sel[0].id;
    else if (sel[0].type === 'param') pendingInsert.afterKey = 'param:' + sel[0].name;
  }
}

// Expand every group on the path to a param row, so one filed into a collapsed
// group is actually visible when we select it.
function expandTo(name) {
  (function rec(arr, chain) {
    for (const n of arr) {
      if (n.type === 'param' && n.name === name) { chain.forEach(g => g.collapsed = false); return true; }
      if (n.type === 'group' && rec(n.children, chain.concat(n))) return true;
    }
    return false;
  })(state.tree, []);
}

// Place a freshly-added param node under the selection recorded at add time.
function placeNewParam(node) {
  if (pendingInsert.insideId) {
    const g = nodeByKey('group:' + pendingInsert.insideId);
    if (g) { g.children.push(node); return true; }
  } else if (pendingInsert.afterKey) {
    const target = nodeByKey(pendingInsert.afterKey);
    if (target) { const loc = findParent(target); if (loc) { loc.arr.splice(loc.idx + 1, 0, node); return true; } }
  }
  return false;
}

// Within every children array, parameters come before groups (stable). Returns
// true if anything moved.
function enforceOrder(arr) {
  let changed = false;
  const ps = arr.filter(n => n.type === 'param' || n.type === 'draft');  // drafts ride with params
  const gs = arr.filter(n => n.type === 'group');
  const sorted = ps.concat(gs);
  for (let i = 0; i < arr.length; i++) { if (arr[i] !== sorted[i]) { changed = true; break; } }
  if (changed) { arr.length = 0; arr.push(...sorted); }
  for (const g of gs) { if (enforceOrder(g.children)) changed = true; }
  return changed;
}

// ── Render ───────────────────────────────────────────────────────────────────

function render() {
  if (isEditing()) { dirty = true; return; }
  reconcileTree();
  const root = document.getElementById('groups-root');
  root.innerHTML = '<div id="drop-line"></div>' + renderNodes(state.tree, 0, []);
  applySelection();
}

function renderNodes(arr, depth, basePath) {
  let html = '';
  for (let i = 0; i < arr.length; i++) {
    const path = basePath.concat(i);
    const n = arr[i];
    html += (n.type === 'group') ? renderGroup(n, depth, path)
          : (n.type === 'draft') ? renderDraftRow(n, depth, path)
          : renderParamRow(n, depth, path);
  }
  return html;
}

function renderGroup(node, depth, path) {
  const pathJson = esc(JSON.stringify(path));
  const pad = 12 + depth * INDENT;             // group name sits at this indent
  let inner = node.collapsed ? '' : renderNodes(node.children, depth + 1, path);
  // Expanded but empty → show an "(empty)" placeholder line (matches the Browser).
  if (!node.collapsed && (!node.children || node.children.length === 0)) {
    const ppad = 8 + (depth + 1) * INDENT;
    inner = `<div class="empty-row" style="padding-left:${ppad}px">(empty)</div>`;
  }
  return `
<div class="group">
  <div class="group-header node-row" draggable="true"
       data-path="${pathJson}" data-depth="${depth}" data-kind="group" data-id="${esc(node.id)}"
       data-key="group:${esc(node.id)}"
       style="padding-left:${pad}px"
       ondragstart="onDragStart(event)" ondragend="onDragEnd(event)"
       onclick="onGroupClick(event)" ondblclick="onGroupDblClick(event)"
       onmousedown="maybeDragOff(event)">
    <span class="group-name">${esc(node.name)}</span>
  </div>
  <div class="group-children">${inner}</div>
</div>`;
}

function renderParamRow(node, depth, path) {
  const p = params.find(x => x.name === node.name) || { name: node.name, expression: '', unit: '', comment: '' };
  const driven = p.kind === 'driven';
  const pathJson = esc(JSON.stringify(path));
  // Indent the name so it lines up one level in from its group's name (the group
  // name sits after the arrow, so add ARROW_W to match, minus the input's padding).
  const pad = 8 + depth * INDENT + (ARROW_W - 6);
  const exprRaw = pending[p.name] !== undefined ? pending[p.name] : p.expression;
  const multiline = layoutFor(p.name) !== null;
  let exprVal = toDisplayExpr(exprRaw);     // "10 mm" → "10mm", "45 deg" → "45°"
  // A driven dimension's value goes in brackets, the same way Fusion brackets a
  // reference dimension in the viewport — it reads as "measured, not set".
  if (driven) exprVal = (relinkName === p.name) ? '(click a dimension…)' : '(' + exprVal + ')';
  const nameTitle = p.comment ? ` title="${esc(p.comment)}"` : '';
  const nameSize = Math.max((p.name || '').length + 1, 2);
  const exprSize = Math.max((exprVal || '').length + 1, 2);
  const ev = 'onfocus="onFieldFocus(event)" onblur="onFieldBlur(event)" ' +
             'onkeydown="onFieldKey(event)" onmousedown="dragOff(event)" ondblclick="enterEdit(event)"';
  return `
<div class="param-row node-row${driven ? ' driven' : ''}" draggable="true"
     data-path="${pathJson}" data-depth="${depth}" data-kind="param" data-name="${esc(p.name)}"
     data-key="param:${esc(p.name)}"
     ondragstart="onDragStart(event)" ondragend="onDragEnd(event)">
  <div class="col-name" style="padding-left:${pad}px">
    <input type="text" data-param="${esc(p.name)}" data-field="name"${nameTitle} readonly
      value="${esc(p.name)}" size="${nameSize}" spellcheck="false"
      oninput="autosize(event)" ${ev}>
  </div>
  <div class="col-expr">
    <input type="text" data-param="${esc(p.name)}" data-field="expression" readonly
      value="${esc(exprVal)}" size="${exprSize}" spellcheck="false" autocomplete="off"
      oninput="onExprInput(event)" ${ev}>${multiline ? '<span class="ml-mark">↵</span>' : ''}
  </div>
</div>`;
}

// A draft (placeholder) parameter: empty name and/or expression, not yet in Fusion.
// Both fields are always editable (single-click to type) and show "[ ]" when empty.
// Filling both with a valid name + expression promotes it to a real parameter.
function renderDraftRow(node, depth, path) {
  const pathJson = esc(JSON.stringify(path));
  const pad = 8 + depth * INDENT + (ARROW_W - 6);
  const nameSize = Math.max((node.name || '').length + 1, 3);
  const exprSize = Math.max((node.expression || '').length + 1, 3);
  const ev = 'onfocus="onDraftFocus(event)" onblur="onDraftBlur(event)" ' +
             'onkeydown="onDraftKey(event)" onmousedown="dragOff(event)"';
  return `
<div class="param-row draft node-row" draggable="true"
     data-path="${pathJson}" data-depth="${depth}" data-kind="draft" data-id="${esc(node.id)}"
     data-key="draft:${esc(node.id)}">
  <div class="col-name" style="padding-left:${pad}px">
    <input type="text" data-draft="${esc(node.id)}" data-field="name" placeholder="[ ]"
      value="${esc(node.name)}" size="${nameSize}" spellcheck="false" autocomplete="off"
      oninput="autosize(event)" ${ev}>
  </div>
  <div class="col-expr">
    <input type="text" data-draft="${esc(node.id)}" data-field="expression" placeholder="[ ]"
      value="${esc(node.expression)}" size="${exprSize}" spellcheck="false" autocomplete="off"
      oninput="onExprInput(event)" ${ev}>
  </div>
</div>`;
}

// ── Draft (placeholder) parameters ──────────────────────────────────────────────

function draftById(id) {
  let found = null;
  (function walk(arr) {
    for (const n of arr) {
      if (n.type === 'draft' && n.id === id) found = n;
      else if (n.children) walk(n.children);
    }
  })(state.tree);
  return found;
}

function onDraftFocus(e) {
  const id = e.target.dataset.draft;
  selectSingle('draft:' + id);
  const r = e.target.closest('.node-row'); if (r) r.draggable = false;   // let text be selected
}

function onDraftKey(e) {
  if (acHandleKey(e)) return;
  if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); e.target.blur(); }
  else if (e.key === 'Escape') { e.stopPropagation(); e.target.blur(); }
}

function onDraftBlur(e) {
  const t = e.target;
  const node = draftById(t.dataset.draft);
  acHide();
  const row = t.closest('.node-row'); if (row) row.draggable = true;
  if (!node) return;
  node[t.dataset.field] = t.value;     // store the typed value back into the draft
  // Promote as soon as both fields are valid; otherwise persist + re-render so the
  // "[ ]" placeholders and sizing stay correct.
  setTimeout(() => {
    if (isEditing()) return;           // focus moved to the other draft field — wait
    if (!promoteDraft(node)) { saveGroups(); render(); }
  }, 0);
}

// Turn a fully-filled draft into a real Fusion parameter, in place (keeping its
// position in the tree). Returns true if it was promoted.
function promoteDraft(node) {
  const name = (node.name || '').trim();
  const exprDisp = (node.expression || '').trim();
  if (!/^[A-Za-z_]\w*$/.test(name) || !exprDisp) return false;   // not ready yet
  if (params.find(p => p.name === name)) { alert('A parameter named "' + name + '" already exists.'); return false; }
  const expr = fromDisplayExpr(exprDisp);
  const unit = node.unit == null ? 'mm' : node.unit;   // '' is a real choice (unitless)
  const comment = node.comment || '';
  // Convert the draft node to a param node in place.
  delete node.id; delete node.expression; delete node.unit; delete node.comment;
  node.type = 'param'; node.name = name;
  state.selected = new Set(['param:' + name]); state.anchor = 'param:' + name;
  pendingAdds[name] = { param: { name, expression: expr, unit, comment, deps: 0 }, ts: Date.now() };
  saveGroups();
  deriveParams();
  render();
  fetch('/add', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, expression: expr, unit, comment })
  }).then(r => r.json()).then(res => {
    if (!(res && res.ok)) {
      delete pendingAdds[name];
      // Roll the param node back into a draft so the user's input isn't lost.
      node.type = 'draft'; node.id = newId(); node.name = name;
      node.expression = exprDisp; node.unit = unit; node.comment = comment;
      saveGroups(); deriveParams(); render();
      alert((res && res.error) || 'Failed to add parameter "' + name + '".');
    }
  }).catch(() => {
    delete pendingAdds[name];
    node.type = 'draft'; node.id = newId(); node.name = name;
    node.expression = exprDisp; node.unit = unit; node.comment = comment;
    saveGroups(); deriveParams(); render();
  });
  return true;
}

// Create an empty draft under the current selection (or at the root) and focus it.
function addDraft(name, expression, unit, comment) {
  const node = { type: 'draft', id: newId(), name: name || '', expression: expression || '',
                 unit: unit == null ? 'mm' : unit, comment: comment || '' };
  const sel = selectedNodes();
  if (sel.length === 1 && sel[0].type === 'group') sel[0].children.unshift(node);
  else if (sel.length === 1) { const loc = findParent(sel[0]); if (loc) loc.arr.splice(loc.idx + 1, 0, node); else state.tree.push(node); }
  else state.tree.push(node);
  state.selected = new Set(['draft:' + node.id]); state.anchor = 'draft:' + node.id;
  saveGroups();
  render();
  // Focus the first empty field so the user can type immediately.
  const field = !node.name ? 'name' : 'expression';
  const inp = document.querySelector('.param-row.draft[data-id="' + cssAttr(node.id) + '"] input[data-field="' + field + '"]');
  if (inp) { inp.focus(); inp.select(); }
}

// ── Selection (single / Cmd-cumulative / Shift-range) ───────────────────────────

function selectSingle(key) { state.selected = new Set([key]); state.anchor = key; applySelection(); }
function clearSelection() { state.selected.clear(); state.anchor = null; applySelection(); }

function applySelection() {
  document.querySelectorAll('.node-row').forEach(r => {
    r.classList.toggle('selected', state.selected.has(r.dataset.key));
  });
}

function visibleKeys() {
  return [...document.querySelectorAll('#groups-root .node-row')].map(r => r.dataset.key);
}

// Common click handling for a row, honouring Cmd (toggle) and Shift (range).
function handleRowClick(e, key) {
  const order = visibleKeys();
  if (e.shiftKey && state.anchor && order.includes(state.anchor)) {
    const a = order.indexOf(state.anchor), b = order.indexOf(key);
    const [lo, hi] = a < b ? [a, b] : [b, a];
    state.selected = new Set(order.slice(lo, hi + 1));   // anchor stays put
  } else if (e.metaKey) {
    if (state.selected.has(key)) state.selected.delete(key);
    else state.selected.add(key);
    state.anchor = key;
  } else {
    state.selected = new Set([key]);
    state.anchor = key;
  }
  applySelection();
}

let _lastGroupClick = { key: null, t: 0 };
function onGroupClick(e) {
  if (e.target.closest('.group-del') || e.target.classList.contains('group-name-input')) return;
  const key = e.currentTarget.dataset.key;
  if (e.metaKey || e.shiftKey) { handleRowClick(e, key); return; }
  // ⌥-click (a held Space — Karabiner/remapd put the Option on the click) selects the
  // group and leaves it open or shut — or, already the whole selection, deselects it.
  if (e.altKey) {
    if (state.selected.size === 1 && state.selected.has(key)) clearSelection();
    else handleRowClick(e, key);
    return;
  }
  // Only treat a click as the second half of a double-click (→ skip the toggle,
  // let ondblclick rename) when it lands on the SAME group in quick succession.
  // macOS bumps e.detail for any click that closely follows another — even on a
  // different row — which used to swallow legitimate single clicks on a group
  // right after selecting a parameter.
  const now = Date.now();
  const repeat = e.detail > 1 && _lastGroupClick.key === key && (now - _lastGroupClick.t) < 600;
  _lastGroupClick = { key, t: now };
  if (repeat) return;
  handleRowClick(e, key);
  toggleCollapseEl(e.currentTarget);
}

// Click on empty space (below the list, or a gap) clears the selection.
function onMainClick(e) {
  if (e.target.closest('.node-row')) return;
  if (e.target.closest('#modal-bg')) return;
  clearSelection();
}

// Resolve selected row keys back to tree nodes, in visible order.
function selectedNodes() {
  return visibleKeys().filter(k => state.selected.has(k)).map(nodeByKey).filter(Boolean);
}

function nodeByKey(key) {
  const i = key.indexOf(':');
  const kind = key.slice(0, i), id = key.slice(i + 1);
  let found = null;
  (function walk(arr) {
    for (const n of arr) {
      if (kind === 'param' && n.type === 'param' && n.name === id) found = n;
      else if (kind === 'group' && n.type === 'group' && n.id === id) found = n;
      else if (kind === 'draft' && n.type === 'draft' && n.id === id) found = n;
      if (n.children) walk(n.children);
    }
  })(state.tree);
  return found;
}

// Group the current selection into a new group (Cmd-G). Descendants of a selected
// group ride along inside it; the new group lands where the topmost item was.
function groupSelection() {
  if (document.activeElement) document.activeElement.blur();
  let nodes = selectedNodes();
  // Drop any node whose ancestor is also selected (it moves with the ancestor).
  nodes = nodes.filter(n => !nodes.some(m => m !== n && m.type === 'group' && isDescendant(m, n)));
  if (!nodes.length) return;

  const first = nodes[0];
  const loc = findParent(first);
  if (!loc) return;
  const group = { id: newId(), type: 'group', name: 'Group', collapsed: false, children: [] };
  loc.arr.splice(loc.idx, 1, group);          // replace the topmost item with the group
  for (const n of nodes) if (n !== first) removeNode(n);
  group.children = nodes.slice();             // move all (in visible order) inside

  saveGroups();
  state.selected = new Set(['group:' + group.id]);
  state.anchor = 'group:' + group.id;
  render();
  startRenameGroupById(group.id);   // name field active, ready to type
}

// Put a group's name straight into rename mode (after a render).
function startRenameGroupById(id) {
  const span = document.querySelector('.group-header[data-id="' + id + '"] .group-name');
  if (span) startRenameGroup({ target: span, stopPropagation() {} });
}

// Double-click empty space on a group header toggles it (same as the arrow). The
// name's own dblclick (rename) stops propagation, so it won't reach here.
// Double-click a group renames it (cancelling the pending single-click collapse).
function onGroupDblClick(e) {
  if (e.target.closest('.group-del') || e.target.classList.contains('group-name-input')) return;
  const span = e.currentTarget.querySelector('.group-name');
  if (span) startRenameGroup({ target: span, stopPropagation() {} });
}

function toggleCollapseEl(header) {
  if (!header) return;
  const node = nodeByPath(JSON.parse(header.dataset.path));
  if (node) setGroupCollapsed(node, !node.collapsed, false);
}

// ── Field editing (name / expression) ──────────────────────────────────────────

function fkey(name, field) { return name + ' ' + field; }

function isEditing() {
  if (bigEdit) return true;        // the full-window editor owns the keyboard while open
  const a = document.activeElement;
  return !!(a && a.tagName === 'INPUT' && !a.readOnly && a.closest && a.closest('#groups-root'));
}

// Double-click a name/expression to edit it; a single click just selects the row.
function startRelink(name) {
  relinkName = name;                 // optimistic; /params confirms within a tick
  relinkPending = { name, ts: Date.now() };
  relinkArmedAt = Date.now();
  render();
  fetch('/relink', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, why: 'enter' }) }).catch(() => { relinkName = ''; render(); });
}

function cancelRelink(why) {
  relinkName = '';
  relinkPending = { name: '', ts: Date.now() };
  render();
  fetch('/relink', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: '', why: why || 'cancel' }) }).catch(() => {});
}

// True for a row backed by a driven (reference) dimension — measured by Fusion.
function isDriven(name) {
  const p = params.find(x => x.name === name);
  return !!(p && p.kind === 'driven');
}

function enterEdit(e) {
  const t = e.target;
  if (t.tagName !== 'INPUT') return;
  if (!t.readOnly) {
    // Already editing: a SECOND double-click, on a word that names another real
    // parameter, drills into IT instead of the browser's native "select this word" —
    // see "Nested edit stack" below.
    if (t.dataset.field === 'expression') tryEnterNested(t);
    return;
  }
  if (t.dataset.field === 'expression') {
    // A double-click that opens a field FRESH (not arrived at via the nested stack —
    // see navigatingNested) starts a new trail rather than extending the old one.
    if (!navigatingNested) { editStack = []; renderEditBreadcrumb(); }
    // Fusion measures a driven dimension, so there is no expression to edit. The name
    // field is deliberately still editable — renaming the parameter is allowed.
    if (isDriven(t.dataset.param)) return;
    // An expression that already has line breaks can't be shown in a one-line field
    // without losing them, so it always opens in the big box.
    const ml = layoutFor(t.dataset.param);
    if (ml !== null) { openBigEdit(t.dataset.param, ml); return; }
  }
  t.readOnly = false;
  beginEdit(t);     // set up BEFORE focus(): if the single-click already focused this
  t.focus();        // read-only input, focus() won't re-fire onFieldFocus.
  t.select();
}

// ── Nested edit stack ────────────────────────────────────────────────────────
// Double-clicking a WORD inside an expression that names another real parameter
// opens THAT one's own row for editing instead — recursively, as deep as the
// references go. Deliberately simple about what "leaving" a level means: focusing
// the nested row's field blurs this one, and blur already commits whatever was
// typed (see onFieldBlur) — exactly what would happen clicking to any other row by
// hand, so nesting spends no special effort preserving an unfinished edit. editStack
// remembers the NAMES on the trail (root first) so Escape and the breadcrumb can
// step back to editing the parameter you came from, not just close outward.
let editStack = [];
// True only for the duration of a call to enterExpression() made BY this stack
// (tryEnterNested / stepEditBack) — how enterEdit tells "arrived here via nesting"
// from "a fresh double-click on an unrelated row", which resets the trail instead.
let navigatingNested = false;

// The identifier at `pos` in `text` — same word-boundary rule as fusion_value's
// tokenAtCaret, just local to this file rather than shared (no other reason to pull
// naming/param_naming's own regex machinery in for one line).
function wordAt(text, pos) {
  const IDENT = /[A-Za-z0-9_]/;
  if (!IDENT.test(text[pos] || '') && !IDENT.test(text[pos - 1] || '')) return null;
  let a = pos, b = pos;
  while (a > 0 && IDENT.test(text[a - 1])) a--;
  while (b < text.length && IDENT.test(text[b])) b++;
  return text.slice(a, b) || null;
}

function tryEnterNested(t) {
  const word = wordAt(t.value, t.selectionStart);
  if (!word || word === t.dataset.param) return;      // no word here, or its own name
  if (!params.some(p => p.name === word)) return;      // not a real parameter
  editStack.push(t.dataset.param);
  navigatingNested = true;
  try { enterExpression(word); } finally { navigatingNested = false; }
  renderEditBreadcrumb();
}

// Escape (see onFieldKey) or a breadcrumb crumb's own click: step back to editing
// the parameter this one was reached from. Silently does nothing at the root — the
// caller falls through to its own escape behaviour (closing the field outright).
function stepEditBack() {
  if (!editStack.length) return false;
  const prev = editStack.pop();
  navigatingNested = true;
  try { enterExpression(prev); } finally { navigatingNested = false; }
  renderEditBreadcrumb();
  return true;
}

// Jump straight to depth `i` of the trail (a breadcrumb crumb was clicked), rather
// than stepping back one at a time.
function jumpEditBreadcrumb(i) {
  if (i < 0 || i >= editStack.length) return;
  const name = editStack[i];
  editStack.length = i;
  navigatingNested = true;
  try { enterExpression(name); } finally { navigatingNested = false; }
  renderEditBreadcrumb();
}

function renderEditBreadcrumb() {
  const el = document.getElementById('edit-breadcrumb');
  if (!el) return;
  if (!editStack.length) { el.classList.add('hide'); el.innerHTML = ''; return; }
  el.classList.remove('hide');
  const parts = editStack.map((name, i) =>
    '<span class="crumb" onclick="jumpEditBreadcrumb(' + i + ')">' + esc(name) + '</span>');
  const current = [...state.selected].find(k => k.startsWith('param:'));
  parts.push('<span class="crumb current">' +
             esc(current ? current.slice(6) : '…') + '</span>');
  el.innerHTML = parts.join('<span class="sep">›</span>');
}

// Begin an edit: strip the trailing unit from expressions ("10 mm" → "10") and
// record the original so onFieldBlur can commit / re-append the unit. Idempotent.
function beginEdit(t) {
  const key = fkey(t.dataset.param, t.dataset.field);
  if (editing[key]) return;
  let strippedUnit = '';
  if (t.dataset.field === 'expression') {
    lastEditOpenAt = Date.now();
    const p = params.find(x => x.name === t.dataset.param);
    const unit = p ? (p.unit || '') : '';
    // Value shown is the display form ("10mm" / "45°"); strip that unit glyph.
    const stripped = stripTrailingUnit(t.value, displayUnit(unit));
    if (stripped !== null) { t.value = stripped; strippedUnit = unit; }
    autosize(t);
  }
  selectSingle('param:' + t.dataset.param);
  editing[key] = { orig: t.value, strippedUnit };
}

// Unit shown to the user: degrees as the ° glyph, everything else as-is. A
// unitless parameter ('') shows nothing after its value.
function displayUnit(u) { return u === 'deg' ? '°' : (u || ''); }

// The three unit choices, cycled by the Add dialog's toggle. '' = unitless: the
// parameter is a plain number (counts, ratios, multipliers).
const UNITS = ['mm', 'deg', ''];
function unitLabel(u) { return u === '' ? 'none' : displayUnit(u); }

// Fusion expression → display: drop the space before mm and show deg as °.
function toDisplayExpr(expr) {
  let e = String(expr == null ? '' : expr);
  e = e.replace(/(\d(?:\.\d+)?)\s*deg\b/g, '$1°');
  e = e.replace(/(\d(?:\.\d+)?)\s+mm\b/g, '$1mm');
  return e;
}

// Display → Fusion expression: turn ° back into deg (mm needs no change).
function fromDisplayExpr(text) {
  return String(text).replace(/(\d(?:\.\d+)?)\s*°/g, '$1deg');
}

// Whitespace/case-insensitive compare so "12mm" (what we send) matches "12 mm"
// (what Fusion may store), letting the optimistic pending value clear.
function normExpr(s) { return String(s == null ? '' : s).replace(/\s+/g, '').toLowerCase(); }

function onFieldFocus(e) {
  const t = e.target;
  if (t.readOnly) return;   // single-click focus on a label — not editing
  beginEdit(t);
}

function stripTrailingUnit(expr, unit) {
  if (!unit) return null;
  const re = new RegExp('^\\s*([-+]?[0-9.]+)\\s*' + escapeRegex(unit) + '\\s*$');
  const m = String(expr).match(re);
  return m ? m[1] : null;
}

function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// +1 char of slack: `size` is in average-width chars, so wide glyphs can clip the
// last character without a small buffer.
function autosize(e) {
  const t = e.target || e;
  if (t.tagName === 'TEXTAREA') return;      // the big box fills its panel; `size` means nothing there
  t.size = Math.max((t.value || '').length + 1, 2);
}

function onFieldKey(e) {
  if (acHandleKey(e)) return;
  if (e.key === 'Enter') {
    // A driven row has no expression to open, so swallowing Enter here would strand it:
    // the field handler runs first (inline onkeydown) and its stopPropagation would keep
    // the document handler — which arms the viewport pick — from ever seeing the key.
    // Clicking a row focuses its input, so this is the normal case, not an edge one.
    if (e.target.readOnly && isDriven(e.target.dataset.param)) return;
    e.preventDefault(); e.stopPropagation();
    // Read-only (focused but not editing) → Enter opens the expression; otherwise
    // Enter commits the current edit. stopPropagation avoids the document handler
    // re-opening it after blur.
    if (e.target.readOnly) enterExpression(e.target.dataset.param);
    else if (wantsBigEdit(e.target)) openBigEditFrom(e.target);
    else e.target.blur();
  } else if (e.key === 'Escape') {
    e.stopPropagation();
    const t = e.target;
    const k = fkey(t.dataset.param, t.dataset.field);
    if (editing[k]) { t.value = editing[k].orig; delete editing[k]; }
    // Nested: Escape closes the CURRENT level first, back to the parameter this one
    // was reached from — not straight out. stepEditBack() focuses that row's field,
    // which blurs (and so closes) this one as a side effect; only the last Escape,
    // at the root of the trail, falls through to the plain t.blur() below.
    if (t.dataset.field === 'expression' && stepEditBack()) return;
    t.blur();
  }
}

function onFieldBlur(e) {
  const t = e.target;
  const name = t.dataset.param, field = t.dataset.field;
  const k = fkey(name, field);
  const rec = editing[k];
  delete editing[k];
  acHide();
  const row = t.closest('.param-row'); if (row) row.draggable = true;
  t.readOnly = true;             // back to label mode; double-click to edit again

  if (rec) {
    const typed = t.value.trim();
    if (typed !== rec.orig.trim()) {
      let toSend = typed;
      if (field === 'expression' && rec.strippedUnit && /^[-+]?[0-9.]+$/.test(typed)) {
        toSend = typed + rec.strippedUnit;          // no space: "12mm" / "12deg"
      }
      if (field === 'expression') toSend = fromDisplayExpr(toSend);
      commitField(name, field, toSend);
    }
  }

  // Restore the expression's display text (with unit) immediately on blur, so the
  // unit reappears even when focus jumps to another field (no full re-render then).
  if (field === 'expression') {
    const shown = pending[name] !== undefined ? pending[name]
                : (params.find(x => x.name === name) || {}).expression;
    if (shown !== undefined) { t.value = toDisplayExpr(shown); autosize(t); }
  }

  setTimeout(() => {
    if (!isEditing()) {
      dirty = false; render();
      // Committed/closed out rather than stepping back into a parent (that path
      // returns before ever reaching a plain blur — see onFieldKey) — nothing left
      // to jump back into, so the trail goes with it.
      if (editStack.length) { editStack = []; renderEditBreadcrumb(); }
    }
  }, 0);
}

function commitField(name, field, val) {
  let body;
  if (field === 'expression') {
    if (!val) return;
    pending[name] = val;
    pendingMeta[name] = { was: (params.find(x => x.name === name) || {}).expression, ts: Date.now() };
    body = { name, expression: val };
  } else if (field === 'name') {
    // Fusion cannot hold a space in a parameter name and ignores one that does without
    // erroring, so squash them here — before the optimistic rename below commits to a
    // name Fusion would never take.
    val = ParamNaming.joinName(val);
    if (!val || val === name) return;
    if (params.find(x => x.name === val)) { alert('A parameter named "' + val + '" already exists.'); return; }
    // Rename the param node in the tree so its placement survives.
    (function upd(arr) {
      for (const n of arr) {
        if (n.type === 'param' && n.name === name) n.name = val;
        else if (n.children) upd(n.children);
      }
    })(state.tree);
    if (state.selected.has('param:' + name)) {
      state.selected.delete('param:' + name);
      state.selected.add('param:' + val);
      if (state.anchor === 'param:' + name) state.anchor = 'param:' + val;
    }
    saveGroups();
    // Keep the row under its new name in `params` until Fusion confirms, so the
    // render triggered right after this blur doesn't reconcile it out of its group.
    pendingRenames[val] = { old: name, ts: Date.now() };
    renameLayout(name, val);
    deriveParams();
    body = { name, newName: val };
  }
  fetch('/set', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).catch(() => { delete pending[name]; delete pendingMeta[name]; });
}

// ── Drag & drop (reorder + nest, Photoshop-style) ──────────────────────────────

let dragPath = null;       // path of the node being dragged
let dropInfo = null;       // {targetPath, after, inside} | {root:true} | null
let _pendingDeselect = null; // param key a click deselects (see mousedown)
let _pendingSingle = null; // param key whose collapse-to-single is deferred (see mousedown)

// While editing, turn off the row's draggable so click-drag inside the input
// selects text. When read-only (not editing) we leave the row draggable so it can
// be dragged from anywhere, including over the text.
function dragOff(e) {
  if (e.target.readOnly) return;
  const r = e.target.closest('.node-row'); if (r) r.draggable = false;
}
// The group-name rename input does the same to its header.
function maybeDragOff(e) {
  if (e.target.classList.contains('group-name-input')) {
    const r = e.target.closest('.node-row'); if (r) r.draggable = false;
  }
}

function onDragStart(e) {
  const row = e.currentTarget;
  _pendingSingle = null;     // a drag happened → keep the multi-selection intact
  dragPath = JSON.parse(row.dataset.path);
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', row.dataset.kind);
  setTimeout(() => row.classList.add('drag-source'), 0);
}

function onDragEnd() {
  document.querySelectorAll('.drag-source').forEach(el => el.classList.remove('drag-source'));
  hideDropIndicator();
  dragPath = null; dropInfo = null;
  clearSelection();            // releasing a drag clears the selection
}

function onListDragOver(e) {
  if (!dragPath) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const rows = [...document.querySelectorAll('#groups-root .node-row')];
  const y = e.clientY;
  let target = null, after = false, inside = false, insideTop = false;

  for (const r of rows) {
    const rc = r.getBoundingClientRect();
    if (y >= rc.top && y <= rc.bottom) {
      target = r;
      const rel = (y - rc.top) / rc.height;
      if (r.dataset.kind === 'group') {
        const node = nodeByPath(JSON.parse(r.dataset.path));
        const expanded = node && !node.collapsed && node.children && node.children.length;
        if (rel < 0.30) { after = false; }       // top → before the group (as sibling)
        else if (expanded) { inside = true; insideTop = true; }  // anywhere lower on an
        // expanded group → inside it (no finicky "after"; its children are right there).
        else if (rel > 0.70) { after = true; }   // collapsed group: bottom → after
        else { inside = true; }                  // collapsed group: middle → inside
      } else {
        after = rel > 0.5;
      }
      break;
    }
  }

  if (!target) {
    if (rows.length && y > rows[rows.length - 1].getBoundingClientRect().bottom) {
      dropInfo = { root: true };
      showDropIndicatorAtEnd();
    } else { hideDropIndicator(); dropInfo = null; }
    return;
  }

  dropInfo = { targetPath: JSON.parse(target.dataset.path), after, inside, insideTop };
  showDropIndicator(target, after, inside);
}

function showDropIndicator(targetEl, after, inside) {
  document.querySelectorAll('.group.drop-inside').forEach(el => el.classList.remove('drop-inside'));
  const line = document.getElementById('drop-line');
  if (inside) {
    line.style.display = 'none';
    const grp = targetEl.closest('.group');
    if (grp) grp.classList.add('drop-inside');
    return;
  }
  const root = document.getElementById('groups-root');
  const rr = root.getBoundingClientRect();
  const rc = targetEl.getBoundingClientRect();
  const depth = parseInt(targetEl.dataset.depth) || 0;
  line.style.left = (8 + depth * INDENT) + 'px';
  line.style.right = '8px';
  line.style.top = ((after ? rc.bottom : rc.top) - rr.top) + 'px';
  line.style.display = 'block';
}

function showDropIndicatorAtEnd() {
  document.querySelectorAll('.group.drop-inside').forEach(el => el.classList.remove('drop-inside'));
  const root = document.getElementById('groups-root');
  const rows = root.querySelectorAll('.node-row');
  const line = document.getElementById('drop-line');
  if (!rows.length) { line.style.display = 'none'; return; }
  const rr = root.getBoundingClientRect();
  const rc = rows[rows.length - 1].getBoundingClientRect();
  line.style.left = '8px'; line.style.right = '8px';
  line.style.top = (rc.bottom - rr.top) + 'px';
  line.style.display = 'block';
}

function hideDropIndicator() {
  const line = document.getElementById('drop-line');
  if (line) line.style.display = 'none';
  document.querySelectorAll('.group.drop-inside').forEach(el => el.classList.remove('drop-inside'));
}

function keyOf(n) {
  if (n.type === 'param') return 'param:' + n.name;
  if (n.type === 'draft') return 'draft:' + n.id;
  return 'group:' + n.id;
}

// Selected nodes minus any whose ancestor is also selected (they ride along).
function topLevelSelected(nodes) {
  return nodes.filter(n => !nodes.some(m => m !== n && m.type === 'group' && isDescendant(m, n)));
}

function onListDrop(e) {
  if (!dragPath || !dropInfo) return;
  e.preventDefault();
  const dragNode = nodeByPath(dragPath);
  if (!dragNode) { onDragEnd(); return; }

  // If the grabbed item is part of a multi-selection, move the whole selection.
  let moving = [dragNode];
  if (state.selected.has(keyOf(dragNode))) {
    const sel = topLevelSelected(selectedNodes());
    if (sel.length > 1) moving = sel;
  }

  if (dropInfo.root) {
    for (const n of moving) removeNode(n);
    state.tree.push(...moving);
    saveGroups(); render(); return;
  }

  const targetNode = nodeByPath(dropInfo.targetPath);
  if (!targetNode || moving.includes(targetNode)) { onDragEnd(); return; }
  // Can't drop a moving group into itself or a descendant.
  for (const m of moving) {
    if (m.type === 'group' && isDescendant(m, targetNode)) { onDragEnd(); return; }
  }

  if (dropInfo.inside && targetNode.type === 'group') {
    if (depthOf(targetNode) + 1 > 9) { onDragEnd(); return; }   // cap nesting at 10 levels
    for (const n of moving) removeNode(n);
    // insideTop (hovering an expanded group's header) lands at the top, right
    // under the header where the cursor is; otherwise append to the end.
    if (dropInfo.insideTop) targetNode.children.unshift(...moving);
    else targetNode.children.push(...moving);
  } else {
    for (const n of moving) removeNode(n);
    const loc = findParent(targetNode);     // recompute after removal (object identity)
    if (!loc) { state.tree.push(...moving); saveGroups(); render(); return; }
    loc.arr.splice(loc.idx + (dropInfo.after ? 1 : 0), 0, ...moving);
  }
  saveGroups(); render();
}

// ── Group management ──────────────────────────────────────────────────────────

// "+ Group" / Cmd-G: wrap the selection in a new group, or (nothing selected)
// add an empty group at the root. The new group is selected; double-click to rename.
function addGroup() {
  if (state.selected.size > 0) { groupSelection(); return; }
  const group = { id: newId(), type: 'group', name: 'Group', collapsed: false, children: [] };
  state.tree.push(group);
  saveGroups();
  state.selected = new Set(['group:' + group.id]);
  state.anchor = 'group:' + group.id;
  render();
  startRenameGroupById(group.id);   // name field active, ready to type
}

// Remove a group, moving its contents up a level. No confirmation.
function deleteGroupNode(node) {
  const loc = findParent(node);
  if (loc) loc.arr.splice(loc.idx, 1, ...node.children);
}

// Cmd-Delete: delete every selected group (contents move up). Selected params ignored
// (deleting a param node would just re-appear on the next sync from Fusion).
function deleteSelectedGroups() {
  const groups = selectedNodes().filter(n => n.type === 'group');
  if (!groups.length) return;
  for (const g of groups) deleteGroupNode(g);
  clearSelection();
  saveGroups();
  render();
}

// Ungroup (the `ungroup` master shortcut): only when everything selected is a group —
// a parameter picked inside a group never ungroups the group around it.
function ungroupSelection() {
  const sel = selectedNodes();
  if (!sel.length || sel.some(n => n.type !== 'group')) return;
  deleteSelectedGroups();
}

// Cmd-Backspace: if any group is selected, delete the group(s) (contents move up).
// Otherwise, if a single parameter is selected, delete it from Fusion (with the
// reassignment prompt below).
function deleteSelection() {
  const drafts = selectedNodes().filter(n => n.type === 'draft');
  if (drafts.length) { for (const d of drafts) removeNode(d); clearSelection(); saveGroups(); render(); return; }
  const groups = selectedNodes().filter(n => n.type === 'group');
  if (groups.length) { deleteSelectedGroups(); return; }
  deleteSelectedParam();
}

// Cmd-D: delete the selected parameter from Fusion. If anything depends on it
// (other parameters, sketch dimensions, feature inputs…), prompt for a parameter
// to replace those references with, then delete.
function deleteSelectedParam() {
  // A lone draft (not yet a real parameter) just gets removed from the tree.
  const drafts = selectedNodes().filter(n => n.type === 'draft');
  if (drafts.length) { for (const d of drafts) removeNode(d); clearSelection(); saveGroups(); render(); return; }
  // Derive straight from state.selected (not the DOM via selectedNodes), so a
  // poll-driven re-render that briefly rebuilds the rows can't make this no-op.
  const names = [...state.selected].filter(k => k.startsWith('param:')).map(k => k.slice(6));
  if (names.length !== 1) return;     // single-parameter delete only
  const name = names[0];
  const p = params.find(x => x.name === name) || {};
  // Nothing depends on it → just delete, no dialog. Only prompt for a replacement
  // when something would break.
  if ((+p.deps || 0) === 0) submitDelete(name);
  else openDeleteDialog(name);
}

function openDeleteDialog(name) {
  if (document.getElementById('modal-bg')) return;
  const p = params.find(x => x.name === name) || {};
  const deps = +p.deps || 0;
  const others = params.filter(x => x.name !== name).map(x => x.name);

  let body;
  if (deps > 0) {
    const dl = others.map(n => `<option value="${esc(n)}"></option>`).join('');
    body = `
    <p style="font-size:12px;color:var(--text-dim);margin-bottom:12px;line-height:1.5;">
      <b style="color:var(--text);">${esc(name)}</b> is used by ${deps} thing${deps === 1 ? '' : 's'}.
      Replace those references with a parameter or expression (leave blank to delete anyway):</p>
    <label style="margin-bottom:12px;">Replacement
      <input id="d-repl" list="d-repl-list" autocomplete="off" spellcheck="false"
             placeholder="parameter name or expression">
      <datalist id="d-repl-list">${dl}</datalist></label>`;
  } else {
    body = `<p style="font-size:12px;color:var(--text-dim);margin-bottom:14px;line-height:1.5;">
      Delete <b style="color:var(--text);">${esc(name)}</b>? Nothing depends on it.</p>`;
  }

  const html = `
<div id="modal-bg" onmousedown="if(event.target===this)closeDeleteDialog()">
  <div id="modal">
    <h2>Delete Parameter</h2>
    ${body}
    <div id="modal-err"></div>
    <div class="modal-btns">
      <button class="btn" onmousedown="event.preventDefault()" onclick="closeDeleteDialog()">Cancel</button>
      <button class="btn btn-accent" onmousedown="event.preventDefault()"
        onclick="submitDelete('${cssAttr(name).replace(/'/g, "\\'")}')">Delete</button>
    </div>
  </div>
</div>`;
  document.body.insertAdjacentHTML('beforeend', html);
  const sel = document.getElementById('d-repl');
  if (sel) sel.focus();
  document.getElementById('modal-bg').addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDeleteDialog();
    else if (e.key === 'Enter') { e.preventDefault(); submitDelete(name); }
  });
}

function closeDeleteDialog() { const m = document.getElementById('modal-bg'); if (m) m.remove(); }

function submitDelete(name) {
  const sel = document.getElementById('d-repl');
  const replacement = sel ? sel.value : '';
  closeDeleteDialog();

  // Hide the row instantly; deriveParams() keeps it hidden until Fusion confirms
  // removal, or un-hides it if the delete fails (e.g. still in use, no replacement).
  pendingDeletes[name] = Date.now();
  clearLayout(name);
  if (state.selected.has('param:' + name)) clearSelection();
  deriveParams();
  render();

  fetch('/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(replacement ? { name, replacement } : { name })
  }).then(r => r.json()).then(res => {
    if (!(res && res.ok)) {
      delete pendingDeletes[name]; deriveParams(); render();
      alert((res && res.error) || 'Could not delete "' + name + '".');
    }
  }).catch(() => { delete pendingDeletes[name]; deriveParams(); render(); });
}

// Cmd-R: rename the first selected item (same as double-clicking its name).
function renameSelected() {
  const nodes = selectedNodes();
  if (!nodes.length) return;
  const n = nodes[0];
  if (n.type === 'param') {
    const inp = document.querySelector(
      '.param-row[data-name="' + cssAttr(n.name) + '"] input[data-field="name"]');
    if (inp) enterEdit({ target: inp });
  } else {
    const span = document.querySelector('.group-header[data-id="' + n.id + '"] .group-name');
    if (span) startRenameGroup({ target: span, stopPropagation() {} });
  }
}

function cssAttr(s) { return String(s).replace(/["\\]/g, '\\$&'); }

// FusionDock intercepts Cmd+R at the AppKit level (WebKit swallows it as "reload"
// before the page ever sees a keydown) and calls window.__rename on whichever pane
// the cursor is over. Without this hook Cmd+R renamed in the Browser pane but did
// nothing over Parameters. Cmd+D and the rest still arrive as normal keydowns.
window.__rename = function () {
  if (isEditing()) return;                                   // already typing in a field
  if (document.getElementById('modal-bg')) return;
  if (document.getElementById('settings-bg')) return;
  renameSelected();
};

// Open a parameter's expression for editing (same as double-clicking it): the
// one-line field normally, the full-window box when the expression has line breaks.
function enterExpression(name) {
  const ml = layoutFor(name);
  if (ml !== null) { openBigEdit(name, ml); return; }
  const inp = document.querySelector(
    '.param-row[data-name="' + cssAttr(name) + '"] input[data-field="expression"]');
  if (inp) enterEdit({ target: inp });
}

// ── Multi-line expressions ─────────────────────────────────────────────────────
//
// Fusion won't hold a multi-line expression: it accepts one, strips the newlines
// and stores the flat form ("Width * 2\n+ Height" comes back as "Width * 2 +
// Height"). Everything it does to an expression it stores is whitespace-only, so
// the line breaks are kept here, keyed by parameter, and re-applied whenever the
// stored layout still flattens to the expression Fusion actually holds. If the
// expression is changed anywhere else — Fusion's own dialog, another window — the
// layout no longer describes it and is dropped, and the row goes back to one line.

// The stored line breaks for a parameter, or null if it has none (or they're stale).
function layoutFor(name) {
  const text = docLayouts[name];
  if (text === undefined) return null;
  const p = params.find(x => x.name === name);
  const cur = pending[name] !== undefined ? pending[name] : (p ? p.expression : undefined);
  if (cur === undefined) return null;        // parameter not in this snapshot — keep the layout
  if (normExpr(fromDisplayExpr(flattenExpr(text))) !== normExpr(cur)) {
    delete docLayouts[name];
    saveGroups();
    return null;
  }
  return text;
}

function setLayout(name, text) { docLayouts[name] = text; saveGroups(); }
function clearLayout(name) {
  if (docLayouts[name] === undefined) return;
  delete docLayouts[name];
  saveGroups();
}
function renameLayout(oldName, newName) {
  if (docLayouts[oldName] === undefined) return;
  docLayouts[newName] = docLayouts[oldName];
  delete docLayouts[oldName];
  saveGroups();
}

// Multi-line text → the single line Fusion will store: trailing space per line
// gone, blank lines dropped, the rest joined with one space.
function flattenExpr(text) {
  return String(text).split('\n').map(l => l.trim()).filter(Boolean).join(' ');
}

// A second Enter, pressed right after the expression field opened and before
// anything was typed, asks for more room. Typed text means you meant to commit, so
// only an untouched field qualifies — Enter after typing still commits as it did.
function wantsBigEdit(t) {
  if (t.dataset.field !== 'expression') return false;
  if (Date.now() - lastEditOpenAt > DOUBLE_ENTER_MS) return false;
  const rec = editing[fkey(t.dataset.param, t.dataset.field)];
  return !!rec && t.value === rec.orig;
}

// Hand the one-line field's edit over to the big box. It's seeded from the full
// expression, not the field's text — the field strips a simple trailing unit
// ("10mm" → "10") and a complex expression carries its units inside it.
function openBigEditFrom(t) {
  const name = t.dataset.param;
  const p = params.find(x => x.name === name);
  const raw = pending[name] !== undefined ? pending[name] : (p ? p.expression : '');
  openBigEdit(name, toDisplayExpr(raw));
  t.blur();     // no-op if focusing the box already blurred it; the value is unchanged,
}               // so onFieldBlur commits nothing.

function openBigEdit(name, text) {
  if (bigEdit) return;
  const p = params.find(x => x.name === name) || { name, unit: '' };
  const u = displayUnit(p.unit);
  bigEdit = { name, orig: text };
  acHide();
  document.body.insertAdjacentHTML('beforeend', `
<div id="bigedit-bg" onmousedown="if(event.target!==document.getElementById('be-text'))event.preventDefault()">
  <div id="bigedit">
    <div id="bigedit-hdr">
      <span class="be-name">${esc(name)}</span>
      ${u ? `<span class="be-unit">${esc(u)}</span>` : ''}
    </div>
    <textarea id="be-text" spellcheck="false" autocomplete="off" autocapitalize="off"
      oninput="onExprInput(event)" onkeydown="onBigKey(event)"></textarea>
    <div id="bigedit-hint"><b>↵</b> commit &nbsp;·&nbsp; <b>⇧↵</b> new line &nbsp;·&nbsp; <b>⎋</b> cancel</div>
  </div>
</div>`);
  const ta = document.getElementById('be-text');
  ta.value = text;
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function onBigKey(e) {
  if (acHandleKey(e)) return;      // autocomplete gets first refusal, as in the row fields
  if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) {
    // Shift/Option+Enter break the line; a bare Enter commits — the key that opened
    // the box also closes it, so a complex expression never needs the mouse.
    if (e.shiftKey || e.altKey) return;
    e.preventDefault(); e.stopPropagation();
    closeBigEdit(true);
  } else if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    closeBigEdit(false);
  }
}

function closeBigEdit(commit) {
  const be = bigEdit;
  if (!be) return;
  const ta = document.getElementById('be-text');
  const text = ta ? ta.value : '';
  acHide();
  bigEdit = null;                                   // before render(): isEditing() reads it
  const bg = document.getElementById('bigedit-bg');
  if (bg) bg.remove();
  if (commit) applyBigEdit(be.name, text);
  dirty = false;
  render();
}

// Commit the box: keep the line breaks here, send Fusion the flat expression. An
// edit that only re-wraps the lines still saves the layout even though Fusion sees
// no change at all.
function applyBigEdit(name, text) {
  const lines = text.replace(/\r/g, '').split('\n').map(l => l.replace(/\s+$/, ''));
  while (lines.length && !lines[0].trim()) lines.shift();
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  const flat = flattenExpr(lines.join('\n'));
  if (!flat) return;                                // emptied → leave the parameter alone
  const expr = fromDisplayExpr(flat);
  if (lines.length > 1) setLayout(name, lines.join('\n'));
  else clearLayout(name);
  const p = params.find(x => x.name === name);
  const cur = pending[name] !== undefined ? pending[name] : (p ? p.expression : '');
  if (normExpr(cur) !== normExpr(expr)) commitField(name, 'expression', expr);
}

// Single-letter group names expand to the canonical engineering-folder names, and
// any siblings among them are reordered into a fixed stack order.
const GROUP_ORDER = ['Dimension', 'Offset', 'Position', 'Features'];
function applyGroupNameMagic(node) {
  const map = { d: 'Dimension', p: 'Position', o: 'Offset', f: 'Features' };
  const full = map[(node.name || '').trim().toLowerCase()];
  if (full) node.name = full;
  const loc = findParent(node);
  const arr = loc ? loc.arr : state.tree;
  const idxs = [];
  for (let i = 0; i < arr.length; i++)
    if (arr[i].type === 'group' && GROUP_ORDER.includes(arr[i].name)) idxs.push(i);
  if (idxs.length > 1) {
    const ordered = idxs.map(i => arr[i])
      .sort((a, b) => GROUP_ORDER.indexOf(a.name) - GROUP_ORDER.indexOf(b.name));
    idxs.forEach((i, k) => { arr[i] = ordered[k]; });
  }
}

function startRenameGroup(e) {
  e.stopPropagation();
  const span = e.target;
  const header = span.closest('.node-row');
  const node = nodeByPath(JSON.parse(header.dataset.path));
  if (!node) return;
  header.draggable = false;

  const input = document.createElement('input');
  input.className = 'group-name-input';
  input.value = node.name;
  span.replaceWith(input);
  input.focus(); input.select();

  let done = false;
  function commit() {
    if (done) return; done = true;
    const nn = input.value.trim();
    if (nn) { node.name = nn; applyGroupNameMagic(node); }
    saveGroups();
    render();
  }
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') input.blur();
    if (ev.key === 'Escape') { input.value = node.name; input.blur(); }
    ev.stopPropagation();
  });
}

// ── Persist tree ───────────────────────────────────────────────────────────────

function saveGroups() {
  if (!currentDocId) return;   // don't persist until a document is active (avoids clobber)
  allGroups[currentDocId] = { tree: state.tree, layouts: docLayouts };
  if (saveGroupsTimer) clearTimeout(saveGroupsTimer);
  saveGroupsTimer = setTimeout(() => {
    fetch('/groups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ byDoc: allGroups })
    }).catch(() => {});
  }, 250);
}

// ── Column resize ──────────────────────────────────────────────────────────────

function initColResize() {
  try {
    const v = localStorage.getItem('nameCol');
    if (v) document.documentElement.style.setProperty('--name-col', parseInt(v) + 'px');
  } catch (_) {}
  const d = document.getElementById('col-divider');
  d.addEventListener('mousedown', e => {
    e.preventDefault();
    d.classList.add('dragging');
    const startX = e.clientX;
    const cur = getComputedStyle(document.documentElement).getPropertyValue('--name-col');
    const startW = parseInt(cur) || 150;
    function mv(ev) {
      // Clamp: keep room for the names on the left and the expression column on the
      // right so neither gets dragged out of the window or over the other's text.
      const min = 80;
      const max = Math.max(min + 1, window.innerWidth - 120);
      const w = Math.min(Math.max(startW + (ev.clientX - startX), min), max);
      document.documentElement.style.setProperty('--name-col', w + 'px');
    }
    function up() {
      document.removeEventListener('mousemove', mv);
      document.removeEventListener('mouseup', up);
      d.classList.remove('dragging');
      const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--name-col'));
      try { localStorage.setItem('nameCol', w); } catch (_) {}
    }
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
  });
}

// ── Expression autocomplete (parameter names) ──────────────────────────
//
// Matches the way Fusion's own expression fields behave: the typed fragment is
// matched anywhere in a parameter name (not just at the start), and the best
// match is completed inline with the added text selected, so carrying on typing
// replaces it and Tab/Enter/→ accepts it.

let ac = { open: false, input: null, items: [], idx: 0, start: 0, end: 0 };
let acGhost = false;    // an inline completion is currently sitting selected in the field
let acGhostEnd = 0;     // where that completion ends (the text after it must survive)
let acTyped = '';       // the fragment actually typed, before any inline completion

function onExprInput(e) {
  autosize(e);
  // Never inline-complete while deleting — otherwise Backspace would immediately
  // put the removed characters back and the field couldn't be cleared.
  const del = !!(e.inputType && /delete/i.test(e.inputType));
  acShow(e.target, !del);
}

function acTokenAt(input) {
  const v = input.value, c = input.selectionStart;
  let s = c;
  while (s > 0 && /[A-Za-z0-9_]/.test(v[s - 1])) s--;
  return { text: v.slice(s, c), start: s, end: c };
}

// Rank a candidate against the typed fragment. Lower is better; null = no match.
//   0 — name starts with it              (Wid → Width)
//   1 — starts a word / camel-case hump   (Thick → VeneerThickness)
//   2 — plain substring                   (hick → VeneerThickness)
function acRank(name, frag) {
  const i = name.toLowerCase().indexOf(frag);
  if (i < 0) return null;
  if (i === 0) return 0;
  const prev = name[i - 1], ch = name[i];
  const hump = prev === '_' || (/[a-z0-9]/.test(prev) && ch === ch.toUpperCase() && /[A-Za-z]/.test(ch));
  return hump ? 1 : 2;
}

function acShow(input, allowInline) {
  const tok = acTokenAt(input);
  acGhost = false;
  acTyped = tok.text;
  if (tok.text.length < 1) return acHide();
  const frag = tok.text.toLowerCase();
  const scored = [];
  for (const p of params) {
    if (p.name.toLowerCase() === frag) continue;      // already typed in full
    const r = acRank(p.name, frag);
    if (r !== null) scored.push({ name: p.name, rank: r });
  }
  if (!scored.length) return acHide();
  // Prefix matches first, then camel-case humps, then plain substrings; shortest
  // (closest) name wins within a rank, alphabetical to break ties.
  scored.sort((a, b) => a.rank - b.rank || a.name.length - b.name.length ||
                        a.name.localeCompare(b.name));
  ac = { open: true, input, items: scored.slice(0, 12).map(x => x.name),
         idx: 0, start: tok.start, end: tok.end };
  acRender();
  if (allowInline) acInline();
}

// Bold the matched span so it's obvious why each row is in the list.
function acItemHtml(name) {
  const frag = acTyped.toLowerCase();
  const i = frag ? name.toLowerCase().indexOf(frag) : -1;
  if (i < 0) return esc(name);
  return esc(name.slice(0, i)) + '<b>' + esc(name.slice(i, i + frag.length)) + '</b>' +
         esc(name.slice(i + frag.length));
}

function acRender() {
  let el = document.getElementById('ac');
  if (!el) { el = document.createElement('div'); el.id = 'ac'; document.body.appendChild(el); }
  el.innerHTML = ac.items.map((n, i) =>
    `<div class="ac-item ${i === ac.idx ? 'sel' : ''}" onmousedown="acPick(event,${i})">${acItemHtml(n)}</div>`
  ).join('');

  // Position: prefer below the field, but flip above when there isn't room — the
  // Params pane is short, so a popup hung below a lower row would be invisible.
  const r  = acAnchorRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  el.style.display  = 'block';
  el.style.maxHeight = 'none';
  el.style.minWidth = Math.max(r.width, 140) + 'px';
  el.style.maxWidth = Math.max(120, vw - 8) + 'px';
  const need  = el.offsetHeight;
  const below = vh - r.bottom - 4, above = r.top - 4;
  if (need <= below || below >= above) {
    el.style.bottom = 'auto';
    el.style.top    = r.bottom + 'px';
    el.style.maxHeight = Math.max(60, below) + 'px';
  } else {
    el.style.top    = 'auto';
    el.style.bottom = (vh - r.top) + 'px';
    el.style.maxHeight = Math.max(60, above) + 'px';
  }
  el.style.left = Math.max(4, Math.min(r.left, vw - el.offsetWidth - 4)) + 'px';
  const sel = el.querySelector('.ac-item.sel');
  if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
}

// Inline completion: append the rest of the best match and leave it selected, so
// typing on replaces it. Only for names that actually START with what was typed
// (completing a mid-name match inline would rewrite text behind the caret), and
// only when nothing but the completion would follow the caret.
function acInline() {
  const input = ac.input, name = ac.items[ac.idx];
  if (!acAtSegmentEnd(input)) return;
  if (!name.toLowerCase().startsWith(acTyped.toLowerCase())) return;
  if (name.length === acTyped.length) return;
  // Write the parameter's own casing, not what was typed — Fusion parameter names
  // are case-sensitive, so completing "thick" must yield "Thickness".
  input.value = input.value.slice(0, ac.start) + name + input.value.slice(ac.end);
  acGhostEnd = ac.start + name.length;
  input.setSelectionRange(ac.start + acTyped.length, acGhostEnd);
  autosize(input);
  acGhost = true;
}

// Where a completion may be shown inline: the end of a one-line field, or the end
// of the current line in the big box — anywhere else it would sit in front of text
// you already wrote, which reads as a typo rather than a suggestion.
function acAtSegmentEnd(input) {
  if (ac.end === input.value.length) return true;
  return input.tagName === 'TEXTAREA' && input.value[ac.end] === '\n';
}

// Drop the inline completion, restoring exactly what was typed (including its case).
function acDropGhost() {
  if (!acGhost) return;
  const input = ac.input;
  acGhost = false;
  if (!input) return;
  input.value = input.value.slice(0, ac.start) + acTyped + input.value.slice(acGhostEnd);
  const pos = ac.start + acTyped.length;
  input.setSelectionRange(pos, pos);
  autosize(input);
}

// Keep the inline completion and put the caret after it.
function acKeepGhost() {
  if (!acGhost) return false;
  const input = ac.input;
  acGhost = false;
  input.setSelectionRange(acGhostEnd, acGhostEnd);
  acHide();
  return true;
}

function acHide() { ac.open = false; acGhost = false; acTyped = ''; const el = document.getElementById('ac'); if (el) el.style.display = 'none'; }

function acMove(d) {
  if (!ac.open) return;
  acDropGhost();
  ac.idx = (ac.idx + d + ac.items.length) % ac.items.length;
  acRender();
  acInline();
}

function acAccept() {
  if (!ac.open) return false;
  const name = ac.items[ac.idx];
  const input = ac.input;
  acDropGhost();
  const v = input.value;
  input.value = v.slice(0, ac.start) + name + v.slice(ac.end);
  const pos = ac.start + name.length;
  input.setSelectionRange(pos, pos);
  autosize(input);
  acHide();
  return true;
}

function acPick(e, i) { e.preventDefault(); ac.idx = i; acAccept(); ac.input.focus(); }

// What the popup hangs off. A one-line field is small enough to be its own anchor;
// the big box fills the window, so there the popup follows the caret instead.
function acAnchorRect() {
  const el = ac.input;
  if (el.tagName !== 'TEXTAREA') return el.getBoundingClientRect();
  return acCaretRect(el) || el.getBoundingClientRect();
}

// Caret position in a textarea, measured by laying the text up to the caret into a
// hidden div styled like the textarea and reading where that ends. There's no API
// for this; the mirror is the standard way to get it.
function acCaretRect(ta) {
  try {
    const cs  = getComputedStyle(ta);
    const div = document.createElement('div');
    for (const p of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing',
                     'lineHeight', 'textTransform', 'wordSpacing', 'textIndent',
                     'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'])
      div.style[p] = cs[p];
    const padL = parseFloat(cs.paddingLeft) || 0, padR = parseFloat(cs.paddingRight) || 0;
    Object.assign(div.style, {
      position: 'absolute', visibility: 'hidden', top: '0px', left: '-9999px',
      boxSizing: 'content-box', border: '0', whiteSpace: 'pre-wrap',
      overflowWrap: 'break-word', wordWrap: 'break-word',
      width: Math.max(0, ta.clientWidth - padL - padR) + 'px',
    });
    div.textContent = ta.value.slice(0, ta.selectionStart);
    const marker = document.createElement('span');
    marker.textContent = '\u200b';
    div.appendChild(marker);
    document.body.appendChild(div);
    const box = ta.getBoundingClientRect();
    const lh  = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) || 12) * 1.4;
    const x = box.left + marker.offsetLeft - ta.scrollLeft;
    const y = box.top  + marker.offsetTop  - ta.scrollTop;
    div.remove();
    return { left: x, top: y, right: x, bottom: y + lh, width: 0, height: lh };
  } catch (_) {
    return null;
  }
}

// True when the typed fragment is itself the full name of an existing parameter.
function acExactParam() { return !!acTyped && params.some(p => p.name === acTyped); }

function acHandleKey(e) {
  if (!ac.open) return false;
  if (e.key === 'ArrowDown') { e.preventDefault(); acMove(1);  return true; }
  if (e.key === 'ArrowUp')   { e.preventDefault(); acMove(-1); return true; }
  if (e.key === 'Enter' || e.key === 'Tab') {
    // If what's typed is already a parameter name in full, Enter means "commit this
    // expression", not "swap in the longer name that also contains it".
    if (acExactParam()) { acHide(); return false; }
    if (acAccept()) { e.preventDefault(); return true; }
  }
  // → / End accept the inline completion without leaving the field (as in Fusion).
  if (acGhost && (e.key === 'ArrowRight' || e.key === 'End')) { e.preventDefault(); acKeepGhost(); return true; }
  if (e.key === 'Escape')    { acDropGhost(); acHide(); e.preventDefault(); return true; }
  // Typing an operator (anything that can't be part of a name) accepts the inline
  // completion first, so "wi" + "*" gives "Width*" rather than "wi*".
  if (acGhost && e.key.length === 1 && !e.metaKey && !e.ctrlKey && !/[A-Za-z0-9_]/.test(e.key)) {
    acKeepGhost();            // the character itself still gets inserted normally
    return false;
  }
  return false;
}

// ── Add-parameter dialog ───────────────────────────────────────────────────────

// Where a newly-created parameter should land once Fusion reports it (see
// reconcileTree). Set when the Add dialog is submitted.
let pendingInsert = null;   // { name, afterKey } | { name, insideId } | { name }

let modalUnit = 'mm';       // cycles 'mm' → 'deg' → '' (unitless)

function openAddDialog() {
  if (document.getElementById('modal-bg')) return;
  modalUnit = 'mm';
  // Field order so Tab goes Expression → Name; the unit toggle sits at the bottom.
  const html = `
<div id="modal-bg" onmousedown="if(event.target===this)closeAddDialog()">
  <div id="modal">
    <h2>Add Parameter</h2>
    <label>Expression<input id="m-expr" type="text" spellcheck="false" autocomplete="off"
      oninput="onExprInput(event)" onkeydown="onModalKey(event)" onblur="acHide()"></label>
    <label>Name<input id="m-name" type="text" spellcheck="false" autocomplete="off"
      onkeydown="onModalKey(event)"></label>
    <label>Comment<input id="m-comment" type="text" spellcheck="false" autocomplete="off"
      onkeydown="onModalKey(event)"></label>
    <div class="unit-row">
      <span>Unit</span>
      <button id="m-unit-btn" class="btn" onmousedown="event.preventDefault()"
        title="Click to cycle: mm → ° → none (a plain number)"
        onclick="toggleModalUnit()">mm</button>
    </div>
    <div id="modal-err"></div>
    <div class="modal-btns">
      <button class="btn" onmousedown="event.preventDefault()" onclick="closeAddDialog()">Cancel</button>
      <button class="btn btn-accent" onmousedown="event.preventDefault()" onclick="submitAdd()">Add</button>
    </div>
  </div>
</div>`;
  document.body.insertAdjacentHTML('beforeend', html);
  document.getElementById('m-expr').focus();   // expression first
}

function toggleModalUnit() {
  modalUnit = UNITS[(UNITS.indexOf(modalUnit) + 1) % UNITS.length];
  const b = document.getElementById('m-unit-btn');
  if (b) b.textContent = unitLabel(modalUnit);     // mm / ° / none
}

function closeAddDialog() { acHide(); const m = document.getElementById('modal-bg'); if (m) m.remove(); }

function onModalKey(e) {
  if (acHandleKey(e)) return;
  if (e.key === 'Enter')  { e.preventDefault(); submitAdd(); }
  if (e.key === 'Escape') { closeAddDialog(); }
}

function modalErr(msg) { const el = document.getElementById('modal-err'); if (el) el.textContent = msg; }

function submitAdd() {
  const g = id => (document.getElementById(id) || {}).value || '';
  const name = ParamNaming.joinName(g('m-name'));
  const exprRaw = g('m-expr').trim();
  const comment = g('m-comment');
  const unit = modalUnit;

  if (name && !/^[A-Za-z_]\w*$/.test(name)) { modalErr('Name must start with a letter/underscore.'); return; }
  if (name && params.find(p => p.name === name)) { modalErr('A parameter named "' + name + '" already exists.'); return; }

  // Empty name and/or expression → drop a draft placeholder (shown as "[ ]") that
  // can be filled in later; it becomes a real parameter once both are valid.
  if (!name || !exprRaw) { closeAddDialog(); addDraft(name, exprRaw, unit, comment); return; }

  const expr = fromDisplayExpr(exprRaw);

  // Remember where to drop the new param: under the selected item (after a param,
  // or inside a selected group), else at the root end.
  pendingInsert = { name };
  const sel = selectedNodes();
  if (sel.length === 1) {
    if (sel[0].type === 'group') pendingInsert.insideId = sel[0].id;
    else pendingInsert.afterKey = 'param:' + sel[0].name;
  }

  // Show the new parameter immediately (don't wait for Fusion's next sync). The
  // optimistic row is kept by deriveParams() until Fusion reports it for real, or
  // expires (and reverts) if Fusion never does.
  pendingAdds[name] = { param: { name, expression: expr, unit, comment, deps: 0 }, ts: Date.now() };
  closeAddDialog();
  deriveParams();
  render();

  fetch('/add', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, expression: expr, unit, comment })
  }).then(r => r.json()).then(res => {
    if (!(res && res.ok)) {
      // Rejected before it even reached Fusion — roll back now.
      delete pendingAdds[name]; pendingInsert = null;
      deriveParams(); render();
      alert((res && res.error) || 'Failed to add parameter "' + name + '".');
    }
  }).catch(() => {
    delete pendingAdds[name]; pendingInsert = null;
    deriveParams(); render();
  });
}

// ── Settings / hotkeys (Cmd-,) ──────────────────────────────────────────────────
//
// Each action has built-in default key(s) (always active) plus any user-added
// custom bindings. "shared" actions use the same id in the Browser window and are
// persisted to a shared settings file, so a binding added here also works there.
// Window-specific actions are namespaced ("params:…"). Custom bindings are purely
// additive — defaults are never removed — so this can't break existing shortcuts.
//
// `general`: this action is a GENERAL VERB (fusion_ui/general.json, set from
// Control Room). Its chord is read from there at load, never spelled here — Rename
// is one thing whether it happens in Fusion, in the Browser window or in this one.

const ACTIONS = {
  'nav-down':  { label: 'Move selection down', scope: 'shared', def: ['C', '↓'], run: () => moveSelection(1) },
  'nav-up':    { label: 'Move selection up',   scope: 'shared', def: ['R', '↑'], run: () => moveSelection(-1) },
  'nav-bottom': { label: 'Jump to section bottom', scope: 'shared', def: [], general: 'nav-bottom', run: () => jumpSelection(1) },
  'nav-top':    { label: 'Jump to section top',    scope: 'shared', def: [], general: 'nav-top',    run: () => jumpSelection(-1) },
  'expand':    { label: 'Expand group',        scope: 'shared', def: ['F', '→'], run: () => setCollapsedSelected(false) },
  'collapse':  { label: 'Collapse group',      scope: 'shared', def: ['D', '←'], run: () => setCollapsedSelected(true) },
  'rename':    { label: 'Rename',              scope: 'shared', def: [], general: 'rename', run: () => renameSelected() },
  'settings':  { label: 'Open settings',       scope: 'shared', def: ['⌘,'],     run: () => openSettings() },
  'params:edit-expr': { label: 'Edit expression', scope: 'params', def: ['↵'], run: () => { const s = selectedNodes(); if (s.length === 1 && s[0].type === 'param') enterExpression(s[0].name); } },
  'params:add-param': { label: 'Add parameter',   scope: 'params', def: ['⌘⇧N'], run: () => openAddDialog() },
  'params:add-group': { label: 'New group',       scope: 'params', def: ['⌘G'],  run: () => addGroup() },
  'params:delete':    { label: 'Delete',          scope: 'params', def: ['⌘D', '⌘⌫'], general: 'delete', run: () => deleteSelection() },
  'ungroup':          { label: 'Ungroup',         scope: 'shared', def: [], general: 'ungroup', run: () => ungroupSelection() },
  'params:save':      { label: 'Save document',   scope: 'params', def: ['⌘S'],  run: () => saveFusion() },
};

// The dialog, the custom bindings and the general chords are fusion_ui/keys.js's
// panel — the same one the Browser window uses.
const hotkeyPanel = Keys.panel({
  actions: ACTIONS,
  sections: [['Shared (both windows)', 'shared'], ['Parameters window', 'params']],
  dispatchGeneral: true,
  blocked: () => !!document.getElementById('settings-bg') ||
                 !!document.getElementById('modal-bg') || isEditing(),
  buttonClass: 'btn',
});
function openSettings() { hotkeyPanel.open(); }
function closeSettings() { hotkeyPanel.close(); }

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

init();
