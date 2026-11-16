// MenuAim — an open flyout stays open while the pointer is on its way to it.
//
// The pointer heading from a "▸" item to its flyout cuts across the items below or
// above it. Those items must not take over (shut the flyout, open their own) while
// the pointer is still travelling toward the flyout — only once it stops on one, or
// moves off the course. Leaving the menu altogether shuts nothing: like a macOS menu,
// the flyout stays until another item is settled on, a click, or ⎋.
//
//   MenuAim.hover(item, act, fly)  on entering a menu item: `act` runs now, or — while
//                                  the pointer is aiming at `fly` (an element, or a
//                                  function returning the open flyout) — once it
//                                  settles on `item`. Never if it leaves `item` first.
//   MenuAim.cancel()               the pointer reached the flyout: drop what was waiting.
//
// Used by the list kit's menus (list.js) and the Browser's own (fusion_browser).
(function () {
  const pts = [];             // the last few pointer moves: [x, y, ms]
  let timer = null, over = null;
  document.addEventListener("mousemove", e => {
    pts.push([e.clientX, e.clientY, performance.now()]);
    if (pts.length > 8) pts.shift();
  }, true);

  // Is the pointer moving into `fly`? Inside the triangle from where it was ~150 ms ago
  // to the flyout's near edge (a little taller than the flyout, to forgive a wobble).
  function aiming(fly) {
    if (!fly || !fly.isConnected) return false;
    const now = performance.now();
    const recent = pts.filter(p => now - p[2] < 150);
    if (recent.length < 2) return false;                 // stopped
    const [x, y] = recent[recent.length - 1], [x0, y0] = recent[0];
    const r = fly.getBoundingClientRect();
    const ex = x0 <= r.left ? r.left : x0 >= r.right ? r.right : null;
    if (ex === null) return false;                       // started over it: not travelling to it
    if (Math.abs(ex - x) >= Math.abs(ex - x0)) return false;   // not getting closer
    const t = (x - x0) / (ex - x0), slop = 24;
    return y >= y0 + (r.top - slop - y0) * t - 2 && y <= y0 + (r.bottom + slop - y0) * t + 2;
  }

  function hover(item, act, fly) {
    over = item;
    clearTimeout(timer); timer = null;
    const go = () => {
      timer = null;
      if (over !== item || !item.isConnected || !item.matches(":hover")) return;
      if (aiming(typeof fly === "function" ? fly() : fly)) { timer = setTimeout(go, 50); return; }
      act();
    };
    go();
  }
  function cancel() { over = null; clearTimeout(timer); timer = null; }

  window.MenuAim = { hover, cancel, aiming };
})();
