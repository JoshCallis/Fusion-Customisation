/* Enough of a DOM for the value window's page script to load under jsc, so the REAL
   entries()/score()/updateAC() can be exercised against real parameters. */
function __el(){
  return {
    value: "", className: "", innerHTML: "", textContent: "", placeholder: "",
    style: {}, dataset: {}, offsetTop: 0, offsetHeight: 0, scrollHeight: 20,
    selectionStart: 0, selectionEnd: 0, checked: false,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    // Enough of a parent for Chips.attach to succeed (it needs somewhere to insert its
    // paint layer) — see attach()'s own parentNode check in chips.js. Without this every
    // chipsAttach() call in index.js silently no-ops under the harness, which is fine for
    // the pure Chips.* functions cases.js exercises directly but would leave the STATEFUL
    // half (editAll, caretAtEnd, refreshAll — anything keyed on stateOf(el)) untestable.
    parentNode: { insertBefore(){},
                  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } } },
    addEventListener(){}, removeEventListener(){},
    setSelectionRange(a, b){ this.selectionStart = a; this.selectionEnd = b; },
    focus(){}, blur(){},
    getBoundingClientRect(){ return { height: 100, width: 300, top: 0, left: 0 }; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    closest(){ return null; }, scrollIntoView(){}, remove(){},
    appendChild(c){ if (c) c.parentNode = this; },
    insertBefore(c){ if (c) c.parentNode = this; }
  };
}
var __els = {};
var document = {
  getElementById(id){ return __els[id] || (__els[id] = __el()); },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  createElement(){ return __el(); },
  // Kept, so a test can fire the document-level events a field reacts to
  // (chips.js's selectionchange) the way the browser would after each keystroke.
  __listeners: {},
  addEventListener(type, fn){ (this.__listeners[type] = this.__listeners[type] || []).push(fn); },
  removeEventListener(){},
  activeElement: null,
  scrollingElement: { scrollTop: 0 },
  body: __el()
};
var window = {
  addEventListener(){}, removeEventListener(){},
  webkit: undefined, location: { href: "" }
};
var fetch = function(){ return Promise.reject(new Error("no network in the harness")); };
var setInterval = function(){ return 0; };
var setTimeout = function(){ return 0; };
var clearTimeout = function(){};
var requestAnimationFrame = function(){ return 0; };
var console = { log: print, warn: print, error: print };
var location = { href: "", reload: function(){} };
