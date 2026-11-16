#!/usr/bin/env python3
"""
fusion_ui/webwin.py — the HTTP plumbing every companion-window server shares.

Each window's server.py (fusion_params 8731, fusion_browser 8732, fusion_shortcuts
8733, fusion_value 8734, fusion_timeline 8736) used to carry its own copy of: the send helper, the CORS
preflight, a dozen near-identical "serve this shared file, or a stub if it's gone"
branches, the /settings pair, an atomic writer and make_server/main. The copies
drifted — the Browser server never got the CORS preflight, so a cross-origin JSON
POST to it would vanish with nothing in any log, exactly the bug the other three
had each already hit and fixed separately. They live here once now; a server.py
owns only what is its own: its state, its queue, its routes.

    class Handler(webwin.Handler):
        HERE = HERE                          # index.{html,css,js} come from here
        def do_GET(self):
            if self.serve_static():          # index.*, every shared fusion_ui asset,
                return                       # /settings, /health
            route = self.route
            if route == "/state": ...
            else: self.not_found()

A window picking up another shared engine (planes.js, chips.js…) needs only the
<script> tag: every shared asset is served by every window.

LOADED BY PATH, FRESH EACH TIME. Servers run inside Fusion's one Python process,
imported by their add-ins via spec_from_file_location. A plain `import webwin`
would cache this module in sys.modules for the life of Fusion, so an edit here
would only arrive with a Fusion restart; load() instead gives each server.py its
own fresh copy, so reloading the add-in picks it up like any other server edit.
Nothing here holds state, which is what makes private copies safe. Use the
three-line bootstrap at the top of any server.py:

    _spec = importlib.util.spec_from_file_location(
        "fusion_webwin", os.path.join(FUSION_UI, "webwin.py"))
    webwin = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(webwin)
"""

import http.server, importlib.util, json, os, queue, re, socketserver, tempfile, time

FUSION_UI = os.path.dirname(os.path.abspath(__file__))

# Shared settings, one file for every window.
SETTINGS_FILE = os.path.join(FUSION_UI, "settings.json")

_JS = "application/javascript; charset=utf-8"
_JS_STUB = "application/javascript"


def _js(name):
    # A missing engine answers with a comment, not a 404 body that parses as nothing:
    # every page guards its calls behind `typeof <Engine>`, so a missing file costs
    # that one feature rather than killing the whole script.
    return (name, _JS, 404, "/* %s missing */" % name, _JS_STUB)


