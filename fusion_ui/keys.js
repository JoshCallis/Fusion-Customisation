/* ─────────────────────────────────────────────────────────────────────────────
   fusion_ui/keys.js — chords, and the ⌘, Hotkeys panel.

   The Browser and Params windows each carried their own copy of both: the
   "cmd+shift+x" chord spelling (seqOf / normSeq / seqLabel) and the settings
   dialog that lists every ACTIONS entry with its default keys, lets the user add
   custom ones and persists them to the shared /settings file. The copies had
   already drifted (one escaped quotes in its inline handlers, one did not; one
   fetched /settings uncached, one did not). They live here once now.

       const { seqOf, normSeq, seqLabel } = Keys;
       const hotkeyPanel = Keys.panel({ actions: ACTIONS, ... });
       document.addEventListener('keydown', hotkeyPanel.onCaptureKey, true);
       document.addEventListener('keydown', hotkeyPanel.onCustomKey, true);

   THE CHORD SPELLING. A stored chord is lower-case, "+"-joined, modifiers first
   in the fixed order cmd, ctrl, alt, shift, then the key — the order seqOf()
   builds from a real keypress, so a normSeq()'d stored chord compares with ===.
   Fusion's own keymap writes "shift+alt+c"; normSeq reorders it, and folds the
   long names (command/meta, option, control) into the short ones.

   HELD SPACE. In the viewport Space held is a layer (Karabiner's "Fusion, Space
   Held" + remapd's `fusion-space` rows: Space+T → ⌥⇧T), but that rule is keyed to
   FUSION frontmost — over a dock/window page (which takes key on hover) Space and
   the letter arrive bare. So Space is tracked here, once, and seqOf spells a key
   pressed under it "space+t" — ahead of cmd — which is how the Browser server's
   /fusionkeys folds those rows in. "space" is a modifier only when a key follows
   it; a bare "space" is still the key. lookup(map, e) falls back to the chord
   without it, so an unbound Space+key still means the key.

   THE PANEL. Each action is `{label, scope, def: [labels], general?, run}`.
   `def` is display-only — the page's own key handler owns those keys. What the
   panel owns is the CUSTOM bindings (settings.custom, {actionId: [chord,…]}),
   which it dispatches from a capture-phase listener ahead of the page's own
   handler, and which are purely additive: defaults are never removed, so an
   empty settings file changes nothing. `general: '<verb>'` marks an action whose
   chord is a Master Shortcut (fusion_ui/general.json, set from Control Room) —
   shown as a default read from /general, never spelled in a page. Whether that
   chord is also DISPATCHED here is the page's call (`dispatchGeneral`): Params
   runs it, the Browser receives it through Fusion's keymap instead. One action
   can opt in on its own with `dispatch: true` — a verb that only means something
   in the focused window (ungroup), so there is no Fusion command to route it.
   ──────────────────────────────────────────────────────────────────────────── */
