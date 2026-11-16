/* Enough of a DOM for the parameter editor's page script to load under jsc, so the
   REAL reconcileTree()/derivedGroupChildren() etc. can be exercised directly. Same
   shim fusion_value/test uses — the contract (auto-vivifying elements, everything a
   no-op) is generic enough that both windows' boot code runs cleanly against it. */
function __el(){
  return {
    value: "", className: "", innerHTML: "", textContent: "", placeholder: "",
    style: {}, dataset: {}, offsetTop: 0, offsetHeight: 0, scrollHeight: 20,
    selectionStart: 0, selectionEnd: 0, checked: false,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    parentNode: { insertBefore(){},
                  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } } },
    addEventListener(){}, removeEventListener(){},
    setSelectionRange(a, b){ this.selectionStart = a; this.selectionEnd = b; },
    focus(){}, blur(){},
    getBoundingClientRect(){ return { height: 100, width: 300, top: 0, left: 0 }; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    closest(){ return null; }, scrollIntoView(){}, appendChild(){}, remove(){}
  };
}
var __els = {};
var document = {
  getElementById(id){ return __els[id] || (__els[id] = __el()); },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  createElement(){ return __el(); },
  addEventListener(){}, removeEventListener(){},
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
var alert = function(msg){ print("ALERT: " + msg); };
