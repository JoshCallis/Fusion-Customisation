// FusionShortcuts — WKWebView window for the Fusion keyboard-shortcut browser.
//
// Points at http://localhost:8733 (served by the Fusion Shortcuts add-in). Retries
// until the server is up, so it can be launched before or after Fusion. Window
// position is remembered across sessions via setFrameAutosaveName.
//
// Beyond the usual companion-window boilerplate this one owns KEY CAPTURE. When
// the page is waiting for the user to press a shortcut, JS posts `true` to the
// "capture" message handler and every keyDown is intercepted here instead of
// reaching the page or the menu bar — otherwise macOS would eat exactly the
// combinations most worth binding (⌘W closes the window, ⌘Q quits, ⌘X/C/V/A/Z go
// to the Edit menu) before JS ever saw them.
//
// Captured keys are identified by hardware keyCode, not by character: with Shift
// held, charactersIgnoringModifiers still reports the shifted glyph ("?" for
// shift+/), which would name the binding something Fusion never uses.

import Cocoa
import WebKit
import CoreGraphics

let PORT = 8733
let URL_STR = "http://127.0.0.1:\(PORT)"

// macOS virtual keycode → the key name Fusion uses in its keymap ("shift+alt+b",
// "command+up", "control+tab", "shift+command+f1").
let KEY_NAMES: [UInt16: String] = [
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
    11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t",
    18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5", 25: "9", 26: "7", 28: "8", 29: "0",
    24: "=", 27: "-", 30: "]", 33: "[", 39: "'", 41: ";", 42: "\\", 43: ",", 44: "/", 47: ".",
    50: "`",
    31: "o", 32: "u", 34: "i", 35: "p", 37: "l", 38: "j", 40: "k", 45: "n", 46: "m",
    // Names match what Fusion actually writes in its keymap — it says "return"
    // and "backspace", not "enter" and "delete".
    36: "return", 48: "tab", 49: "space", 51: "backspace", 53: "escape", 117: "delete",
    115: "home", 119: "end", 116: "pageup", 121: "pagedown",
    123: "left", 124: "right", 125: "down", 126: "up",
    122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6", 98: "f7", 100: "f8",
    101: "f9", 109: "f10", 103: "f11", 111: "f12",
]

