// fusion_ui/list.js — THE LIST KIT. One behaviour for every list a window draws.
//
// A page describes what is UNIQUE to it — its items, its groups, what a row shows,
// what its editor asks, what its verbs do — and the kit owns everything that should
// feel the same on every page: the cursor, ⌘-pick, the open editor, collapsing,
// nesting and dragging groups, renaming in place, the right-click menu, the editor's
// button row, empty states, Find, and what ⏎ / ⎋ / the Control Room nav keys do.
// The rules it implements are written down in fusion_ui/UI.md; a new list feature
// goes HERE first, so every page with that shape gets it at once.
//
// Immediate mode, like the pages that use it: a page's render calls draw() every
// time, and the kit's own state (cursor, pick, open editor…) lives in a state object
// kept per list key, so it survives the redraw. A page leaves out a handler and the
// kit refuses that verb — no page ever needs "not on this page" code.
//
//   const st = ListKit.state("remap/keys");
//   ListKit.draw(container, st, {
//     items:  [{ id, gid, label, off, title }],   // already filtered, in order
//             // + drag/edit/rename: false — a row that can't be grabbed, opened
//             //   or renamed (a placeholder: By Modifier's empty key slots)
//             // + style: a CSS string put on the row (a grid cell's grid-column / grid-row)
//             // + rename: true with renameValue (the field's starting text) and emptyOk
//             //   (an empty name is committed too, as "clear") — a note, not a name
//     groups: [{ id, label, depth, parent, collapsed, own, rename, draggable, tag, cls, off }] | null,
//     name(it), row(it) -> [nodes], groupHead(g) -> [nodes],
//     editor(it|null, draft, st) -> node, draftOf(it), onClose(),
//     renameItem(it, v), renameGroup(g, v), newGroup(parentId) -> {id},
//     ungroup(groups),            // dissolve these; what was in them moves up
//     onCollapse(g, collapsed),   // omitted: collapse is kept by the kit, per list
//     toggle(items), toggleGroups(groups), menu(it, sel) -> [...],
//     groupMenu(g, sel, e) -> [...],   // e: the right-click (where on the heading it landed);
//                                      // a group with `cut: false` offers no Cut
//     color(itemIds, groupIds, c),   // the menu's Color ▸; c is a COLORS id or "" (none).
//                                    // A row or group with `color` wears it on its name.
//     drag: { order: bool, into: bool, mixed: bool, onto: bool,
//             can(drag, target) -> bool, start(drag), end() },
//                                 // onto: a row drop lands ON the row (`onto: id`),
//                                 // not before it — a slot list, where rows swap
//     onDrop({items, groups, into, before, onto, end, last, lead}),   // lead: the key grabbed
//     placed: bool,               // subgroups sit among the rows: a group's `place` is
//                                 // absent (top) | { before: item id } | { end: true };
//                                 // `last` on a drop = below the last row of `into`
//     add: { label, draft() } | { label, placeholder, name(label) }
//        | { label, options: [{ label, draft() }] },   // one button, a menu of kinds
//     enter(it, rowNode), find(), searching, empty, markUnfiled,
//     lead(it) -> [nodes], groupLead(g) -> [nodes],   // drawn BEFORE the name (an icon)
//     sequence: [{ item } | { group }],   // reading order with rows and headings MIXED
//     blocks: bool,               // each depth-0 group (heading, bodies, subgroups) is wrapped
//                                 // in div.list-block, and loose rows get a block of their own —
//                                 // so a page can flow whole groups into columns
//     bodyExtra(g, rows) -> [nodes],  // appended to each group body drawn (a grid's lead column)
//     clearFind() -> bool,        // ⎋ at the end of its chain: the page's own search box
//     enter(it, node): see below; ⏎ with no editor open is nav-in on the cursor row
//   });
//
// `sequence` is for a list whose order means something across groups — the Fusion
// timeline, where a group sits where its first member happened and loose rows run on
// either side of it. Without it the kit draws loose rows first and then each group's
// body, which is right for a filed list and wrong for a history. Each entry names an
// item from `items` or a group from `groups` (both still passed, for lookups); an
// item's own `depth` indents it, and a collapsed group hides every entry after it that
// sits deeper, exactly as the flattened-groups walk does.
//
// Loaded by a plain <script> tag, like chips.js; defines one global.
var ListKit = (function () {
  "use strict";

  const el = (t, c, txt) => {
    const e = document.createElement(t);
    if (c) e.className = c;
    if (txt != null) e.textContent = txt;
    return e;
  };
  // A field nobody wants macOS's word completion, spelling fixes or inline predictions
  // in: a name or a search, not prose. (WebKit's inline predictions also need the
  // web view's allowsInlinePredictions off — RemapApp/main.swift.)
  const noSuggest = i => {
    for (const [a, v] of [["autocomplete", "off"], ["autocorrect", "off"], ["autocapitalize", "off"],
                          ["spellcheck", "false"], ["writingsuggestions", "false"]]) i.setAttribute(a, v);
    return i;
  };

  const K = {
    onChange: () => {},      // the page's render(); set once by the host page
    onChangeSoon: null,      // a selection-only change: see soon() (null = onChange)
    // Whether a PLAIN click moves the cursor (the selection). Control Room says no:
    // there a click only opens/shuts, and selecting is ⌥-click (a held Space) or ⌘.
    clickSelects: true,
    states: {},
    active: null,            // the list drawn last — the one the keys talk to
    drag: null,              // { items: [...ids], groups: [...ids] } mid-drag
    inlineCancel: null,      // an open rename field's ⎋
    inlineNode: null,        // …and its field: one no longer in the page is not open
    menu: null,              // the open right-click menu
    shield: null,            // what blocks the page under it (see showMenu)
    menuTail: null,          // () -> items every right-click menu ends with
    sub: null,               // the open submenu of the right-click menu
    cut: null,               // { key, items, groups } — Cut, waiting for Paste
    drawn: new Set(),        // states drawn since the page's last reset()
    notes: {},               // page messages, by slot
  };

  // ── State ──────────────────────────────────────────────────────────────────
  const COLLAPSE_KEY = "listkit.collapsed:";
  function loadCollapsed(key) {
    try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY + key) || "[]")); }
    catch (e) { return new Set(); }
  }
  function saveCollapsed(st) {
    try { localStorage.setItem(COLLAPSE_KEY + st.key, JSON.stringify([...st.collapsed])); }
    catch (e) {}
  }

  function state(key) {
    return K.states[key] || (K.states[key] = {
      key,
      cur: null,             // "i:<id>" / "g:<id>" — the keyboard's place
      pick: new Set(),       // ⌘-/⇧-clicked; moves, hides, removes as one
      anchor: null,          // where a ⇧-click's run starts: the last plain or ⌘ click
      open: null,            // item id whose editor is open, or "__new__"
      draft: null,           // what that editor is editing
      renaming: null,        // "i:<id>" / "g:<id>" while a name is typed
      adding: false,         // the add-by-name row is open
      query: null,           // the built-in Find filter, or null when shut
      collapsed: loadCollapsed(key),   // groups the kit collapses itself
      seen: [],              // cursor order, as the last draw showed it
      spec: null, editorNode: null, scroll: false,
    });
  }

  const ik = id => "i:" + id, gk = id => "g:" + id;
  const kindOf = k => k && k[0];
  const idOf = k => k && k.slice(2);

  function itemOf(st, id) { return (st.spec.items || []).find(x => String(x.id) === String(id)); }
  function groupOf(st, id) { return (st.spec.groups || []).find(g => String(g.id) === String(id)); }

  // What a verb acts on: the pick, or failing that the cursor.
  function selection(st, kind) {
    const out = [...st.pick].filter(k => kindOf(k) === kind).map(idOf);
    if (out.length) return out;
    if (kind === "i" && st.open && st.open !== "__new__") return [st.open];
    return st.cur && kindOf(st.cur) === kind ? [idOf(st.cur)] : [];
  }
  // A menu on a picked thing means all of the pick; on anything else, just it.
  function selFor(st, key) {
    return st.pick.has(key) ? [...st.pick].filter(k => kindOf(k) === kindOf(key)).map(idOf)
                            : [idOf(key)];
  }

  function changed() { K.onChange(); scrollCur(); }
  function scrollCur() {
    const st = K.active;
    if (!st || !st.scroll) return;
    st.scroll = false;
    const n = st.cur && document.querySelector('[data-key="' + CSS.escape(st.cur) + '"]');
    if (n) n.scrollIntoView({ block: "nearest" });
  }
  // The cursor moved without anything else changing: repaint the mark in place
  // rather than redraw, so a click inside a row's own field keeps its focus.
  function paintCur(st) {
    document.querySelectorAll(".row.cur, .group-head.cur").forEach(n => n.classList.remove("cur"));
    const n = st.cur && document.querySelector('[data-key="' + CSS.escape(st.cur) + '"]');
    if (n) n.classList.add("cur");
  }
  // Only the cursor / the pick changed: paint both in place at once. A page that draws
  // nothing else from the selection passes a no-op K.onChangeSoon and is not redrawn —
  // Control Room's grid takes ~100 ms a draw, and every click waited that long.
  function soon(st) {
    document.querySelectorAll(".row.picked, .group-head.picked").forEach(n => n.classList.remove("picked"));
    for (const k of st.pick)
      document.querySelectorAll('[data-key="' + CSS.escape(k) + '"]').forEach(n => n.classList.add("picked"));
    paintCur(st);
    (K.onChangeSoon || K.onChange)();
  }

  function open(st, it) {
    const spec = st.spec;
    st.open = it ? it.id : null;
    st.draft = it && spec.draftOf ? spec.draftOf(it) : null;
    if (it && K.clickSelects) st.cur = ik(it.id);
    if (spec.onClose) spec.onClose();
    changed();
  }
  function close(st) {
    st = st || K.active;
    if (!st) return;
    const was = st.open !== null || st.adding;
    st.open = null; st.draft = null; st.adding = false; st.editorNode = null;
    if (st.spec && st.spec.onClose) st.spec.onClose();
    if (was) K.onChange();
  }

  function setCollapsed(st, g, collapsed) {
    if (st.spec.onCollapse && g.collapsed !== undefined) {
      g.collapsed = collapsed;
      st.spec.onCollapse(g, collapsed);
    } else {
      if (collapsed) st.collapsed.add(String(g.id)); else st.collapsed.delete(String(g.id));
      saveCollapsed(st);
    }
  }
  function isCollapsed(st, g) {
    if (st.spec.searching || (st.query || "").trim()) return false;   // a search opens everything
    return st.spec.onCollapse && g.collapsed !== undefined ? !!g.collapsed
                                                            : st.collapsed.has(String(g.id));
  }

  function pick(st, key) {
    // The first ⌘-click takes the cursor's row along, as in Finder: a plain click then
    // ⌘-clicks reads as one selection, and a page that draws the cursor like a pick
    // (the timeline) otherwise left that first row out of every verb.
    if (!st.pick.size && st.cur && st.cur !== key && st.seen.includes(st.cur)) st.pick.add(st.cur);
    if (st.pick.has(key)) st.pick.delete(key); else st.pick.add(key);
    st.anchor = key;
    soon(st);
  }
  // ⇧-click: the whole run, as drawn, between the anchor and this — as in Finder and
  // the Browser. The anchor is the last thing clicked or ⌘-picked without ⇧, so a
  // second ⇧-click re-measures from the same place; the run replaces the pick.
  // A list drawn as blocks side by side (spec.blocks — By Modifier's grid) takes the
  // run as SEEN: the rectangle between the two rows on screen, not every row its
  // reading order puts between them (whole columns of other apps).
  function pickRange(st, key) {
    const from = st.anchor && st.seen.includes(st.anchor) ? st.anchor : st.cur;
    const a = st.seen.indexOf(from), b = st.seen.indexOf(key);
    if (a < 0 || b < 0) return pick(st, key);
    const rect = st.spec.blocks && st.container ? rangeByRect(st, from, key) : null;
    st.pick = rect || new Set(st.seen.slice(Math.min(a, b), Math.max(a, b) + 1));
    st.cur = key;
    soon(st);
  }
  // Every drawn row whose centre lies in the box spanning rows `from` and `to`; null
  // when either is not on screen.
  function rangeByRect(st, from, to) {
    const nodeOf = k => st.container.querySelector('.row[data-key="' + CSS.escape(k) + '"]');
    const na = nodeOf(from), nb = nodeOf(to);
    if (!na || !nb) return null;
    const ra = na.getBoundingClientRect(), rb = nb.getBoundingClientRect();
    const L = Math.min(ra.left, rb.left), R = Math.max(ra.right, rb.right);
    const T = Math.min(ra.top, rb.top), B = Math.max(ra.bottom, rb.bottom);
    const out = new Set();
    for (const n of st.container.querySelectorAll(".row[data-key]")) {
      const k = n.dataset.key;
      if (kindOf(k) !== "i" || !st.seen.includes(k)) continue;
      const r = n.getBoundingClientRect(), x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
      if (r.width && x >= L && x <= R && y >= T && y <= B) out.add(k);
    }
    return out.size ? out : null;
  }
  // A click's modifier, if it picks: ⇧ a run, ⌘ one more. True when it was handled.
  function clickPicks(st, key, e) {
    if (e.shiftKey && !e.metaKey) { pickRange(st, key); return true; }
    if (e.metaKey) { pick(st, key); return true; }
    return false;
  }

  // ── Drawing ────────────────────────────────────────────────────────────────
  // An inline field whose node was redrawn away without finishing is not open: let
  // go of it, or it holds every list key (quiet) until the window reloads.
  function inlineLive() {
    if (K.inlineCancel && K.inlineNode && !K.inlineNode.isConnected) {
      K.inlineCancel = null; K.inlineNode = null;
    }
    return !!K.inlineCancel;
  }

  function draw(container, st, spec) {
    inlineLive();
    st.spec = spec;
    st.container = container;
    K.active = st;
    K.drawn.add(st);
    st.seen = [];
    st.editorNode = null;
    const items0 = spec.items || [];
    const groups = spec.groups || null;

    // Anything the pick or cursor names that has since gone is dropped, or it goes
    // on being counted by a page that cannot show it.
    const live = new Set(items0.map(x => ik(x.id)).concat((groups || []).map(g => gk(g.id))));
    for (const k of [...st.pick]) if (!live.has(k)) st.pick.delete(k);
    if (st.cur && !live.has(st.cur)) st.cur = null;
    if (st.open && st.open !== "__new__" && !live.has(ik(st.open))) { st.open = null; st.draft = null; }
    // A cut that has lost what it named (moved, removed) loses that part of itself.
    if (K.cut && K.cut.key === st.key && !spec.searching) {
      K.cut.items = K.cut.items.filter(i => live.has(ik(i)));
      K.cut.groups = K.cut.groups.filter(i => live.has(gk(i)));
      if (!K.cut.items.length && !K.cut.groups.length) K.cut = null;
    }

    // The built-in Find: a page with a search box of its own answers find() itself.
    let items = items0;
    if (st.query !== null && !spec.find) {
      const bar = el("div", "filters findbar");
      const q = el("input");
      q.dataset.nocaps = "1";             // a search is not a name
      noSuggest(q);
      q.placeholder = "Find";
      q.value = st.query;
      q.oninput = () => { st.query = q.value; K.onChange(); };
      q.onkeydown = e => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); st.query = null; K.onChange(); }
      };
      bar.appendChild(q);
      container.appendChild(bar);
      setTimeout(() => {
        if (document.activeElement === document.body || !document.activeElement) {
          q.focus(); try { q.setSelectionRange(q.value.length, q.value.length); } catch (e) {}
        }
      }, 0);
      K.findInput = q;
      const t = st.query.trim().toLowerCase();
      if (t) items = items0.filter(x => nameOf(spec, x).toLowerCase().includes(t));
    }
    const searching = !!spec.searching || !!(st.query || "").trim();

    let shown = 0;
    const drawRows = (parent, list, unfiled) => {
      for (const it of list) {
        st.seen.push(ik(it.id));
        const r = row(st, it);
        if (unfiled) r.classList.add("unfiled");
        parent.appendChild(r);
        if (st.open === it.id && spec.editor) parent.appendChild(editorNode(st, it));
        shown++;
      }
    };

    if (spec.sequence) {
      // One walk, in the page's own order. `under` is the depth being hidden below a
      // collapsed heading, or -1 — the same rule as the flattened-groups walk below.
      const shownItems = new Set(items.map(x => String(x.id)));
      let under = -1;
      for (const ent of spec.sequence) {
        if (ent.group) {
          const g = ent.group, depth = g.depth || 0;
          if (under >= 0 && depth > under) continue;
          under = -1;
          container.appendChild(groupHead(st, g, depth));
          st.seen.push(gk(g.id));
          if (isCollapsed(st, g)) under = depth;
        } else if (ent.item) {
          const it = ent.item, depth = it.depth || 0;
          if (under >= 0 && depth > under) continue;
          if (!shownItems.has(String(it.id))) continue;
          st.seen.push(ik(it.id));
          const r = row(st, it);
          r.classList.add("seq");
          r.style.setProperty("--depth", depth);
          container.appendChild(r);
          shown++;
        }
      }
    } else if (!groups) {
      drawRows(container, items, false);
    } else {
      const known = new Set(groups.map(g => String(g.id)));
      const byGroup = {};
      const loose = [];
      for (const it of items) {
        if (it.gid != null && it.gid !== "" && known.has(String(it.gid)))
          (byGroup[it.gid] = byGroup[it.gid] || []).push(it);
        else loose.push(it);
      }
      // Rows no group claims come first, with no heading of their own — so they
      // cannot hide inside a guessed group and be missed.
      let host = container;
      const block = loose_ => { if (spec.blocks) { host = el("div", "list-block" + (loose_ ? " loose" : "")); container.appendChild(host); } };
      if (loose.length) block(true);
      drawRows(host, loose, !!spec.markUnfiled);

      // Each row's next in its own group: where a group dropped on its lower half goes.
      st.nextRow = new Map();
      for (const list of Object.values(byGroup))
        list.forEach((it, i) => st.nextRow.set(String(it.id), i + 1 < list.length ? list[i + 1].id : null));
      if (spec.placed) drawPlaced(st, container, groups, byGroup, searching, drawRows);
      // Groups arrive flattened in reading order with a depth on each, so a tree
      // is drawn by walking a list and skipping what sits under something
      // collapsed. `under` is the depth being hidden below, or -1.
      let under = -1;
      for (const g of spec.placed ? [] : groups) {
        const depth = g.depth || 0;
        if (under >= 0 && depth > under) continue;
        under = -1;
        const mine = byGroup[g.id] || [];
        // A search shows what matched, not every heading it did not match under.
        if (searching && !mine.length) continue;
        if (!depth) block();
        host.appendChild(groupHead(st, g, depth));
        st.seen.push(gk(g.id));
        if (isCollapsed(st, g)) { under = depth; continue; }
        const body = el("div", "group-body");
        // One step in from the heading, as a custom property: it is the ROWS'
        // border and background that must still reach the full width of the list.
        body.style.setProperty("--depth", depth + 1);
        if (spec.drag && spec.drag.into) dropTarget(st, body, { into: g.id });
        drawRows(body, mine, false);
        if (spec.bodyExtra) for (const n of spec.bodyExtra(g, mine) || []) if (n) body.appendChild(n);
        host.appendChild(body);
      }
    }

    // Only when there is truly nothing to list — rows tucked inside collapsed groups
    // are still there, and "Nothing matches." under a full list of headings was wrong.
    if (!shown && !items.length) {
      const txt = searching ? "Nothing matches."
        : typeof spec.empty === "function" ? spec.empty() : (spec.empty || "Nothing here yet.");
      container.appendChild(el("div", "empty", txt));
    }

    // The floor of the list is "no group at all": dropping a subgroup here brings
    // it back to the top, the only way out of being inside something.
    // In a `sequence` list the floor is after the last row instead.
    if (spec.sequence && spec.drag && spec.drag.order) {
      const floor = el("div", "group-floor");
      dropTarget(st, floor, { end: true });
      container.appendChild(floor);
    } else if (groups && spec.drag && spec.drag.into) {
      const floor = el("div", "group-floor");
      dropTarget(st, floor, { into: "" });
      container.appendChild(floor);
    }

    // Adding: a new item's editor, or a row that asks for its name.
    if (st.open === "__new__" && spec.editor) {
      const n = editorNode(st, null);
      container.appendChild(n);
      setTimeout(() => n.scrollIntoView({ behavior: "smooth", block: "nearest" }), 0);
    } else if (st.adding && spec.add && spec.add.name) {
      const r = el("div", "row adding");
      const i = el("input");
      i.placeholder = spec.add.placeholder || "Name";
      inlineField(i, v => spec.add.name(v), () => { st.adding = false; K.onChange(); });
      r.appendChild(i);
      container.appendChild(r);
    } else if (spec.add) {
      const b = el("button", "addbtn", spec.add.label);
      const kinds = spec.add.options || [];
      // MORE THAN ONE KIND OF THING TO ADD is still ONE button: the menu belongs
      // on it, not beside it, so a list never grows a second "+ New …". Picking a
      // kind opens the ordinary new-item editor with that kind's draft — the
      // choice is which draft, never a different way of adding.
      b.onclick = e => {
        if (kinds.length > 1) showMenu(e, kinds.map(k =>
          ({ label: k.label, run: () => startAdd(st, k) })), { tail: false });
        else startAdd(st);
      };
      container.appendChild(b);
    }
  }

  // A `placed` list: a subgroup sits AMONG its parent's rows, where its `place` puts
  // it — absent, at the top (above the rows); { before: item id }, just above that
  // row; { end: true }, below the last. A place naming a row that is no longer in the
  // parent counts as the top. The rows between two subgroups are a body of their own,
  // each one still "into" the parent. Collapsing and searching as in the flat walk.
  function drawPlaced(st, container, groups, byGroup, searching, drawRows) {
    const ids = new Set(groups.map(g => String(g.id)));
    const kids = new Map(), roots = [];
    for (const g of groups) {
      const p = g.parent != null && ids.has(String(g.parent)) ? String(g.parent) : null;
      if (p === null) { roots.push(g); continue; }
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(g);
    }
    let host = container;
    const body = (g, depth, rows) => {
      const b = el("div", "group-body");
      b.style.setProperty("--depth", depth + 1);
      if (st.spec.drag && st.spec.drag.into) dropTarget(st, b, { into: g.id });
      drawRows(b, rows, false);
      if (st.spec.bodyExtra) for (const n of st.spec.bodyExtra(g, rows) || []) if (n) b.appendChild(n);
      host.appendChild(b);
    };
    const walk = (g, n) => {
      if (n > 12) return;
      const depth = g.depth || 0, mine = byGroup[g.id] || [];
      const sub = kids.get(String(g.id)) || [];
      // A search shows what matched, not every heading it did not match under.
      if (searching && !mine.length) { for (const k of sub) walk(k, n + 1); return; }
      host.appendChild(groupHead(st, g, depth));
      st.seen.push(gk(g.id));
      if (isCollapsed(st, g)) return;
      const here = new Set(mine.map(it => String(it.id)));
      const top = [], end = [], at = new Map();
      for (const k of sub) {
        const p = k.place || {};
        if (p.end) end.push(k);
        else if (p.before != null && here.has(String(p.before))) {
          const b = String(p.before);
          if (!at.has(b)) at.set(b, []);
          at.get(b).push(k);
        } else top.push(k);
      }
      if (!mine.length) body(g, depth, []);      // an empty group still takes a drop
      for (const k of top) walk(k, n + 1);
      let run = [];
      for (const it of mine) {
        const before = at.get(String(it.id));
        if (before) {
          if (run.length) body(g, depth, run);
          run = [];
          for (const k of before) walk(k, n + 1);
        }
        run.push(it);
      }
      if (run.length) body(g, depth, run);
      for (const k of end) walk(k, n + 1);
    };
    for (const g of roots) {
      if (st.spec.blocks) { host = el("div", "list-block"); container.appendChild(host); }
      walk(g, 0);
    }
  }

  // Is group `gid` the group `anc`, or somewhere inside it?
  function isUnder(spec, gid, anc) {
    const byId = new Map((spec.groups || []).map(g => [String(g.id), g]));
    for (let w = gid, n = 0; w != null && w !== "" && n < 16; n++) {
      if (String(w) === String(anc)) return true;
      w = (byId.get(String(w)) || {}).parent;
    }
    return false;
  }

  function startAdd(st, kind) {
    const add = st.spec.add;
    if (!add) return false;
    // The key (New) never opens a menu — it takes the first kind, which is the
    // ordinary one. Named kinds are a pointing choice, like the pick.
    const k = kind || (add.draft || add.name ? add : (add.options || [])[0]);
    if (!k) return false;
    st.pick.clear();
    if (k.draft) { st.open = "__new__"; st.draft = k.draft(); if (st.spec.onClose) st.spec.onClose(); }
    else st.adding = true;
    K.onChange();
    return true;
  }

  function nameOf(spec, it) { return spec.name ? spec.name(it) : (it.label || String(it.id)); }

  function editorNode(st, it) {
    const n = st.spec.editor(it, st.draft, st);
    trapTab(n);
    st.editorNode = n;
    return n;
  }

  function row(st, it) {
    const spec = st.spec, key = ik(it.id);
    const r = el("div", "row" + (st.open === it.id ? " open" : "")
                             + (it.off ? " off" : "")
                             + (st.renaming === key && spec.renameItem ? " renaming" : "")
                             + (st.pick.has(key) ? " picked" : "")
                             + (st.cur === key ? " cur" : "")
                             + (isCut(st, "i", it.id) ? " cut" : "")
                             + (it.cls ? " " + it.cls : ""));
    r.dataset.key = key;
    if (it.style) r.style.cssText += ";" + it.style;
    tag(r, it.color);
    for (const n of (spec.lead ? spec.lead(it) : []) || []) if (n) r.appendChild(n);

    if (st.renaming === key && spec.renameItem) {
      const i = el("input", "rename");
      i.value = it.renameValue != null ? it.renameValue : nameOf(spec, it);
      // With several picked, the field sits on one and its commit names them all.
      const many = st.pick.has(key) && st.pick.size > 1
        ? [...st.pick].filter(k => kindOf(k) === "i").map(k => itemOf(st, idOf(k))).filter(x => x && x.rename !== false)
        : [it];
      inlineField(i, v => { for (const x of many) spec.renameItem(x, v); },
                  () => { st.renaming = null; K.onChange(); }, it.emptyOk);
      // Typed in the name's own face, so the swap changes nothing but the caret.
      r.appendChild(i);
      nameFace(i, () => el("span", "id", "x"), f => { f.style.textDecoration = "none"; });
    } else {
      const name = el("span", "id", nameOf(spec, it));
      if (it.title) name.title = it.title;
      // A single click is the row's; a double-click — read off the click count,
      // which outlives the redraw the first click caused — renames. Renaming
      // closes the editor: a name being typed and a key being chosen are two jobs.
      name.onclick = e => {
        if ((e.metaKey || e.shiftKey) && clickPicks(st, key, e)) { e.stopPropagation(); return; }
        if (e.detail !== 2 || !spec.renameItem || it.rename === false) return;
        e.stopPropagation();
        if (st.open === it.id) { st.open = null; st.draft = null; if (spec.onClose) spec.onClose(); }
        st.renaming = key; K.onChange();
      };
      r.appendChild(name);
    }
    for (const n of (spec.row ? spec.row(it) : []) || []) if (n) r.appendChild(n);

    r.oncontextmenu = e => {
      const items = [];
      if (spec.renameItem && it.rename !== false)
        items.push({ label: "Rename", run: () => { st.renaming = key; K.onChange(); } });      for (const m of (spec.menu ? spec.menu(it, selFor(st, key)) : []) || []) items.push(m);
      for (const m of colorItems(st, key)) items.push(m);
      for (const m of moveItems(st, key, it.gid != null && it.gid !== "" ? String(it.gid) : "")) items.push(m);
      if (!items.length && !tailItems().length) { e.preventDefault(); return; }
      st.cur = key; paintCur(st);
      showMenu(e, items);
    };

    // The cursor lands on the press, not the release: a plain click on a row that
    // will take the cursor paints it at once; the click that follows decides the rest
    // (open, deselect) from what the cursor was BEFORE the press (st.down).
    r.addEventListener("mousedown", e => {
      if (e.button || e.metaKey || e.shiftKey || e.altKey || e.ctrlKey) return;
      if (e.target.closest && e.target.closest("input, select, button, .facet, .sw, .chip.key")) return;
      if (!(K.clickSelects || spec.editor)) return;
      st.down = { key, was: st.cur === key && !st.pick.size };
      if (st.cur !== key) { st.cur = key; paintCur(st); }
    });
    r.onclick = e => {
      const down = st.down && st.down.key === key ? st.down : null;
      st.down = null;
      if ((e.metaKey || e.shiftKey) && clickPicks(st, key, e)) { e.preventDefault(); return; }
      // A click on the row's own control (a key field, a switch) is that
      // control's: the cursor follows, and nothing is redrawn under it.
      if (e.target.closest && e.target.closest("input, select, button, .facet, .sw, .chip.key")) {
        if (K.clickSelects || e.altKey) { st.cur = key; paintCur(st); }
        return;
      }
      // Clicked while it is the whole selection: deselect it — on ⌥ always, on a plain
      // click for a row with nothing to open, or where a click only selects (Control
      // Room); elsewhere a plain click opens the row.
      if ((down ? down.was : st.cur === key) && !st.pick.size
          && (e.altKey || (!spec.editor && K.clickSelects) || (spec.editor && !K.clickSelects))) {
        st.cur = null; soon(st); return;
      }
      st.hit = key;
      st.pick.clear();
      st.anchor = key;
      // ⌥-click (a held Space, see groupHead) selects the row and leaves its editor be.
      if (e.altKey) { st.cur = key; soon(st); return; }
      // Control Room: a click only selects an item; F / ⏎ (nav-in) opens it.
      if (!K.clickSelects && spec.editor) { st.cur = key; soon(st); return; }
      st.cur = K.clickSelects ? key : null;
      if (spec.editor && it.edit !== false) open(st, st.open === it.id ? null : it);
      else K.onChange();
    };

    dragSource(st, r, key, st.renaming !== key && it.drag !== false);
    if (spec.drag && (spec.drag.order || spec.drag.rows || spec.drag.onto))
      dropTarget(st, r, { before: { item: it.id }, gid: it.gid });
    return r;
  }

  function groupHead(st, g, depth) {
    const spec = st.spec, key = gk(g.id);
    const own = g.own !== false;
    // `rename: false` — still ours to file into and nest in, but its name is fixed.
    const named = own && !!spec.renameGroup && g.rename !== false;
    const head = el("div", "group-head" + (isCollapsed(st, g) ? " collapsed" : "")
                                        + (st.renaming === key && spec.renameGroup ? " renaming" : "")
                                        + (st.pick.has(key) ? " picked" : "")
                                        + (st.cur === key ? " cur" : "")
                                        + (depth ? " sub" : "")
                                        + (g.off ? " off" : "")
                                        + (isCut(st, "g", g.id) ? " cut" : "")
                                        + (g.cls ? " " + g.cls : ""));
    head.dataset.key = key;
    tag(head, g.color);
    head.style.setProperty("--depth", depth);

    // NO TWIRL. The heading opens and closes wherever it is clicked, so an arrow
    // saying what a click would do says it twice. Twirling is local and
    // synchronous, which is what lets the name take a plain click like the rest
    // of the heading and still be renamed by a double-click: the second click is
    // read off its own count and the twirl the first one did is flipped back.
    const twirl = () => { setCollapsed(st, g, !isCollapsed(st, g)); K.onChange(); };
    for (const n of (spec.groupLead ? spec.groupLead(g) : []) || []) if (n) head.appendChild(n);
    if (st.renaming === key && spec.renameGroup) {
      const i = el("input", "rename");
      i.value = g.label || "";
      inlineField(i, v => spec.renameGroup(g, v), () => { st.renaming = null; K.onChange(); });
      head.appendChild(i);
      nameFace(i, () => el("h2", "", "x"));
    } else if (g.label != null) {
      const h = el("h2", "", g.label);
      if (named) h.title = "double-click to rename";
      // The name is NOT a drag handle: WebKit begins a drag on the mousedown, and
      // a drag that begins is a double-click that never arrives.
      h.draggable = false;
      h.onclick = e => {
        if ((e.metaKey || e.shiftKey) && clickPicks(st, key, e)) { e.stopPropagation(); return; }
        if (e.detail !== 2 || !named) return;
        e.stopPropagation();
        if (!e.altKey) twirl();                  // undo what the first click did
        st.renaming = key; K.onChange();
      };
      head.appendChild(h);
    }
    for (const n of (spec.groupHead ? spec.groupHead(g) : []) || []) if (n) head.appendChild(n);
    // NO COUNT. What belongs beside a group's name is its name.
    head.appendChild(el("span", "spring"));
    if (g.tag) head.appendChild(el("span", "chip axis", g.tag));

    head.oncontextmenu = e => {
      const items = [];
      if (named)
        items.push({ label: "Rename", run: () => { st.renaming = key; K.onChange(); } });
      if (own && spec.newGroup && spec.nest)
        items.push({ label: "New Group Inside", run: () => newGroup(st, g.id) });
      for (const m of (spec.groupMenu ? spec.groupMenu(g, selFor(st, key), e) : []) || []) items.push(m);
      if (own) for (const m of colorItems(st, key)) items.push(m);
      for (const m of moveItems(st, key, String(g.id), g.cut === false)) items.push(m);
      if (!items.length && !tailItems().length) { e.preventDefault(); return; }
      st.cur = key; paintCur(st);
      showMenu(e, items);
    };
    head.onclick = e => {
      if ((e.metaKey || e.shiftKey) && clickPicks(st, key, e)) { e.preventDefault(); return; }
      if (e.target.closest && e.target.closest("input, select, button, .facet, .chip.key")) return;
      // ⌥ on the group that is already the whole selection deselects it.
      if (e.altKey && st.cur === key && !st.pick.size) { st.cur = null; soon(st); return; }
      st.pick.clear();
      st.anchor = key;
      st.cur = K.clickSelects || e.altKey ? key : null;
      st.hit = key;                // where a Paste lands when the click left no cursor
      // ⌥-click selects the group and leaves it open or shut, as in the Browser. It is
      // what a held Space becomes over these windows: Karabiner adds the Option while
      // Fusion is frontmost, remapd (the tap callback's mouse block) while one of
      // these is. Rows and every other page's expanders follow the same rule.
      if (e.altKey) soon(st); else twirl();
      // What the click left on screen. The page's own copy of the group can lag it
      // (collapse is saved, then reloaded), and a Paste must go by what was seen.
      st.hitOpen = !isCollapsed(st, g);
    };

    dragSource(st, head, key, st.renaming !== key && g.draggable !== false);
    if (spec.drag && (spec.drag.into || spec.drag.order))
      dropTarget(st, head, spec.drag.into ? { into: g.id, head: g.id } : { before: { group: g.id } });
    return head;
  }

  async function newGroup(st, parent, items) {
    const made = await st.spec.newGroup(parent || null, items || []);
    if (made && made.id) st.renaming = gk(made.id);
    K.onChange();
  }

  // ── Dragging ───────────────────────────────────────────────────────────────
  // What is dragged is the pick, if the thing grabbed is in it; otherwise just the
  // thing grabbed, and the pick is let go — the same as clicking it would.
  function dragSource(st, node, key, on) {
    if (!st.spec.drag || !on) return;
    node.draggable = true;
    node.addEventListener("dragstart", e => {
      e.stopPropagation();
      if (!st.pick.has(key)) st.pick.clear();
      const items = selFor(st, key).filter(() => kindOf(key) === "i");
      const groups = kindOf(key) === "g" ? selFor(st, key) : [];
      // A dragged item carries the picked groups with it too, and vice versa.
      for (const k of st.pick) {
        if (kindOf(k) === "g" && !groups.includes(idOf(k))) groups.push(idOf(k));
        if (kindOf(k) === "i" && !items.includes(idOf(k))) items.push(idOf(k));
      }
      K.drag = { items, groups, lead: key };
      node.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", key); } catch (err) {}   // Safari needs a payload
      if (st.spec.drag.start) st.spec.drag.start(K.drag);
    });
    node.addEventListener("dragend", () => {
      node.classList.remove("dragging"); K.drag = null; clearDrop();
      if (st.spec.drag && st.spec.drag.end) st.spec.drag.end();
    });
  }
  function clearDrop() {
    document.querySelectorAll(".dropping, .dropbefore, .dropafter")
      .forEach(n => n.classList.remove("dropping", "dropbefore", "dropafter"));
  }

  // One drop target, and what lands on it means one of three things. On a row, in
  // an ordered list: before that row. On a heading: in an ordered list, before that
  // group; in a filed list, INTO it — or, for a group dropped on the heading's top
  // half, before it as its sibling (half the heading, so both answers are equally
  // easy to give). On a body or the floor: into that group, or to the top.
  // A MIXED list (drag.mixed — the timeline, where rows and groups share one order)
  // lets rows and groups land before either, and any drag use a heading's top half.
  // The floor of a `sequence` list is its END. drag.can(d, target) is the page's
  // veto on top — a target it refuses shows no mark and takes no drop.
  function dropTarget(st, node, t) {
    const spec = st.spec, mixed = !!(spec.drag && spec.drag.mixed);
    const ahead = e => {
      if (!t.head || !K.drag || !(K.drag.groups.length || mixed)) return false;
      const r = node.getBoundingClientRect();
      return e.clientY - r.top < r.height / 2;
    };
    const place = e => {
      const d = K.drag;
      if (!d) return null;
      if (t.end) return { end: true, line: true };
      // A `placed` list takes groups among a group's rows: the top half of a row is
      // above it, the lower half above the next one (or below the last).
      if (t.before && t.before.item !== undefined && spec.placed && t.gid != null && t.gid !== ""
          && d.groups.length && !d.items.length) {
        if (d.groups.some(g => isUnder(spec, t.gid, g))) return null;
        const r = node.getBoundingClientRect();
        if (e.clientY - r.top < r.height / 2) return { before: t.before, line: true };
        const next = (st.nextRow || new Map()).get(String(t.before.item));
        return next != null ? { before: { item: next }, line: true, after: true }
                            : { into: t.gid, last: true, line: true, after: true };
      }
      // An `onto` list: rows land ON a row, never the grabbed one on itself. Several at once
      // (a pick, moved Excel-style) may land on another picked row — a block shifted by
      // one; drag.can decides whether the page takes it.
      if (t.before && t.before.item !== undefined && spec.drag.onto) {
        const on = String(t.before.item), self = d.lead === "i:" + on || (d.items.length === 1 && d.items[0] === on);
        return d.items.length && !d.groups.length && !self ? { onto: t.before.item } : null;
      }
      // A filed list: the lower half of a group's LAST row is below it, at the group's
      // end — otherwise nothing past the last row would take a drop.
      if (t.before && t.before.item !== undefined && spec.drag.into && !spec.sequence && t.gid != null && t.gid !== ""
          && d.items.length && (mixed || !d.groups.length)
          && st.nextRow && st.nextRow.get(String(t.before.item)) === null) {
        const r = node.getBoundingClientRect();
        if (e.clientY - r.top >= r.height / 2)
          return d.items.includes(String(t.before.item)) ? null
            : { into: t.gid, last: true, line: true, after: true };
      }
      // Before itself is where it already is: no drop.
      if (t.before && t.before.item !== undefined)
        return d.items.length && (mixed || !d.groups.length) && !d.items.includes(String(t.before.item))
          ? { before: t.before, line: true } : null;
      if (t.before && t.before.group !== undefined)
        return d.groups.length && (mixed || !d.items.length) && !d.groups.includes(String(t.before.group))
          ? { before: t.before, line: true } : null;
      if (ahead(e))
        return d.groups.includes(String(t.head)) ? null : { before: { group: t.head }, line: true };
      // Not into itself, and not into a group that is moving with it.
      if (t.into && d.groups.includes(String(t.into))) return null;
      return { into: t.into };
    };
    const target = e => {
      const got = place(e);
      if (got && spec.drag && spec.drag.can && !spec.drag.can(K.drag, got)) return null;
      return got;
    };
    node.addEventListener("dragover", e => {
      const got = target(e);
      if (!got) return;
      e.preventDefault(); e.stopPropagation();
      clearDrop();
      node.classList.add(got.after ? "dropafter" : got.line ? "dropbefore" : "dropping");
    });
    node.addEventListener("dragleave", () => node.classList.remove("dropping", "dropbefore", "dropafter"));
    node.addEventListener("drop", e => {
      const got = target(e);
      if (!got) return;
      e.preventDefault(); e.stopPropagation();
      clearDrop();
      const d = K.drag; K.drag = null;
      st.pick.clear();
      if (spec.drag.end) spec.drag.end();
      if (spec.onDrop) spec.onDrop({ items: d.items, groups: d.groups,
                                     into: got.into, before: got.before, onto: got.onto,
                                     end: !!got.end,
                                     last: !!got.last, lead: d.lead });
    });
  }

  // ── Renaming in place ──────────────────────────────────────────────────────
  // The field is typed in the name's own face: size, weight, style and colour read off
  // a probe of what the name would have been. Read once the row is in the page — a
  // detached node has no computed style, so the field fell back to the row's plain face.
  function nameFace(i, makeProbe, extra) {
    const apply = () => {
      if (!i.parentNode) return;
      const probe = makeProbe();
      i.parentNode.appendChild(probe);
      const cs = getComputedStyle(probe);
      i.style.font = cs.font; i.style.color = cs.color; i.style.fontStyle = cs.fontStyle;
      if (extra) extra(i);
      probe.remove();
    };
    if (i.isConnected) apply(); else queueMicrotask(apply);
  }

  // ⏎ commits, ⎋ cancels, losing focus commits. The window's ⎋ reaches it through
  // K.inlineCancel, because inside Remap.app the key never arrives as a keydown.
  function inlineField(i, commit, done, emptyOk) {
    noSuggest(i);
    setTimeout(() => { i.focus({ preventScroll: true }); i.select(); i.scrollLeft = 0; }, 0);
    K.inlineCancel = () => { K.inlineCancel = null; done(); };
    K.inlineNode = i;
    let over = false;
    const finish = save => {
      if (over) return;
      over = true; K.inlineCancel = null; K.inlineNode = null;
      const v = i.value.trim();
      // done() ALWAYS, committed or not: it is what puts the label back in place
      // of the field — and FIRST, so a commit that redraws (a By Modifier note) does
      // not draw the field again: that second field held K.inlineCancel after it was
      // gone, and every list key (⌘R, ⏎, F) went quiet until the window reloaded.
      done();
      if (save && (v || emptyOk)) commit(v);
    };
    i.onkeydown = e => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    };
    // A blur from the node being REMOVED is the list redrawing underneath it, not
    // the hand leaving the field — committing there ends a rename nobody finished.
    // Judged a tick later: a layout that MOVES the field (By Modifier's columns re-flow
    // as it opens) takes focus and hands it straight back, which is not leaving either.
    i.onblur = () => setTimeout(() => { if (i.isConnected && document.activeElement !== i) finish(true); }, 0);
    i.onclick = e => e.stopPropagation();
    i.onmousedown = e => e.stopPropagation();
  }

  // ── A field that finds and picks ────────────────────────────────────────────
  // What spec.enter(it, node) opens on a row that cannot be edited (a By Modifier
  // slot with nothing in it): a field in the name's place, matches in a list under it.
  //   suggestField(rowNode, { load() -> Promise<[{ id, label, hint }]>, pick(entry),
  //                           placeholder, extra(text) -> [entry] })
  // Every word typed must start some word of a name, anywhere in it ("top" finds "Go to
  // Top"; never mid-word), or one word may run several together ("goto" finds "Go To"). Bold entries (our own commands) come first; then names with more typed words matched WHOLE come first, then
  // names that start with what was typed, then whose first word does, then the shorter
  // names; up to 50, scrolled. ↑/↓
  // move through the list, ⏎ picks the highlighted entry (the top one to begin with),
  // a click picks, ⎋ (or leaving the field) cancels. Reusable by any list.
  const nameWords = s => s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  function suggestMatch(all, q) {
    const qw = nameWords(q), ql = q.trim().toLowerCase();
    if (!qw.length) return [];
    const out = [];
    for (const e of all) {
      const nl = e.label.toLowerCase(), nw = nameWords(e.label);
      // Or one word run together ("goto" finds "Go To"): from the start of any word.
      const joined = qw.length === 1 && nw.some((x, n) => nw.slice(n).join("").startsWith(qw[0]));
      if (!joined && !qw.every(w => nw.some(x => x.startsWith(w)))) continue;
      const whole = qw.filter(w => nw.includes(w)).length;
      out.push({ e, len: nl.length, whole, rank: nl.startsWith(ql) ? 0 : nw[0] && nw[0].startsWith(qw[0]) ? 1 : 2 });
    }
    out.sort((a, b) => !!b.e.bold - !!a.e.bold || b.whole - a.whole || a.rank - b.rank || a.len - b.len || a.e.label.localeCompare(b.e.label));
    return out.slice(0, 50).map(x => x.e);
  }
  function suggestField(node, o) {
    const name = node.querySelector(".id");
    const i = el("input", "rename suggest");
    i.dataset.nocaps = "1";
    noSuggest(i);
    i.placeholder = o.placeholder || "Find a command";
    if (name) name.replaceWith(i); else node.appendChild(i);
    const menu = el("div", "facetmenu ctxmenu suggestlist");
    menu.hidden = true;
    document.body.appendChild(menu);
    let shown = [], at = 0, over = false, all = null;
    const done = () => {
      if (over) return;
      over = true; K.inlineCancel = null; K.inlineNode = null; menu.remove();
      removeEventListener("scroll", onScroll, true); removeEventListener("resize", place);
      K.onChange();
    };
    // Fixed to the row, not the screen: re-placed whenever anything scrolls (capture —
    // the page or any pane under it) or the window resizes.
    const place = () => {
      const row = node.isConnected ? node : (i.parentElement || node);
      const r = row.getBoundingClientRect();
      menu.style.position = "fixed";
      menu.style.left = (r.left + 16) + "px";
      menu.style.top = r.bottom + "px";
      menu.style.minWidth = Math.max(260, r.width * 0.5) + "px";
    };
    const onScroll = e => { if (!menu.contains(e.target)) place(); };
    addEventListener("scroll", onScroll, true);
    addEventListener("resize", place);
    const paint = () => {
      menu.innerHTML = "";
      menu.hidden = !shown.length;
      shown.forEach((e, n) => {
        const d = el("div", "facetitem" + (n === at ? " cur" : "") + (e.bold ? " bold" : ""), e.label);
        if (e.hint) d.appendChild(el("span", "sub", e.hint));
        d.onmousedown = ev => { ev.preventDefault(); ev.stopPropagation(); choose(e); };
        d.onmouseenter = () => { at = n; menu.querySelectorAll(".facetitem").forEach((x, m) => x.classList.toggle("cur", m === n)); };
        menu.appendChild(d);
      });
      place();
    };
    const choose = e => { if (over) return; done(); o.pick(e); };
    const refresh = () => {
      shown = all ? suggestMatch(all, i.value) : [];
      // o.extra(text): entries made from what was typed (a new key's output…), after the matches.
      if (o.extra) shown = shown.concat(o.extra(i.value) || []);
      at = 0; paint();
    };
    K.inlineCancel = () => { K.inlineCancel = null; done(); };
    K.inlineNode = i;
    // o.typesEscape: a field where a key is TYPED (a remap's output) takes ⎋ as the key
    // Escape — it is spelled into the field — instead of cancelling. A page that answers
    // ⎋ itself (Remap.app hands it over, never as a keydown) calls i.escapeKey.
    if (o.typesEscape) i.escapeKey = () => { i.value = "escape"; refresh(); };
    setTimeout(() => i.focus(), 0);
    Promise.resolve(o.load()).then(list => { all = list || []; refresh(); }).catch(() => { all = []; });
    i.oninput = refresh;
    i.onkeydown = e => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); if (i.escapeKey) i.escapeKey(); else done(); }
      else if (e.key === "Enter") { e.preventDefault(); if (shown[at]) choose(shown[at]); }
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!shown.length) return;
        at = (at + (e.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
        menu.querySelectorAll(".facetitem").forEach((x, m) => x.classList.toggle("cur", m === at));
        const cur = menu.querySelector(".facetitem.cur");
        if (cur) cur.scrollIntoView({ block: "nearest" });
      }
    };
    // A page that moves the row (By Modifier's column flow) blurs the field mid-move and
    // focuses it again after: only a blur that is still a blur a tick later cancels.
    i.onblur = () => setTimeout(() => { if (i.isConnected && document.activeElement !== i) done(); }, 0);
    i.onclick = e => e.stopPropagation();
    i.onmousedown = e => e.stopPropagation();
    return i;
  }

  // ── Move To, Cut, Paste ────────────────────────────────────────────────────
  // Offered by every list that can take a drop INTO a group (drag.into + onDrop):
  // the menu's other way of doing what a drag does, through the same onDrop, so a
  // page never writes it twice. Move To ▸ lists the group tree; Cut remembers what
  // is picked (dimmed until Paste, ⎋, a new Cut or leaving the page); Paste moves
  // it into a group, or into the group of the row it was asked on.
  const canMove = st => !!(st.spec.onDrop && st.spec.drag && st.spec.drag.into && st.spec.groups);
  const isCut = (st, kind, id) => !!K.cut && K.cut.key === st.key
    && (kind === "i" ? K.cut.items : K.cut.groups).includes(String(id));

  // What a menu on `key` carries: the same set a drag from it would.
  function carry(st, key) {
    const items = kindOf(key) === "i" ? selFor(st, key) : [];
    const groups = kindOf(key) === "g" ? selFor(st, key) : [];
    if (st.pick.has(key)) for (const k of st.pick) {
      if (kindOf(k) === "g" && !groups.includes(idOf(k))) groups.push(idOf(k));
      if (kindOf(k) === "i" && !items.includes(idOf(k))) items.push(idOf(k));
    }
    return { items: items.map(String), groups: groups.map(String) };
  }
  function below(st, gids) {                 // these groups and everything under them
    const out = new Set(gids);
    for (let grew = true; grew;) {
      grew = false;
      for (const g of st.spec.groups || [])
        if (g.parent && out.has(String(g.parent)) && !out.has(String(g.id))) { out.add(String(g.id)); grew = true; }
    }
    return out;
  }
  // Where a carried set may go: [{ into, label, depth }], Top Level first.
  function targets(st, d) {
    const spec = st.spec, gs = spec.groups || [];
    const ok = into => !spec.drag.can || spec.drag.can(d, { into });
    const gidOfItem = i => { const it = itemOf(st, i); return it && it.gid != null && it.gid !== "" ? String(it.gid) : ""; };
    const parentOf = i => { const g = groupOf(st, i); return g && g.parent ? String(g.parent) : ""; };
    const sameHome = d.groups.length ? null
      : (d.items.length && d.items.every(i => gidOfItem(i) === gidOfItem(d.items[0])) ? gidOfItem(d.items[0]) : null);
    const sameUp = !d.items.length && d.groups.length && d.groups.every(i => parentOf(i) === parentOf(d.groups[0]))
      ? parentOf(d.groups[0]) : null;
    const home = sameHome !== null ? sameHome : sameUp;
    const barred = below(st, d.groups);
    const out = [];
    if (home !== "" && ok("")) out.push({ into: "", label: "Top Level", depth: 0 });
    for (const g of gs) {
      const id = String(g.id);
      if (g.own === false || barred.has(id) || id === home || !ok(id)) continue;
      out.push({ into: id, label: g.label, depth: g.depth || 0 });
    }
    return out;
  }
  function moveTo(st, d, into, before) {
    if (!d.items.length && !d.groups.length) return;
    if (into && below(st, d.groups).has(String(into))) { flash("A group can't go inside itself.", "err"); return; }
    st.pick.clear();
    if (K.cut) K.cut = null;
    st.spec.onDrop(before ? { items: d.items, groups: d.groups, before }
                          : { items: d.items, groups: d.groups, into });
    K.onChange();
  }
  function cut(st, d) {
    if (!d.items.length && !d.groups.length) return false;
    K.cut = { key: st.key, items: d.items, groups: d.groups };
    st.pick.clear();
    // A list that parks cut rows loose (pasteLoose) pastes to the top when nothing is
    // selected: the cursor that was on the cut row must not turn that into a drop ON it.
    if (st.spec.pasteLoose) st.cur = null;
    K.onChange();
    return true;
  }
  // Paste where the cursor is: on an expanded group, INSIDE it; on a collapsed one,
  // UNDERNEATH it (its next sibling's place — or the end of its parent when it has
  // none, or when commands are carried, which a sibling slot cannot hold); on a row,
  // in that row's group.
  function pasteAt(st, key) {
    if (!K.cut || K.cut.key !== st.key) return false;
    const d = { items: K.cut.items, groups: K.cut.groups };
    const g = key && kindOf(key) === "g" ? groupOf(st, idOf(key)) : null;
    const it = key && kindOf(key) === "i" ? itemOf(st, idOf(key)) : null;
    // A page that takes drops ONTO a row (By Modifier's slots): pasting on a row is
    // that drop, through the same onDrop.
    if (it && st.spec.drag.onto && !d.groups.length) {
      if (st.spec.drag.can && !st.spec.drag.can(d, { onto: it.id })) return false;
      st.pick.clear();
      K.cut = null;
      st.spec.onDrop({ items: d.items, groups: d.groups, onto: it.id });
      K.onChange();
      return true;
    }
    let into = it && it.gid != null && it.gid !== "" ? String(it.gid) : "", before = null;
    if (g) {
      const open = key === st.hit && st.cur !== key && st.hitOpen !== undefined
        ? st.hitOpen : !isCollapsed(st, g);
      if (open) into = String(g.id);
      else {
        const gs = st.spec.groups, par = String(g.parent || "");
        into = par;
        if (!d.items.length) {
          for (let j = gs.indexOf(g) + 1; j < gs.length; j++) {
            if ((gs[j].depth || 0) > (g.depth || 0)) continue;
            if (String(gs[j].parent || "") === par && !d.groups.includes(String(gs[j].id)))
              before = { group: String(gs[j].id) };
            break;
          }
        }
      }
    }
    if (st.spec.drag.can && !st.spec.drag.can(d, before ? { before } : { into })) return false;
    moveTo(st, d, into, before);
    return true;
  }
  // The menu rows a row or heading adds. `home`: the group a Paste on it lands in.
  function moveItems(st, key, home, noCut) {
    if (!canMove(st)) return [];
    const d = carry(st, key), out = [];
    const to = targets(st, d);
    if (to.length) out.push({ label: "Move To", items: to.map(t => ({
      label: "\u2003".repeat(t.depth) + t.label, run: () => moveTo(st, d, t.into) })) });
    if (!noCut) out.push({ label: "Cut", run: () => cut(st, d) });
    if (K.cut && K.cut.key === st.key)
      out.push({ label: "Paste", run: () => pasteAt(st, key) });
    return out;
  }

  // ── Color ──────────────────────────────────────────────────────────────────
  // A colour given by hand to rows and groups (spec.color): Color ▸ in the right-click
  // menu, on what the menu carries (the pick, as a drag would). Stored by the page —
  // the kit only draws `color` and asks for a new one. Finder's tag set; the tokens
  // are theme.css's --tag-*.
  const COLORS = [["red", "Red"], ["orange", "Orange"], ["yellow", "Yellow"],
                  ["green", "Green"], ["blue", "Blue"], ["purple", "Purple"], ["gray", "Gray"]];
  const COLOR_IDS = new Set(COLORS.map(c => c[0]));
  function tag(node, c) {
    if (!c || !(COLOR_IDS.has(c) || c === "pink" || c === "teal")) return;   // pink: By Modifier's D-Pad, teal: its trackpad rolls — not in the menu
    node.classList.add("tagged");
    node.style.setProperty("--tag", "var(--tag-" + c + ")");
  }
  function colorItems(st, key) {
    if (!st.spec.color) return [];
    const d = carry(st, key);
    const set = c => { st.pick.clear(); st.spec.color(d.items, d.groups, c); K.onChange(); };
    // In the menu itself, between dividers — not a flyout (asked for).
    return [{ sep: true }].concat(COLORS.map(([c, label]) => ({ label, swatch: c, run: () => set(c) })),
      [{ label: "No Color", swatch: "", run: () => set("") }, { sep: true }]);
  }

  // ── The right-click menu ───────────────────────────────────────────────────
  // What can be DONE to a row or a group lives here and nowhere on the row: a list
  // read for its keys should not carry a button per line. One at a time, at the
  // pointer, gone on the next click or ⎋.
  //
  // THE TAIL (config menuTail() -> [items]): what every menu on the page carries
  // below a divider, whatever was right-clicked — Control Room's Collapse All. Not on
  // an add button's menu of kinds ({ tail: false }).
  //
  // MODAL. While a menu is up, a shield covers the page under it: a click anywhere
  // else only shuts the menu — no button, row or link beneath hears it, and nothing
  // under it lights up on hover. The shield outlives the menu by one click, so the
  // mouseup/click of the press that shut it land on the shield too.
  const tailItems = () => (K.menuTail && K.menuTail()) || [];
  function dropShield() { if (K.shield) { K.shield.remove(); K.shield = null; } }
  // The open menu follows any scroller holding what it was opened on: each scroll
  // moves it (and its flyout) by the same amount. The document's own scroll needs
  // nothing — the menu is placed in page coordinates.
  function followScroll(anchor) {
    const at = new Map();
    for (let n = anchor && anchor.parentElement; n && n !== document.body; n = n.parentElement)
      at.set(n, [n.scrollLeft, n.scrollTop]);
    const on = ev => {
      const was = at.get(ev.target);
      if (!was) return;
      const dx = ev.target.scrollLeft - was[0], dy = ev.target.scrollTop - was[1];
      at.set(ev.target, [ev.target.scrollLeft, ev.target.scrollTop]);
      for (const m of [K.menu, K.sub]) if (m) {
        m.style.left = (parseFloat(m.style.left) - dx) + "px";
        m.style.top = (parseFloat(m.style.top) - dy) + "px";
      }
    };
    document.addEventListener("scroll", on, true);
    K.unfollow = () => document.removeEventListener("scroll", on, true);
  }
  function closeMenu() {
    if (K.unfollow) { K.unfollow(); K.unfollow = null; }
    dropShield();
    closeSub();
    if (K.menu) { K.menu.remove(); K.menu = null; return true; }
    return false;
  }
  function closeSub() {
    if (K.sub) { K.sub.remove(); K.sub = null; }
    if (K.subRow) { K.subRow.classList.remove("subopen"); K.subRow = null; }
  }
  // Another row takes over from an open flyout only once the pointer SETTLES on it:
  // crossing rows on the way to the flyout must not shut it (menu_aim.js). Leaving the
  // menu for the page shuts nothing either — a click or ⎋ does.
  const aim = () => window.MenuAim || { hover: (row, act) => act(), cancel() {} };
  function shield() {
    const s = el("div", "menushield");
    const swallow = ev => { ev.preventDefault(); ev.stopPropagation(); };
    // Only a press that STARTED on the shield ends it: the right-click that opened the
    // menu releases onto the shield too.
    let pressed = false;
    // A repeat of the opening press is not a press: Remap.app receives each right-click
    // TWICE — a second full down/up at the same point ~15 ms after the first release
    // (traced 2026-10-12) — and that second down shut the menu (the chip menu flashed).
    s.onmousedown = ev => {
      swallow(ev);
      const at = K.menuAt;
      if (ev.button === 2 && at && Date.now() - at.t < 450
          && Math.abs(ev.clientX - at.x) < 8 && Math.abs(ev.clientY - at.y) < 8) return;
      pressed = true; closeSub(); if (K.menu) { K.menu.remove(); K.menu = null; }
      if (K.unfollow) { K.unfollow(); K.unfollow = null; }
    };
    s.onmouseup = ev => {
      swallow(ev);
      if (pressed) setTimeout(() => { if (K.shield === s) dropShield(); }, 0);
    };
    // A click/contextmenu closes it only after a press that started on the shield: the
    // web view may deliver a second contextmenu on the opening right-click's release,
    // which landed here and shut the menu the instant it opened (it flashed).
    s.onclick = s.ondblclick = s.oncontextmenu = ev => { swallow(ev); if (pressed && K.shield === s) closeMenu(); };
    // A wheel over the shield scrolls what is under it, and the menu rides along with
    // its row (followScroll) — it stays on the binding, not on the screen.
    s.onwheel = ev => {
      swallow(ev);
      s.style.pointerEvents = "none";
      let n = document.elementFromPoint(ev.clientX, ev.clientY);
      s.style.pointerEvents = "";
      for (; n && n !== document.body; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (/(auto|scroll)/.test(cs.overflowY + cs.overflowX)
            && (n.scrollHeight > n.clientHeight || n.scrollWidth > n.clientWidth)) break;
      }
      if (n && n !== document.body) n.scrollBy(ev.deltaX, ev.deltaY);
      else window.scrollBy(ev.deltaX, ev.deltaY);
    };
    document.body.appendChild(s);
    K.shield = s;
  }
  // The menu's own rows, into `m`. An item with `items: [...]` is a SUBMENU: it opens
  // beside its row on hover or click, and the next row hovered shuts it again.
  function menuRows(m, items) {
    for (const it of items) {
      // { sep: true } is a divider — between what a row offers and what is about the
      // whole list (a page's menu tail).
      // Never two in a row, and never one at the top or the bottom.
      if (it.sep) {
        if (m.lastChild && !m.lastChild.classList.contains("menusep")) m.appendChild(el("div", "menusep"));
        continue;
      }
      // { info: true } is a line to read, not a command (a session's Status, its counts).
      const d = el("div", "facetitem" + (it.danger ? " danger" : "")
                                      + (it.disabled || it.info ? " disabled" : "")
                                      + (it.info ? " info" : "")
                                      + (it.items ? " hassub" : ""), it.label);
      if (it.swatch !== undefined) {
        const sw = el("span", "swatch" + (it.swatch ? "" : " none"));
        if (it.swatch) sw.style.setProperty("--tag", "var(--tag-" + it.swatch + ")");
        d.prepend(sw);
      }
      if (it.items) {
        const openSub = ev => {
          if (ev) ev.stopPropagation();
          if (it.disabled || (K.subRow === d && K.sub)) return;
          closeSub();
          const sub = el("div", "facetmenu ctxmenu ctxsub");
          menuRows(sub, it.items);
          sub.onmouseenter = () => aim().cancel();
          document.body.appendChild(sub);
          const r = d.getBoundingClientRect(), w = sub.getBoundingClientRect();
          sub.style.left = (r.right + window.scrollX - 2 + w.width > innerWidth - 8
                            ? r.left + window.scrollX - w.width + 2 : r.right + window.scrollX - 2) + "px";
          sub.style.top = Math.max(window.scrollY + 4,
            Math.min(r.top + window.scrollY - 4, window.scrollY + innerHeight - w.height - 8)) + "px";
          K.sub = sub; K.subRow = d;
          d.classList.add("subopen");
        };
        d.onmouseenter = () => { if (!m.classList.contains("ctxsub")) aim().hover(d, openSub, () => K.sub); };
        d.onclick = openSub;
      } else {
        d.onmouseenter = () => { if (!m.classList.contains("ctxsub")) aim().hover(d, closeSub, () => K.sub); };
        d.onclick = ev => { ev.stopPropagation(); if (it.disabled) return; closeMenu(); it.run(); };
      }
      m.appendChild(d);
    }
  }
  function showMenu(e, items, opts) {
    e.preventDefault(); e.stopPropagation();
    // The SAME press again: the web view can deliver a second contextmenu on the
    // release, and it reached the page's catch-all (an empty menu) or a redrawn chip,
    // which shut the menu just opened — the chip menu that flashed away. Ignored.
    const now = Date.now(), at = K.menuAt;
    if (K.menu && at && now - at.t < 450 && Math.abs(e.clientX - at.x) < 8 && Math.abs(e.clientY - at.y) < 8) return;
    if (!(opts && opts.tail === false)) {
      const tail = tailItems();
      if (tail.length) items = items.concat([{ sep: true }], tail);
    }
    // Nothing to show is never a reason to shut what is open.
    if (!items.some(it => !it.sep)) return;
    closeMenu();
    K.menuAt = { t: now, x: e.clientX, y: e.clientY };
    shield();
    const m = el("div", "facetmenu ctxmenu");
    menuRows(m, items);
    if (m.lastChild && m.lastChild.classList.contains("menusep")) m.lastChild.remove();
    // opts.under: a dropdown hung below that element (a chip's), not at the pointer.
    const u = opts && opts.under ? opts.under.getBoundingClientRect() : null;
    const x = u ? u.left + window.scrollX : e.pageX, y = u ? u.bottom + window.scrollY + 4 : e.pageY;
    m.style.left = x + "px";
    m.style.top = y + "px";
    document.body.appendChild(m);
    const r = m.getBoundingClientRect();
    if (r.right > innerWidth - 8) m.style.left = (u ? u.right + window.scrollX - r.width : x - r.width) + "px";
    if (r.bottom > innerHeight - 8)
      m.style.top = Math.max(window.scrollY + 4, u ? u.top + window.scrollY - 4 - r.height : y - r.height) + "px";
    K.menu = m;
    followScroll(u ? opts.under : e.target);
  }

  // ── Tab stays inside what is being edited ──────────────────────────────────
  // And skips the buttons: Cancel and Save are ⎋ and ⏎, and a tab stop that only
  // duplicates a key which already works is a stop you have to tab past.
  function trapTab(wrap) {
    wrap.addEventListener("keydown", e => {
      if (e.key !== "Tab" || e.metaKey || e.ctrlKey || e.altKey) return;
      const stops = [...wrap.querySelectorAll("input, select, .facetbtn, [tabindex]")]
        .filter(n => !n.disabled && n.offsetParent !== null && !n.closest(".actions"));
      if (!stops.length) return;
      e.preventDefault();
      const i = stops.indexOf(document.activeElement);
      const next = e.shiftKey ? (i <= 0 ? stops.length - 1 : i - 1)
                              : (i < 0 || i === stops.length - 1 ? 0 : i + 1);
      stops[next].focus();
      if (stops[next].select) try { stops[next].select(); } catch (err) {}
    });
  }

  // ── The editor's button row ────────────────────────────────────────────────
  // Always in this order: the message (left, with the whole width to say its
  // piece), anything extra, the destructive one, Cancel, then the primary — where
  // macOS puts the default button, with the destructive one furthest from it.
  //   actions({ msg: {text, kind}, extra: [btn], destructive: {label, run},
  //             cancel: fn, primary: {label, run, disabled} })
  function actions(o) {
    const act = el("div", "actions");
    const m = o.msg || null;
    const msg = el("span", "msg" + (m && m.kind ? " " + m.kind : ""), m ? m.text : "");
    act.appendChild(msg);
    for (const b of o.extra || []) act.appendChild(b);
    if (o.destructive) {
      const d = el("button", "danger", o.destructive.label);
      d.disabled = !!o.destructive.disabled;
      d.onclick = () => o.destructive.run();
      act.appendChild(d);
    }
    if (o.cancel !== false) {
      const c = el("button", "", "Cancel");
      c.onclick = () => (o.cancel ? o.cancel() : close());
      act.appendChild(c);
    }
    if (o.primary) {
      const p = el("button", "primary", o.primary.label);
      p.disabled = !!o.primary.disabled;
      p.onclick = () => { if (!p.disabled) o.primary.run(msg); };
      act.appendChild(p);
    }
    act.msg = msg;
    return act;
  }
  // A message set on an editor's own line from inside its Save.
  function say(msgNode, text, kind) {
    msgNode.className = "msg" + (kind ? " " + kind : "");
    msgNode.textContent = text;
  }

  // ── Page messages ──────────────────────────────────────────────────────────
  // Where a message goes when there is no editor to hold it: a card on the page,
  // by slot, until the next try or the next page. Never silence — a save that
  // failed and said nothing is a save you believe happened.
  //   attempt(slot, () => post(...)) — "setting…", then the error or nothing.
  function note(slot) { return K.notes[slot] || null; }
  function setNote(slot, v) { if (v) K.notes[slot] = v; else delete K.notes[slot]; }
  function noteCard(slot) {
    const n = note(slot);
    return n ? el("div", "card " + (n.kind || ""), n.text) : null;
  }
  function resultNote(res) {
    if (res && res.ok) return (res.notes || []).length ? { kind: "warn", text: res.notes.join("  ") } : null;
    const err = res && (res.error || (res.errors || []).join("  "));
    return { kind: "err", text: err || "did not take" };
  }
  async function attempt(slot, fn, busy) {
    setNote(slot, { text: busy || "setting…" }); K.onChange();
    let res;
    try { res = await fn(); } catch (e) { res = { ok: false, error: String(e) }; }
    setNote(slot, resultNote(res));
    if (K.reload) await K.reload(); else K.onChange();
    return res;
  }
  // Messages with no editor to hold them share one stack at the bottom left, so
  // none lands on another: a newer one sits above the older, and the page's
  // standing notes (pinNotes) stay underneath. Each has a ✕; an error stays
  // until it is closed, anything else goes by itself.
  function msgStack() {
    let s = document.getElementById("msgstack");
    if (!s) {
      s = el("div", ""); s.id = "msgstack";
      s.appendChild(el("div", "msgpinned"));
      document.body.appendChild(s);
    }
    return s;
  }
  function msgBar(text, kind, onClose) {
    const bar = el("div", "msg flash " + (kind || ""));
    bar.appendChild(el("span", "", text));
    const x = el("button", "msgx", "✕");
    x.type = "button"; x.title = "Close";
    x.onclick = e => { e.stopPropagation(); bar.remove(); if (onClose) onClose(); };
    bar.appendChild(x);
    return bar;
  }
  function flash(text, kind) {
    const bar = msgBar(text, kind);
    msgStack().prepend(bar);
    if (kind !== "err") setTimeout(() => bar.remove(), 4200);
  }
  // The page's standing notes, [[kind, text, title?], …] — redrawn whole on every
  // call; one closed with ✕ stays closed until it stops being said.
  const notesShut = new Set();
  function pinNotes(items) {
    const box = msgStack().querySelector(".msgpinned");
    box.textContent = "";
    const said = new Set((items || []).map(([kind, text]) => kind + "\n" + text));
    for (const k of [...notesShut]) if (!said.has(k)) notesShut.delete(k);
    for (const [kind, text, more] of items || []) {
      const key = kind + "\n" + text;
      if (notesShut.has(key)) continue;
      const bar = msgBar(text, kind, () => notesShut.add(key));
      if (more) bar.title = more;
      box.appendChild(bar);
    }
  }

  // ── The keys ───────────────────────────────────────────────────────────────
  // The Control Room command ids (controlroom_keys.json): the host page reads the
  // chord and hands the id here. True means it was this list's to answer.
  function command(id) {
    const st = K.active;
    if (!st || !st.spec || st.renaming || st.adding) return false;
    const spec = st.spec;
    const editing = st.open !== null;
    const cur = st.cur;
    const parentKey = k => {
      if (!k) return null;
      if (kindOf(k) === "i") { const it = itemOf(st, idOf(k)); return it && it.gid != null && it.gid !== "" && groupOf(st, it.gid) ? gk(it.gid) : null; }
      const g = groupOf(st, idOf(k)); return g && g.parent ? gk(g.parent) : null;
    };

    if (id === "nav-down" || id === "nav-up") {
      if (editing) {                         // leaves the open item: shut it, then step
        if (st.open === "__new__" || st.adding) return true;
        if (!cur) st.cur = ik(st.open);
        close(st);
      }
      const d = id === "nav-down" ? 1 : -1, n = st.seen.length;
      if (!n) return true;
      const at = st.seen.indexOf(st.cur);
      st.cur = st.seen[at < 0 ? (d > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, at + d))];
      st.pick.clear();
    } else if (id === "nav-bottom" || id === "nav-top") {
      // Space + C / R: the edge of the cursor's run of siblings, then up a level
      // (Keys.sectionJump — a page without keys.js has no such verb).
      if (editing) {
        if (st.open === "__new__" || st.adding) return true;
        if (!st.cur) st.cur = ik(st.open);
        close(st);
      }
      if (typeof Keys === "undefined" || !st.seen.length) return false;
      const depth = k => { let d = 0; for (let p = parentKey(k); p; p = parentKey(p)) d++; return d; };
      const i = Keys.sectionJump(st.seen.map(k => ({ depth: depth(k) })),
                                 st.seen.indexOf(st.cur), id === "nav-bottom" ? 1 : -1);
      if (i >= 0) st.cur = st.seen[i];
      st.pick.clear();
    } else if (id === "nav-in") {
      if (!cur || editing) return true;
      if (kindOf(cur) === "g") {
        const g = groupOf(st, idOf(cur));
        if (!g) return true;
        if (isCollapsed(st, g)) setCollapsed(st, g, false);
        else {
          // Open already: step to its first child, if it has one.
          const next = st.seen[st.seen.indexOf(cur) + 1];
          if (next && parentKey(next) === cur) st.cur = next;
        }
      } else {
        const it = itemOf(st, idOf(cur));
        const node = () => document.querySelector('[data-key="' + CSS.escape(cur) + '"]');
        // spec.enterFirst: "go in" is the page's own (its enter) even on an editable row;
        // the editor only when enter returns false.
        if (it && spec.enterFirst && spec.enter && spec.enter(it, node()) !== false) return true;
        if (it && spec.editor && it.edit !== false) { st.scroll = true; open(st, it); return true; }
        if (it && spec.enter && !spec.enterFirst) { spec.enter(it, node()); return true; }
      }
    } else if (id === "nav-out") {
      if (editing) { close(st); return true; }
      if (!cur) return true;
      let g = kindOf(cur) === "g" ? groupOf(st, idOf(cur)) : null;
      if (!g || isCollapsed(st, g)) {
        const p = parentKey(cur);
        g = p ? groupOf(st, idOf(p)) : null;
        if (g) st.cur = p;
      }
      if (g && !isCollapsed(st, g)) setCollapsed(st, g, true);
    } else if (id === "deselect") {
      st.pick.clear(); st.cur = null;
      if (editing) { close(st); return true; }
    } else if (id === "rename") {
      // Not over an open editor: what is half-typed there would be thrown away.
      // Several picked items rename together: the field opens on the last-edited one
      // (the cursor, if picked) and its commit names every picked item.
      const allItems = st.pick.size > 1 && [...st.pick].every(p => kindOf(p) === "i");
      const k = st.pick.size === 1 ? [...st.pick][0]
              : allItems ? (st.pick.has(cur) ? cur : [...st.pick].pop())
              : (st.pick.size ? null : cur);
      // The one exception is the open row's own header, selected on purpose (⌥-click):
      // renaming it closes its editor first, the way a double-click on the name does.
      const ownHead = editing && st.open !== "__new__" && k === ik(st.open);
      if (!k || (editing && !ownHead)) return false;
      if (kindOf(k) === "i" ? !spec.renameItem
          : !(spec.renameGroup && (groupOf(st, idOf(k)) || {}).own !== false
              && (groupOf(st, idOf(k)) || {}).rename !== false)) return false;
      if (ownHead) { st.open = null; st.draft = null; st.editorNode = null; if (spec.onClose) spec.onClose(); }
      st.renaming = k;
    } else if (id === "new-group") {
      if (!spec.newGroup || editing) return false;
      // Beside the cursor: in the group it sits in.
      const p = parentKey(cur);
      newGroup(st, spec.nest && p ? idOf(p) : null, selection(st, "i"));
      return true;
    } else if (id === "ungroup") {
      // Only what is SELECTED, and only when all of it is groups: a row picked inside
      // a group never ungroups the group around it — the kit declines and the key
      // passes on.
      if (!spec.ungroup || editing) return false;
      const keys = st.pick.size ? [...st.pick] : (cur ? [cur] : []);
      if (!keys.length || keys.some(k => kindOf(k) !== "g")) return false;
      const gs = keys.map(k => groupOf(st, idOf(k))).filter(g => g && g.own !== false);
      if (gs.length !== keys.length) return false;
      st.pick.clear(); st.cur = null;
      spec.ungroup(gs);
      K.onChange();
      return true;
    } else if (id === "toggle-enabled") {
      // Groups picked (and no rows): the groups' own off, where the page has one.
      const gids = selection(st, "g");
      if (gids.length && !selection(st, "i").length) {
        const gs = gids.map(i => groupOf(st, i)).filter(Boolean);
        if (!spec.toggleGroups || !gs.length) return false;
        spec.toggleGroups(gs);
        return true;
      }
      if (!spec.toggle) return false;
      const ids = selection(st, "i");
      const its = ids.map(i => itemOf(st, i)).filter(Boolean);
      if (!its.length) return false;
      spec.toggle(its);
      return true;
    } else if (id === "cut") {
      if (!canMove(st) || editing) return false;
      const d = { items: selection(st, "i").map(String), groups: selection(st, "g").map(String) };
      return cut(st, d);
    } else if (id === "paste") {
      const typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (!canMove(st) || typing || !K.cut || K.cut.key !== st.key) return false;
      // spec.pasteLoose: with nothing selected, Paste goes to the top level (into ""),
      // not into the group last clicked (By Modifier parks commands there).
      return pasteAt(st, st.cur || (spec.pasteLoose ? null : st.hit));
    } else if (id === "find") {
      if (spec.find) { spec.find(); return true; }
      st.query = st.query === null ? "" : st.query;
      K.onChange();
      if (K.findInput) { K.findInput.focus(); K.findInput.select(); }
      return true;
    } else if (id === "new") {
      return startAdd(st);
    } else return false;
    st.scroll = true;
    changed();
    return true;
  }

  // The arrow keys: the cursor steps to the row drawn next to it in that direction —
  // on screen, so in a grid of blocks (By Modifier) ← / → reach the next column.
  // A plain list's ↑ / ↓ is nav-up / nav-down; nothing beside it, the cursor stays.
  // True means it was this list's to answer.
  function step(dir) {
    const st = K.active;
    if (!st || !st.spec || st.renaming || st.adding || !st.seen.length) return false;
    const vert = dir === "up" || dir === "down";
    if (vert && !st.spec.blocks) return command(dir === "down" ? "nav-down" : "nav-up");
    if (st.open !== null) {
      if (st.open === "__new__") return true;
      if (!st.cur) st.cur = ik(st.open);
      close(st);
    }
    const root = st.container || document;
    const nodeOf = k => root.querySelector('[data-key="' + CSS.escape(k) + '"]');
    const from = st.cur && nodeOf(st.cur);
    if (!from) { st.cur = st.seen[0]; st.pick.clear(); st.scroll = true; changed(); return true; }
    const a = from.getBoundingClientRect(), seen = new Set(st.seen);
    // Gap along the step, then how far off-line (0 when the two overlap across it);
    // off-line counts heavily, so the cell in the same row/column beats a nearer diagonal.
    let best = null, bestScore = Infinity;
    for (const n of root.querySelectorAll(".row[data-key], .group-head[data-key]")) {
      const k = n.dataset.key;
      if (k === st.cur || !seen.has(k)) continue;
      const r = n.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const gap = dir === "down" ? r.top - a.bottom : dir === "up" ? a.top - r.bottom
                : dir === "right" ? r.left - a.right : a.left - r.right;
      if (gap < -2) continue;
      const off = vert ? Math.max(0, r.left - a.right, a.left - r.right)
                       : Math.max(0, r.top - a.bottom, a.top - r.bottom);
      const score = Math.max(0, gap) + off * 20
                  + (vert ? Math.abs(r.left - a.left) : Math.abs(r.top - a.top)) * 0.01;
      if (score < bestScore) { bestScore = score; best = k; }
    }
    if (best) st.cur = best;
    st.pick.clear();
    st.scroll = true;
    changed();
    return true;
  }

  // ⎋ for the list, innermost first: a name being typed, the open editor or add
  // row, the Find filter, the pick, the cursor. True means something was undone.
  function escape() {
    if (inlineLive()) { const f = K.inlineCancel; K.inlineCancel = null; K.inlineNode = null; f(); return true; }
    const st = K.active;
    if (!st) return false;
    if (st.open !== null || st.adding) { close(st); return true; }
    if (st.query !== null && !st.spec.find) { st.query = null; K.onChange(); return true; }
    if (K.cut) { K.cut = null; K.onChange(); return true; }
    if (st.pick.size) { st.pick.clear(); K.onChange(); return true; }
    if (st.cur) { st.cur = null; K.onChange(); return true; }
    // Last: the page's own search box, when it has text in it.
    if (st.spec.clearFind) return !!st.spec.clearFind();
    return false;
  }

  // ⏎ for the list: the open editor's primary button — THIS list's open editor,
  // not the first thing on the page that looks like one.
  function enter() {
    const st = K.active;
    // No editor open: ⏎ on the cursor row is F — nav-in (open it, or its enter()).
    if (st && st.spec && st.open === null && !st.adding && !st.renaming && st.cur
        && kindOf(st.cur) === "i"
        && !(document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)))
      return command("nav-in");
    if (!st || !st.editorNode || !st.editorNode.isConnected) return false;
    const btn = st.editorNode.querySelector(".actions button.primary");
    if (btn && !btn.disabled) btn.click();
    return true;
  }

  // Leaving a page: nothing open, typed or half-renamed follows you back.
  function leave() {
    const st = K.active;
    if (st) {
      st.open = null; st.draft = null; st.adding = false; st.renaming = null;
      st.query = null; st.pick.clear();
    }
    K.inlineCancel = null; K.inlineNode = null;
    K.notes = {};
    K.cut = null;
    K.active = null;
  }
  // Before every page draw: a page with no list must not answer the list keys.
  function reset() { K.active = null; K.findInput = null; K.drawn.clear(); }

  // Every group of every list the page drew since its last reset() — the menu tail's
  // Collapse All / Expand All. A page with folds of its own does those itself.
  function collapseAll(collapsed) {
    for (const st of K.drawn)
      for (const g of st.spec.groups || []) if (isCollapsed(st, g) !== collapsed) setCollapsed(st, g, collapsed);
    K.onChange();
  }

  return {
    config(o) { Object.assign(K, o); },
    state, draw, reset, leave,
    open, close, command, step, escape, enter, selection,
    inlineField, suggestField, showMenu, closeMenu, menuOpen: () => !!K.menu, trapTab, collapseAll,
    actions, say, note, setNote, noteCard, attempt, flash, pinNotes,
    get active() { return K.active; },
    inlineOpen: inlineLive, noSuggest,
    clearCut() { if (K.cut) { K.cut = null; K.onChange(); } },
  };
})();
