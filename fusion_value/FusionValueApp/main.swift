// FusionValue — the dimension value / new parameter dialog window.
//
// Third of the Fusion companion windows (Params 8731, Browser 8732, this 8734 —
// 8733 is Fusion's own). Unlike those two it is a TRANSIENT dialog: the process runs
// hidden from the moment the add-in starts it, and the window only appears when the
// add-in arms a session — under the cursor for a dimension (which is on the canvas
// right there), centred on screen for a new parameter (which is nowhere). That is why
// it is launched once and kept alive rather than spawned per use — `open -a` costs a
// few hundred ms, which is very visible on something that stands in for an inline box.
//
// LSUIElement (see build.sh): no Dock tile, no app switcher entry. It still activates
// while the dialog is up, because WKWebView will not take keyboard input while its app
// is inactive — non-activating panels and acceptsFirstMouse do not get around that
// (see the note in FusionBrowser's main.swift). Focus is handed straight back to Fusion
// the moment the dialog closes, so the round trip is invisible.

import Cocoa
import WebKit

let PORT = 8734
let URL_STR = "http://127.0.0.1:\(PORT)"

// Wide enough to be the point of the exercise (Fusion's own box is ~90pt), tall enough
// that the suggestion list drops below the panel without the native window resizing
// while you type. The area below the panel is transparent.
//
// WIDTH IS NOT FIXED any more. The page asks for one — by the width of what is in the
// box, and by the grip on the panel's right edge — and this is only where it starts.
// It is a var rather than a constant because every frame below is computed from it.
let PANEL_W_DEFAULT: CGFloat = 400
// Floors setPanelWidth and the session-open width. Below the value box's own comfort —
// the command-search palette is what actually needs to get this low, at ~240pt.
let PANEL_W_MIN: CGFloat = 200
var PANEL_W: CGFloat = PANEL_W_DEFAULT

// The transparent margin the page holds around its panel so the panel's own drop shadow
// has somewhere to fall. The window is borderless and the page paints everything, so a
// panel drawn flush to the window edge had its shadow clipped square down both sides —
// the shadow was there below the panel and simply stopped at the left and right edges.
// The window is therefore wider than the panel by this much on each side, and every
// frame below places the PANEL, not the window.
//
// It must match --panel-gutter in fusion_ui/theme.css. Two numbers rather than one
// because they live on opposite sides of a WKWebView: the page cannot size its window
// and this cannot read the page's CSS.
var GUTTER: CGFloat { 26 * uiZoom }   // CSS px × UI zoom (WindowShell § UI zoom)

var WIN_W: CGFloat { PANEL_W + GUTTER * 2 }
// The room below the panel is where the suggestion list drops; the gutter is added on
// top of it rather than taken out of it, so that room is what it always was.
var WIN_H: CGFloat { 400 * uiZoom + GUTTER }

// Cursor offset: down and to the right, the direction a menu opens, so the panel never
// covers the dimension you are looking at.
let CURSOR_DX: CGFloat = 14
let CURSOR_DY: CGFloat = 14

// Clearance from the top of the screen for a top-middle-placed panel — clear of the
// menu bar and traffic lights without looking stuck to the edge.
let TOP_GAP: CGFloat = 40

// The command-search palette's clamp reserve: at open its panel is just the empty
// query row (~40pt), but #sresults grows to 6 rows of --row-h (30px) the moment you
// type — space frameUnderCursor's Y-clamp never sees, since it clamps on the height
// AT OPEN. A low cursor then anchors that few-pt-tall empty panel just above the
// screen's bottom edge, and the results that show up a keystroke later render into
// the "space below" the clamp deliberately treats as clippable — off screen, looking
// like the hotkey did nothing. Six rows (180pt) + the query row's own height, rounded
// up, so the clamp reserves room for the palette actually full rather than empty.
var SEARCH_RESERVE_H: CGFloat { 240 * uiZoom }

let FUSION_BUNDLE_PREFIXES = ["com.autodesk.fusion", "com.autodesk.mas.fusion"]