# route -> (file in fusion_ui/, content type, code if missing, body if missing,
#           content type if missing)
SHARED_ASSETS = {
    "/theme.css":          ("theme.css", "text/css; charset=utf-8", 404,
                            "/* theme.css missing */", "text/css"),
    # The general chords (Rename, Delete…) every window reads instead of spelling a
    # key of its own.
    "/general":            ("general.json", "application/json", 200,
                            '{"actions": []}', "application/json"),
    # Every word starts with a capital, in every window — a house rule.
    "/capitalise.js":      _js("capitalise.js"),
    # A finger count is drawn as a hand ("Two Fingers" → ✌-style icon), never spelled.
    "/fingers.js":         _js("fingers.js"),
    # The situational variable engine (naming.py is the add-ins' twin).
    "/naming.js":          _js("naming.js"),
    # The plane arithmetic for the sketch box (planes.py is the add-ins' twin).
    "/planes.js":          _js("planes.js"),
    # The chip editor: expressions typed as items rather than characters.
    "/chips.js":           _js("chips.js"),
    # A dictated sentence → an expression over the real parameter names.
    "/spoken_expr.js":     _js("spoken_expr.js"),
    # The list kit: rows, groups, cursor, pick, editor, rename, drag — one
    # behaviour for every list a window draws (UI.md is its rulebook).
    "/list.js":            _js("list.js"),
    # A flyout stays open while the pointer travels to it (list.js menus, the Browser's).
    "/menu_aim.js":        _js("menu_aim.js"),
    # Chord spelling and the ⌘, Hotkeys panel (Browser + Params).
    "/keys.js":            _js("keys.js"),
    # What a timeline item is called on screen (timeline rows, Browser feature rows).
    "/tl_names.js":        _js("tl_names.js"),
    # The glyph a timeline item draws and the kind colour it wears (same two places).
    "/tl_icons.js":        _js("tl_icons.js"),
    # The window-only timeline groups (groups.json) — the timeline's and the Browser's.
    "/tl_groups.js":       _js("tl_groups.js"),
    # The command-search palette (ranker, list, right-click sheet) — Fusion's and
    # Photoshop's are the same code; each page passes an adapter.
    "/search.js":          _js("search.js"),
    "/search.css":         ("search.css", "text/css; charset=utf-8", 404,
                            "/* search.css missing */", "text/css"),
    "/list.css":           ("list.css", "text/css; charset=utf-8", 404,
                            "/* list.css missing */", "text/css"),
    # The structured feature/type/anchor naming convention and its vocabulary.
    "/param_naming.js":    _js("param_naming.js"),
    "/param_naming.json":  ("param_naming.json", "application/json", 404,
                            json.dumps({"types": [], "anchors": [], "axes": []}),
                            "application/json"),
    # Words exempt from that convention (the stepper's "None" type), set in Control Room.
    "/param_exempt.json":  ("param_exempt.json", "application/json", 200,
                            '{"words": []}', "application/json"),
    # Fusion command id -> chord (the shared registry's keys), so a window can answer
    # the chord a Fusion command owns — FusionValue's stepper reads DeleteSelectionCmd's.
    "/fusion_bindings.json": ("bindings.json", "application/json", 200,
                              "{}", "application/json"),
    "/variables.json":     ("variables.json", "application/json", 404,
                            json.dumps({"dimensions": []}), "application/json"),
    # The extras flag (Extra Dark / Extra Light — macOS's own appearance plus this
    # file, set by contrast_theme/toggle_appearance.sh) and the script every page
    # loads in <head> to follow it. Not appearance.json: that one is the Appearance
    # Toggle add-in's.
    "/extra.json":         ("extra.json", "application/json", 200,
                            '{"on": false}', "application/json"),
    "/appearance.js":      _js("appearance.js"),
    # Super Dark's compensation (appearance.js § Super Dark compensation): the red
    # filter's colour + intensity and how hard to lift what it darkens — written by
    # Control Room (remap/server.py) whenever config.json "redfilter" changes.
    "/sdark_comp.json":    ("sdark_comp.json", "application/json", 200,
                            '{"color": "#ff0000", "intensity": 100, "compensate": 100}',
                            "application/json"),
    # The Fusion Browser's selection-highlight lift, one value per appearance —
    # set in Control Room ▸ Light / Dark — and the script that mirrors it onto
    # the window (browser_sel.js, load after appearance.js).
    "/browser_sel.json":   ("browser_sel.json", "application/json", 200,
                            json.dumps({"light": 0, "dark": 0, "xdark": 0, "xlight": 0}),
                            "application/json"),
    "/browser_sel.js":     _js("browser_sel.js"),
    # Menus and dropdowns stay inside the window and scroll when taller (the UI zoom
    # makes every one taller). Every page loads it after appearance.js.
    "/fit_popups.js":      _js("fit_popups.js"),
}

# route -> (file in the window's own dir, content type, code if missing, body, type)
WINDOW_ASSETS = {
    "/":           ("index.html", "text/html; charset=utf-8", 500,
                    "index.html missing", "text/plain"),
    "/index.html": ("index.html", "text/html; charset=utf-8", 500,
                    "index.html missing", "text/plain"),
    "/index.css":  ("index.css", "text/css; charset=utf-8", 404,
                    "/* index.css missing */", "text/css"),
    "/index.js":   ("index.js", _JS, 404, "/* index.js missing */", _JS_STUB),
}


def shared(name):
    """Absolute path of a file in fusion_ui/."""
    return os.path.join(FUSION_UI, name)


