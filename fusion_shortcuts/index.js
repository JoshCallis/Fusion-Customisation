// ── State ─────────────────────────────────────────────────────────────────────

let commands = [];      // [{id,name,tooltip,group,panel,kind}]
let groups   = [];      // ordered group labels
let hotkeys  = [];      // [{seq, commands:[{id,arg,isDefault}]}]
let recents  = [];      // [{id, ts}] most-recent-first
let byId     = {};      // id -> command
let bindings = {};      // id -> [{seq, isDefault}]
let seqMap   = {};      // seq -> [{id, isDefault}]
// The action registry from fusion_ui/hotkeys.py: everything any add-in has declared,
// the domain it applies in, the key it claims, and the context Fusion is in right now.
// Fusion's keymap knows none of this — see hotkeys.py for why the claim list is ours.
let registry = { actions: [], bindings: {}, keys: {}, axes: {}, context: {} };
let actById  = {};      // id -> action

let tab        = 'all';
let query      = '';
// group label -> true (collapsed) / false (expanded). A group that isn't in here
// hasn't been touched and uses the default below.
let groupState = {};
let lookupSeq  = '';    // sequence the reverse-lookup field is filtering by
let capture    = null;  // {mode:'assign', cmdId} | {mode:'lookup'} | null
let lastResult = 0;

// Fusion's own modifier bindings, so pressing one here still reaches Fusion.
let fusionKeys = {};
let closeHotkey = '';

let _lastBeat = -1;     // add-in sync heartbeat from /data
let _beatStalls = 0;    // consecutive polls where it didn't move

// ── Key formatting ────────────────────────────────────────────────────────────

// Fusion writes sequences as "shift+command+alt+control+key", lower case.
const MOD_ORDER = ['shift', 'command', 'alt', 'control'];
// Displayed in the Mac convention (⌃⌥⇧⌘), which is not the storage order.
const GLYPH_ORDER = ['control', 'alt', 'shift', 'command'];
const MOD_GLYPH = { control: '⌃', alt: '⌥', shift: '⇧', command: '⌘' };
const KEY_GLYPH = {
  up: '↑', down: '↓', left: '←', right: '→', space: 'Space', tab: '⇥',
  escape: '⎋', backspace: '⌫', delete: '⌦', pageup: '⇞', pagedown: '⇟',
  home: '↖', end: '↘',
  // Fusion's keymap spells these "return"/"backspace"; keep the DOM names as
  // aliases so a sequence built from a plain keyboard event renders the same.
  return: '↩', enter: '↩',
};

function fmtSeq(seq) {
  const parts = String(seq || '').toLowerCase().split('+');
  const key = parts.pop() || '';
  const mods = GLYPH_ORDER.filter(m => parts.includes(m)).map(m => MOD_GLYPH[m]).join('');
  return mods + (KEY_GLYPH[key] || (key.length === 1 ? key.toUpperCase() : key.toUpperCase()));
}

// Native capture payload {key, shift, command, alt, control} → Fusion's spelling.
function payloadSeq(p) {
  const mods = MOD_ORDER.filter(m => p[m]);
  return [...mods, p.key].join('+');
}

// ── Data ──────────────────────────────────────────────────────────────────────

function index() {
  byId = {}; bindings = {}; seqMap = {};
  actById = {};
  for (const a of registry.actions || []) actById[a.id] = a;
  for (const c of commands) byId[c.id] = c;
  for (const e of hotkeys) {
    const seq = (e.seq || '').toLowerCase();
    seqMap[seq] = [];
    for (const c of e.commands) {
      (bindings[c.id] = bindings[c.id] || []).push({ seq, isDefault: c.isDefault });
      seqMap[seq].push({ id: c.id, isDefault: c.isDefault });
    }
  }
  // Registry assignments sit alongside Fusion's, not inside them. An action bound
  // only here still has to show its key and still has to answer the reverse lookup —
  // that's the whole reason this window exists rather than Fusion's own panel.
  for (const [id, seq] of Object.entries(registry.bindings || {})) {
    const s2 = (seq || '').toLowerCase();
    if (!s2) continue;
    const have = (bindings[id] || []).some(b => b.seq === s2);
    if (!have) (bindings[id] = bindings[id] || []).push({ seq: s2, isDefault: false });
    seqMap[s2] = seqMap[s2] || [];
    if (!seqMap[s2].some(b => b.id === id)) seqMap[s2].push({ id, isDefault: false });
  }
}

