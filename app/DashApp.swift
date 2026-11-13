// A native Mac window around a local web dashboard.
//
// Reads app.json from the bundle: the dashboard URL, and the command that starts its
// server. If nothing answers at the URL, the app runs that command in the repo
// (DashRepoRoot in Info.plist, set by build.sh), waits for it, and stops it again on
// quit. A server that was already running (launchd, a terminal) is left alone.

import Cocoa
import WebKit

struct Config: Decodable {
    let name: String
    let url: String
    let start: String?       // shell command, run with zsh -lc from `cwd`
    let cwd: String?         // relative to the repo root
    let bootSeconds: Double?
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var cfg: Config!
    var home: URL!
    var repo = ""
    var window: NSWindow!
    var web: WKWebView!
    var overlay: NSStackView!
    var spinner: NSProgressIndicator!
    var status: NSTextField!
    var retry: NSButton!
    var server: Process?
    var booting = false
    var logURL: URL!

    // MARK: launch

    func applicationDidFinishLaunching(_ n: Notification) {
        guard let u = Bundle.main.url(forResource: "app", withExtension: "json"),
              let d = try? Data(contentsOf: u),
              let c = try? JSONDecoder().decode(Config.self, from: d),
              let h = URL(string: c.url) else {
            fatal("app.json is missing or invalid."); return
        }
        cfg = c; home = h
        repo = Bundle.main.object(forInfoDictionaryKey: "DashRepoRoot") as? String ?? ""
        let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs")
        logURL = logs.appendingPathComponent("\(c.name)-app.log")
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
        boot()
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }

    func windowShouldClose(_ s: NSWindow) -> Bool { window.orderOut(nil); return false }

    func applicationWillTerminate(_ n: Notification) {
        guard let p = server, p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    // MARK: server

    func ping(_ done: @escaping (Bool) -> Void) {
        var r = URLRequest(url: home, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1.5)
        r.httpMethod = "GET"
        URLSession.shared.dataTask(with: r) { _, resp, _ in
            DispatchQueue.main.async { done(resp is HTTPURLResponse) }
        }.resume()
    }

    func boot() {
        if booting { return }
        booting = true
        show("Connecting to \(cfg.name)…", spinning: true)
        ping { up in
            if up { self.booting = false; self.loadHome(); return }
            guard let cmd = self.cfg.start else {
                self.booting = false
                self.show("Nothing is answering at \(self.cfg.url).", spinning: false); return
            }
            if self.server?.isRunning != true { self.startServer(cmd) }
            self.show("Starting \(self.cfg.name)…", spinning: true)
            self.wait(until: Date().addingTimeInterval(self.cfg.bootSeconds ?? 90))
        }
    }

    func startServer(_ cmd: String) {
        let dir = (repo as NSString).appendingPathComponent(cfg.cwd ?? ".")
        guard FileManager.default.fileExists(atPath: dir) else {
            show("Can't find the project at \(dir). Rebuild the app from the repo (app/build.sh).", spinning: false)
            return
        }
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try? FileHandle(forWritingTo: logURL)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "cd \(shq(dir)) && exec \(cmd)"]
        p.standardOutput = log; p.standardError = log
        p.standardInput = FileHandle.nullDevice
        do { try p.run(); server = p } catch {
            show("Couldn't start the server: \(error.localizedDescription)", spinning: false)
        }
    }