def load(name):
    """fusion_ui/<name>.py as a fresh private module (see the header for why)."""
    spec = importlib.util.spec_from_file_location("fusion_ui_" + name, shared(name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def atomic_write(path, text):
    """Write via a temp file + rename, so a reader never sees half a file and a crash
    mid-write never leaves one behind."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def drain(q):
    """Everything waiting in queue `q`, removed, oldest first — what every add-in's
    main-thread tick takes from its server's command queue (pop_commands)."""
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def page_stamp(here, extra=()):
    """Newest mtime across everything a window's page is built from, or 0 if unreadable.

    The WKWebView reads its sources once, at launch, so a page that wants to notice it
    has gone stale is told this and reloads when it moves. "Everything" is the window's
    own index.{html,css,js} plus every shared fusion_ui/ asset its index.html loads —
    read off the page's own <script src>/<link href> tags, so a page that picks up a new
    engine is covered without anyone remembering to add it here — plus `extra` (shared
    files the page fetches rather than loads, e.g. variables.json).

    Unreadable means DO NOT reload: a stamp of 0 never differs from itself, so a
    transient stat failure cannot throw away a window mid-use.
    """
    html = os.path.join(here, "index.html")
    try:
        with open(html, encoding="utf-8") as f:
            refs = re.findall(r'(?:src|href)="(/[^"]+)"', f.read())
    except OSError:
        return 0
    paths = [html] + [os.path.join(here, n) for n in ("index.css", "index.js")]
    paths += [shared(SHARED_ASSETS[r][0]) for r in refs if r in SHARED_ASSETS]
    paths += [shared(n) for n in extra]
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            return 0
    return int(newest)


def seq_base():
    """Where a server's edge-detect counters (`seq`, `uiSeq`…) start: wall-clock ms.

    A window process outlives its server — Fusion restarts, the add-in reloads, and
    the already-running window keeps its last-seen counter. Starting a new server at 0
    left that window waiting for `seq > lastSeq` while the new count climbed from
    nothing, so every session it armed went unrendered and the window, still polling,
    passed every liveness check: Command Search and Extrude "stopped working" after
    any Fusion restart, until enough sessions had been opened to overtake the old
    count. Seeded from the clock, a restarted server's counter is already past
    anything an older one reached (one bump per ms would be needed to catch up), and
    it stays exact in a JS number."""
    return int(time.time() * 1000)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Handler(http.server.BaseHTTPRequestHandler):
    HERE = ""        # the window's own dir; set by the subclass

    def log_message(self, *args):
        pass

    @property
    def route(self):
        """The path without its query string."""
        return self.path.split("?", 1)[0]

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200, **dumps_kw):
        self._send(code, json.dumps(obj, **dumps_kw))

    def ok(self, **extra):
        self.send_json(dict(ok=True, **extra))

    def fail(self, code, error):
        self.send_json({"ok": False, "error": str(error)}, code)

    def not_found(self):
        self._send(404, b"not found", "text/plain")

    def _send_file(self, path, spec):
        _name, ctype, miss_code, miss_body, miss_type = spec
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(miss_code, miss_body, miss_type)

    def serve_static(self):
        """Answer the GETs every window shares; True if this was one of them."""
        route = self.route
        if route in WINDOW_ASSETS:
            spec = WINDOW_ASSETS[route]
            self._send_file(os.path.join(self.HERE, spec[0]), spec)
        elif route in SHARED_ASSETS:
            spec = SHARED_ASSETS[route]
            self._send_file(shared(spec[0]), spec)
        elif route == "/settings":
            try:
                with open(SETTINGS_FILE) as f:
                    self._send(200, f.read())
            except OSError:
                self.send_json({})
        elif route == "/health":
            self.send_json({"ok": True})
        else:
            return False
        return True

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def read_json_body(self):
        """The POSTed JSON object ({} for no body), or None after answering 400."""
        body = self.read_body()
        try:
            return json.loads(body.decode("utf-8")) if body else {}
        except ValueError as e:
            self.fail(400, e)
            return None

    def serve_shared_post(self, data):
        """Answer the POSTs every window shares; True if this was one of them."""
        if self.route == "/settings":
            try:
                atomic_write(SETTINGS_FILE, json.dumps(data, indent=2))
                self.ok()
            except OSError as e:
                self.fail(500, e)
            return True
        return False

    def do_OPTIONS(self):
        """CORS preflight.

        The windows talk to each other's servers — the Browser page posts parameter
        edits to Params (8731) and Value (8734), Value's command-search palette posts
        /run to Shortcuts (8733). A POST carrying Content-Type: application/json is
        not a "simple" request, so WebKit asks permission first; without an answer
        here the preflight gets BaseHTTPRequestHandler's 501, which carries no CORS
        headers, and the real request is never sent: the call simply never arrives,
        with nothing in any log to say so.
        """
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server(socketserver.ThreadingTCPServer):
    """Built to be stopped fast, because add-ins stop it on Fusion's main thread
    (every Reload Recent AddIns). Request threads are daemon and NOT joined on
    close — ThreadingMixIn's default makes server_close() wait for every in-flight
    request, and a page's /wait long-poll sits for up to 20s (that was a 6s+
    beachball per reload). And serve_forever polls at 50ms rather than 0.5s, which
    is what shutdown() waits out."""
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def serve_forever(self, poll_interval=0.05):
        super().serve_forever(poll_interval)


def make_server(port, handler):
    """Ready-to-serve threading server on 127.0.0.1. Raises OSError if the port is
    taken (another instance is already serving)."""
    return _Server(("127.0.0.1", port), handler)


def main(title, make, port):
    """Standalone run (`python3 <window>/server.py`) — serves the UI for styling
    work with no Fusion attached."""
    try:
        httpd = make()
    except OSError as e:
        print("%s: could not bind port %d (%s)." % (title, port, e))
        return
    print("%s running at http://localhost:%d" % (title, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