async function load() {
  try {
    const d = await fetch('/data').then(r => r.json());
    commands = d.commands || [];
    groups   = d.groups   || [];
    hotkeys  = d.hotkeys  || [];
    recents  = d.recents  || [];
    registry = d.registry || registry;
    index();
    if (d.result && d.result.ts && d.result.ts !== lastResult) {
      lastResult = d.result.ts;
      showStatus(d.result.msg, d.result.ok);
      endHandoff();          // the modal returned, so the add-in ran again
    }
    // A stalled heartbeat means Fusion's main thread is blocked — almost always a
    // modal dialog of its own. Don't let hover-to-focus raise this window over it.
    // Guarded on the field existing: an older add-in that predates it would report
    // undefined every poll, which reads as permanently stalled and would leave the
    // window unable to raise itself at all.
    if (typeof d.beat !== 'number') {
      if (_beatStalls >= 2) native('allowRaise', true);
      _beatStalls = 0;
    } else if (d.beat === _lastBeat) {
      if (++_beatStalls === 2) native('allowRaise', false);
    } else {
      _lastBeat = d.beat;
      if (_beatStalls >= 2) native('allowRaise', true);
      _beatStalls = 0;
    }
    render();
  } catch (_) { /* server restarting — the next poll picks it up */ }
}

async function loadStatic() {
  try { fusionKeys = await fetch('/fusionkeys').then(r => r.json()); } catch (_) {}
  try { closeHotkey = (await fetch('/hotkey').then(r => r.json())).hotkey || ''; } catch (_) {}
  try {
    const p = await fetch('/prefs').then(r => r.json());
    if (p && typeof p === 'object') {
      groupState = p.groupState || {};
      if (p.tab) tab = p.tab;
    }
  } catch (_) {}
}

function savePrefs() {
  fetch('/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ groupState, tab }) }).catch(() => {});
}

function post(path, body) {
  return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) }).catch(() => {});
}

// Same, but the reply matters — /bind answers whether Fusion still needs an anchor.
function postJson(path, body) {
  return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) }).then(r => r.json()).catch(() => ({}));
}

// ── Domains ───────────────────────────────────────────────────────────────────

// Read a domain back as English. The axis vocabulary is in fusion_ui/contexts.json;
// this only decides how each axis=value READS, and falls back to "axis value" for
// anything new, so adding an axis never needs a change here first.
const DOMAIN_PHRASE = {
  'command:none':        'nothing running',
  'command:extrude':     'while extruding',
  'command:dimension':   'while dimensioning',
  'env:sketch':          'in a sketch',
  'env:viewport':        'in the viewport',
  'env:dialog':          'in our dialogs',
  'sketch:in':           'in a sketch',
  'sketch:out':          'outside a sketch',
  'focus:viewport':      'in the viewport',
  'focus:browser':       'in the browser',
  'focus:params':        'in parameters',
  'focus:value':         'in the value box',
  'workspace:design':    'in Design',
  'workspace:render':    'in Render',
  'selection:none':      'with nothing selected',
  'selection:dimension': 'with a dimension selected',
  'selection:body':      'with a body selected',
  'selection:sketch':    'with a sketch selected',
  'selection:component': 'with a component selected',
};

function domainText(a) {
  const raw = (a && a.whenRaw) || {};
  const axes = Object.keys(raw);
  if (!axes.length) return 'everywhere';
  return axes.map(ax => [].concat(raw[ax])
    .map(v => DOMAIN_PHRASE[ax + ':' + v] || (ax + ' ' + v)).join(' or ')).join(', ');
}

// Does this action's domain hold right NOW? Uses the expanded value sets, so an
// alias ("extrude") still matches the command id Fusion reports.
function domainLive(a) {
  const when = (a && a.when) || {};
  const ctx = registry.context || {};
  for (const ax of Object.keys(when))
    if (!when[ax].includes(ctx[ax])) return false;
  return true;
}