let FUSION_BUNDLE_PREFIXES = ["com.autodesk.fusion"]

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                   WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var hoverTimer: Timer!
    // Was the pointer already inside our frame on the LAST tick? See the timer below —
    // it is what tells a fresh crossing-in from merely resting here, which matters only
    // while FusionValue holds the keyboard.
    var wasInside = false
    var spaceTimer: Timer!
    // Whether this window lives on the space currently on screen. See
    // refreshSpaceMembership().
    var onCurrentSpace = true
    var capturing = false
    // False while Fusion's main thread is blocked (its own modal is up). Hover-to-
    // focus must not raise us over a Fusion dialog.
    var allowRaise = true
    // True from the moment we ask Fusion to open its shortcut prompt until it's
    // dismissed: we get out of the way entirely for that handoff.
    var handingOff = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installEditMenu()   // WindowShell; the capture monitor below swallows keys ahead of it
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // JS → native: "close" shuts the window (so pressing the open-shortcut again
        // while this window has focus toggles it off); "capture" turns key
        // interception on and off.
        config.userContentController.add(self, name: "close")
        config.userContentController.add(self, name: "capture")
        // Posted just before the page asks the add-in to open Fusion's shortcut
        // prompt: that prompt is modal on Fusion's own window, so Fusion has to be
        // frontmost or it opens behind us and never sees the keystroke.
        config.userContentController.add(self, name: "focusFusion")
        // "allowRaise" gates hover-to-focus; "handoff" hides us for the duration of
        // Fusion's shortcut prompt.
        config.userContentController.add(self, name: "allowRaise")
        config.userContentController.add(self, name: "handoff")

        webView = WKWebView(frame: .zero, configuration: config)

        followUIZoom { [unowned self] in [self.webView] }   // WindowShell § UI zoom
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        // Don't paint white before the page loads — let the black window show through.
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 12.0, *) { webView.underPageBackgroundColor = .black }
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.black.cgColor

        let style: NSWindow.StyleMask = [.titled, .closable, .resizable, .miniaturizable]
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1000, height: 720),
            styleMask: style,
            backing: .buffered,
            defer: false)
        window.title = "Shortcuts"
        window.contentView = webView
        window.minSize = NSSize(width: 620, height: 360)

        // Dark, minimal title bar (same treatment as the other companion windows):
        // transparent so the black background shows through, no title text, no
        // traffic lights. The bar stays draggable.
        // The appearance is deliberately NOT pinned: setting window.appearance also pins
        // the WKWebView's prefers-color-scheme, and the page's palette follows the
        // system through that (fusion_ui/theme.css, both appearances black).
        window.backgroundColor = NSColor.black
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.standardWindowButton(.closeButton)?.isHidden = true
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true

        // Subtle grey close button pinned to the very top-left, added to the theme
        // frame so the hidden traffic lights don't reserve space in front of it.
        let closeBtn = NSButton(title: "", target: self, action: #selector(closeWindowAction))
        closeBtn.isBordered = false
        closeBtn.bezelStyle = .regularSquare
        closeBtn.attributedTitle = NSAttributedString(string: "✕", attributes: [
            .foregroundColor: closeGlyphColor(),
            .font: NSFont.systemFont(ofSize: 11, weight: .regular),
        ])
        closeBtn.translatesAutoresizingMaskIntoConstraints = false
        if let themeFrame = window.contentView?.superview {
            themeFrame.addSubview(closeBtn)
            NSLayoutConstraint.activate([
                closeBtn.leadingAnchor.constraint(equalTo: themeFrame.leadingAnchor, constant: 8),
                closeBtn.topAnchor.constraint(equalTo: themeFrame.topAnchor, constant: 7),
                closeBtn.widthAnchor.constraint(equalToConstant: 16),
                closeBtn.heightAnchor.constraint(equalToConstant: 16),
            ])
        }

        // Space-BOUND, exactly like FusionDock: the window lives on whichever space
        // Fusion is on and slides away WITH Fusion during a swipe. canJoinAllSpaces
        // would do the opposite (show it on every space), and a plain window would
        // be left behind on the space it was opened on. moveToActiveSpace re-homes
        // it each time we orderFront.
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]

        // Deliberately left at .normal. A .floating window does sit above Fusion's
        // viewport without any activation trickery, but it is also skipped by the
        // three-finger window-drag gesture, and it would sit above Fusion's own
        // dialogs too — measured, "Change Keyboard Shortcut" is kCGWindowLayer 0,
        // exactly the same level as Fusion's main window, so no level exists that is
        // above one and below the other. Staying in front of the viewport is instead
        // done by hover-to-focus raising the app, suppressed while a Fusion dialog
        // is up (see allowRaise).

        // No open/close animation: closing a .titled window fades its frame out and
        // that fade flashes a bright white outline against this black UI.
        window.animationBehavior = .none

        window.setFrameAutosaveName("FusionShortcutsMain")
        window.makeKeyAndOrderFront(nil)
        if UserDefaults.standard.object(forKey: "NSWindow Frame FusionShortcutsMain") == nil {
            window.center()
        }

        // Follow Fusion: visible only while Fusion (or this window) is frontmost, so
        // switching to another app on this space puts it away and coming back brings
        // it with Fusion. Deliberately no activeSpaceDidChange observer — a swipe
        // doesn't re-activate any app, so the window just travels with Fusion; an
        // orderFront during a swipe would DRAG it onto the space being swiped to.
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(frontmostChanged),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)

        tryLoad()

        // Focus-follows-mouse, same rationale as the other windows: WKWebView won't
        // deliver a click to the page while the app is inactive, so activate as soon
        // as the cursor is over us.
        //
        // The space guard is load-bearing, not a nicety. Without it, swiping to
        // another space while the pointer happened to sit inside this window's
        // screen rect activated us from the space we'd just left; that activation
        // fired didActivateApplication → orderFront → moveToActiveSpace, dragging
        // this window onto the new space. It dragged FusionDock along too, because
        // the dock's isFusionFront() matches any app whose name contains "fusion" —
        // which "FusionShortcuts" does — so our activation read to it as Fusion
        // coming forward. Both windows then followed the swipe instead of staying put.
        //
        // The guard has to answer "is this window on the space I'm looking at?" and
        // nothing built into NSWindow answers it reliably: isOnActiveSpace only
        // started returning true once the window had been dragged, and
        // occlusionState is the wrong question entirely — a window merely covered by
        // Fusion isn't .visible either, yet hovering over it is exactly how it gets
        // raised back. onCurrentSpace (below) uses the window-server's on-screen
        // list, which includes obscured windows but excludes other spaces.
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            guard let self = self, let w = self.window,
                  w.isVisible, self.onCurrentSpace, !NSApp.isActive,
                  self.allowRaise, !self.handingOff else {
                self?.wasInside = false
                return
            }
            let inside = w.frame.contains(NSEvent.mouseLocation)
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
            if state.fusionValueOpen {
                if inside && !self.wasInside { NSApp.activate(ignoringOtherApps: true) }
            } else if inside {
                NSApp.activate(ignoringOtherApps: true)
            }
        }

        spaceTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.refreshSpaceMembership()
        }

        // Key capture. Only active between "capture true" and "capture false", so
        // normal typing (search box, arrow keys) is untouched the rest of the time.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.capturing else { return event }
            let f = event.modifierFlags
            // A bare modifier press isn't a binding — wait for the real key.
            guard let name = KEY_NAMES[event.keyCode] else { return nil }
            let payload: [String: Any] = [
                "key": name,
                "shift": f.contains(.shift),
                "command": f.contains(.command),
                "alt": f.contains(.option),
                "control": f.contains(.control),
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: payload),
                  let json = String(data: data, encoding: .utf8) else { return nil }
            self.webView.evaluateJavaScript("window.__capturedKey && window.__capturedKey(\(json))")
            return nil       // swallow, so the menu bar / WebKit never see it
        }

        // Echo modifiers as they're held, so the lookup field fills in while you
        // press rather than staying blank until the final key.
        NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            guard let self = self, self.capturing else { return event }
            let f = event.modifierFlags
            let payload: [String: Any] = [
                "shift": f.contains(.shift), "command": f.contains(.command),
                "alt": f.contains(.option),  "control": f.contains(.control),
            ]
            if let data = try? JSONSerialization.data(withJSONObject: payload),
               let json = String(data: data, encoding: .utf8) {
                self.webView.evaluateJavaScript("window.__captureMods && window.__captureMods(\(json))")
            }
            return event
        }
    }

    // ── Following Fusion across spaces ────────────────────────────────────────

    @objc func frontmostChanged() { updateVisibility() }

    /// True when this window is on the space that's currently displayed.
    ///
    /// The window server's on-screen list contains every window on the current
    /// space — including ones completely hidden behind Fusion — and nothing from
    /// other spaces. That's precisely the distinction needed: "buried under Fusion"
    /// must still allow hover-to-raise, "sitting on another space" must not, or the
    /// raise drags this window (and FusionDock with it) across on every swipe.
    func refreshSpaceMembership() {
        guard let w = window else { return }
        let id = CGWindowID(w.windowNumber)
        guard let list = CGWindowListCopyWindowInfo(
                [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
        else { return }
        onCurrentSpace = list.contains {
            ($0[kCGWindowNumber as String] as? CGWindowID) == id
        }
    }

    func fusionApp() -> NSRunningApplication? {
        return NSWorkspace.shared.runningApplications.first { app in
            let bid = (app.bundleIdentifier ?? "").lowercased()
            if FUSION_BUNDLE_PREFIXES.contains(where: { bid.hasPrefix($0) }) { return true }
            return (app.localizedName ?? "") == "Autodesk Fusion"
        }
    }

    func isFusionFront() -> Bool {
        guard let front = NSWorkspace.shared.frontmostApplication else { return false }
        if front.bundleIdentifier == Bundle.main.bundleIdentifier { return true }   // ourselves
        let bid = (front.bundleIdentifier ?? "").lowercased()
        if FUSION_BUNDLE_PREFIXES.contains(where: { bid.hasPrefix($0) }) { return true }
        return (front.localizedName ?? "").lowercased().contains("fusion")
    }

    func updateVisibility() {
        guard let w = window else { return }
        if handingOff { return }          // deliberately out of the way
        if isFusionFront() {
            w.orderFront(nil)                     // show WITHOUT stealing key from Fusion
            // Activating Fusion from another space fires didActivate while the
            // OUTGOING space is still current, so moveToActiveSpace can land us on
            // the wrong one. Once the switch settles, re-home if we missed.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                guard let self = self else { return }
                self.refreshSpaceMembership()
                guard self.isFusionFront(), !self.onCurrentSpace else { return }
                w.orderOut(nil)
                w.orderFront(nil)
            }
        } else if onCurrentSpace {
            // Only hide when another app was focused on the space we're actually on.
            // Ordering out a window parked on Fusion's space is what made it look
            // closed after a swipe away and back: a swipe re-activates nothing, so
            // no notification ever arrives to order it front again.
            w.orderOut(nil)
        }
    }

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

    // Prevent navigation away from the local server.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let host = action.request.url?.host, host == "127.0.0.1" { decisionHandler(.allow) }
        else { decisionHandler(.cancel) }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!,
                 withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }

    // Terminate straight away rather than close(): closing runs the window-teardown
    // path, and that is what flashes a white frame outline on the way out.
    @objc func closeWindowAction() { NSApp.terminate(nil) }

    // MARK: - WKScriptMessageHandler

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        switch message.name {
        case "close":   NSApp.terminate(nil)
        case "capture": capturing = (message.body as? Bool) ?? false
        case "focusFusion": fusionApp()?.activate(options: [])
        case "allowRaise":
            allowRaise = (message.body as? Bool) ?? true
        case "handoff":
            // Just a flag — deliberately NOT orderOut. Activating Fusion already
            // puts its prompt above us; all that's needed is to stop hover-to-focus
            // yanking us back in front of it. Hiding the window would also risk
            // WebKit throttling the page's poll timer, and that poll is what tells
            // us the prompt was dismissed.
            handingOff = (message.body as? Bool) ?? false
            if !handingOff { updateVisibility() }
        default:        break
        }
    }

    // MARK: - WKUIDelegate (native JS dialogs)

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

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // Exit NOW, skipping Cocoa/WebKit teardown — deallocating WKWebView on the way
    // out is what sometimes crashes and summons the "quit unexpectedly" dialog.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        exit(0)
    }
}

// Die silently no matter how we're killed: a caught crash signal exits cleanly
// before ReportCrash can see it, so no report dialog even on a real crash.
for sig in [SIGTERM, SIGINT, SIGHUP, SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGTRAP, SIGFPE] {
    signal(sig) { _ in _exit(0) }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
