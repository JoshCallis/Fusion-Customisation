// WindowShell.swift — the pieces every companion-window shell shares, compiled into
// each app beside its own main.swift by bin/build_app.sh. Each was pasted into the
// shells (params, browser, shortcuts, value, dock, Control Room); an edit here now
// reaches all of them on their next build.

import Cocoa
import CoreGraphics
import WebKit
import notify

// The close glyph, one shade for each appearance. A dynamic NSColor is re-resolved by
// AppKit whenever the effective appearance changes, so nothing has to be observed.
func closeGlyphColor() -> NSColor {
    return NSColor(name: nil) { ap in
        ap.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            ? NSColor(calibratedWhite: 0.45, alpha: 1.0)
            : NSColor(calibratedWhite: 0.30, alpha: 1.0)
    }
}

// A held letter repeats, the way a held arrow does. macOS's press-and-hold
// (ApplePressAndHoldEnabled, on by default) swallows key repeat for letter keys in
// anything that takes text input — WKWebView included — so a held C (Move selection
// down) fired once and stopped. Turned off for THIS app only, in its own defaults
// domain (which outranks the global one); no page here needs the accent popup. Call
// before app.run(). The page then sees ordinary keydowns with e.repeat set, at the
// system's own key-repeat rate.
func letHeldKeysRepeat() {
    UserDefaults.standard.set(false, forKey: "ApplePressAndHoldEnabled")
}

// One window-list walk for the two facts the hover timer needs.
//
// fusionValueOpen: whether FusionValue's dialog window is currently on screen.
// Checked by window list rather than frontmost-app: a viewport click makes
// FUSION frontmost for the instant between the click and FusionValue's own
// release handler reclaiming it (its hover timer, same idea), and this
// window's hover timer can land in that gap. Racing on frontmost-app let a
// hover here win that race and steal the dialog's focus out from under it the
// moment you picked something in the viewport — the dialog's window stays on
// screen for its whole session, click or no click, so this has no such gap.
//
// menuOpen: whether a menu (Fusion's menu bar, or a right-click popup) is
// currently open. Confirmed by sampling CGWindowListCopyWindowInfo live while
// a Fusion menu was open: it shows up owned by Fusion at kCGWindowLayer 101,
// exactly CGWindowLevelForKey(.popUpMenuWindow). A menu drawn over this window
// closes the instant we activate — even though it's the thing on top — so the
// hover timer has to leave it alone while one is open.
func windowState() -> (fusionValueOpen: Bool, menuOpen: Bool) {
    guard let list = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: AnyObject]]
    else { return (false, false) }
    let myPid = Int(ProcessInfo.processInfo.processIdentifier)
    let menuLevel = CGWindowLevelForKey(.popUpMenuWindow)
    var fusionValueOpen = false
    var menuOpen = false
    for w in list {
        if (w[kCGWindowOwnerName as String] as? String) == "FusionValue" { fusionValueOpen = true }
        let pid = (w[kCGWindowOwnerPID as String] as? Int) ?? -1
        let layer = (w[kCGWindowLayer as String] as? Int) ?? -1
        if layer == menuLevel && pid != myPid { menuOpen = true }
    }
    return (fusionValueOpen, menuOpen)
}

// The menu bar every shell installs at launch. Without one, a WKWebView text field gets
// no ⌘X/C/V/A/Z: those arrive as the Edit menu's key equivalents and route down the
// responder chain to the focused input. (Agent apps — no menu bar shown — still need it
// installed for the key equivalents.) Quit + a standard Edit menu, nothing else.
func installEditMenu() {
    let mainMenu = NSMenu()

    let appItem = NSMenuItem(); mainMenu.addItem(appItem)
    let appMenu = NSMenu(); appItem.submenu = appMenu
    appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

    let editItem = NSMenuItem(); mainMenu.addItem(editItem)
    let editMenu = NSMenu(title: "Edit"); editItem.submenu = editMenu
    editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    editMenu.addItem(NSMenuItem.separator())
    editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

    NSApp.mainMenu = mainMenu
}

