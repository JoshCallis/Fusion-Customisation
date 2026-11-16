// FusionBrowser — minimal WKWebView window for the custom component browser.
//
// Points at http://127.0.0.1:8732 (served by the Fusion Browser add-in).
// Retries until the server is up. Window position is remembered across sessions.

import Cocoa
import WebKit
import CoreGraphics

let PORT = 8732
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
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // JS posts to window.webkit.messageHandlers.close to toggle the window shut
        // when the open-shortcut is pressed while the window has focus.
        config.userContentController.add(self, name: "close")

        webView = WKWebView(frame: .zero, configuration: config)

        followUIZoom { [unowned self] in [self.webView] }   // WindowShell § UI zoom
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 12.0, *) { webView.underPageBackgroundColor = .black }
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.black.cgColor

        let style: NSWindow.StyleMask = [.titled, .closable, .resizable, .miniaturizable]
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 760),
            styleMask: style,
            backing: .buffered,
            defer: false)
        window.title = "Browser"
        window.contentView = webView

        // The appearance is deliberately NOT pinned: setting window.appearance also pins
        // the WKWebView's prefers-color-scheme, and the page's palette follows the
        // system through that (fusion_ui/theme.css, both appearances black).
        window.backgroundColor = NSColor.black
        window.level = .floating
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.standardWindowButton(.closeButton)?.isHidden = true
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true

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

        window.setFrameAutosaveName("FusionBrowserMain")
        window.makeKeyAndOrderFront(nil)
        if UserDefaults.standard.object(forKey: "NSWindow Frame FusionBrowserMain") == nil {
            window.center()
        }
        tryLoad()

        // Focus-follows-mouse: when the cursor is over this window, activate the app
        // so a single click selects an item directly — no preceding "activate the
        // window" click. WKWebView won't deliver a click to the page while the app is
        // inactive (the content is in another process, gated on NSApp being active),
        // and acceptsFirstMouse / non-activating panels / tracking areas can't fix it
        // (the latter even break click delivery). Polling the cursor location touches
        // nothing in the view tree and needs no special permission; by the time you
        // click (human reaction >> 80 ms) the app is already active.
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

        // WebKit swallows Cmd+E ("Use Selection for Find") and Cmd+R reloads the
        // page, so those never reach the JS keydown handler. Intercept them here
        // and forward to the page; return nil to consume the event.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self else { return event }
            guard event.modifierFlags.contains(.command) else { return event }
            let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
            if chars == "e" {
                self.webView.evaluateJavaScript("window.__toggleVis && window.__toggleVis()")
                return nil
            }
            if chars == "r" {
                self.webView.evaluateJavaScript("window.__rename && window.__rename()")
                return nil
            }
            return event
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

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let host = action.request.url?.host, host == "127.0.0.1" {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!,
                 withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.tryLoad() }
    }

    @objc func closeWindowAction() { window.close() }

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.name == "close" { window.close() }
    }

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

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { return true }

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