    func wait(until deadline: Date) {
        if let p = server, !p.isRunning {
            booting = false
            show("The server stopped while starting (exit \(p.terminationStatus)).\n\n\(logTail())", spinning: false)
            return
        }
        if Date() > deadline {
            booting = false
            show("\(cfg.name) didn't answer within \(Int(cfg.bootSeconds ?? 90))s.\n\n\(logTail())", spinning: false)
            return
        }
        ping { up in
            if up { self.booting = false; self.loadHome() }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.wait(until: deadline) } }
        }
    }

    func logTail() -> String {
        guard let s = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        let lines = s.split(separator: "\n", omittingEmptySubsequences: false).suffix(12)
        return lines.joined(separator: "\n") + "\n\nFull log: \(logURL.path)"
    }

    func shq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    // MARK: window

    func buildWindow() {
        let conf = WKWebViewConfiguration()
        conf.websiteDataStore = .default()
        conf.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: .zero, configuration: conf)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        web.allowsMagnification = true
        if #available(macOS 13.3, *) { web.isInspectable = true }
        web.isHidden = true

        spinner = NSProgressIndicator()
        spinner.style = .spinning; spinner.controlSize = .regular
        status = NSTextField(wrappingLabelWithString: "")
        status.alignment = .center
        status.font = .systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.preferredMaxLayoutWidth = 640
        status.isSelectable = true
        retry = NSButton(title: "Try again", target: self, action: #selector(reload(_:)))
        retry.bezelStyle = .rounded
        overlay = NSStackView(views: [spinner, status, retry])
        overlay.orientation = .vertical; overlay.spacing = 14
        overlay.translatesAutoresizingMaskIntoConstraints = false

        let root = NSView()
        web.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(web); root.addSubview(overlay)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.topAnchor.constraint(equalTo: root.topAnchor),
            web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            overlay.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            overlay.centerYAnchor.constraint(equalTo: root.centerYAnchor),
            overlay.widthAnchor.constraint(lessThanOrEqualToConstant: 680),
        ])

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = cfg.name
        window.contentView = root
        window.delegate = self
        window.minSize = NSSize(width: 720, height: 480)
        window.center()
        window.setFrameAutosaveName("main")
        window.tabbingMode = .disallowed
        window.makeKeyAndOrderFront(nil)
    }

    func show(_ msg: String, spinning: Bool) {
        status.stringValue = msg
        spinner.isHidden = !spinning
        spinning ? spinner.startAnimation(nil) : spinner.stopAnimation(nil)
        retry.isHidden = spinning
        overlay.isHidden = false
        web.isHidden = true
    }

    func loadHome() {
        web.load(URLRequest(url: home))
    }

    func isLocal(_ u: URL?) -> Bool {
        guard let u = u else { return true }
        if u.scheme == "about" || u.scheme == "blob" || u.scheme == "data" { return true }
        return u.host == home.host && u.port == home.port
    }

    // MARK: menus

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let m = NSMenu(title: title); items.forEach(m.addItem)
            let it = NSMenuItem(); it.submenu = m; main.addItem(it)
        }
        func item(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; return i
        }
        let n = cfg.name
        sub(n, [item("About \(n)", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
                .separator(),
                item("Hide \(n)", #selector(NSApplication.hide(_:)), "h"),
                item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
                item("Show All", #selector(NSApplication.unhideAllApplications(_:)), ""),
                .separator(),
                item("Quit \(n)", #selector(NSApplication.terminate(_:)), "q")])
        sub("Edit", [item("Undo", Selector(("undo:")), "z"),
                     item("Redo", Selector(("redo:")), "z", [.command, .shift]),
                     .separator(),
                     item("Cut", #selector(NSText.cut(_:)), "x"),
                     item("Copy", #selector(NSText.copy(_:)), "c"),
                     item("Paste", #selector(NSText.paste(_:)), "v"),
                     item("Select All", #selector(NSText.selectAll(_:)), "a")])
        sub("View", [item("Reload", #selector(reload(_:)), "r"),
                     item("Home", #selector(goHome(_:)), "h", [.command, .shift]),
                     item("Back", #selector(goBack(_:)), "["),
                     item("Forward", #selector(goForward(_:)), "]"),
                     .separator(),
                     item("Actual Size", #selector(zoomReset(_:)), "0"),
                     item("Zoom In", #selector(zoomIn(_:)), "="),
                     item("Zoom Out", #selector(zoomOut(_:)), "-"),
                     .separator(),
                     item("Open in Browser", #selector(openInBrowser(_:)), "o", [.command, .shift]),
                     item("Show Server Log", #selector(openLog(_:)), "l", [.command, .shift]),
                     item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])])
        sub("Window", [item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
                       item("Zoom", #selector(NSWindow.performZoom(_:)), ""),
                       item("Close", #selector(NSWindow.performClose(_:)), "w"),
                       .separator(),
                       item("Show \(n)", #selector(showMain(_:)), "1")])
        NSApp.mainMenu = main
    }

    @objc func reload(_ s: Any?) {
        if web.isHidden || web.url == nil { boot() } else { web.reload() }
    }
    @objc func goHome(_ s: Any?) { loadHome() }
    @objc func goBack(_ s: Any?) { web.goBack() }
    @objc func goForward(_ s: Any?) { web.goForward() }
    @objc func zoomReset(_ s: Any?) { web.pageZoom = 1 }
    @objc func zoomIn(_ s: Any?) { web.pageZoom = min(web.pageZoom + 0.1, 3) }
    @objc func zoomOut(_ s: Any?) { web.pageZoom = max(web.pageZoom - 0.1, 0.3) }
    @objc func openInBrowser(_ s: Any?) { NSWorkspace.shared.open(web.url ?? home) }
    @objc func openLog(_ s: Any?) {
        if FileManager.default.fileExists(atPath: logURL.path) { NSWorkspace.shared.open(logURL) }
    }
    @objc func showMain(_ s: Any?) { window.makeKeyAndOrderFront(nil) }

    // MARK: navigation

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if a.shouldPerformDownload { decisionHandler(.download); return }
        if !isLocal(a.request.url), a.targetFrame?.isMainFrame ?? true,
           let u = a.request.url, ["http", "https", "mailto"].contains(u.scheme ?? "") {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disp = (r.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if !r.canShowMIMEType || disp.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
        } else { decisionHandler(.allow) }
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        overlay.isHidden = true; web.isHidden = false
        spinner.stopAnimation(nil)
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        let code = (e as NSError).code
        if code == NSURLErrorCancelled || code == 102 { return }  // 102: frame load interrupted (download)
        boot()  // server went away: restart it (or say why not)
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { w.reload() }

    // window.open / target=_blank
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration,
                 for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url {
            if isLocal(u) { w.load(a.request) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }

    // MARK: JS dialogs, file pickers

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = cfg.name; a.informativeText = m
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = cfg.name; a.informativeText = m
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText d: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = cfg.name; a.informativeText = p
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = d ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let o = NSOpenPanel()
        o.allowsMultipleSelection = p.allowsMultipleSelection
        o.canChooseDirectories = p.allowsDirectories
        o.canChooseFiles = true
        o.beginSheetModal(for: window) { completionHandler($0 == .OK ? o.urls : nil) }
    }

    // MARK: downloads → ~/Downloads

    func webView(_ w: WKWebView, navigationAction a: WKNavigationAction, didBecome d: WKDownload) { d.delegate = self }
    func webView(_ w: WKWebView, navigationResponse r: WKNavigationResponse, didBecome d: WKDownload) { d.delegate = self }

    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename name: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let base = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var url = dir.appendingPathComponent(name), i = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = dir.appendingPathComponent(ext.isEmpty ? "\(base) \(i)" : "\(base) \(i).\(ext)"); i += 1
        }
        completionHandler(url)
    }

    func downloadDidFinish(_ d: WKDownload) {
        if let u = d.progress.fileURL {
            DistributedNotificationCenter.default().post(name: .init("com.apple.DownloadFileFinished"), object: u.path)
        }
    }

    func download(_ d: WKDownload, didFailWithError e: Error, resumeData: Data?) {
        let a = NSAlert(); a.messageText = "Download failed"; a.informativeText = e.localizedDescription
        a.beginSheetModal(for: window)
    }

    func fatal(_ m: String) {
        let a = NSAlert(); a.messageText = "Can't start"; a.informativeText = m; a.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