// Every action that claims a key. What makes the double-up legible: the row can say
// this key is shared, and the reverse lookup can list who else is on it.
function claimantsOf(seq) {
  return (registry.actions || []).filter(a => (a.seq || '').toLowerCase() === seq);
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function matches(c) {
  if (!query) return true;
  const q = query.toLowerCase();
  if (c.name.toLowerCase().includes(q)) return true;
  if (c.id.toLowerCase().includes(q)) return true;
  if ((c.panel || '').toLowerCase().includes(q)) return true;
  if ((c.group || '').toLowerCase().includes(q)) return true;
  // Search shortcuts too, both as stored ("shift+alt+b") and as shown ("⇧⌥B").
  for (const b of bindings[c.id] || [])
    if (b.seq.includes(q) || fmtSeq(b.seq).toLowerCase().includes(q)) return true;
  return false;
}

function visible() {
  let list = commands;
  if (lookupSeq) {
    const ids = new Set((seqMap[lookupSeq] || []).map(b => b.id));
    list = list.filter(c => ids.has(c.id));
  } else if (tab === 'assigned') {
    list = list.filter(c => (bindings[c.id] || []).length);
  } else if (tab === 'recent') {
    const order = {}; recents.forEach((r, i) => { order[r.id] = i; });
    list = list.filter(c => c.id in order).sort((a, b) => order[a.id] - order[b.id]);
  }
  return list.filter(matches);
}

// Everything the rendered list depends on. The poll runs every 1.2s and rebuilding
// innerHTML unconditionally tore the row out from under the cursor — :hover was
// lost and reapplied, so hovering flashed once a second. Re-render only on a real
// change; a poll that brings back identical data now does nothing at all.
function renderSig() {
  return JSON.stringify([
    tab, query, lookupSeq, groupState,
    commands.length, hotkeys, recents.map(r => r.id),
    registry.bindings, registry.context,
    capture && capture.mode === 'assign' ? capture.cmdId : '',
  ]);
}
let _lastSig = null;

function render(force) {
  const sig = renderSig();
  if (!force && sig === _lastSig) return;
  _lastSig = sig;
  // WebKit can clamp the scroller to 0 while #list is replaced: put it back after.
  const main = document.getElementById('main'), scrolled = main ? main.scrollTop : 0;
  try { draw(); } finally { if (main && main.scrollTop !== scrolled) main.scrollTop = scrolled; }
}

function draw() {
  const list = visible();
  const root = document.getElementById('list');
  if (!commands.length) {
    root.innerHTML = `<div class="empty">Waiting for Fusion…</div>`;
    return;
  }
  if (!list.length) {
    root.innerHTML = `<div class="empty">${lookupSeq
      ? fmtSeq(lookupSeq) + ' isn’t assigned to anything.'
      : 'Nothing matches.'}</div>`;
    return;
  }

  // Recent is inherently ordered by time, so it stays one flat list — grouping it
  // by workspace would destroy the only thing that makes it useful.
  if (tab === 'recent' && !lookupSeq) {
    root.innerHTML = groupHtml('Recently run', false) + list.map(rowHtml).join('');
    return;
  }

  const buckets = {};
  for (const c of list) (buckets[c.group] = buckets[c.group] || []).push(c);
  const order = groups.filter(g => buckets[g]).concat(
    Object.keys(buckets).filter(g => !groups.includes(g)).sort());

  let html = '';
  for (const g of order) {
    const items = buckets[g];
    const isCollapsed = isGroupCollapsed(g);
    html += groupHtml(g, isCollapsed);
    if (isCollapsed) continue;
    for (const c of items) html += rowHtml(c);
  }
  root.innerHTML = html;
}

// Fusion names a lot of its tabs and panels in block capitals ("SHEET METAL",
// "BASE FEATURE SOLID", "CREATE"). Shown verbatim the whole window shouts, so
// all-caps words get title-cased; anything already mixed-case ("Direct Mesh
// Editing", "Milling") is left exactly as Fusion wrote it.
// Short all-caps words are acronyms, not shouting — "3D PCB" must not become
// "3d Pcb" — so only words longer than three characters get folded.
function pretty(s) {
  return String(s || '').replace(/[^\s▸]+/g, w =>
    (/[a-z]/.test(w) || w.length <= 3) ? w : w.charAt(0) + w.slice(1).toLowerCase());
}

function groupHtml(label, isCollapsed) {
  return `<div class="grp-hdr" data-group="${esc(label)}">
    <span class="twirl ${isCollapsed ? 'collapsed' : ''}">▾</span>
    <span class="grp-name">${esc(pretty(label))}</span>
  </div>`;
}

function rowHtml(c) {
  const bs = bindings[c.id] || [];
  const hit = lookupSeq && bs.some(b => b.seq === lookupSeq);
  const cls = ['row'];
  if (bs.length) cls.push('bound');
  if (hit) cls.push('hit');

  const act = actById[c.id];
  if (act) cls.push('action');
  if (capture && capture.mode === 'assign' && capture.cmdId === c.id) cls.push('capturing');

  const keys = bs.map(b =>
    `<span class="key ${b.isDefault ? '' : 'custom'}${
       claimantsOf(b.seq).length > 1 ? ' shared' : ''}">${esc(fmtSeq(b.seq))}</span>`).join('');

  const dom = act
    ? `<span class="dom${domainLive(act) ? ' live' : ''}">${esc(domainText(act))}</span>` : '';

  return `<div class="${cls.join(' ')}" data-id="${esc(c.id)}">
    <span class="r-name">${esc(c.name)}</span>${dom}
    <span class="r-keys" data-act="assign">${
      capture && capture.mode === 'assign' && capture.cmdId === c.id
        ? '<span class="key">press a key…</span>' : keys}</span>
  </div>`;
}

// One delegated handler for the whole list. Interpolating labels and ids into
// inline onclick= attributes breaks the moment one contains an apostrophe (some
// localized workspace names do), so nothing in a row carries executable markup.
function onListClick(e) {
  const grp = e.target.closest('.grp-hdr');
  // ⌥-click (a held Space) never folds: this list has no selection to give it.
  if (grp) { if (!e.altKey) toggleGroup(grp.dataset.group); return; }
  const row = e.target.closest('.row');
  if (!row) return;
  if (!e.target.closest('[data-act="assign"]')) return;
  // Declared actions are assigned here; everything else is still Fusion's to bind.
  if (actById[row.dataset.id]) assignAction(row.dataset.id);
  else assign(row.dataset.id);
}

// Default state: folded. A workspace's worth of commands per group makes an
// all-open list unreadable, and the one group worth having open on arrival is his
// own add-ins. Searching overrides it — a hit inside a folded group must show.
const ALWAYS_OPEN = 'My add-ins';
function isGroupCollapsed(g) {
  if (query || lookupSeq) return false;
  if (g in groupState) return groupState[g];
  return g !== ALWAYS_OPEN;
}

function toggleGroup(g) {
  groupState[g] = !isGroupCollapsed(g);
  savePrefs(); render();
}

function setTab(t) { tab = t; syncTabs(); savePrefs(); render(); }

function syncTabs() {
  for (const el of document.querySelectorAll('.tab'))
    el.classList.toggle('sel', el.dataset.tab === tab);
}

function showStatus(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg || '';
  el.className = msg ? (ok ? 'ok' : 'err') : '';
  if (msg) setTimeout(() => {
    if (el.textContent === msg) { el.textContent = ''; el.className = ''; }
  }, 4000);
}

// ── Capture (assign + reverse lookup) ─────────────────────────────────────────

// Native side swallows every keyDown while capture is on, so combinations macOS
// would otherwise intercept (⌘W, ⌘Q, ⌘X…) still reach us.
function nativeCapture(on) {
  try { window.webkit.messageHandlers.capture.postMessage(!!on); } catch (_) {}
}

function startCapture(c) {
  capture = c;
  nativeCapture(true);
  document.getElementById('lookup').classList.add('armed');
  setLookupText('');       // arming clears the field, like clicking into a text box
}

function endCapture() {
  if (!capture) return;
  capture = null;
  nativeCapture(false);
  document.getElementById('lookup').classList.remove('armed');
}

// Setting a shortcut hands the command to Fusion's own "Change Keyboard Shortcut"
// prompt, which applies it live, resolves conflicts and persists it. Fusion is
// brought forward first — the prompt is modal on Fusion's window, so if this app
// stayed frontmost it would open behind and swallow the keystrokes.
// Leaving the field blank in that prompt is how a binding gets cleared, which is
// why there's no separate remove control here.
// A DECLARED action takes its key here, not in Fusion's dialog.
//
// Fusion's keymap only has to hold one binding per key — the anchor — for us to hear
// the key at all. If the key already has one, assigning is a one-line edit to
// fusion_ui/bindings.json and nothing else on that key is touched: no conflict
// prompt, no binding taken off anything, no Fusion round trip. Only a key we have
// never claimed before needs the dialog, and only once, to create that anchor.
function assignAction(id) {
  if (capture && capture.mode === 'assign' && capture.cmdId === id) { endCapture(); render(true); return; }
  startCapture({ mode: 'assign', cmdId: id });
  showStatus('Press a key for this action — ⌫ clears it, ⎋ cancels.', true);
  render(true);
}

async function bindAction(id, seq) {
  const act = actById[id];
  // Refuse a double-up that isn't safe, and say which claimant it would fight with
  // rather than silently taking the key off it.
  if (seq && act) {
    const clash = claimantsOf(seq).filter(o => o.id !== id && !exclusiveWith(o, act));
    if (clash.length) {
      showStatus(`${fmtSeq(seq)} is already ${clash[0].name} ${domainText(clash[0])} — `
                 + `that overlaps this one.`, false);
      return;
    }
  }
  const res = await postJson(seq ? '/bind' : '/unbind', { cmd_id: id, seq });
  if (!seq) { showStatus('Cleared.', true); return; }
  const others = claimantsOf(seq).filter(o => o.id !== id);
  if (res.anchored === false) {
    // First time this key has been claimed: Fusion needs to hold it once so it fires
    // something we can intercept. That is the only trip through Fusion's dialog.
    showStatus(`${fmtSeq(seq)} is new — confirm it once in Fusion.`, true);
    assign(id);
  } else if (others.length) {
    showStatus(`${fmtSeq(seq)} — shared with ${others.map(o => o.name).join(', ')}.`, true);
  } else {
    showStatus(`${fmtSeq(seq)} assigned.`, true);
  }
}

// Two actions can share a key exactly when some axis rules out ever wanting both at
// once. Mirrors hotkeys.py::exclusive — same rule, both sides.
function exclusiveWith(a, b) {
  const wa = a.when || {}, wb = b.when || {};
  return Object.keys(wa).some(ax =>
    wb[ax] && !wa[ax].some(v => wb[ax].includes(v)));
}

function assign(id) {
  // Step aside for the whole handoff: Fusion forward, this window out of the way.
  // The add-in only reports a result once the modal has been dismissed, so the
  // result timestamp changing is our cue that it's over (see load()).
  try { window.webkit.messageHandlers.focusFusion.postMessage(''); } catch (_) {}
  native('handoff', true);
  awaitingDialog = true;
  setTimeout(() => post('/assign', { cmd_id: id }), 120);
  showStatus('Setting in Fusion’s dialog…', true);
  // Safety net: if the result never arrives (add-in reloaded, Fusion killed) the
  // window must not stay hidden.
  clearTimeout(_handoffTimer);
  _handoffTimer = setTimeout(endHandoff, 180000);
}

let awaitingDialog = false;
let _handoffTimer = null;

function endHandoff() {
  if (!awaitingDialog) return;
  awaitingDialog = false;
  clearTimeout(_handoffTimer);
  native('handoff', false);
}

function native(name, body) {
  try { window.webkit.messageHandlers[name].postMessage(body); } catch (_) {}
}

function armLookup() {
  if (capture && capture.mode === 'lookup') { endCapture(); return; }
  startCapture({ mode: 'lookup' });
}

function clearLookup() {
  endCapture();
  lookupSeq = '';
  setLookupText('');
  render();
}

function setLookupText(s) {
  const t = document.getElementById('lk-text');
  t.textContent = s;
  document.getElementById('lk-clear').style.display = s ? '' : 'none';
}

// Live modifier echo while the field is armed, so holding ⌃⌥ shows "⌃⌥" before the
// real key lands — the field reads like something you're typing into rather than a
// box that stays blank and then jumps to an answer.
window.__captureMods = function (p) {
  if (!capture) return;
  const mods = GLYPH_ORDER.filter(m => p[m]).map(m => MOD_GLYPH[m]).join('');
  setLookupText(mods);
};

// Called from Swift with the raw modifier state + hardware key name. Two modes:
// the reverse lookup (which combination owns what), and assigning a declared action,
// which is captured here rather than in Fusion because the binding is ours to write.
window.__capturedKey = function (p) {
  if (!capture) return;
  const mode = capture.mode, cmdId = capture.cmdId;
  const bare = !p.shift && !p.command && !p.alt && !p.control;
  endCapture();

  if (mode === 'assign') {
    render(true);
    if (p.key === 'escape' && bare) { showStatus('', true); return; }
    if ((p.key === 'backspace' || p.key === 'delete') && bare) { bindAction(cmdId, ''); return; }
    bindAction(cmdId, payloadSeq(p));
    return;
  }

  if (p.key === 'escape' && bare) {
    setLookupText(lookupSeq ? fmtSeq(lookupSeq) : '');
    return;
  }
  lookupSeq = payloadSeq(p);
  setLookupText(fmtSeq(lookupSeq));
  render();
};

// ── Global keys ───────────────────────────────────────────────────────────────

function isTyping() {
  const a = document.activeElement;
  return a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');
}

function seqFromEvent(e) {
  const mods = [];
  if (e.shiftKey) mods.push('shift');
  if (e.metaKey) mods.push('command');
  if (e.altKey) mods.push('alt');
  if (e.ctrlKey) mods.push('control');
  let k = (e.key || '').toLowerCase();
  const map = { arrowup: 'up', arrowdown: 'down', arrowleft: 'left', arrowright: 'right',
                ' ': 'space', enter: 'return', backspace: 'backspace' };
  k = map[k] || k;
  return [...mods, k].join('+');
}

function onKey(e) {
  if (capture) return;                       // native side owns the keyboard now

  if (e.key === 'Escape') {
    // One press does both when the search box has focus — clearing but staying in
    // the field meant every exit took two presses.
    if (isTyping()) {
      const q = document.getElementById('q');
      q.value = ''; query = ''; q.blur(); render();
      e.preventDefault(); return;
    }
    if (lookupSeq) { clearLookup(); e.preventDefault(); return; }
    if (query) { document.getElementById('q').value = ''; query = ''; render(); e.preventDefault(); return; }
  }

  // The shortcut that opened this window closes it again (Fusion never sees the
  // key while we have focus, so the add-in's toggle can't do it).
  const seq = seqFromEvent(e);
  if (closeHotkey && seq === closeHotkey.toLowerCase()) {
    e.preventDefault();
    try { window.webkit.messageHandlers.close.postMessage('close'); } catch (_) {}
    return;
  }

  // Slash / Cmd+F jump to the search box.
  if (!isTyping() && (e.key === '/' || (e.metaKey && e.key === 'f'))) {
    e.preventDefault(); document.getElementById('q').focus(); return;
  }

  // Anything else that Fusion has bound with a modifier is forwarded to Fusion, so
  // its shortcuts keep working while this window is focused (same as the other
  // companion windows). Bare keys are left alone — they're typing.
  if (!isTyping() && seq.includes('+') && fusionKeys[seq]) {
    e.preventDefault(); post('/run', { cmd_id: fusionKeys[seq] });
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  await loadStatic();
  syncTabs();
  document.getElementById('q').addEventListener('input', e => {
    query = e.target.value.trim(); render();
  });
  document.getElementById('list').addEventListener('click', onListClick);
  document.addEventListener('keydown', onKey, true);
  // Clicking anywhere outside the lookup field ends capture — with no banner to
  // dismiss, clicking away is the obvious way out (Escape still works too).
  document.addEventListener('mousedown', e => {
    if (!capture) return;
    // e.target isn't always an Element (it can be the document itself), and this
    // runs in the capture phase — throwing here would swallow the click.
    const el = e.target instanceof Element ? e.target : null;
    if (!el || !el.closest('#lookup')) endCapture();
  }, true);
  // The catalog walk over every workspace is too costly to run per tick, so ask
  // for a fresh one each time the window opens.
  await post('/rebuild');
  await load();
  setInterval(load, 1200);
}

init();
