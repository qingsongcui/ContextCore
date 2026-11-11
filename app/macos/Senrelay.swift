import Cocoa
import WebKit
import Darwin

final class Desktop: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var retry: NSButton!
    var upgradeButton: NSButton!
    var actionStack: NSStackView!
    var service: Process?
    var connecting = false
    var recoveryStep = 0
    var recoveredFaults: [[String: Any]] = []
    let verificationDirectory: URL? = {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: "--verify-ui"), args.count > index + 1 else { return nil }
        return URL(fileURLWithPath: args[index + 1], isDirectory: true)
    }()
    let verifyScenario: String = {
        let args = CommandLine.arguments
        guard args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-scenario"), args.count > index + 1 else { return "normal" }
        return args[index + 1]
    }()
    let verifyDataDirectory: URL? = {
        let args = CommandLine.arguments
        guard args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-data-dir"), args.count > index + 1 else { return nil }
        return URL(fileURLWithPath: args[index + 1], isDirectory: true)
    }()
    let verifyHomeDirectory: URL? = {
        let args = CommandLine.arguments
        guard args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-home-dir"), args.count > index + 1 else { return nil }
        return URL(fileURLWithPath: args[index + 1], isDirectory: true)
    }()
    var verifySoakSeconds: TimeInterval {
        let args = CommandLine.arguments
        guard verificationDirectory != nil, let index = args.firstIndex(of: "--verify-soak-seconds"), args.count > index + 1,
              let seconds = Double(args[index + 1]), seconds.isFinite, seconds >= 1, seconds <= 259200 else { return 0 }
        return seconds
    }
    var verifySoakInterval: TimeInterval {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: "--verify-soak-interval"), args.count > index + 1,
              let seconds = Double(args[index + 1]), seconds.isFinite, seconds >= 1, seconds <= 60 else { return 60 }
        return seconds
    }
    var verifyServiceExecutableOverride: URL? = {
        let args = CommandLine.arguments
        guard args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-service-bin"), args.count > index + 1 else { return nil }
        return URL(fileURLWithPath: args[index + 1])
    }()
    static let maxStartupAttempts = 90
    var verifyMaxAttempts: Int = {
        let args = CommandLine.arguments
        guard args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-max-attempts"), args.count > index + 1, let count = Int(args[index + 1]), count >= 0 else { return Desktop.maxStartupAttempts }
        return count
    }()
    lazy var allowVerifyAutostart: Bool = {
        guard verificationDirectory != nil else { return false }
        let args = CommandLine.arguments
        return args.contains("--verify-autostart") || ["autostart", "service-start-failure-recover"].contains(verifyScenario)
    }()
    var autostartedService = false
    var upgradedService = false
    lazy var expectedRelease: [String: Any] = {
        guard let url = Bundle.main.url(forResource: "desktop-release", withExtension: "json"), let data = try? Data(contentsOf: url), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }; return value
    }()
    let origin: URL = {
        let args = CommandLine.arguments
        if args.contains("--verify-ui"), let index = args.firstIndex(of: "--verify-port"), args.count > index + 1, let port = Int(args[index + 1]), (1024...65535).contains(port) {
            return URL(string: "http://127.0.0.1:\(port)")!
        }
        return URL(string: "http://127.0.0.1:8420")!
    }()
    var serviceDataDir: URL {
        if let dir = verifyDataDirectory { return dir }
        if let verifyDir = verificationDirectory { return verifyDir.appendingPathComponent("data", isDirectory: true) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ContextCore", isDirectory: true)
    }
    var serviceHomeDir: URL {
        if let dir = verifyHomeDirectory { return dir }
        if let verifyDir = verificationDirectory { return verifyDir.appendingPathComponent("home", isDirectory: true) }
        return FileManager.default.homeDirectoryForCurrentUser
    }
    var serviceExecutable: URL {
        if verificationDirectory != nil, let override = verifyServiceExecutableOverride { return override }
        return Bundle.main.resourceURL!.appendingPathComponent("service/senrelay-service")
    }
    var serviceLogsDir: URL {
        if let verifyDir = verificationDirectory { return verifyDir.appendingPathComponent("logs", isDirectory: true) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Senrelay", isDirectory: true)
    }

    lazy var language: String = {
        if verificationDirectory != nil {
            let args = CommandLine.arguments
            if let index = args.firstIndex(of: "--verify-language"), args.count > index + 1, ["zh", "en"].contains(args[index + 1]) { return args[index + 1] }
            return "zh"
        }
        if let saved = UserDefaults.standard.string(forKey: "SenrelayLanguage"), ["zh", "en"].contains(saved) { return saved }
        return (Locale.preferredLanguages.first ?? "en").hasPrefix("zh") ? "zh" : "en"
    }()
    func text(_ zh: String, _ en: String) -> String { language == "zh" ? zh : en }
    func updateLanguage(_ next: String) {
        guard ["zh", "en"].contains(next), next != language else { return }
        language = next
        if verificationDirectory == nil { UserDefaults.standard.set(next, forKey: "SenrelayLanguage") }
        buildMenus()
        retry?.title = text("重新连接", "Reconnect")
        upgradeButton?.title = text("更新本机后台并重连", "Update Local Service & Reconnect")
    }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let source = message.frameInfo.securityOrigin
        guard message.name == "senrelayLanguage", message.frameInfo.isMainFrame,
              source.host == origin.host, source.port == origin.port, source.protocol == "http",
              let value = message.body as? String else { return }
        updateLanguage(value)
    }

    func buildMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: text("关于 Senrelay", "About Senrelay"), action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: text("退出 Senrelay", "Quit Senrelay"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: text("编辑", "Edit")); editItem.submenu = edit
        for (title, selector, key) in [(text("撤销", "Undo"), "undo:", "z"), (text("重做", "Redo"), "redo:", "Z"), (text("剪切", "Cut"), "cut:", "x"), (text("复制", "Copy"), "copy:", "c"), (text("粘贴", "Paste"), "paste:", "v"), (text("全选", "Select All"), "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        let viewItem = NSMenuItem(); menu.addItem(viewItem)
        let view = NSMenu(title: text("窗口", "Window")); viewItem.submenu = view
        view.addItem(NSMenuItem(title: text("关闭窗口", "Close Window"), action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        view.addItem(.separator())
        view.addItem(withTitle: text("重新连接", "Reconnect"), action: #selector(reconnect), keyEquivalent: "r")
        view.addItem(withTitle: text("更新本机后台并重连", "Update Local Service & Reconnect"), action: #selector(upgradeService), keyEquivalent: "u")
        view.addItem(withTitle: text("显示主窗口", "Show Main Window"), action: #selector(show), keyEquivalent: "0")
        NSApp.mainMenu = menu
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Senrelay · 续境"
        window.minSize = NSSize(width: 800, height: 560)
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("SenrelayMainWindow")
        let config = WKWebViewConfiguration()
        config.websiteDataStore = verificationDirectory == nil ? .default() : .nonPersistent()
        config.userContentController.add(self, name: "senrelayLanguage")
        config.userContentController.addUserScript(WKUserScript(source: """
          (() => {
            const send = () => window.webkit.messageHandlers.senrelayLanguage.postMessage(document.documentElement.lang.startsWith('zh') ? 'zh' : 'en');
            new MutationObserver(send).observe(document.documentElement, {attributes:true, attributeFilter:['lang']});
            send();
          })();
          """, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        web = WKWebView(frame: window.contentView!.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self; web.uiDelegate = self
        window.contentView!.addSubview(web)
        status = NSTextField(wrappingLabelWithString: text("正在连接本机服务…", "Connecting to your local service…"))
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(status)
        upgradeButton = NSButton(title: text("更新本机后台并重连", "Update Local Service & Reconnect"), target: self, action: #selector(upgradeService))
        upgradeButton.bezelStyle = .rounded; upgradeButton.keyEquivalent = "\r"; upgradeButton.isHidden = true; upgradeButton.translatesAutoresizingMaskIntoConstraints = false
        retry = NSButton(title: text("重新连接", "Reconnect"), target: self, action: #selector(reconnect))
        retry.bezelStyle = .rounded; retry.isHidden = true; retry.translatesAutoresizingMaskIntoConstraints = false
        actionStack = NSStackView(views: [upgradeButton, retry])
        actionStack.orientation = .horizontal
        actionStack.spacing = 12
        actionStack.alignment = .centerY
        actionStack.detachesHiddenViews = true
        actionStack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(actionStack)
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: window.contentView!.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor),
            status.widthAnchor.constraint(lessThanOrEqualToConstant: 560),
            actionStack.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 20),
            actionStack.centerXAnchor.constraint(equalTo: status.centerXAnchor)
        ])
        window.center(); if verificationDirectory == nil { show() }; reconnect()
    }
    @objc func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func about() {
        let version = Bundle.main.object(forInfoDictionaryKey: "SenrelayReleaseVersion") as? String
            ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Senrelay · 续境", .applicationVersion: version, .credits: NSAttributedString(string: text("项目、记忆与 Agent 设置保存在这台 Mac。\n关闭窗口或退出 App 不会停止后台服务。", "Projects, memory, and agent settings are stored on this Mac.\nClosing the window or quitting the app keeps the background service running."))])
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    @objc func reconnect() {
        guard !connecting else { return }
        connecting = true; status.isHidden = false; web.isHidden = true; retry.isHidden = true; upgradeButton.isHidden = true
        status.stringValue = text("正在连接本机服务…", "Connecting to your local service…")
        probe(attempt: 0, mayStart: true)
    }
    var localCommandTimeout: TimeInterval {
        let args = CommandLine.arguments
        guard verificationDirectory != nil,
              let index = args.firstIndex(of: "--verify-command-timeout"), args.count > index + 1,
              let seconds = Double(args[index + 1]), seconds.isFinite, seconds >= 1, seconds <= 90 else { return 90 }
        return seconds
    }
    func runLocalCommand(_ process: Process, timeout: TimeInterval) throws -> Bool {
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        try process.run()
        if exited.wait(timeout: .now() + timeout) == .success {
            return process.terminationStatus == 0
        }
        // Only this Process instance was launched here. Never signal a service
        // discovered on the shared port or a PID read from service metadata.
        if process.isRunning { process.terminate() }
        if exited.wait(timeout: .now() + 2) != .success {
            if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) }
            if exited.wait(timeout: .now() + 2) != .success {
                throw NSError(domain: "SenrelayLocalCommand", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "Timed-out helper could not be reaped"])
            }
        }
        throw NSError(domain: "SenrelayLocalCommand", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Local helper timed out"])
    }
    func isLocalPortOpen(_ port: Int) -> Bool {
        guard (1...65535).contains(port) else { return false }
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }
        let flags = Darwin.fcntl(fd, F_GETFL, 0)
        if flags >= 0 { _ = Darwin.fcntl(fd, F_SETFL, flags | O_NONBLOCK) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port)).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let connectResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if connectResult == 0 { return true }
        guard errno == EINPROGRESS else { return false }
        var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
        guard Darwin.poll(&pfd, 1, 250) > 0 else { return false }
        var soError: Int32 = 0
        var len = socklen_t(MemoryLayout<Int32>.size)
        guard Darwin.getsockopt(fd, SOL_SOCKET, SO_ERROR, &soError, &len) == 0 else { return false }
        return soError == 0
    }
    func captureCommandOutput(_ executable: String, _ arguments: [String], timeout: TimeInterval = 3) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = arguments
        proc.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = FileHandle.nullDevice
        let exited = DispatchSemaphore(value: 0)
        proc.terminationHandler = { _ in exited.signal() }
        do {
            try proc.run()
        } catch {
            return nil
        }
        if exited.wait(timeout: .now() + timeout) != .success {
            if proc.isRunning { proc.terminate() }
            if exited.wait(timeout: .now() + 1) != .success, proc.isRunning {
                Darwin.kill(proc.processIdentifier, SIGKILL)
            }
            return nil
        }
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)
    }
    func listeningPidsOnPort(_ port: Int) -> [pid_t] {
        guard let output = captureCommandOutput("/usr/sbin/lsof", ["-n", "-P", "-ti", "tcp:\(port)", "-sTCP:LISTEN"]) else { return [] }
        let selfPid = Darwin.getpid()
        var seen = Set<pid_t>()
        var result: [pid_t] = []
        for line in output.split(whereSeparator: \.isNewline) {
            if let pid = pid_t(line.trimmingCharacters(in: .whitespaces)), pid > 0, pid != selfPid, seen.insert(pid).inserted {
                result.append(pid)
            }
        }
        return result
    }
    func isOwnedContextCoreProcess(_ pid: pid_t) -> Bool {
        guard let uidText = captureCommandOutput("/bin/ps", ["-p", "\(pid)", "-o", "uid="]),
              let uid = UInt32(uidText.trimmingCharacters(in: .whitespacesAndNewlines)),
              uid == Darwin.getuid() else { return false }
        guard let commText = captureCommandOutput("/bin/ps", ["-p", "\(pid)", "-o", "comm="]) else { return false }
        let comm = commText.trimmingCharacters(in: .whitespacesAndNewlines)
        return ["Python", "python", "contextcore", "senrelay-service"].contains { comm.contains($0) }
    }
    @objc func upgradeService() {
        guard !connecting else { return }
        connecting = true; status.isHidden = false; web.isHidden = true; retry.isHidden = true; upgradeButton.isHidden = true
        status.stringValue = text("正在更新本机后台服务…", "Updating local background service…")
        let exe = serviceExecutable
        let dataPath = serviceDataDir.path
        let homePath = serviceHomeDir.path
        let targetPort = origin.port ?? 8420
        let commandTimeout = localCommandTimeout
        DispatchQueue.global(qos: .userInitiated).async {
            // A damaged candidate must not stop a healthy older service.
            let preflight = Process()
            preflight.executableURL = exe
            preflight.arguments = ["-m", "app.cli", "check-install"]
            preflight.environment = ["HOME": homePath, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
            preflight.standardOutput = FileHandle.nullDevice
            preflight.standardError = FileHandle.nullDevice
            do {
                guard try self.runLocalCommand(preflight, timeout: commandTimeout) else {
                    DispatchQueue.main.async {
                        self.fail(self.text("无法启动本机服务。请检查 App 是否完整，并查看本机服务日志。", "The local service could not start. Check that the app bundle is complete and review the local service log."), allowUpgrade: true)
                    }
                    return
                }
            } catch {
                DispatchQueue.main.async {
                    self.fail(self.text("配套后台检查失败或超时，原有后台未停止。请检查 App 是否完整，然后点击“重新连接”或重试更新。", "The bundled service check failed or timed out. The original service was not stopped. Check the app bundle, then reconnect or retry the update."), allowUpgrade: true)
                }
                return
            }
            let stop = Process()
            stop.executableURL = exe
            stop.arguments = ["-m", "app.cli", "stop", "--data-dir", dataPath]
            stop.environment = ["HOME": homePath, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
            stop.standardOutput = FileHandle.nullDevice
            stop.standardError = FileHandle.nullDevice
            var stopSucceeded = false
            do {
                stopSucceeded = try self.runLocalCommand(stop, timeout: commandTimeout)
            } catch {
                DispatchQueue.main.async {
                    self.fail(self.text("未能自动更新本机后台服务。请先停止旧后台或查看 ~/Library/Logs/Senrelay/service.log，再点击“重新连接”。", "Could not automatically update the local service. Stop the older service or check ~/Library/Logs/Senrelay/service.log, then click Reconnect."), allowUpgrade: true)
                }
                return
            }
            var portClearedBySignal = false
            if self.isLocalPortOpen(targetPort) {
                var signaledAny = false
                for pid in self.listeningPidsOnPort(targetPort) where self.isOwnedContextCoreProcess(pid) {
                    if Darwin.kill(pid, SIGTERM) == 0 {
                        signaledAny = true
                    }
                }
                if signaledAny {
                    let deadline = Date().addingTimeInterval(5.0)
                    while Date() < deadline && self.isLocalPortOpen(targetPort) {
                        Thread.sleep(forTimeInterval: 0.1)
                    }
                    if !self.isLocalPortOpen(targetPort) {
                        portClearedBySignal = true
                    }
                }
            }
            if self.isLocalPortOpen(targetPort) {
                let holders = self.listeningPidsOnPort(targetPort)
                let holderDesc = holders.isEmpty
                    ? self.text("未知或非当前用户进程", "an unknown or other-user process")
                    : "PID " + holders.map(String.init).joined(separator: ", ")
                DispatchQueue.main.async {
                    self.fail(self.text("本机端口 \(targetPort) 仍被其他进程占用（\(holderDesc)）。请先关闭占用该端口的程序后再点击“重新连接”。", "Local port \(targetPort) is still in use (\(holderDesc)). Stop the process holding port \(targetPort), then click Reconnect."), allowUpgrade: true)
                }
                return
            }
            DispatchQueue.main.async {
                guard stopSucceeded || portClearedBySignal else {
                    self.fail(self.text("未能自动更新本机后台服务。请先停止旧后台或查看 ~/Library/Logs/Senrelay/service.log，再点击“重新连接”。", "Could not automatically update the local service. Stop the older service or check ~/Library/Logs/Senrelay/service.log, then click Reconnect."), allowUpgrade: true)
                    return
                }
                do {
                    try self.startService()
                    self.upgradedService = true
                    self.probe(attempt: 0, mayStart: false)
                } catch {
                    self.fail(self.text("无法启动本机服务。请检查 App 是否完整，并查看本机服务日志。", "The local service could not start. Check that the app bundle is complete and review the local service log."), allowUpgrade: true)
                }
            }
        }
    }
    func probe(attempt: Int, mayStart: Bool) {
        var request = URLRequest(url: origin.appendingPathComponent("api/release"))
        request.timeoutInterval = 2; request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                if let data = data, let http = response as? HTTPURLResponse, http.statusCode == 200,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   json["api_contract"] as? Int == 2, let release = json["release_id"] as? String,
                   release == self.expectedRelease["release_id"] as? String,
                   let hash = json["frontend_sha256"] as? String, hash == self.expectedRelease["frontend_sha256"] as? String {
                    self.connecting = false
                    self.upgradeButton.isHidden = true
                    var destination = URLComponents(url: self.origin, resolvingAgainstBaseURL: false)!
                    if self.verificationDirectory != nil { destination.queryItems = [URLQueryItem(name: "lang", value: self.language)] }
                    self.web.load(URLRequest(url: destination.url!)); return
                }
                if let data = data, let http = response as? HTTPURLResponse, http.statusCode == 200,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   json["api_contract"] is Int, json["release_id"] is String {
                    self.fail(self.text("检测到本机正在运行旧版或其他版本的后台服务。点击“更新本机后台并重连”即可切换到当前 App 配套后台，已保存的项目与记忆数据不会丢失。", "An older or different local service is currently running. Click “Update Local Service & Reconnect” to switch to this app’s bundled service without losing your saved projects or memories."), allowUpgrade: true); return
                }
                if let http = response as? HTTPURLResponse {
                    if [502, 503, 504].contains(http.statusCode), self.service?.isRunning == true, attempt < self.verifyMaxAttempts {
                        let delay = self.verificationDirectory == nil ? 0.5 : 0.15
                        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.probe(attempt: attempt + 1, mayStart: false) }
                        return
                    }
                    if (500...599).contains(http.statusCode) {
                        self.fail(self.text("本机服务返回内部错误（HTTP \(http.statusCode)）。请查看 ~/Library/Logs/Senrelay/service.log，然后点击“重新连接”重试。", "The local service returned an internal error (HTTP \(http.statusCode)). Review ~/Library/Logs/Senrelay/service.log, then click Reconnect."), allowUpgrade: true); return
                    }
                    self.fail(self.text("本机端口被其他程序占用。请关闭冲突程序后点击“重新连接”；不会停止其他程序。", "Another program is using the local port. Close the conflicting program and click Reconnect. Other programs will not be stopped.")); return
                }
                if response != nil {
                    self.fail(self.text("本机端口被其他程序占用。请关闭冲突程序后点击“重新连接”；不会停止其他程序。", "Another program is using the local port. Close the conflicting program and click Reconnect. Other programs will not be stopped.")); return
                }
                var nextMayStart = mayStart
                let urlErrorCode = (error as? URLError)?.code
                let canAutostartOnError = urlErrorCode == .cannotConnectToHost || urlErrorCode == .timedOut || urlErrorCode == .networkConnectionLost
                if nextMayStart && (self.verificationDirectory == nil || self.allowVerifyAutostart) && canAutostartOnError {
                    if self.service?.isRunning != true {
                        do {
                            try self.startService()
                            self.autostartedService = true
                        } catch {
                            self.fail(self.text("无法启动本机服务。请检查 App 是否完整，并查看本机服务日志。", "The local service could not start. Check that the app bundle is complete and review the local service log.")); return
                        }
                    }
                    nextMayStart = false
                } else if nextMayStart && !(self.verificationDirectory == nil || self.allowVerifyAutostart) {
                    nextMayStart = false
                }
                guard attempt < self.verifyMaxAttempts else {
                    self.fail(self.text("本机服务未能就绪。请查看 ~/Library/Logs/Senrelay/service.log，然后点击“重新连接”重试。", "The local service did not become ready. Review ~/Library/Logs/Senrelay/service.log, then click Reconnect.")); return
                }
                let delay = self.verificationDirectory == nil ? 0.5 : 0.15
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.probe(attempt: attempt + 1, mayStart: nextMayStart) }
            }
        }.resume()
    }
    func startService() throws {
        let process = Process()
        process.executableURL = serviceExecutable
        try FileManager.default.createDirectory(at: serviceHomeDir, withIntermediateDirectories: true)
        var arguments = ["-m", "app.cli", "serve", "--data-dir", serviceDataDir.path]
        if verificationDirectory != nil, let port = origin.port {
            arguments += ["--port", "\(port)"]
        }
        process.arguments = arguments
        process.environment = ["HOME": serviceHomeDir.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        let logs = serviceLogsDir
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let file = logs.appendingPathComponent("service.log")
        if !FileManager.default.fileExists(atPath: file.path) { FileManager.default.createFile(atPath: file.path, contents: nil) }
        let handle = try FileHandle(forWritingTo: file); handle.seekToEndOfFile()
        process.standardOutput = handle; process.standardError = handle
        try process.run(); service = process
        try? handle.close()
    }
    func fail(_ message: String, allowUpgrade: Bool = false) {
        connecting = false
        status.stringValue = message
        status.isHidden = false
        web.isHidden = true
        retry.isHidden = false
        upgradeButton.isHidden = !allowUpgrade
        guard let directory = verificationDirectory else { return }
        let snapshot: [String: Any] = [
            "scenario": verifyScenario,
            "language": language,
            "native_language": language,
            "status_text": status.stringValue,
            "status_visible": !status.isHidden,
            "web_hidden": web.isHidden,
            "retry_visible": !retry.isHidden,
            "retry_title": retry.title,
            "upgrade_visible": !upgradeButton.isHidden,
            "upgrade_title": upgradeButton.title,
            "recovery_step": recoveryStep
        ]
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? JSONSerialization.data(withJSONObject: snapshot, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("error-state-\(recoveryStep).json"))
        try? JSONSerialization.data(withJSONObject: snapshot, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("error-state.json"))
        if handleVerifyFailureRecovery(snapshot: snapshot, allowUpgrade: allowUpgrade) {
            return
        }
        fputs(message + "\n", stderr)
        exit(1)
    }
    func handleVerifyFailureRecovery(snapshot: [String: Any], allowUpgrade: Bool) -> Bool {
        if verifyScenario == "actual-webkit-termination" && recoveryStep == 0 &&
            snapshot["status_text"] as? String == text("页面进程已退出。请重新连接，已保存的项目数据仍在本机。", "The workspace process stopped. Reconnect to reopen it. Saved project data remains on this Mac.") {
            recoveryStep += 1
            recoveredFaults.append(snapshot)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { self.retry.performClick(nil) }
            return true
        }
        if verifyScenario == "version-mismatch-recover" && recoveryStep == 0 && allowUpgrade {
            recoveryStep += 1
            recoveredFaults.append(snapshot)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.upgradeButton.performClick(nil) }
            return true
        }
        if verifyScenario == "service-start-failure-recover" && recoveryStep == 0 {
            recoveryStep += 1
            recoveredFaults.append(snapshot)
            verifyServiceExecutableOverride = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.retry.performClick(nil) }
            return true
        }
        if verifyScenario == "unreachable-port-recover" && recoveryStep == 0 {
            recoveryStep += 1
            recoveredFaults.append(snapshot)
            allowVerifyAutostart = true
            verifyMaxAttempts = Desktop.maxStartupAttempts
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.retry.performClick(nil) }
            return true
        }
        if verifyScenario == "normal" && recoveryStep < 3 {
            recoveryStep += 1
            recoveredFaults.append(snapshot)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.retry.performClick(nil) }
            return true
        }
        return false
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        status.isHidden = true; retry.isHidden = true; upgradeButton.isHidden = true; web.isHidden = false
        guard verificationDirectory != nil else { return }
        if verifyScenario == "normal" && recoveryStep < 3 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                if self.recoveryStep == 0 {
                    self.webView(self.web, didFailProvisionalNavigation: nil, withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotConnectToHost))
                } else if self.recoveryStep == 1 {
                    self.webView(self.web, didFail: nil, withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorNetworkConnectionLost))
                } else if self.recoveryStep == 2 {
                    self.webViewWebContentProcessDidTerminate(self.web)
                }
            }
            return
        }
        verifyRenderedUI(attempt: 0)
    }
    func verifyRenderedUI(attempt: Int) {
        let script = """
        const nav = [...document.querySelectorAll('.main-nav button')];
        const controls = [...document.querySelectorAll('.sidebar-controls > *')];
        const sidebar = document.querySelector('.sidebar');
        const serviceOnline = Boolean(document.querySelector('.local-status .status-dot.online'));
        if (!sidebar || nav.length !== 3 || nav.some(x => x.disabled) || !serviceOnline) return null;
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const waitPage = async () => {
          for (let i = 0; i < 60; i++) {
            const busy = document.querySelector('.skeleton-stack');
            const heading = document.querySelector('.page-surface h1, .page-surface h2');
            if (!busy && heading && heading.textContent.trim().length > 0) return heading.textContent.trim();
            await sleep(50);
          }
          return '';
        };
        const pagesVisited = [];
        for (const index of [0, 1, 2]) {
          const btn = document.querySelectorAll('.main-nav button')[index];
          btn.click();
          await sleep(40);
          const heading = await waitPage();
          const page = new URLSearchParams(location.search).get('page') || 'projects';
          pagesVisited.push({page, heading, hasErrorBanner: Boolean(document.querySelector('.service-banner'))});
        }
        const settingsBtn = document.querySelector('.sidebar-controls > button');
        if (settingsBtn) {
          settingsBtn.click();
          await sleep(40);
          const heading = await waitPage();
          const page = new URLSearchParams(location.search).get('page') || 'settings';
          pagesVisited.push({page, heading, hasErrorBanner: Boolean(document.querySelector('.service-banner'))});
        }
        document.querySelectorAll('.main-nav button')[0].click();
        await sleep(40);
        await waitPage();
        const themeBtns = [...document.querySelectorAll('.theme-switch button')];
        let themeSwitchOk = false;
        if (themeBtns.length === 3) {
          themeBtns[1].click();
          await sleep(40);
          const darkOk = document.documentElement.classList.contains('dark') && themeBtns[1].getAttribute('aria-pressed') === 'true';
          themeBtns[0].click();
          await sleep(40);
          const lightOk = document.documentElement.classList.contains('light') && themeBtns[0].getAttribute('aria-pressed') === 'true';
          themeBtns[2].click();
          await sleep(40);
          const systemOk = themeBtns[2].getAttribute('aria-pressed') === 'true';
          themeSwitchOk = darkOk && lightOk && systemOk;
        }
        const navAfter = [...document.querySelectorAll('.main-nav button')];
        const controlsAfter = [...document.querySelectorAll('.sidebar-controls > *')];
        const rects = controlsAfter.map(x => {const r=x.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}});
        const names = navAfter.map(x=>x.querySelector('span').textContent);
        const labels = navAfter.map(x => {
          const span=x.querySelector('span'), r=span.getBoundingClientRect(), range=document.createRange();
          range.selectNodeContents(span);
          return {name:span.textContent,height:r.height,width:r.width,textWidth:range.getBoundingClientRect().width,lines:range.getClientRects().length,rowHeight:x.getBoundingClientRect().height};
        });
        const language = document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
        const expectedPages = ['projects','agents','usage','settings'];
        const navHeight = navAfter[0].getBoundingClientRect().height;
        const allPagesOk = JSON.stringify(pagesVisited.map(p=>p.page)) === JSON.stringify(expectedPages) && pagesVisited.every(p => p.heading.length > 0 && !p.hasErrorBanner);
        const sidebarBounds = sidebar.getBoundingClientRect();
        const footerFits = rects.every((r,i)=>r.height>=24 && r.width>=24 && r.x>=sidebarBounds.x && r.x+r.width<=sidebarBounds.right+1 && r.y>=0 && r.y+r.height<=innerHeight && (i===0 || r.x>=rects[i-1].x+rects[i-1].width));
        const navigationFits = labels.every(x=>x.rowHeight>=24 && x.height<=x.rowHeight && x.lines===1 && x.textWidth<=x.width+1);
        const labelsLocalized = names.every(name=>name.trim().length>0 && (language!=='en' || !/[一-鿿]/.test(name))) && new Set(names).size===3;
        const ok = labelsLocalized && controlsAfter.length === 3 && Math.max(...rects.map(x=>x.y))-Math.min(...rects.map(x=>x.y)) < 1 && footerFits && navigationFits && !document.querySelector('.secondary-nav') && allPagesOk && themeSwitchOk;
        return {ok,names,labels,language,serviceOnline,serviceLabel:document.querySelector('.local-status').textContent.trim(),controls:rects,navHeight,sidebarWidth:sidebar.getBoundingClientRect().width,viewport:{width:innerWidth,height:innerHeight},url:location.href,pagesVisited,themeSwitchOk};
        """
        web.callAsyncJavaScript(script, arguments: [:], in: nil, in: .page) { outcome in
            let value = try? outcome.get()
            guard var result = value as? [String: Any] else {
                if attempt < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.verifyRenderedUI(attempt: attempt + 1) }; return }
                self.fail("桌面界面验证超时"); return
            }
            let initialLang = self.language
            result["native_language"] = initialLang
            result["native_menu_titles"] = NSApp.mainMenu?.items.compactMap { $0.submenu?.title }
            result["native_about_title"] = NSApp.mainMenu?.items.first?.submenu?.items.first?.title
            result["native_retry_title"] = self.retry.title
            result["native_upgrade_title"] = self.upgradeButton.title
            result["scenario"] = self.verifyScenario
            result["autostarted_service"] = self.autostartedService
            result["upgraded_service"] = self.upgradedService
            result["service_pid"] = self.service?.processIdentifier ?? 0
            result["recovered_faults"] = self.recoveredFaults
            guard result["language"] as? String == initialLang else { self.fail("Native menu language did not follow the workspace"); return }
            self.verifyResizeAndLanguageSync(initialLang: initialLang, baseResult: result)
        }
    }
    func verifyResizeAndLanguageSync(initialLang: String, baseResult: [String: Any]) {
        var result = baseResult
        window.setContentSize(NSSize(width: 800, height: 560))
        window.layoutIfNeeded()
        let compactSize = ["width": Int(window.contentView!.bounds.width), "height": Int(window.contentView!.bounds.height)]
        window.setContentSize(NSSize(width: 1080, height: 700))
        window.layoutIfNeeded()
        let mediumSize = ["width": Int(window.contentView!.bounds.width), "height": Int(window.contentView!.bounds.height)]
        window.setContentSize(NSSize(width: 1240, height: 820))
        window.layoutIfNeeded()
        let restoredSize = ["width": Int(window.contentView!.bounds.width), "height": Int(window.contentView!.bounds.height)]
        result["window_resize_checks"] = ["min_800x560": compactSize, "medium_1080x700": mediumSize, "restored_1240x820": restoredSize]

        let targetLang = initialLang == "zh" ? "en" : "zh"
        let toggleScript = """
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const buttons = [...document.querySelectorAll('.sidebar .language-switch button')];
        const target = buttons.find(b => b.getAttribute('aria-pressed') === 'false');
        if (target) target.click();
        await sleep(120);
        return document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
        """
        web.callAsyncJavaScript(toggleScript, arguments: [:], in: nil, in: .page) { toggledOutcome in
            let toggledWebLang = (try? toggledOutcome.get()) as? String ?? ""
            let toggledNativeLang = self.language
            let toggledMenus = NSApp.mainMenu?.items.compactMap { $0.submenu?.title } ?? []
            let toggledRetry = self.retry.title
            let toggledUpgrade = self.upgradeButton.title
            let restoreScript = """
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const buttons = [...document.querySelectorAll('.sidebar .language-switch button')];
            const target = buttons.find(b => b.getAttribute('aria-pressed') === 'false');
            if (target) target.click();
            await sleep(120);
            return document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
            """
            self.web.callAsyncJavaScript(restoreScript, arguments: [:], in: nil, in: .page) { restoredOutcome in
                let restoredWebLang = (try? restoredOutcome.get()) as? String ?? ""
                let syncOk = (toggledWebLang == targetLang && toggledNativeLang == targetLang && restoredWebLang == initialLang && self.language == initialLang)
                result["live_language_sync"] = [
                    "ok": syncOk,
                    "toggled_to": toggledNativeLang,
                    "toggled_menu_titles": toggledMenus,
                    "toggled_retry_title": toggledRetry,
                    "toggled_upgrade_title": toggledUpgrade,
                    "restored_to": self.language
                ]
                guard syncOk else { self.fail("Live language sync between WKWebView and native AppKit controls failed"); return }
                let directory = self.verificationDirectory!
                do {
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                    try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("native-ui.json"))
                } catch { self.fail(error.localizedDescription); return }
                guard result["ok"] as? Bool == true else { self.fail("桌面导航或底部布局与参考截图不一致"); return }
                let config = WKSnapshotConfiguration()
                config.afterScreenUpdates = true
                config.rect = NSRect(x: 0, y: 0, width: 240, height: self.web.bounds.height)
                self.web.takeSnapshot(with: config) { image, error in
                    guard let image = image, let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) else { self.fail("无法保存桌面渲染截图"); return }
                    do { try png.write(to: directory.appendingPathComponent("native-sidebar.png")) } catch { self.fail(error.localizedDescription); return }
                    print("Native UI verification passed")
                    if self.verifyScenario == "actual-service-interruption" && self.recoveryStep == 0 {
                        self.verifyServiceInterruption(directory: directory)
                        return
                    }
                    if self.verifyScenario == "actual-webkit-termination" && self.recoveryStep == 0 {
                        do { try Data("ready\n".utf8).write(to: directory.appendingPathComponent("webkit-termination-ready"), options: .atomic) }
                        catch { self.fail(error.localizedDescription); return }
                        DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                            if self.recoveryStep == 0 { self.fail("Actual WebKit termination was not observed") }
                        }
                        return
                    }
                    if self.verifySoakSeconds > 0 {
                        self.observeVerification(started: Date(), previous: Date(), cycles: 0, gaps: 0)
                    } else { NSApp.terminate(nil) }
                }
            }
        }
    }
    func verifyServiceInterruption(directory: URL) {
        do { try Data("ready\n".utf8).write(to: directory.appendingPathComponent("service-interruption-ready"), options: .atomic) }
        catch { fail(error.localizedDescription); return }
        let offlineScript = """
        for (let n = 0; n < 150; n++) {
          await new Promise(resolve => setTimeout(resolve, 100));
          const notice = document.querySelector('.connection-note, .service-banner');
          if (notice && notice.textContent.trim() && !document.querySelector('.local-status .status-dot.online')) {
            try { const r = await fetch('/health'); if (r.ok) continue; } catch {}
            return {notice: notice.textContent.trim(), retryAvailable: Boolean(notice.querySelector('button')), footer: document.querySelector('.local-status')?.textContent.trim(), staleOnlineIndicator: false};
          }
        }
        throw new Error('No actionable disconnected state');
        """
        web.callAsyncJavaScript(offlineScript, arguments: [:], in: nil, in: .page) { outcome in
            guard let receipt = (try? outcome.get()) as? [String: Any], receipt["retryAvailable"] as? Bool == true else {
                self.fail("Service interruption was not shown with a recovery action"); return
            }
            do { try JSONSerialization.data(withJSONObject: receipt).write(to: directory.appendingPathComponent("service-offline.json"), options: .atomic) }
            catch { self.fail(error.localizedDescription); return }
            let onlineScript = """
            for (let n = 0; n < 200; n++) {
              await new Promise(resolve => setTimeout(resolve, 100));
              try {
                const health = await fetch('/health');
                if (!health.ok || !(await health.json()).ok) continue;
                document.querySelector('.connection-note button, .service-banner button')?.click();
                await new Promise(resolve => setTimeout(resolve, 150));
                if (!document.querySelector('.connection-note, .service-banner') && document.querySelector('.local-status .status-dot.online')) return true;
              } catch {}
            }
            throw new Error('Service did not recover');
            """
            self.web.callAsyncJavaScript(onlineScript, arguments: [:], in: nil, in: .page) { outcome in
                guard (try? outcome.get()) as? Bool == true else { self.fail("Service reconnection failed"); return }
                self.recoveryStep = 1
                do { try Data("recovered\n".utf8).write(to: directory.appendingPathComponent("service-recovered"), options: .atomic) }
                catch { self.fail(error.localizedDescription); return }
                self.verifyRenderedUI(attempt: 0)
            }
        }
    }
    func observeVerification(started: Date, previous: Date, cycles: Int, gaps: Int) {
        guard let directory = verificationDirectory else { return }
        let now = Date()
        let elapsed = now.timeIntervalSince(started)
        let gapCount = gaps + (now.timeIntervalSince(previous) > max(10, verifySoakInterval * 3) ? 1 : 0)
        let complete = elapsed >= verifySoakSeconds
        let status = complete ? (gapCount == 0 ? "completed" : "completed_with_gaps") : "running"
        let receipt: [String: Any] = ["status": status, "cycles": cycles, "elapsed_seconds": elapsed,
            "requested_seconds": verifySoakSeconds, "observation_gaps": gapCount,
            "continuous_24h_observed": complete && gapCount == 0 && verifySoakSeconds >= 86400,
            "release_id": expectedRelease["release_id"] as? String ?? "", "hidden": true]
        do {
            try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
                .write(to: directory.appendingPathComponent("native-soak.json"), options: .atomic)
        } catch { fail(error.localizedDescription); return }
        if complete { NSApp.terminate(nil); return }
        let script = """
        const pages = ['projects','agents','usage','settings'];
        const index = cycle % pages.length;
        const button = index === 3 ? document.querySelector('.sidebar-controls > button') : document.querySelectorAll('.main-nav button')[index];
        if (!button || button.disabled) throw new Error('Navigation unavailable');
        button.click();
        for (let n = 0; n < 60; n++) {
          await new Promise(resolve => setTimeout(resolve, 50));
          const current = new URLSearchParams(location.search).get('page') || 'projects';
          const heading = document.querySelector('.page-surface h1, .page-surface h2');
          if (current === pages[index] && heading?.textContent.trim() && !document.querySelector('.skeleton-stack')) {
            const health = await fetch('/health');
            if (!health.ok || !(await health.json()).ok || document.querySelector('.service-banner, .inline-error')) throw new Error('Page/service failed');
            return {page: current, heading: heading.textContent.trim(), serviceOnline: Boolean(document.querySelector('.local-status .status-dot.online'))};
          }
        }
        throw new Error('Page did not become ready');
        """
        web.callAsyncJavaScript(script, arguments: ["cycle": cycles], in: nil, in: .page) { outcome in
            guard let observed = (try? outcome.get()) as? [String: Any], observed["serviceOnline"] as? Bool == true else {
                self.fail("Hidden native stability page check failed"); return
            }
            do {
                let path = directory.appendingPathComponent("native-soak-pages.jsonl")
                if !FileManager.default.fileExists(atPath: path.path) { FileManager.default.createFile(atPath: path.path, contents: nil) }
                let handle = try FileHandle(forWritingTo: path)
                defer { try? handle.close() }
                try handle.seekToEnd()
                var line = try JSONSerialization.data(withJSONObject: ["cycle": cycles, "elapsed_seconds": elapsed, "page": observed])
                line.append(0x0a)
                try handle.write(contentsOf: line)
            } catch { self.fail(error.localizedDescription); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + self.verifySoakInterval) {
                self.observeVerification(started: started, previous: now, cycles: cycles + 1, gaps: gapCount)
            }
        }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { if (error as NSError).code != NSURLErrorCancelled { fail(text("页面加载失败，请点击“重新连接”重试。", "The workspace could not load. Click Reconnect to retry.")) } }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { if (error as NSError).code != NSURLErrorCancelled { fail(text("页面加载中断，请点击“重新连接”重试。", "The workspace stopped loading. Click Reconnect to retry.")) } }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { fail(text("页面进程已退出。请重新连接，已保存的项目数据仍在本机。", "The workspace process stopped. Reconnect to reopen it. Saved project data remains on this Mac.")) }
    func internalURL(_ url: URL) -> Bool { url.scheme == "http" && url.host == "127.0.0.1" && url.port == origin.port }
    func external(_ url: URL) { if ["https", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) } }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if internalURL(url) || url.scheme == "blob" {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else { if action.navigationType == .linkActivated { external(url) }; decisionHandler(.cancel) }
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { if internalURL(url) { webView.load(navigationAction.request) } else { external(url) } }; return nil
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: text("确认", "Confirm")); alert.addButton(withTitle: text("取消", "Cancel"))
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        if verificationDirectory != nil { fail("Download failed during native verification"); return }
        let alert = NSAlert(); alert.messageText = text("下载未完成", "Download did not finish")
        alert.informativeText = text("请检查保存位置和本机服务，再重新导出。", "Check the save location and local service, then export again.")
        alert.beginSheetModal(for: window) { _ in }
    }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.url : nil) }
    }
}
let app = NSApplication.shared
let delegate = Desktop()
app.delegate = delegate
app.setActivationPolicy(CommandLine.arguments.contains("--verify-ui") ? .prohibited : .regular)
app.run()
