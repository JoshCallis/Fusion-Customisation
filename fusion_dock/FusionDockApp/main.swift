// FusionDock — one borderless left-edge window holding the component Browser
// (http://127.0.0.1:8732, served by the Browser add-in) and, swapped with it, the
// vertical timeline (http://127.0.0.1:8736, the Timeline add-in) — two WKWebViews, one
// visible at a time (setDockMode, driven by the pages' `dockMode`). Parameters live inside
// the Browser page itself now (its own sketches/parameters toggle), so this
// window is Browser-only — it used to also carry a bottom Parameters pane
// (served by the Parameters add-in on 8731); that pane is retired.
//
// No title bar, no headers, no close button — toggled on/off entirely by the
// Fusion "Browser" shortcut (which pgrep/pkill/opens this app).
//
// Space-following (the whole point of "nesting"): the window is shown ONLY while
// Fusion — or this dock itself — is the frontmost app. Swipe to another space and
// its app becomes frontmost → we orderOut; swipe back to Fusion → orderFront. No
// yabai sticky rule needed (that one shows on EVERY space, the opposite of this).

import Cocoa
import WebKit
import CoreGraphics

let DOCK_WIDTH: CGFloat = 320
// Window fill: black at this alpha, so Fusion shows through the dock. The container
// paints it (the pages themselves are transparent — see theme.css), which keeps both
// panes at exactly the same tint. 0 = no fill at all: just the text and icons over
// Fusion. (The opaque strip beside the corner cut is separate — see applyNotch.)
let DOCK_ALPHA: CGFloat = 0.0
// A rectangular bite out of the BOTTOM-RIGHT corner, so the start of Fusion's
// timeline — which sits under the dock now that it's on the left edge — stays
// visible and clickable. Anchored by its LEFT edge (a fixed distance from the screen
// edge), so widening the dock makes the cut WIDER rather than sliding it right: what
// it uncovers stays uncovered. Set NOTCH_H to 0 to fill the corner back in.
// Invisible grab strip at the TOP of the right edge: drag it to widen or narrow the
// dock (the left edge stays pinned to the screen). No visible chrome — the only tell
// is the cursor turning into the horizontal resize arrows over it. Kept short so the
// rest of the right edge still belongs to the list underneath.
let NOTCH_LEFT: CGFloat = 131      // from the window's left edge
let NOTCH_H: CGFloat = 50

// A second, page-driven pass-through band at the BOTTOM of the window: empty space
// below the last row of the Browser list. Unlike the notch (fixed geometry, masked
// out), this one is unmasked content whose extent the page itself reports as
// liveBottom (see publishLiveBottom() in fusion_browser/index.js) — the band grows
// and shrinks with the list, and disappears entirely (liveBottom == -1) while it
// overflows, a menu/drag/press is live, or there are no rows. See passScreenRect().

let HANDLE_W: CGFloat = 6
let HANDLE_H: CGFloat = 90
let DOCK_WIDTH_MIN: CGFloat = 220
let DOCK_WIDTH_KEY = "dockWidth"        // remembered across launches (UserDefaults)
let BROWSER_URL = "http://127.0.0.1:8732"
// The dock's second panel: the vertical timeline (fusion_timeline/, served by the
// Timeline add-in). One of the two is visible at a time; either page posts `dockMode`
// ("browser" / "timeline") when the add-in's mode changes, and setDockMode swaps them.
let TIMELINE_URL = "http://127.0.0.1:8736"
let FUSION_BUNDLE_PREFIXES = ["com.autodesk.fusion"]   // matches fusion360

// The resize strip. Invisible (no drawing at all) — it exists for its cursor and its
// drag. Live-resizes the window as you pull, and remembers the width on release.
final class ResizeHandle: NSView {
    weak var owner: AppDelegate?
    private var startX: CGFloat = 0
    private var startW: CGFloat = 0
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .resizeLeftRight) }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(rect: bounds,
                                       options: [.activeAlways, .mouseEnteredAndExited, .cursorUpdate],
                                       owner: self, userInfo: nil))
    }
    override func cursorUpdate(with event: NSEvent) { NSCursor.resizeLeftRight.set() }
    override func mouseEntered(with event: NSEvent) { NSCursor.resizeLeftRight.set() }
    override func mouseExited(with event: NSEvent)  { NSCursor.arrow.set() }
    override func mouseDown(with event: NSEvent) {
        startX = NSEvent.mouseLocation.x
        startW = window?.frame.width ?? 0
        NSCursor.resizeLeftRight.set()
    }
    override func mouseDragged(with event: NSEvent) {
        owner?.setDockWidth(startW + (NSEvent.mouseLocation.x - startX))
        NSCursor.resizeLeftRight.set()
    }
    override func mouseUp(with event: NSEvent) { owner?.saveDockWidth() }
}