// Borderless windows can't become key by default, which would leave both text fields
// unable to receive a single keystroke.
class ValueWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                   WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: ValueWindow!
    var webView: WKWebView!
    // Set while we are deliberately giving focus back to Fusion.
    var closing = false
    // Drag state: the cursor and window origin at the moment the press landed, plus the
    // timer that slides the window to follow. See beginDrag.
    var dragTimer: Timer?
    var hoverTimer: Timer?
    // Scroll/pinch gesture state, for reclaimIfLost's "wait until it's over". A phased
    // (trackpad) gesture is open from its first event until its phase — or its
    // momentum — ends; an unphased one (a mouse wheel) is over after a short quiet.
    var lastScroll: TimeInterval = 0
    var scrollOpen = false
    var dragMouseStart: NSPoint = .zero
    var dragWinStart: NSPoint = .zero
    // Set while a "top"/"center"-placed panel hasn't been dragged yet, so a width change
    // (typing, the resize grip) keeps it centred instead of holding the left edge. A drag
    // means the user picked where it sits, so beginDrag clears it.
    var centredMidX: CGFloat?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installEditMenu()

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // The window spends almost all its life ordered out, and WebKit throttles a
        // hidden page's timers (and App-Naps its content process) — so the page's 60ms
        // /state poll really ran every ~1s, and the palette/dialog appeared 0.6-1s after
        // the key. The poll IS the open path, so it must run at full rate while hidden.
        // Private WKPreferences setters, reached via KVC's `_set<Key>:` lookup; guarded
        // so a WebKit that drops them just falls back to the throttled behaviour.
        for key in ["hiddenPageDOMTimerThrottlingEnabled",
                    "pageVisibilityBasedProcessSuppressionEnabled"]
        where config.preferences.responds(to: NSSelectorFromString("_set" + key.prefix(1).uppercased() + key.dropFirst() + ":")) {
            config.preferences.setValue(false, forKey: key)
        }
        config.userContentController.add(self, name: "session")
        config.userContentController.add(self, name: "drag")
        // How wide the page wants to be. See setPanelWidth.
        config.userContentController.add(self, name: "size")
        // Where the panel is inside the window; see updateClickThrough.
        config.userContentController.add(self, name: "rect")

        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: WIN_W, height: WIN_H),
                            configuration: config)
        // PANEL_W is points the page asked for at the old zoom; carry it to the new one.
        var zoomWas = uiZoom
        followUIZoom({ [unowned self] in [self.webView] }, changed: { [unowned self] in
            PANEL_W = min(max(PANEL_W / zoomWas * uiZoom, PANEL_W_MIN), 2000)
            zoomWas = uiZoom
            guard self.window.isVisible else { return }
            var f = self.window.frame
            f.origin.y += f.height - WIN_H
            f.size = CGSize(width: WIN_W, height: WIN_H)
            self.window.setFrame(f, display: true)
        })
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        // The same three as the dock's panes, and for the same reason: drawsBackground
        // alone is what makes the page transparent TODAY, but WebKit also paints an
        // "under page" colour behind the content and the view has a layer of its own,
        // and either one going opaque would turn the panel's translucency (and the
        // empty space around it) into a solid rectangle. Stating all three means the
        // window cannot quietly stop compositing across an OS update.
        if #available(macOS 12.0, *) { webView.underPageBackgroundColor = .clear }
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.clear.cgColor

        window = ValueWindow(contentRect: NSRect(x: 0, y: 0, width: WIN_W, height: WIN_H),
                             styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.delegate = self
        // The appearance is deliberately NOT pinned: setting window.appearance also pins
        // the WKWebView's prefers-color-scheme, and the page's palette follows the
        // system through that (fusion_ui/theme.css, both appearances black).
        window.isOpaque = false
        window.backgroundColor = .clear
        // The page draws its own rounded shadow; a native one would trace the full
        // rectangle including the transparent space under the panel.
        window.hasShadow = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]

        // Focus-follows-mouse, same as FusionBrowser and FusionDock. This app is an
        // accessory (.accessory policy), and clicking the floating window of an accessory
        // app does not reliably activate it — so WKWebView, which refuses input while its
        // app is inactive, never sees the click. Without this, moving the cursor onto the
        // dialog after touching the viewport left it dead to both clicks and keys: you
        // could neither drag it nor resume typing. Polling the cursor position touches
        // nothing in the view tree and needs no permission.
        // The fallback. The event hooks below are what make it immediate; this catches
        // anything they miss (and the moment a held button or a scroll finally ends).
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.presenceTick += 1
            if self.presenceTick % 5 == 0 { self.syncFusionPresence() }
            self.reclaimIfLost()
            self.updateClickThrough()
        }

        // Every click release, anywhere — Fusion's viewport and UI included. Monitoring
        // mouse events globally needs no permission. These make the reclaim land on the
        // release itself rather than up to a timer tick later, and catch a click so short
        // (under the old 80ms poll) that the press was never seen at all — which is why
        // the dialog only "sometimes" came back after a click.
        NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseUp, .rightMouseUp, .otherMouseUp]) { [weak self] _ in
            DispatchQueue.main.async { self?.reclaimIfLost() }
        }
        // A left/right PRESS anywhere outside the panel — the page decides whether that
        // dismisses (only the command-search palette does; see __clickOutside in
        // index.js). Middle presses are reanchor's orbit/pan and never count. A global
        // monitor sees only presses that went to other apps, which includes our own
        // click-through area; the local one catches a press on the transparent margin
        // while the window still takes mouse events.
        NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            DispatchQueue.main.async { self?.clickedOutside() }
        }
        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] e in
            guard let self = self, let w = self.window, e.window === w,
                  let r = self.panelRect else { return e }
            let p = e.locationInWindow
            if !r.insetBy(dx: -4, dy: -4).contains(NSPoint(x: p.x, y: w.frame.height - p.y)) {
                DispatchQueue.main.async { self.clickedOutside() }
            }
            return e
        }
        // A click on the panel while this app is inactive. WKWebView never hands that
        // mousedown to the page (see FusionBrowser's main.swift), so a click from the
        // viewport straight into a field was lost and the reclaim put the caret back in
        // the field it was guarding — "Distance, other side" jumping back to Distance.
        // The app still gets the event, so its point goes to the page, which focuses the
        // text field there (__clickAt in index.js).
        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown]) { [weak self] e in
            guard let self = self, let w = self.window, e.window === w, w.isVisible,
                  !(NSApp.isActive && w.isKeyWindow) else { return e }
            let p = self.webView.convert(e.locationInWindow, from: nil)
            let x = p.x
            let y = self.webView.isFlipped ? p.y : self.webView.bounds.height - p.y
            DispatchQueue.main.async {
                NSApp.activate(ignoringOtherApps: true)
                w.makeKeyAndOrderFront(nil)
                w.makeFirstResponder(self.webView)
                self.webView.evaluateJavaScript("window.__clickAt && window.__clickAt(\(x / uiZoom), \(y / uiZoom))")
            }
            return e
        }
        // Scrolls take focus WITHOUT a click: reanchor activates Fusion 70ms into a
        // 2-finger scroll over the canvas so that it zooms. No button is ever down, so
        // a release-based rule never came back from a zoom. Tracked here so the reclaim
        // waits for the gesture (and its momentum) to finish rather than yanking focus
        // mid-zoom.
        NSEvent.addGlobalMonitorForEvents(matching: [.scrollWheel, .magnify, .rotate]) { [weak self] e in
            self?.noteScroll(e)
        }
        // Anything becoming frontmost by other means — reanchor's post-orbit activation
        // (it defers focusing Fusion until the fingers LIFT, i.e. after the synthetic
        // middle-button release, so the release check alone always ran too early), a
        // keyboard-driven activation, a Fusion window raising itself.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] _ in
            self?.reclaimIfLost()
            // Fusion sometimes activates a window of its own a beat after the app itself.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { self?.reclaimIfLost() }
        }

        // Load now, stay hidden. By the time the first hotkey lands the page is up and
        // polling, so the dialog appears in one frame.
        tryLoad()
    }

    // ── Show / hide ──────────────────────────────────────────────────────────

    /// Frame for a panel of `height` points hung below-right of the cursor, nudged back
    /// onto the screen it is on rather than allowed to run off an edge.
    func frameUnderCursor(panelHeight: CGFloat, dx: CGFloat = CURSOR_DX, dy: CGFloat = CURSOR_DY) -> NSRect {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(p) })
            ?? NSScreen.main
        let vis = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

        // GUTTER comes off both: the panel's top-left is what goes under the cursor,
        // and the panel sits that far inside the window on each side.
        var x = p.x + dx - GUTTER
        // Cocoa's origin is bottom-left: the panel's TOP goes just under the cursor.
        var y = p.y - dy - WIN_H + GUTTER

        if x + GUTTER + PANEL_W > vis.maxX {                      // flip to the left
            x = p.x - dx - PANEL_W - GUTTER
        }
        if x + GUTTER < vis.minX { x = vis.minX - GUTTER + 4 }
        // Only the panel itself has to stay on screen — the empty space below it is
        // where the suggestion list goes, and clipping that is better than shoving the
        // panel far away from the cursor it is meant to sit under.
        if y + WIN_H - GUTTER - panelHeight < vis.minY {
            y = vis.minY - WIN_H + GUTTER + panelHeight + 4
        }
        if y + WIN_H - GUTTER > vis.maxY { y = vis.maxY - WIN_H + GUTTER }
        return NSRect(x: x, y: y, width: WIN_W, height: WIN_H)
    }

    /// Frame for a panel of `height` points centred on the screen the cursor is on.
    /// Used by the new-parameter session, which has no dimension on the canvas to sit
    /// beside — so the cursor says nothing about where the dialog belongs and the middle
    /// of the screen is the one place it is always findable.
    func frameCentered(panelHeight: CGFloat) -> NSRect {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(p) })
            ?? NSScreen.main
        let vis = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        // The PANEL is what gets centred, not the window: the window is mostly empty
        // space held below the panel for the suggestion list, and centring that would
        // push the panel itself well above the middle.
        let x = vis.midX - PANEL_W / 2 - GUTTER
        let y = vis.midY + panelHeight / 2 - WIN_H + GUTTER
        return NSRect(x: x, y: max(y, vis.minY - WIN_H + GUTTER + panelHeight + 4),
                      width: WIN_W, height: WIN_H)
    }

    /// Frame for a panel of `height` points, centred horizontally on the cursor's screen
    /// and pinned near the top of it — for a command dialog (Extrude, Array, Mirror, the
    /// plane box) that has no single dimension on the canvas to sit beside but still
    /// belongs somewhere predictable rather than dead centre.
    func frameTop(panelHeight: CGFloat) -> NSRect {
        let p = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { $0.frame.contains(p) })
            ?? NSScreen.main
        let vis = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let x = vis.midX - PANEL_W / 2 - GUTTER
        let y = vis.maxY - TOP_GAP - WIN_H + GUTTER
        return NSRect(x: x, y: max(y, vis.minY - WIN_H + GUTTER + panelHeight + 4),
                      width: WIN_W, height: WIN_H)
    }

    /// `keep` is a session being re-armed while it is already on screen — the extrude
    /// panel gets one every time something is picked in the viewport, because a pick is
    /// the one input the panel does not own. Nothing moves for those, and nothing is
    /// activated either: the window is already up, and grabbing the keyboard back in the
    /// middle of picking would fight the very click that caused this. The height needs no
    /// frame change either way — the window is a fixed rectangle with the panel drawn at
    /// its top, so a taller panel grows into space that is already there.
    func showDialog(panelHeight: CGFloat, place: String) {
        closing = false
        // Nothing to keep if it is not up yet — that is a first open wearing the wrong
        // word, and it still needs placing.
        if place == "keep" && window.isVisible { return }
        // "corner": the panel's top-left lands exactly on the pointer, no offset either
        // way — used by the command-search palette, which opens wherever you were
        // about to type rather than beside a dimension. GUTTER still comes off both
        // axes (see frameUnderCursor's comment) — that is what makes "exactly" true.
        let frame: NSRect
        switch place {
        case "center": frame = frameCentered(panelHeight: panelHeight)
        case "top":     frame = frameTop(panelHeight: panelHeight)
        case "corner":  frame = frameUnderCursor(panelHeight: max(panelHeight, SEARCH_RESERVE_H),
                                                  dx: 0, dy: 0)
        default:        frame = frameUnderCursor(panelHeight: panelHeight)
        }
        if place == "center" || place == "top" {
            centredMidX = frame.midX
        } else {
            centredMidX = nil
        }
        window.setFrame(frame, display: true)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(webView)
        // Activating can land the caret back at 0 — the page restores the one it opened with.
        webView.evaluateJavaScript("window.__shown && window.__shown()")
    }

    func hideDialog() {
        guard window.isVisible else { return }
        endDrag()
        closing = true
        hiddenForSlot = false
        window.alphaValue = 1
        window.ignoresMouseEvents = false
        window.orderOut(nil)
        activateFusion()
        // Cleared on the next runloop hop, once the resignKey that our own handover
        // provokes has already been and gone.
        DispatchQueue.main.async { [weak self] in self?.closing = false }
    }

    // ── Keeping the keyboard ─────────────────────────────────────────────────
    // While the dialog is up it owns the keyboard: whatever took it — a click or a pick
    // in the viewport, an orbit, a pan, a zoom, a click in Fusion's own UI or in a
    // companion window — it comes straight back the moment that is over. LEVEL-triggered
    // (is it lost right now, and is nothing in progress?) rather than edge-triggered on a
    // button release, because the ways focus leaves don't all have a release: a scroll-
    // zoom has none, and reanchor's orbit activates Fusion only after its release.
    //
    // Only from Fusion or one of its companions. Switching to another app on purpose
    // (⌘-Tab, a click on a different app's window) is left alone.
    func reclaimIfLost() {
        guard let w = window, w.isVisible, !closing, !hiddenForSlot else { return }
        if NSApp.isActive && w.isKeyWindow { return }
        // Something is still happening — the click, drag, orbit or zoom that took focus.
        // Reclaiming now would cut it off; the release/quiet re-runs this.
        guard NSEvent.pressedMouseButtons == 0, !scrollBusy() else { return }
        guard frontmostIsFusionFamily() else { return }
        // A menu open over us closes the instant we activate (see WindowShell.swift).
        guard !windowState().menuOpen else { return }
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
        w.makeFirstResponder(webView)
        webView.evaluateJavaScript("window.__refocus && window.__refocus()")
    }

    // The window is a fixed rectangle with the panel drawn in its top; the rest is
    // transparent. Clicks and scrolls there must reach whatever is underneath — above
    // all Fusion's canvas, where the filter's 2-finger zoom has to see a canvas and not
    // this window's web view. So mouse events are ignored whenever the pointer is off
    // the panel (top-left origin, in view points, as the page reports it).
    // The dialog is floating-level, so slots' sub-layer shuffling never buries it: a
    // dialog left open would follow you to another slot. It belongs to Fusion, so it is
    // shown only while Fusion's own window is what is on top at its centre; otherwise
    // it is faded out (and click-through) until Fusion is back.
    var hiddenForSlot = false
    var presenceTick = 0
    func syncFusionPresence() {
        guard let w = window, w.isVisible else { hiddenForSlot = false; return }
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                    kCGNullWindowID) as? [[String: AnyObject]] else { return }
        let fusion: Set<String> = ["Fusion", "Autodesk Fusion"]
        var canvas: CGRect?
        for info in list {
            guard (info[kCGWindowLayer as String] as? Int) == 0,
                  let owner = info[kCGWindowOwnerName as String] as? String, fusion.contains(owner),
                  let b = info[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: b as CFDictionary),
                  r.width > 600, r.height > 400 else { continue }
            canvas = r
            break
        }
        var onTop = false
        if let c = canvas {
            let pt = CGPoint(x: c.midX, y: c.midY)
            for info in list {
                guard (info[kCGWindowLayer as String] as? Int) == 0,
                      let b = info[kCGWindowBounds as String] as? NSDictionary,
                      let r = CGRect(dictionaryRepresentation: b as CFDictionary),
                      r.contains(pt) else { continue }
                onTop = fusion.contains((info[kCGWindowOwnerName as String] as? String) ?? "")
                break
            }
        } else {
            onTop = true            // no Fusion canvas to judge by (closed, or an unusual layout): leave it be
        }
        hiddenForSlot = !onTop
        w.alphaValue = onTop ? 1 : 0
        if !onTop && !w.ignoresMouseEvents { w.ignoresMouseEvents = true }
    }

    var panelRect: NSRect?
    func updateClickThrough() {
        guard let w = window else { return }
        guard w.isVisible, !hiddenForSlot, let r = panelRect else {
            if w.ignoresMouseEvents { w.ignoresMouseEvents = false }
            return
        }
        // Never flip mid-click or mid-drag: the press has to land where it began.
        guard NSEvent.pressedMouseButtons == 0, dragTimer == nil else { return }
        let p = NSEvent.mouseLocation, f = w.frame
        let pad: CGFloat = 4
        let inside = r.insetBy(dx: -pad, dy: -pad)
            .contains(NSPoint(x: p.x - f.minX, y: f.maxY - p.y))
        if w.ignoresMouseEvents == inside { w.ignoresMouseEvents = !inside }
    }

    func clickedOutside() {
        guard let w = window, w.isVisible, !hiddenForSlot else { return }
        webView.evaluateJavaScript("window.__clickOutside && window.__clickOutside()")
    }

    func frontmostIsFusionFamily() -> Bool {
        guard let app = NSWorkspace.shared.frontmostApplication else { return true }
        let bid = (app.bundleIdentifier ?? "").lowercased()
        let name = (app.localizedName ?? "").lowercased()
        return FUSION_BUNDLE_PREFIXES.contains(where: { bid.hasPrefix($0) })
            || bid.hasPrefix("com.josh.fusion") || name.contains("fusion")
    }

    func noteScroll(_ e: NSEvent) {
        lastScroll = ProcessInfo.processInfo.systemUptime
        let ph = e.phase, mo = e.momentumPhase
        if ph.isEmpty && mo.isEmpty {
            scrollOpen = false                                  // a wheel: quiet-timed only
        } else if mo.contains(.ended) || mo.contains(.cancelled) ||
                  ph.contains(.ended) || ph.contains(.cancelled) {
            scrollOpen = false        // momentum may still follow; the quiet window covers the gap
        } else {
            scrollOpen = true
        }
        if !scrollOpen {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.27) { [weak self] in self?.reclaimIfLost() }
        }
    }

    func scrollBusy() -> Bool {
        let dt = ProcessInfo.processInfo.systemUptime - lastScroll
        // An open gesture with fingers resting still sends nothing; 3s bounds a lost end.
        return scrollOpen ? dt < 3.0 : dt < 0.25
    }

    /// Hand activation back to Fusion (same matching rule the dock uses).
    /// Fusion itself, by bundle id. NOT "any app whose name says fusion": the first such
    /// in runningApplications is often `fusion_busy` (a windowless helper) or one of our
    /// own windows, and activating that left the keyboard with nobody — the next key
    /// went nowhere until a second click in the viewport.
    func activateFusion() {
        let apps = NSWorkspace.shared.runningApplications
        let fusion = apps.first(where: { app in
            let bid = (app.bundleIdentifier ?? "").lowercased()
            return FUSION_BUNDLE_PREFIXES.contains(where: { bid.hasPrefix($0) })
        }) ?? apps.first(where: { app in
            app.activationPolicy == .regular
                && (app.localizedName ?? "").lowercased().contains("fusion")
                && app.processIdentifier != ProcessInfo.processInfo.processIdentifier
        })
        fusion?.activate(options: [])
    }

    // ── Dragging ─────────────────────────────────────────────────────────────
    // Borderless window + WKWebView: the web content consumes the events AppKit would
    // need to move the window by its background, so the page tells us when the button
    // is down and we follow the cursor ourselves until it comes up.
    // ── Width ────────────────────────────────────────────────────────────────
    /// Take the width the page asked for. It asks for two reasons — the grip on the
    /// panel's edge is being dragged, or what is in the box no longer fits — and both
    /// arrive here as a number of points for the PANEL, the window being that plus a
    /// gutter on each side.
    ///
    /// The left edge stays put, because that is the edge the panel was placed by: a
    /// box that grew leftwards while you typed into it would walk out from under the
    /// dimension it belongs to. Growing off the right of the screen is the one case
    /// that moves it, and then only by as much as it has to.
    func setPanelWidth(_ w: CGFloat) {
        let screen = NSScreen.screens.first(where: { $0.frame.contains(window.frame.origin) })
            ?? NSScreen.main
        let vis = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let maxW = max(PANEL_W_MIN, vis.width - GUTTER * 2 - 40)
        let want = min(max(w, PANEL_W_MIN), maxW)
        if abs(want - PANEL_W) < 0.5 { return }
        PANEL_W = want
        guard window != nil, window.isVisible else { return }
        var f = window.frame
        f.size.width = WIN_W
        if let midX = centredMidX {
            f.origin.x = midX - f.width / 2
        }
        if f.maxX - GUTTER > vis.maxX { f.origin.x = vis.maxX - f.width + GUTTER }
        if f.origin.x + GUTTER < vis.minX { f.origin.x = vis.minX - GUTTER }
        window.setFrame(f, display: true)
    }

    func beginDrag() {
        centredMidX = nil
        dragMouseStart = NSEvent.mouseLocation
        dragWinStart = window.frame.origin
        dragTimer?.invalidate()
        dragTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 120.0, repeats: true) { [weak self] _ in
            guard let self = self, self.window.isVisible else { return }
            let p = NSEvent.mouseLocation
            self.window.setFrameOrigin(NSPoint(
                x: self.dragWinStart.x + (p.x - self.dragMouseStart.x),
                y: self.dragWinStart.y + (p.y - self.dragMouseStart.y)))
        }
    }

    func endDrag() {
        dragTimer?.invalidate()
        dragTimer = nil
    }

    // NOTE: there is deliberately no windowDidResignKey → cancel. It used to be here on
    // the "clicking away dismisses a dialog" principle, but orbiting, panning and
    // zooming the viewport all activate Fusion — so every camera move threw the dialog
    // away mid-edit. The dialog is now dismissed only by Esc or by committing.

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.name == "drag" {
            if (message.body as? Bool) ?? false { beginDrag() } else { endDrag() }
            return
        }
        if message.name == "rect" {
            if let b = message.body as? [String: Any],
               let w = (b["w"] as? NSNumber)?.doubleValue, w > 0,
               let h = (b["h"] as? NSNumber)?.doubleValue, h > 0 {
                panelRect = NSRect(x: pt((b["x"] as? NSNumber)?.doubleValue ?? 0),
                                   y: pt((b["y"] as? NSNumber)?.doubleValue ?? 0),
                                   width: pt(w), height: pt(h))   // page px → pt
            } else {
                panelRect = nil
            }
            updateClickThrough()
            return
        }
        if message.name == "size" {
            if let body = message.body as? [String: Any],
               let w = (body["width"] as? NSNumber)?.doubleValue {
                setPanelWidth(pt(w))
            }
            return
        }
        guard message.name == "session",
              let body = message.body as? [String: Any] else { return }
        if (body["open"] as? Bool) ?? false {
            // Width first: the frames below are computed from it, so taking it after
            // would place a window of one width and then resize it to another.
            if let w = (body["width"] as? NSNumber)?.doubleValue {
                PANEL_W = min(max(pt(w), PANEL_W_MIN), 2000)
            }
            let h = (body["height"] as? NSNumber)?.doubleValue ?? 150
            let place = (body["place"] as? String) ?? "cursor"
            showDialog(panelHeight: pt(h), place: place)
        } else {
            hideDialog()
        }
    }

    // ── Loading ──────────────────────────────────────────────────────────────
    func tryLoad() {
        guard let url = URL(string: URL_STR) else { return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            DispatchQueue.main.async {
                if let r = resp as? HTTPURLResponse, r.statusCode == 200 {
                    self?.webView.load(URLRequest(url: url))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self?.tryLoad() }
                }
            }
        }.resume()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(action.request.url?.host == "127.0.0.1" ? .allow : .cancel)
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!,
                 withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // Exit NOW, skipping Cocoa/WebKit teardown — deallocating WKWebView on the way out
    // is what sometimes crashes and summons macOS's "quit unexpectedly" dialog. There
    // is no state here worth a graceful shutdown.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        exit(0)
    }
}

// Die silently no matter how we're killed.
for sig in [SIGTERM, SIGINT, SIGHUP, SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGTRAP, SIGFPE] {
    signal(sig) { _ in _exit(0) }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