const Keys = (() => {
  const ORDER = ["space", "cmd", "ctrl", "alt", "shift"];
  const ALIAS = { meta: "cmd", command: "cmd", option: "alt", control: "ctrl" };
  const GLYPH = { space: "␣", cmd: "⌘", ctrl: "⌃", alt: "⌥", shift: "⇧" };
  const KEY_GLYPH = { arrowdown: "↓", arrowup: "↑", arrowleft: "←", arrowright: "→",
                      enter: "↵", " ": "␣", space: "␣", backspace: "⌫", escape: "⎋", ",": "," };

  // Space physically down (see HELD SPACE above). Capture phase, so it is current
  // before any page handler reads a chord.
  let spaceDown = false;
  const isSpace = e => e.code === "Space" || e.key === " ";
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("keydown", e => { if (isSpace(e)) spaceDown = true; }, true);
    document.addEventListener("keyup", e => { if (isSpace(e)) spaceDown = false; }, true);
  }
  if (typeof window !== "undefined" && window.addEventListener)
    window.addEventListener("blur", () => { spaceDown = false; });
  const spaceHeld = () => spaceDown;

  // The chord a keydown event spells, already in normSeq order.
  function seqOf(e) {
    const m = [];
    if (spaceDown && !isSpace(e)) m.push("space");
    if (e.metaKey) m.push("cmd");
    if (e.ctrlKey) m.push("ctrl");
    if (e.altKey) m.push("alt");
    if (e.shiftKey) m.push("shift");
    // Space is spelled "space", the way the keymap stores it (normSeq drops a bare " ").
    m.push(isSpace(e) ? "space" : (e.key || "").toLowerCase());
    return m.join("+");
  }

  // A stored chord in the canonical order, for comparing with ===.
  function normSeq(s) {
    const parts = String(s || "").toLowerCase().split("+").map(x => x.trim()).filter(Boolean);
    const mods = []; let k = "";
    parts.forEach((p, i) => {
      p = ALIAS[p] || p;
      if (ORDER.includes(p) && !(p === "space" && i === parts.length - 1)) mods.push(p);
      else k = p;
    });
    mods.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
    return [...mods, k].join("+");
  }

  // What `map` (normSeq'd chord → value) says for this keydown: the chord as held,
  // else — when Space is part of it — the same chord without Space.
  function lookup(map, e) {
    const seq = seqOf(e);
    if (map[seq] != null) return map[seq];
    return seq.startsWith("space+") ? map[seq.slice(6)] : undefined;
  }

  // Space + C / Space + R: where the cursor LANDS, not one row on. `rows` is the list
  // as drawn, `[{depth, band}]` — `band` is the row's category inside its container
  // (the Browser's pinBand: components, Geometry, sketches, parameters, Origin; a page
  // with no categories leaves it out). A SECTION is one band's run of siblings.
  // dir > 0 goes to the bottom of the cursor's section, dir < 0 to its top. Already
  // there → the same edge of the next section along; no section left at this level →
  // up a level, and the same from the row that holds us. The answer always moves in
  // `dir`, so a held key walks the whole list edge by edge — and WRAPS at the end:
  // past the last row down is the first row, past the first up is the last top-level
  // row. `at` is the cursor's index (-1: none — lands on the first row's section
  // edge). Returns an index, or -1.
  function sectionJump(rows, at, dir) {
    const n = rows.length;
    if (!n) return -1;
    const depth = i => rows[i].depth || 0, band = i => rows[i].band || 0;
    const cold = at < 0 || at >= n;
    if (cold) at = 0;
    const beyond = e => cold || (dir > 0 ? e > at : e < at);
    for (let i = at; i >= 0; ) {
      const d = depth(i);
      let lo = i, hi = i;                         // the run of rows under i's parent
      while (lo > 0 && depth(lo - 1) >= d) lo--;
      while (hi < n - 1 && depth(hi + 1) >= d) hi++;
      const sibs = [];
      for (let j = lo; j <= hi; j++) if (depth(j) === d) sibs.push(j);
      const secs = [];                            // sibling runs of one band
      for (const j of sibs) {
        const last = secs[secs.length - 1];
        if (last && band(last[0]) === band(j)) last.push(j); else secs.push([j]);
      }
      const edge = s => dir > 0 ? s[s.length - 1] : s[0];
      for (let k = secs.findIndex(s => s.includes(i)); k >= 0 && k < secs.length; k += dir > 0 ? 1 : -1)
        if (beyond(edge(secs[k]))) return edge(secs[k]);
      i = lo - 1;                                 // the row that holds this level
      if (i >= 0 && depth(i) >= d) break;         // (malformed depths: stop, don't loop)
    }
    // Nothing further in `dir`: wrap. Down from the end → the very first row; up from
    // the start → the last top-level row (not a row buried inside it).
    if (dir > 0) return 0;
    for (let j = n - 1; j >= 0; j--) if (depth(j) === depth(0)) return j;
    return at;
  }

  // "cmd+shift+e" → "⌘⇧E".
  function seqLabel(s) {
    const parts = normSeq(s).split("+");
    const k = parts.pop();
    const mod = parts.map(p => GLYPH[p] || p).join("");
    return mod + (KEY_GLYPH[k] || (k.length === 1 ? k.toUpperCase() : k));
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* opts:
       actions          the page's ACTIONS table
       sections         [[title, scope], …] — the dialog's groups, in order
       api              URL prefix for /general and /settings ("" = same origin)
       dispatchGeneral  run a Master Shortcut's chord here too (default false)
       blocked()        true while custom bindings must not fire (the page is editing
                        text, a modal of its own is up…)
       onGeneral(a)     sees each /general entry first; return true to consume it
                        (a page handling a `kind: "modifier"` verb itself, say)
       extraHtml(h)     more rows after the sections; `h` = {esc, seqLabel}
       buttonClass      the Done button's class — each page's own button style */
  function panel(opts) {
    const actions = opts.actions;
    const api = opts.api || "";
    const blocked = opts.blocked || (() => false);

    let custom = {};          // actionId -> [chord, …], the user's own additions
    let customMap = {};       // normSeq(chord) -> actionId
    let generalMap = {};      // normSeq(chord) -> actionId, from general.json
    let generalLabels = {};   // actionId -> [label], the same, for display
    let capturingFor = null;  // actionId waiting for its new key, or null

    function buildCustomMap() {
      customMap = {};
      for (const id of Object.keys(custom))
        for (const seq of custom[id]) customMap[normSeq(seq)] = id;
    }

    async function loadGeneral() {
      const map = {}, labels = {};
      try {
        const g = await (await fetch(api + "/general", { cache: "no-store" })).json();
        for (const a of (g && g.actions) || []) {
          if (opts.onGeneral && opts.onGeneral(a)) continue;
          if (!a.chord) continue;
          for (const id of Object.keys(actions)) {
            if (actions[id].general !== a.id) continue;
            map[normSeq(a.chord)] = id;
            const l = seqLabel(a.chord);
            if (!(labels[id] = labels[id] || []).includes(l)) labels[id].push(l);
          }
        }
      } catch (_) {}
      generalMap = map; generalLabels = labels;
    }

    async function load() {
      await loadGeneral();
      try {
        const r = await (await fetch(api + "/settings", { cache: "no-store" })).json();
        if (r && typeof r === "object") custom = r.custom || {};
      } catch (_) {
        try { custom = JSON.parse(localStorage.getItem("hotkeyCustom") || "{}"); } catch (_2) {}
      }
      buildCustomMap();
    }

    function save() {
      try { localStorage.setItem("hotkeyCustom", JSON.stringify(custom)); } catch (_) {}
      fetch(api + "/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ custom }) }).catch(() => {});
      buildCustomMap();
    }

    function isOpen() { return !!document.getElementById("settings-bg"); }
    function capturing() { return !!capturingFor; }

    // Capture phase, ahead of the page's own handler. With no custom bindings (and no
    // dispatched general chord) this does nothing, so the page's defaults are untouched.
    function onCustomKey(e) {
      if (capturingFor || blocked()) return;
      let id = lookup(customMap, e);
      if (!id) {
        const g = lookup(generalMap, e);
        if (g && (opts.dispatchGeneral || (actions[g] && actions[g].dispatch))) id = g;
      }
      if (id && actions[id]) { e.preventDefault(); e.stopPropagation(); actions[id].run(); }
    }

    // While capturing, the next non-modifier keypress becomes the new binding.
    function onCaptureKey(e) {
      if (!capturingFor) return;
      if (["Shift", "Meta", "Alt", "Control"].includes(e.key)) return;   // wait for the real key
      e.preventDefault(); e.stopPropagation();
      if (e.key === "Escape") { stopCapture(); return; }
      const id = capturingFor, seq = seqOf(e);
      custom[id] = custom[id] || [];
      if (!custom[id].some(s => normSeq(s) === normSeq(seq))) custom[id].push(seq);
      capturingFor = null;
      save(); render();
    }

    function open() {
      if (isOpen()) return;
      document.body.insertAdjacentHTML("beforeend",
        `<div id="settings-bg"><div id="settings"><h2>Hotkeys</h2>` +
        `<div id="settings-body"></div>` +
        `<div class="modal-btns" style="text-align:right;margin-top:14px;">` +
        `<button class="${esc(opts.buttonClass || "btn")}" id="s-done">Done</button></div>` +
        `</div></div>`);
      document.getElementById("settings-bg").addEventListener("mousedown", e => {
        if (e.target.id === "settings-bg") close();
      });
      const done = document.getElementById("s-done");
      done.addEventListener("mousedown", e => e.preventDefault());   // keep focus where it was
      done.addEventListener("click", close);
      render();
    }

    function close() {
      capturingFor = null;
      const m = document.getElementById("settings-bg"); if (m) m.remove();
    }

    function startCapture(id) { capturingFor = id; render(); }
    function stopCapture() { capturingFor = null; render(); }
    function removeBinding(id, seq) {
      custom[id] = (custom[id] || []).filter(s => normSeq(s) !== normSeq(seq));
      if (!custom[id].length) delete custom[id];
      save(); render();
    }

    function keyRow(label, keysHtml) {
      return `<div class="set-row"><span class="set-label">${esc(label)}</span>` +
             `<span class="set-keys">${keysHtml}</span></div>`;
    }

    function render() {
      const body = document.getElementById("settings-body"); if (!body) return;
      let html = "";
      for (const [title, scope] of opts.sections) {
        const ids = Object.keys(actions).filter(id => actions[id].scope === scope);
        if (!ids.length) continue;
        html += `<div class="set-sec">${esc(title)}</div>`;
        for (const id of ids) {
          const a = actions[id];
          // A general chord is shown as a default it is not this window's to edit.
          const gen = generalLabels[id] || [];
          const defs = gen.concat(a.def).map(d =>
            `<span class="key def"${gen.includes(d) ? ' title="set in Control Room › General"' : ""}>${esc(d)}</span>`).join("");
          const cust = (custom[id] || []).map((s, i) =>
            `<span class="key cust">${esc(seqLabel(s))}<span class="x" data-rm="${esc(id)}" data-i="${i}">×</span></span>`).join("");
          const addBtn = capturingFor === id
            ? `<span class="key adding">press a key… <span class="x" data-cancel="1">×</span></span>`
            : `<span class="addk" data-add="${esc(id)}">+</span>`;
          html += keyRow(a.label, defs + cust + addBtn);
        }
      }
      if (opts.extraHtml) html += opts.extraHtml({ esc, seqLabel, keyRow });
      body.innerHTML = html;
      body.querySelectorAll("[data-add]").forEach(el => el.onclick = () => startCapture(el.dataset.add));
      body.querySelectorAll("[data-cancel]").forEach(el => el.onclick = stopCapture);
      body.querySelectorAll("[data-rm]").forEach(el => el.onclick = () => {
        const id = el.dataset.rm, seq = (custom[id] || [])[+el.dataset.i];
        if (seq != null) removeBinding(id, seq);
      });
    }

    return { load, loadGeneral, open, close, isOpen, capturing, onCustomKey, onCaptureKey };
  }

  return { seqOf, normSeq, seqLabel, lookup, spaceHeld, sectionJump, panel };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Keys;
