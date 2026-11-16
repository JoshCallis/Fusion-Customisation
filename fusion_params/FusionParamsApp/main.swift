// FusionParams — minimal WKWebView window for the Fusion parameter editor.
//
// Points at http://localhost:8731 (served by the Fusion Params add-in).
// Retries until the server is up, so it can be launched before or after Fusion.
// Window position is remembered across sessions via setFrameAutosaveName.

import Cocoa
import WebKit
import CoreGraphics

let PORT = 8731
let URL_STR = "http://127.0.0.1:\(PORT)"

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                   WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var hoverTimer: Timer!
    // Was the pointer already inside our frame on the LAST tick? See the timer below —
    // it is what tells a fresh crossing-in from merely resting here, which matters only
    // while FusionValue holds the keyboard.
    var wasInside = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installEditMenu()
        let config = WKWebViewConfiguration()
        // Allow inline media and disable the default gesture-based back/forward.
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // JS calls window.webkit.messageHandlers.close.postMessage(...) to close the
        // window — used so pressing the open-shortcut again (while this window has
        // focus, so Fusion never sees the key) toggles the window shut.
        config.userContentController.add(self, name: "close")

        webView = WKWebView(frame: .zero, configuration: config)

        followUIZoom { [unowned self] in [self.webView] }   // WindowShell § UI zoom
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        webView.navigationDelegate = self
        // WKWebView ignores JS alert/confirm/prompt unless a UIDelegate handles
        // them — without this the "+ Group" button (which uses prompt()) is a no-op.
        webView.uiDelegate = self
        // Don't paint the default white background before the page loads — let the
        // black window show through, so opening doesn't flash white.
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 12.0, *) { webView.underPageBackgroundColor = .black }
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.black.cgColor

        let style: NSWindow.StyleMask = [.titled, .closable, .resizable, .miniaturizable]
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 720),
            styleMask: style,
            backing: .buffered,
            defer: false)
        window.title = "Parameters"
        window.contentView = webView

        // Dark, minimal title bar: transparent so the black window background shows
        // through it, dark appearance, no title text, and no traffic-light buttons.
        // The bar stays draggable for moving the window.
        // The appearance is deliberately NOT pinned: setting window.appearance also pins
        // the WKWebView's prefers-color-scheme, and the page's palette follows the
        // system through that (fusion_ui/theme.css, both appearances black).
        window.backgroundColor = NSColor.black
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.standardWindowButton(.closeButton)?.isHidden = true
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true

        // Subtle custom close button pinned to the very top-left corner (grey, not
        // red). Added to the window's theme frame so it isn't pushed right by the
        // space the hidden traffic-light buttons would otherwise reserve.
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

        window.setFrameAutosaveName("FusionParamsMain")
        // Persistence (keep it floating above Fusion across Spaces) is handled by a
        // yabai rule for app "FusionParams" — a native .floating level interfered
        // with the three-finger window-drag, so we keep this a normal window.
        window.makeKeyAndOrderFront(nil)

        // Center only if no saved frame exists yet.
        if UserDefaults.standard.object(forKey: "NSWindow Frame FusionParamsMain") == nil {
            window.center()
        }

        tryLoad()

        // Focus-follows-mouse: when the cursor is over this window, activate the app
        // so a single click acts directly — no preceding "activate the window" click.
        // WKWebView won't deliver a click to the page while the app is inactive (the
        // content is in another process, gated on NSApp being active); acceptsFirstMouse
        // and non-activating panels can't fix it. Polling the cursor location touches
        // nothing in the view tree and needs no special permission.
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            guard let self = self, let w = self.window, w.isVisible, !NSApp.isActive else {
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
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                        self?.tryLoad()
                    }
                }
            }
        }.resume()
    }

    // Prevent navigation away from the local server.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let host = action.request.url?.host, host == "127.0.0.1" {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    // Reload if the page errors (e.g. server restarted).
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.tryLoad()
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!,
                 withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.tryLoad()
        }
    }

    @objc func closeWindowAction() { window.close() }

    // MARK: - WKScriptMessageHandler

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.name == "close" { window.close() }
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

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    // Exit NOW, skipping Cocoa/WebKit teardown — deallocating WKWebView on the way
    // out is what sometimes crashes and summons macOS's "quit unexpectedly — send a
    // report?" dialog. There's no state worth a graceful shutdown.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        exit(0)
    }
}

// Die silently no matter how we're killed: a caught crash signal exits cleanly
// before ReportCrash can see it, so no report dialog even on a real crash.
for sig in [SIGTERM, SIGINT, SIGHUP, SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGTRAP, SIGFPE] {
    signal(sig) { _ in _exit(0) }
}

letHeldKeysRepeat()
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