// A page's alert() / confirm() / prompt(), as sheets on the shell's window. WKWebView
// shows nothing for them unless its WKUIDelegate does, so each shell's three delegate
// methods hand straight to these.
func jsAlert(_ message: String, on window: NSWindow, _ done: @escaping () -> Void) {
    let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "OK")
    alert.beginSheetModal(for: window) { _ in done() }
}

func jsConfirm(_ message: String, on window: NSWindow, _ done: @escaping (Bool) -> Void) {
    let alert = NSAlert(); alert.messageText = message
    alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
    alert.beginSheetModal(for: window) { resp in done(resp == .alertFirstButtonReturn) }
}

func jsPrompt(_ prompt: String, defaultText: String?, on window: NSWindow,
              _ done: @escaping (String?) -> Void) {
    let alert = NSAlert(); alert.messageText = prompt
    alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
    field.stringValue = defaultText ?? ""
    alert.accessoryView = field
    alert.window.initialFirstResponder = field
    alert.beginSheetModal(for: window) { resp in
        done(resp == .alertFirstButtonReturn ? field.stringValue : nil)
    }
}

// ── UI zoom ─────────────────────────────────────────────────────────────────────────
// One zoom for every window we draw, like ⌘+ in a browser: ui_zoom.sh writes the factor
// (1.0 = default, never below) to config/ui_zoom and posts UI_ZOOM_NOTE; each shell
// sets WKWebView.pageZoom from it. pageZoom reflows rather than magnifies, so a page
// that fills its window (Browser, dock panes) grows its text within the same frame.
//
// Windows that size THEMSELVES from the page (palette, props, FusionValue, Jump) get
// CSS px from it and must turn them into points with pt() — at 1.2, a 300px-wide panel
// is a 360pt window. Their `changed` callback re-places them when the zoom moves.
let UI_ZOOM_FILE = NSHomeDirectory() + "/Code/Scripts/config/ui_zoom"
let UI_ZOOM_NOTE = "com.callis.uizoom"
var uiZoom: CGFloat = readUIZoom()
private var uiZoomToken: Int32 = 0

func readUIZoom() -> CGFloat {
    let s = (try? String(contentsOfFile: UI_ZOOM_FILE, encoding: .utf8)) ?? ""
    let z = Double(s.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 1
    return CGFloat(min(max(z, 1), 3))
}

func pt(_ css: Double) -> CGFloat { CGFloat(css) * uiZoom }

/// Zoom `views()` now and every time ui_zoom.sh changes the factor, then call `changed`.
/// A closure so a shell that rebuilds its web views is asked for the current ones.
/// A change glides there on an S-curve over UI_ZOOM_GLIDE s (uiZoom, the views and
/// `changed` every frame); a press mid-glide starts a new one from where it is.
let UI_ZOOM_GLIDE = 0.6
private var uiZoomTimer: Timer?

/// The S-curve every UI zoom change rides (ease in-out cubic) — the same one
/// ui_zoom/chrome, the Safari tabs and cursor_scale use.
func uiZoomEase(_ t: Double) -> Double {
    t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2
}

func followUIZoom(_ views: @escaping () -> [WKWebView], changed: (() -> Void)? = nil) {
    for v in views() { v.pageZoom = uiZoom }
    notify_register_dispatch(UI_ZOOM_NOTE, &uiZoomToken, DispatchQueue.main) { _ in
        let from = uiZoom, to = readUIZoom(), t0 = CACurrentMediaTime()
        uiZoomTimer?.invalidate()
        guard abs(to - from) > 0.001 else { return }
        let step = { (timer: Timer) in
            let t = min((CACurrentMediaTime() - t0) / UI_ZOOM_GLIDE, 1)
            uiZoom = from + (to - from) * CGFloat(uiZoomEase(t))
            for v in views() { v.pageZoom = uiZoom }
            changed?()
            if t >= 1 { timer.invalidate() }
        }
        uiZoomTimer = Timer(timeInterval: 1.0 / 60, repeats: true, block: step)
        RunLoop.main.add(uiZoomTimer!, forMode: .common)
    }
}