// Borderless windows can't become key by default, so a text input (rename box)
// would never receive the keyboard. Allow it.
class DockWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                   WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: DockWindow!
    var container: NSView!
    var browserView: WKWebView!
    var timelineView: WKWebView!
    var timelineMode = false          // which of the two panes is showing (setDockMode)
    var visibleView: WKWebView! { timelineMode ? timelineView : browserView }
    var handle: ResizeHandle!         // right-edge drag strip (see ResizeHandle)
    var dockWidth: CGFloat = DOCK_WIDTH
    var hoverTimer: Timer!
    // Was the pointer already inside our frame on the LAST tick? See the timer below —
    // it is what tells a fresh crossing-in from merely resting here, which matters only
    // while FusionValue holds the keyboard.
    var wasInside = false
    // Bottom edge (in the browser page's own top-down pixels, which line up 1:1 with
    // this window's points) of the last visible row, published by the page's
    // publishLiveBottom() — see index.js. -1 means the whole window is live content
    // (the page's default: overflowing list, a menu/drag/press in progress, or no
    // rows at all). Each page reports its own; only the visible pane's counts.
    var liveBottomBrowser: CGFloat = -1
    var liveBottomTimeline: CGFloat = -1
    var liveBottom: CGFloat { timelineMode ? liveBottomTimeline : liveBottomBrowser }
    var stateTimer: Timer!
    // Three-finger-tap detection over the browser pane (see the leftMouseDown monitor):
    // WebKit is unreliable at delivering rapid synthetic clicks (BTT turning a 3-finger
    // tap into a double-click) to the web content, so we watch the native events
    // in-process and drive the go-to-top via JS. Mirrors the C event tap's approach.
    var lastDockClickTs: TimeInterval = 0
    var lastDockClickPid: Int64 = 0
    // reanchor publishes a three-finger tap over THIS window here (it already tracks
    // the gesture for CAD nav). BetterTouchTool's own tap trigger is set to All Apps
    // but only converts the gesture over this window about one time in ten, so the
    // tap is taken from reanchor rather than from BTT's synthetic double-click. The
    // clickCount/pairing test below still stands for the times BTT does fire.
    var tapTimer: Timer!
    var lastDockTapStamp: String = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        installEditMenu()

        let saved = UserDefaults.standard.double(forKey: DOCK_WIDTH_KEY)
        if saved >= Double(DOCK_WIDTH_MIN) { dockWidth = CGFloat(saved) }
        let frame = dockFrame()
        window = DockWindow(contentRect: frame, styleMask: [.borderless],
                            backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.isOpaque = false
        // Clear at the WINDOW level: the fill is painted by the container layer, which
        // is the thing the corner cut masks out. A window background would paint over
        // the cut (it sits behind every view, mask and all).
        window.backgroundColor = .clear
        // The appearance is deliberately NOT pinned: setting window.appearance also pins
        // the WKWebView's prefers-color-scheme, and the page's palette follows the
        // system through that (fusion_ui/theme.css, both appearances black).
        // NO SHADOW. macOS draws a light 1px rim along the shadowed edge of a dark
        // borderless window, and measured off a raw screenshot that rim is exactly
        // this window's first pixel column: x=3119 (Fusion) = rgb(0,0,0), x=3120
        // (our left edge) = rgb(51,51,51), fading to black by x=3122 — identical at
        // every height and across BOTH panes, and gone entirely when the dock is
        // quit. Nothing in this file or in the served pages paints it: the only
        // colours here are two NSColor.black backgrounds, and the pages compute no
        // border/outline/shadow at all. The shadow itself was never visible anyway —
        // the dock is black-on-black against Fusion's viewport, and its top, bottom
        // and right edges are flush with the screen, so the left rim was the only
        // part of it that ever rendered.
        window.hasShadow = false
        window.delegate = self
        // Space-BOUND, not canJoinAllSpaces: the window lives on Fusion's space and
        // slides away WITH Fusion during a swipe (canJoinAllSpaces+stationary kept it
        // painted over the target space until the switch-done notification — the
        // "dock lingers for a beat" bug no observer could fix). moveToActiveSpace
        // re-homes it to the current space every time we orderFront.
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]

        container = NSView(frame: NSRect(origin: .zero, size: frame.size))
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.black.withAlphaComponent(DOCK_ALPHA).cgColor
        window.contentView = container

        browserView = makeWebView(messageName: "close")
        container.addSubview(browserView)
        timelineView = makeWebView(messageName: "close")
        timelineView.isHidden = true
        container.addSubview(timelineView)
        followUIZoom { [unowned self] in [self.browserView, self.timelineView] }   // WindowShell § UI zoom
        handle = ResizeHandle(frame: .zero)      // added last → on top of the panes
        handle.owner = self
        container.addSubview(handle)
        layoutViews()

        load(browserView, BROWSER_URL)
        load(timelineView, TIMELINE_URL)

        // React to real app activations only. Space switches need NO handling: the
        // window is space-bound, so it slides out/in WITH Fusion natively. Deliberately
        // no activeSpaceDidChange observer — a swipe doesn't re-activate any app, so
        // isFusionFront() stays true through it, and any orderFront here would DRAG the
        // dock (moveToActiveSpace) onto the space being swiped to.
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(frontmostChanged),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)
        updateVisibility()

        // Focus-follows-mouse (same rationale as the standalone apps): WKWebView
        // won't deliver a click to the page while the app is inactive, so activate
        // the app as soon as the cursor is over our window. Only while it's on the
        // desktop being shown: isVisible stays true while it's parked on Fusion's, and
        // activating from there made isFusionFront() (ourselves) orderFront it — which,
        // moveToActiveSpace, dragged it over whatever app was under the pointer.
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            guard let self = self, let w = self.window, w.isVisible, w.isOnActiveSpace else { return }
            let p = NSEvent.mouseLocation
            // Inside the corner cut OR the empty band below the list (passScreenRect,
            // reported by the page's own publishLiveBottom), the window plays dead:
            // mouse events go straight to Fusion (the viewport underneath), and we
            // don't steal focus either. The masked notch pixels are fully transparent,
            // but an explicit flag is what makes clicks, drags and scrolls in there —
            // and in the unmasked band — reach Fusion for certain. Held off while a
            // button is down so a drag or click that started on one side of the
            // boundary isn't cut in half mid-gesture.
            let inNotch = self.notchScreenRect().contains(p)
            let passThrough = inNotch || self.passScreenRect().contains(p)
            if w.ignoresMouseEvents != passThrough && NSEvent.pressedMouseButtons == 0 {
                w.ignoresMouseEvents = passThrough
            }
            guard !NSApp.isActive, !passThrough else {
                self.wasInside = false
                return
            }
            let inside = w.frame.contains(p)
            let state = windowState()
            // A menu open over us must not be closed by activating. Treat this tick as
            // a no-op crossing so the menu closing on its own doesn't read as a fresh
            // hover-in once it's gone.
            guard !state.menuOpen else {
                self.wasInside = inside
                return
            }
            defer { self.wasInside = inside }
            // FusionValue wants the keyboard back after ANY click elsewhere, including one
            // in this window (see its own hover timer) — so while IT holds the keyboard,
            // resting here must never steal it back on its own; only a fresh crossing-in
            // does, which is what lets a click here land at all. Coming from anything
            // else the ordinary rule stands: hovering activates, full stop.
            // While FusionValue holds the keyboard this window never activates at all:
            // FusionValue reclaims the moment anything else does, so activating only
            // bounced the keyboard (and restarted its caret). Clicks here are taken
            // natively instead — see the leftMouseDown monitor's valueClick.
            if state.fusionValueOpen {
                // nothing
            } else if inside {
                NSApp.activate(ignoringOtherApps: true)
            }
        }

        // Publish this window's geometry every tick, for reanchor's tap gate.
        stateTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            self?.writeDockFrame()
        }

        // Three-finger tap over the browser pane → top level (see lastDockTapStamp).
        tapTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            self?.pollDockTap()
        }

        // THREE-FINGER TAP over the browser pane → go to top level. A physical
        // double-click must NOT do this, so the gesture is identified the same way
        // FusionScrollFilter.c identifies it over the canvas: the tap is a pair of
        // SYNTHETIC clicks posted by BetterTouchTool (sourceUnixProcessID = BTT's pid),
        // where every real trackpad click carries pid 0. Measured from that tap's own
        // log on this machine: BTT posts its pair 0ms apart with clickState 2 on the
        // second event, while physical clicks are always pid 0 / clickState 1 — the
        // system double-click interval is set so short that a human pair never
        // registers as one. So a synthetic click reporting clickCount >= 2 IS the tap;
        // the tight synthetic pairing is a fallback if a BTT build stops setting it.
        // The old rule paired ANY two clicks within 0.45s and 8px, which fired on
        // ordinary double-clicks and missed taps whose cursor drifted. Detected here
        // rather than in the page because WebKit drops rapid synthetic clicks in the
        // web content. Passes the event through unchanged; the JS decides what to act on.
        NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
            guard let self = self, let w = self.window, w.isVisible else { return event }
            // FusionValue's dialog is up: the click is handed to the page by position and
            // SWALLOWED, so this app never activates and the dialog keeps the keyboard.
            // The page can't take it as a web click — WKWebView delivers nothing while
            // the app is inactive — so it hit-tests the row itself (__valueClick): a
            // parameter goes into the value at the caret, a group opens or shuts.
            if !NSApp.isActive, windowState().fusionValueOpen, self.valueClick(event) { return nil }
            // The source pid is NOT reliable here: the C tap reads it straight off the
            // session tap, but by the time an event is delivered to this app the field
            // can come back 0 (or the CGEvent be gone entirely), which made a pid test
            // reject every tap. So the test is on properties a HUMAN cannot produce on
            // this machine, either one of which is enough:
            //   • clickCount >= 2 — BTT sets clickState 2 on the second click of its
            //     pair, while every physical click here logs 1 (the system double-click
            //     interval is set far too short for a human pair to register), or
            //   • two left-downs within 60ms — BTT posts its pair in one burst, 0-2ms
            //     apart, which no hand can do. Same window FusionScrollFilter.c uses.
            let pid = event.cgEvent?.getIntegerValueField(.eventSourceUnixProcessID) ?? -1
            let now = event.timestamp
            let gap = self.lastDockClickTs == 0 ? -1 : now - self.lastDockClickTs
            let paired = gap >= 0 && gap <= 0.06
            let fire = event.clickCount >= 2 || paired
            if fire {
                self.lastDockClickTs = 0          // consume the pair
                self.handleDockDoubleClick(screenPoint: NSEvent.mouseLocation)
            } else {
                self.lastDockClickTs = now; self.lastDockClickPid = pid
            }
            return event
        }

        // WebKit swallows Cmd+E (Find) and Cmd+R (reload) before the page ever sees a
        // keydown, so intercept them here and hand them to the page. What crosses is
        // the CHORD, not an action name. That WebKit eats these two chords is a fact
        // about WebKit; what they MEAN belongs to Fusion's keymap, which the page
        // consults — so this shell names no action at all, and rebinding rename in the
        // Shortcuts window is not something it has to be taught about.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self else { return event }
            guard event.modifierFlags.contains(.command) else { return event }
            let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
            guard chars == "e" || chars == "r" else { return event }
            var mods = ["cmd"]
            if event.modifierFlags.contains(.control) { mods.append("ctrl") }
            if event.modifierFlags.contains(.option)  { mods.append("alt") }
            if event.modifierFlags.contains(.shift)   { mods.append("shift") }
            let chord = (mods + [chars]).joined(separator: "+")
            let fallback = chars == "e" ? "window.__toggleVis && window.__toggleVis()"
                                        : "window.__rename && window.__rename()"
            self.visibleView?.evaluateJavaScript(
                "window.__key ? window.__key('\(chord)') : (\(fallback))")
            return nil
        }
    }

    // ── Geometry ──────────────────────────────────────────────────────────────
    func dockFrame() -> NSRect {
        // LEFT edge of whichever screen is active (Fusion's, since it's frontmost
        // when we launch / show). Geometry is published to /tmp/fusion_dock_frame
        // every tick, so reanchor's tap gate follows the window rather than assuming
        // a side.
        let screen = NSScreen.main ?? NSScreen.screens.first!
        let vf = screen.visibleFrame
        return NSRect(x: vf.minX, y: vf.minY,
                      width: dockWidth, height: vf.height - menuBarReserve(screen))
    }

    // The menu-bar strip this window has to stay out of.
    //
    // visibleFrame was supposed to handle this by itself — this used to say
    // "visibleFrame keeps us clear of the menu bar+Dock" — and it does, but ONLY
    // while the menu bar is permanently shown. This setup runs with "Automatically
    // hide and show the menu bar" on (_HIHideMenuBar = 1), so that Fusion's viewport
    // reaches the top of the screen, and for an auto-hidden menu bar visibleFrame
    // reserves nothing: it returns the full height and the dock quietly grew up into
    // the strip the menu bar drops into.
    //
    // It was never a z-order problem — .floating is window level 3 and the menu bar
    // is 24, so this window CANNOT draw over it. The dock is also transparent up
    // there (DOCK_ALPHA = 0), so nothing looked wrong. But the window still takes the
    // pointer, and that is what put the left end of the menu bar (Apple, Fusion,
    // File, Edit ...) out of reach behind it.
    //
    // So take the strip off here, and take off only what visibleFrame did NOT
    // already take: with the menu bar pinned visible this returns 0 and the frame is
    // exactly what it has always been.
    func menuBarReserve(_ screen: NSScreen) -> CGFloat {
        let alreadyReserved = screen.frame.maxY - screen.visibleFrame.maxY
        // menuBarHeight is the bar as actually DRAWN. NSStatusBar.system.thickness is
        // the older status-bar metric and is NOT the same number: measured on this
        // display it reads 22 against menuBarHeight's 30, and reserving 22 would leave
        // an 8pt sliver of this window under the bottom of the bar — exactly where the
        // menu titles are, so the problem would look half-fixed. installEditMenu() runs
        // before the first dockFrame(), so mainMenu is always there by now; the
        // thickness is a fallback that should never be reached.
        let barH = NSApp.mainMenu?.menuBarHeight ?? NSStatusBar.system.thickness
        return max(0, barH - alreadyReserved)
    }

    // Live resize from the handle: the left edge is pinned, so the window grows and
    // shrinks to the RIGHT. Clamped so it can't be dragged to nothing or over the
    // whole screen. windowDidResize re-lays the panes and the corner cut.
    func setDockWidth(_ w: CGFloat) {
        guard let win = window else { return }
        let screen = win.screen ?? NSScreen.main ?? NSScreen.screens.first!
        let maxW = max(DOCK_WIDTH_MIN, screen.visibleFrame.width - 100)
        dockWidth = min(max(w.rounded(), DOCK_WIDTH_MIN), maxW)
        win.setFrame(dockFrame(), display: true)
    }
    func saveDockWidth() { UserDefaults.standard.set(Double(dockWidth), forKey: DOCK_WIDTH_KEY) }

    // The cut, in container coordinates (AppKit origin: bottom-left).
    func notchRect(_ size: NSSize) -> NSRect {
        // Cut in both modes: Fusion's own timeline bar may be showing under the corner
        // either way (hiding it is the Hide Native Timeline add-in's, not the dock's).
        let left = min(NOTCH_LEFT, size.width)
        guard NOTCH_H > 0, size.width - left > 0 else { return .zero }
        return NSRect(x: left, y: 0,
                      width: size.width - left, height: min(NOTCH_H, size.height))
    }
    // Same rectangle in screen coordinates, for the cursor test.
    func notchScreenRect() -> NSRect {
        guard let w = window else { return .zero }
        let cut = notchRect(w.frame.size)
        if cut.isEmpty { return .zero }
        return NSRect(x: w.frame.minX + cut.minX, y: w.frame.minY + cut.minY,
                      width: cut.width, height: cut.height)
    }
    // The band below the list, in screen coordinates — same idea as the notch, but
    // reported by the page itself rather than fixed geometry. liveBottom is in the
    // page's own top-down pixels, which line up 1:1 with this window's points (the
    // browser pane fills the container top-to-bottom), so "window height minus
    // liveBottom" is exactly the height of the empty band at the bottom of the
    // window.
    func passScreenRect() -> NSRect {
        guard let w = window, liveBottom >= 0 else { return .zero }
        let bandH = max(0, w.frame.height - liveBottom)
        guard bandH > 0 else { return .zero }
        return NSRect(x: w.frame.minX, y: w.frame.minY, width: w.frame.width, height: bandH)
    }
    // Mask the container so nothing paints in the cut. The webviews are its subviews,
    // so their layers are clipped by this mask too — no per-webview handling needed.
    func applyNotch() {
        guard let layer = container.layer else { return }
        let b = container.bounds
        let cut = notchRect(b.size)
        CATransaction.begin()
        CATransaction.setDisableActions(true)     // no implicit animation on relayout
        defer { CATransaction.commit() }
        // No fill beside the cut. There used to be an opaque black strip there (to
        // hide the timeline running on under the dock); it was the one solid black
        // patch in an otherwise see-through window, and it went.
        if cut.isEmpty { layer.mask = nil; return }
        let path = CGMutablePath()
        path.addRect(b)
        path.addRect(cut)
        let mask = CAShapeLayer()
        mask.frame = b
        mask.path = path
        mask.fillRule = .evenOdd          // everything but the cut
        layer.mask = mask
    }

    func layoutViews() {
        let s = container.bounds.size
        // The pane stops short of the handle column. WKWebView sets the cursor for every
        // mouse-move inside its own bounds, so where it overlapped the handle the two
        // fought and the resize arrows flickered straight back to the arrow. The strip
        // it gives up is the same translucent black as the page behind it, so nothing
        // looks different.
        let paneW = max(0, s.width - HANDLE_W)
        browserView.frame = NSRect(x: 0, y: 0, width: paneW, height: s.height)
        timelineView.frame = browserView.frame
        handle.frame = NSRect(x: s.width - HANDLE_W, y: s.height - HANDLE_H,
                              width: HANDLE_W, height: HANDLE_H)
        applyNotch()
    }

    func windowDidResize(_ notification: Notification) { layoutViews() }

    // ── Visibility / space-following ────────────────────────────────────────────
    @objc func frontmostChanged() { updateVisibility() }

    func isFusionFront() -> Bool {
        guard let front = NSWorkspace.shared.frontmostApplication else { return false }
        // Ourselves — but only where we're shown: active while parked on another desktop
        // must not drag the dock over here.
        if front.bundleIdentifier == Bundle.main.bundleIdentifier { return window.isOnActiveSpace }
        let bid = (front.bundleIdentifier ?? "").lowercased()
        if FUSION_BUNDLE_PREFIXES.contains(where: { bid.hasPrefix($0) }) { return true }
        return (front.localizedName ?? "").lowercased().contains("fusion")
    }

    func updateVisibility() {
        if isFusionFront() {
            window.setFrame(dockFrame(), display: true)   // re-pin (screen may have changed)
            window.orderFront(nil)                        // show WITHOUT stealing key from Fusion
            // Activating Fusion from another space (Cmd-Tab, Dock click) fires didActivate
            // while the OUTGOING space is still active, so moveToActiveSpace can land us on
            // the wrong one. Once the switch settles, re-home if we missed.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                guard let self = self, self.isFusionFront(), !self.window.isOnActiveSpace else { return }
                self.window.orderOut(nil)                 // off the stale space…
                self.window.setFrame(self.dockFrame(), display: true)
                self.window.orderFront(nil)               // …moveToActiveSpace lands it here
            }
        } else {
            // Hide only when we're actually on the visible space (another app was focused
            // HERE). If the dock is parked on Fusion's space while the user is elsewhere,
            // leave it — it slides back in with Fusion on the return swipe.
            if window.isOnActiveSpace { window.orderOut(nil) }
            // Same race as above, mirrored: the activation can land while the OUTGOING
            // desktop (e.g. 3ds Max's) is still active, so the check above passes the
            // dock by — then the flip lands on a desktop it's on. Look again once settled.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                guard let self = self, !self.isFusionFront(), self.window.isOnActiveSpace else { return }
                self.window.orderOut(nil)
            }
        }
    }

    // ── Web views ────────────────────────────────────────────────────────────
    func makeWebView(messageName: String) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        config.userContentController.add(self, name: messageName)
        config.userContentController.add(self, name: "liveBottom")
        // Real keyboard focus, on request. A rename started by a key pressed in the
        // VIEWPORT opens its input in a window that is not the key window, so the
        // caret blinks but every keystroke still goes to Fusion. DOM focus cannot fix
        // that — only the app can — so the page asks for it explicitly.
        config.userContentController.add(self, name: "focus")
        // Which pane the dock shows — posted by either page (see setDockMode).
        config.userContentController.add(self, name: "dockMode")
        let wv = WKWebView(frame: .zero, configuration: config)
        wv.allowsBackForwardNavigationGestures = false
        wv.allowsMagnification = false
        wv.navigationDelegate = self
        wv.uiDelegate = self
        wv.setValue(false, forKey: "drawsBackground")
        if #available(macOS 12.0, *) { wv.underPageBackgroundColor = .clear }
        wv.wantsLayer = true
        wv.layer?.backgroundColor = NSColor.clear.cgColor
        return wv
    }

    // A double-click landed at `screenPoint`. If it's over the browser pane, tell the
    // browser JS (which decides top-level only when it's on empty space, not a row).
    // Publish where this window is, in CG screen coordinates, for reanchor's
    // three-finger-tap gate. It cannot use CGWindowList for this: the dock's window is
    // absent from the on-screen list that process sees (verified from its own log — the
    // walk that works from a shell returns nothing there), so the geometry comes from
    // the window itself. AppKit's origin is bottom-left of the primary screen, CGEvent
    // locations are top-left, hence the flip.
    func writeDockFrame() {
        guard let w = window else { return }
        let f = w.frame
        let primaryMaxY = NSScreen.screens.first?.frame.maxY ?? f.maxY
        let cgY = primaryMaxY - f.maxY
        let visible = w.isVisible && w.isOnActiveSpace
        // Publish only the LIVE part — top of the window down to liveBottom — when the
        // page reports empty space below its list, so reanchor's tap gate
        // (dockFrameContains, bin/reanchor.m) treats the passthrough band as canvas
        // rather than dock. y stays the window's top edge; only the height shrinks.
        let publishedH = liveBottom >= 0 ? min(liveBottom, f.height) : f.height
        let line = String(format: "%.0f %.0f %.0f %.0f %d",
                          f.origin.x, cgY, f.width, publishedH, visible ? 1 : 0)
        try? line.write(toFile: "/tmp/fusion_dock_frame", atomically: true, encoding: .utf8)
    }

    // reanchor stamps DOCK_TAP_FILE the moment three fingers lift over this window
    // without having travelled. Only a FRESH stamp acts, so the file left over from a
    // previous session can't fire one on launch.
    func pollDockTap() {
        guard let raw = try? String(contentsOfFile: "/tmp/fusion_dock_tap", encoding: .utf8)
        else { return }
        let stamp = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !stamp.isEmpty, stamp != lastDockTapStamp, let ms = Double(stamp) else { return }
        lastDockTapStamp = stamp
        guard Date().timeIntervalSince1970 * 1000 - ms < 1500 else { return }   // stale
        handleDockDoubleClick(screenPoint: NSEvent.mouseLocation)
    }

    /// See the leftMouseDown monitor. False when the click is not over the page.
    func valueClick(_ event: NSEvent) -> Bool {
        // The Browser's parameter rows are what a click feeds the value box; the timeline
        // has none, so its clicks are left alone.
        guard !timelineMode, let wv = browserView, event.window === window else { return false }
        var pt = wv.convert(event.locationInWindow, from: nil)
        guard wv.bounds.contains(pt) else { return false }
        if !wv.isFlipped { pt.y = wv.bounds.height - pt.y }
        wv.evaluateJavaScript("window.__valueClick && window.__valueClick(\(pt.x / uiZoom), \(pt.y / uiZoom))")
        return true
    }

    func handleDockDoubleClick(screenPoint p: NSPoint) {
        // Go to top level — nothing to do in timeline mode, where the component is locked.
        guard let w = window, !timelineMode else { return }
        let winPt = w.convertPoint(fromScreen: p)
        guard browserView.frame.contains(winPt) else { return }
        browserView.evaluateJavaScript("window.__dockDblClick && window.__dockDblClick()")
    }

    func load(_ wv: WKWebView, _ urlStr: String) {
        guard let url = URL(string: urlStr) else { return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { [weak wv] _, resp, _ in
            DispatchQueue.main.async {
                if let r = resp as? HTTPURLResponse, r.statusCode == 200 {
                    wv?.load(URLRequest(url: url))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                        self?.load(wv ?? WKWebView(), urlStr)
                    }
                }
            }
        }.resume()
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let host = action.request.url?.host, host == "127.0.0.1" { decisionHandler(.allow) }
        else { decisionHandler(.cancel) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retry(webView)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        retry(webView)
    }
    func retry(_ wv: WKWebView) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            self.load(wv, wv === self.timelineView ? TIMELINE_URL : BROWSER_URL)
        }
    }

    // Show one pane or the other. Idempotent — both pages post the mode, and the hidden
    // one (WebKit throttles its timers) may say it again a second later.
    func setDockMode(_ timeline: Bool) {
        guard timeline != timelineMode else { return }
        let hadFocus = (window?.firstResponder as? NSView)?.isDescendant(of: visibleView) ?? false
        timelineMode = timeline
        browserView.isHidden = timeline
        timelineView.isHidden = !timeline
        applyNotch()
        if hadFocus { window?.makeFirstResponder(visibleView) }
        writeDockFrame()
    }

    // The page posts to messageHandlers.close when its open-shortcut is pressed
    // while focused → toggle the whole dock off (quitting = toggled off; the Fusion
    // shortcut re-launches it).
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "close" { NSApp.terminate(nil) }
        if message.name == "focus" {
            // true  = take key focus so the user can type into an input we just opened
            // false = give it back to Fusion, so finishing a rename leaves the keyboard
            //         where it was before. The page only asks for either when it did
            //         not already have focus, so an edit started inside the dock is
            //         untouched by this.
            if (message.body as? Bool) ?? true {
                NSApp.activate(ignoringOtherApps: true)
                window?.makeKeyAndOrderFront(nil)
                if let wv = message.webView { window?.makeFirstResponder(wv) }
            } else {
                activateFusion()
            }
        }
        if message.name == "liveBottom" {
            let v = (message.body as? NSNumber).map { pt($0.doubleValue) } ?? -1   // page px → pt
            if message.webView === timelineView { liveBottomTimeline = v } else { liveBottomBrowser = v }
        }
        if message.name == "dockMode" {
            setDockMode((message.body as? String) == "timeline")
        }
    }

    /// Hand activation back to Fusion — by bundle id first. A bare name match finds
    /// `fusion_busy` (windowless helper) or our own windows first, and activating one of
    /// those leaves the keyboard with nobody (see FusionValueApp's activateFusion).
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

    // Exit NOW, skipping Cocoa/WebKit teardown — deallocating the WKWebViews on the
    // way out is what sometimes crashes and summons macOS's "quit unexpectedly —
    // send a report?" dialog. There's no state worth a graceful shutdown.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        exit(0)
    }

    // ── JS dialogs (rename prompts etc.) ────────────────────────────────────────
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        jsAlert(message, on: window, completionHandler)
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        jsConfirm(message, on: window, completionHandler)
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        jsPrompt(prompt, defaultText: defaultText, on: window, completionHandler)
    }

}

// Die silently no matter how we're killed: a caught crash signal exits cleanly
// before ReportCrash can see it, so no report dialog even on a real crash.
for sig in [SIGTERM, SIGINT, SIGHUP, SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGTRAP, SIGFPE] {
    signal(sig) { _ in _exit(0) }
}

letHeldKeysRepeat()
let app = NSApplication.shared
// .accessory: no Dock icon, and launching doesn't yank focus away from Fusion.
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
