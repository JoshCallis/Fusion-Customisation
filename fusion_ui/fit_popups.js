// fit_popups.js — every menu, dropdown and flyout a page opens stays inside its window,
// and scrolls when it is taller than the window. Every page loads it (served by
// webwin.SHARED_ASSETS), so no page has to remember: a page places its popup as it
// likes, this nudges it back on screen and caps its height.
//
// Why it exists: the UI zoom (WindowShell.swift § UI zoom) makes every menu taller,
// and the ones that were sized for 100% ran off the bottom of the window with nothing
// to scroll — Control Room's section menu at 220% (2026-10-20).
//
// A popup is anything matching POPUPS that is showing. Checked after any DOM change
// and on resize (the zoom glide resizes the page every frame), once per frame.
(function () {
  "use strict";
  const POPUPS = [
    ".facetmenu", ".ctxmenu", "#navmenu",            // list.js menus, Control Room
    "#ctx", ".ctx-fly", "#dep-menu",                 // Fusion Browser
    "#pdrop",                                        // Max Browser
    ".suggestlist",                                  // ListKit.suggestField
    "[role=menu]", "[role=listbox]", "[data-popup]",
  ].join(", ");
  const EDGE = 4;                                    // px kept clear of the window's edges

  function fit(el) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    let r = el.getBoundingClientRect();
    if (!r.width && !r.height) return;
    const placed = cs.position === "absolute" || cs.position === "fixed";
    // Back inside, left/up first: a popup hung off the right or bottom edge whose page
    // didn't flip it. Only what the page positioned itself can be moved.
    if (placed) {
      const dx = r.right > innerWidth - EDGE ? Math.min(r.right - (innerWidth - EDGE), r.left - EDGE) : 0;
      const dy = r.top < EDGE ? r.top - EDGE : 0;
      if (dx > 0) el.style.left = (parseFloat(cs.left) - dx) + "px";
      if (dy < 0) el.style.top = (parseFloat(cs.top) - dy) + "px";
      if (dx > 0 || dy < 0) r = el.getBoundingClientRect();
    }
    // Taller than the room under its top: cap it there and let it scroll. The page's
    // own cap (a CSS max-height) wins when it is the smaller.
    const room = Math.floor(innerHeight - EDGE - Math.max(r.top, EDGE));
    const own = el.dataset.fitOwnMax !== undefined ? el.dataset.fitOwnMax
              : (el.dataset.fitOwnMax = cs.maxHeight === "none" ? "" : cs.maxHeight);
    const ownPx = own ? parseFloat(own) : Infinity;
    if (el.scrollHeight > room + 1 || el.dataset.fitCapped) {
      const cap = Math.max(60, Math.min(room, ownPx));
      if (el.style.maxHeight !== cap + "px") el.style.maxHeight = cap + "px";
      if (el.style.overflowY !== "auto") el.style.overflowY = "auto";
      el.dataset.fitCapped = "1";
    }
  }

  let queued = false;
  function check() {
    queued = false;
    for (const el of document.querySelectorAll(POPUPS)) fit(el);
  }
  function queue() {
    if (!queued) { queued = true; requestAnimationFrame(check); }
  }

  function start() {
    new MutationObserver(queue).observe(document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ["hidden", "class", "style", "open"],
    });
    addEventListener("resize", queue);
    queue();
  }
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();
