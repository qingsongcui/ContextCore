import Cocoa
import WebKit

/// Menus only request local readbacks. Every configuration or account change opens
/// the same workbench screen that performs its preview, confirmation and readback.
struct WorkbenchDestination: Equatable {
    let page: String
    var agent: String? = nil
    var section: String? = nil

    var queryItems: [URLQueryItem] {
        var result = [URLQueryItem(name: "page", value: page)]
        if let agent { result.append(URLQueryItem(name: "agent", value: agent)) }
        if let section { result.append(URLQueryItem(name: "section", value: section)) }
        return result
    }
}

extension WKWebView {
    func readWorkbenchRecord(_ path: String, origin: URL, completion: @escaping (Any?) -> Void) {
        let script = """
        if (location.origin !== origin) return null;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 5000);
        try {
            const response = await fetch(path, {mode: 'same-origin', credentials: 'same-origin',
                redirect: 'error', cache: 'no-store', signal: controller.signal});
            return response.ok ? await response.json() : null;
        } finally { clearTimeout(timer); }
        """
        callAsyncJavaScript(script, arguments: ["path": path, "origin": origin.absoluteString], in: nil, in: .defaultClient) { result in
            completion(try? result.get())
        }
    }

    func openWorkbenchDestination(_ destination: WorkbenchDestination, origin: URL, completion: @escaping (URL, Bool) -> Void) {
        var target = URLComponents(url: origin, resolvingAgainstBaseURL: false)!
        target.queryItems = destination.queryItems
        guard let next = target.url else { return }
        // Update the existing document so in-memory drafts and browser Back survive a menu jump.
        let script = """
        if (location.origin !== origin || !document.getElementById('root')) return false;
        if (location.href !== next) history.pushState(null, '', next);
        window.dispatchEvent(new PopStateEvent('popstate'));
        window.scrollTo(0, 0);
        return true;
        """
        callAsyncJavaScript(script, arguments: ["next": next.absoluteString, "origin": origin.absoluteString], in: nil, in: .page) { result in
            completion(next, (try? result.get()) as? Bool == true)
        }
    }
}

final class WorkbenchShortcuts: NSObject, NSMenuDelegate {
    typealias JSON = [String: Any]
    typealias Reader = (String, @escaping (Any?) -> Void) -> Void
    let text: (String, String) -> String
    let read: Reader
    let navigate: (WorkbenchDestination) -> Void
    let menu = NSMenu()
    private var hostNames = [String: String]()
    private var hostIDs = [String: [String]]()
    private var generation = 0
    private var loadingHosts = false
    private var statusItem: NSStatusItem?

    private enum Contents {
        case hosts(String)
        case details(String)
        case quotas
    }
    private final class SectionMenu: NSMenu {
        var contents: Contents!
        var refresh = 0
    }

    init(text: @escaping (String, String) -> String, read: @escaping Reader, navigate: @escaping (WorkbenchDestination) -> Void) {
        self.text = text
        self.read = read
        self.navigate = navigate
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        rebuild()
    }

    func installStatusItem() {
        if statusItem == nil {
            statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        }
        let button = statusItem?.button
        button?.image = NSImage(systemSymbolName: "arrow.left.arrow.right", accessibilityDescription: "Senrelay")
        button?.setAccessibilityLabel("Senrelay")
        button?.toolTip = "Senrelay"
        statusItem?.menu = menu
    }

    func rebuild() {
        generation += 1
        hostNames.removeAll()
        hostIDs.removeAll()
        menu.removeAllItems()
        addAction(to: menu, text("打开工作台", "Open Workbench"), .init(page: "home"))
        addAction(to: menu, text("所有助手", "All agents"), .init(page: "agents"))
        addAction(to: menu, text("全局资源库", "Global resource library"), .init(page: "agents", section: "library"))
        menu.addItem(.separator())
        addSection(to: menu, text("常用助手", "Favorite agents"), .hosts("favorite_hosts"))
        addSection(to: menu, text("最近使用", "Recent agents"), .hosts("recent_hosts"))
        menu.addItem(.separator())
        addAction(to: menu, text("用量与性能", "Usage and performance"), .init(page: "usage", section: "overview"))
        addSection(to: menu, text("账号额度", "Account quotas"), .quotas)
    }

    func addNavigationKeys(to parent: NSMenu) {
        addAction(to: parent, text("工作台", "Workbench"), .init(page: "home"), key: "0")
        addAction(to: parent, text("Agent 名片", "Agent card"), .init(page: "personal", section: "profile"))
        addAction(to: parent, text("项目", "Projects"), .init(page: "projects"), key: "1")
        addAction(to: parent, text("所有助手", "All agents"), .init(page: "agents"), key: "2")
        addAction(to: parent, text("全局资源库", "Global resource library"), .init(page: "agents", section: "library"))
        addAction(to: parent, text("用量与性能", "Usage and performance"), .init(page: "usage", section: "overview"), key: "3")
        let item = addAction(to: parent, text("账号额度", "Account quotas"), .init(page: "usage", section: "accounts"), key: "3")
        item.keyEquivalentModifierMask = [.command, .option]
    }

    @discardableResult
    private func addAction(to menu: NSMenu, _ title: String, _ destination: WorkbenchDestination, key: String = "") -> NSMenuItem {
        let item = menu.addItem(withTitle: title, action: #selector(performNavigation(_:)), keyEquivalent: key)
        item.target = self
        item.representedObject = destination
        return item
    }

    @objc private func performNavigation(_ sender: NSMenuItem) {
        if let destination = sender.representedObject as? WorkbenchDestination { navigate(destination) }
    }

    private func info(_ value: String, in menu: NSMenu) {
        let item = menu.addItem(withTitle: value, action: nil, keyEquivalent: "")
        item.isEnabled = false
    }

    private func addSection(to parent: NSMenu, _ title: String, _ contents: Contents) {
        let item = parent.addItem(withTitle: title, action: nil, keyEquivalent: "")
        let section = SectionMenu(title: title)
        section.autoenablesItems = false
        section.delegate = self
        section.contents = contents
        item.submenu = section
        info(text("正在读取…", "Loading…"), in: section)
    }

    func menuWillOpen(_ opened: NSMenu) {
        if opened === menu {
            refreshHosts()
            return
        }
        guard let section = opened as? SectionMenu else { return }
        switch section.contents! {
        case .hosts(let group):
            renderHosts(group, in: section)
        case .details(let id):
            refreshDetails(id, in: section)
        case .quotas:
            refreshQuotas(in: section)
        }
    }

    private func refreshHosts() {
        generation += 1
        let revision = generation
        loadingHosts = true
        read("/api/manage/agent-preferences") { [weak self] value in
            guard let self, self.generation == revision else { return }
            self.loadingHosts = false
            self.hostIDs.removeAll()
            self.hostNames.removeAll()
            if let prefs = value as? JSON, let names = prefs["host_names"] as? [String: String] {
                for (id, rawName) in names {
                    guard id.range(of: #"^[a-z0-9_-]{1,64}$"#, options: .regularExpression) != nil,
                          let name = self.clean(rawName) else { continue }
                    self.hostNames[id] = name
                }
                for key in ["favorite_hosts", "recent_hosts"] {
                    if let ids = prefs[key] as? [String] { self.hostIDs[key] = ids.filter { self.hostNames[$0] != nil } }
                }
            }
            for item in self.menu.items {
                if let section = item.submenu as? SectionMenu, case .hosts(let key) = section.contents! {
                    self.renderHosts(key, in: section)
                }
            }
        }
    }

    private func renderHosts(_ group: String, in section: SectionMenu) {
        section.removeAllItems()
        guard let ids = hostIDs[group] else {
            info(loadingHosts ? text("正在读取…", "Loading…") : text("助手列表暂不可用", "Agent list unavailable"), in: section)
            return
        }
        if ids.isEmpty {
            info(group == "recent_hosts" ? text("尚未使用助手", "No recent agents") : text("尚未收藏助手", "No favorite agents"), in: section)
        }
        for id in ids {
            if let name = hostNames[id] { addSection(to: section, name, .details(id)) }
        }
    }

    private func refreshDetails(_ id: String, in section: SectionMenu) {
        section.refresh += 1
        let request = section.refresh
        section.removeAllItems()
        addAction(to: section, text("模型与服务商…", "Models and providers…"), .init(page: "agents", agent: id, section: "providers"))
        addAction(to: section, text("更改记录与恢复…", "Changes and recovery…"), .init(page: "agents", agent: id, section: "plans"))
        addAction(to: section, text("查看用量…", "View usage…"), .init(page: "usage", agent: id, section: "overview"))
        addAction(to: section, text("查看账号额度…", "View account quotas…"), .init(page: "usage", agent: id, section: "accounts"))
        section.addItem(.separator())
        info(text("正在读取当前配置…", "Reading configuration…"), in: section)
        read("/api/manage/hosts/\(id)/state") { [weak section] value in
            guard let section, section.refresh == request else { return }
            section.removeItem(at: section.numberOfItems - 1)
            guard let state = value as? JSON, state["host_id"] as? String == id, let selection = state["selection"] as? JSON else {
                self.info(self.text("当前配置暂不可用", "Configuration unavailable"), in: section)
                return
            }
            guard selection["provider_id"] is String else {
                self.info(self.text("尚未选定服务商", "No provider selected"), in: section)
                return
            }
            let name = self.clean(selection["provider_name"]) ?? self.text("已选服务商", "Selected provider")
            let model = self.clean(selection["model_id"])
            self.info([name, model].compactMap { $0 }.joined(separator: " · "), in: section)
            let evidence: String
            switch selection["status"] as? String {
            case "verified": evidence = self.text("配置文件已核对", "Configuration file verified")
            case "externally_changed", "modified": evidence = self.text("配置文件有外部更改", "Configuration changed externally")
            case "missing": evidence = self.text("配置文件缺失", "Configuration missing")
            case "update_available": evidence = self.text("配置有待应用的更改", "Configuration update available")
            default: evidence = self.text("配置文件待核对", "Configuration file unverified")
            }
            let mode: String?
            switch selection["connection_mode"] as? String {
            case "direct": mode = self.text("直连", "Direct")
            case "routing": mode = self.text("路由", "Routing")
            case "aggregation": mode = self.text("模型聚合", "Model aggregation")
            default: mode = nil
            }
            self.info([mode, evidence].compactMap { $0 }.joined(separator: " · "), in: section)
        }
    }

    private func refreshQuotas(in section: SectionMenu) {
        section.refresh += 1
        let request = section.refresh
        section.removeAllItems()
        addAction(to: section, text("打开账号额度…", "Open account quotas…"), .init(page: "usage", section: "accounts"))
        section.addItem(.separator())
        info(text("正在读取上次结果…", "Reading last results…"), in: section)
        read("/api/account-quotas") { [weak section] value in
            guard let section, section.refresh == request else { return }
            section.removeItem(at: section.numberOfItems - 1)
            guard let items = (value as? JSON)?["items"] as? [JSON] else {
                self.info(self.text("额度记录暂不可用", "Quota records unavailable"), in: section)
                return
            }
            if items.isEmpty { self.info(self.text("尚无账号", "No accounts yet"), in: section) }
            for account in items.prefix(8) {
                let name = self.clean(account["name"]) ?? self.text("账号", "Account")
                let details = self.quotaReadings(account)
                let summary = details.first ?? self.text("当前额度待核对", "Current quota unverified")
                if details.count <= 1 {
                    self.info("\(name) · \(summary)", in: section)
                } else {
                    let item = section.addItem(withTitle: name, action: nil, keyEquivalent: "")
                    let readings = NSMenu(title: name)
                    readings.autoenablesItems = false
                    item.submenu = readings
                    for detail in details { self.info(detail, in: readings) }
                }
            }
            if items.count > 8 { self.info(self.text("另外 \(items.count - 8) 个账号可在工作台查看", "\(items.count - 8) more in the workbench"), in: section) }
        }
    }

    private func observationTime(_ value: Any?) -> String? {
        guard let raw = value as? String else { return nil }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = iso.date(from: raw) ?? ISO8601DateFormatter().date(from: raw) else { return nil }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    private func quotaReadings(_ account: JSON) -> [String] {
        let snapshot = account["snapshot"] as? JSON
        let windows = (snapshot?["windows"] as? [JSON]) ?? []
        let balances = (snapshot?["balances"] as? [JSON]) ?? []
        var details = [String]()
        for window in windows.prefix(8) {
            guard let used = window["used_percent"] as? NSNumber else { continue }
            var parts = [clean(window["label"]) ?? text("窗口", "Window")]
            if let minutes = window["minutes"] as? Int {
                if minutes.isMultiple(of: 1440) { parts.append(text("\(minutes / 1440) 天", "\(minutes / 1440)d")) }
                else if minutes.isMultiple(of: 60) { parts.append(text("\(minutes / 60) 小时", "\(minutes / 60)h")) }
                else { parts.append(text("\(minutes) 分钟", "\(minutes)m")) }
            }
            let percent = used.doubleValue.formatted(.number.precision(.fractionLength(0...1)))
            parts.append(text("已用 \(percent)%", "\(percent)% used"))
            if let when = observationTime(window["resets_at"]) { parts.append(text("\(when) 重置", "resets \(when)")) }
            details.append(parts.joined(separator: " "))
        }
        if windows.count > 8 { details.append(text("其他窗口可在工作台查看", "More windows in the workbench")) }
        for balance in balances {
            let amount = clean(balance["amount"]) ?? (balance["amount"] as? NSNumber)?.stringValue
            if let amount, let unit = clean(balance["unit"]) {
                let isKeyBudget = account["source"] as? String == "openrouter" && balance["scope"] as? String != "account"
                let kind = isKeyBudget ? text("剩余密钥预算", "Key budget remaining") : text("账号余额", "Account balance")
                details.append("\(kind) · \(amount) \(unit)")
            }
        }
        if snapshot?["credits_unlimited"] as? Bool == true { details.append(text("点数不限", "Unlimited credits")) }
        if snapshot?["no_key_limit"] as? Bool == true { details.append(text("此密钥未设上限", "No limit set on this key")) }
        if !details.isEmpty {
            if let when = observationTime(account["last_success_at"]) { details.append(text("上次读取 \(when)", "Last read \(when)")) }
            else { details.append(text("读取时间未知", "Reading time unknown")) }
        }
        switch account["status"] as? String {
        case "available":
            if details.isEmpty { details.append(text("当前额度待核对", "Current quota unverified")) }
        case "native_only": details.append(text("在助手中查看", "View in the agent"))
        case "unsupported": details.append(text("未提供额度查询", "Quota lookup unavailable"))
        case "not_checked": details.append(text("尚未读取", "No reading yet"))
        case "unauthorized", "expired": details.append(text("需要授权", "Authorization needed"))
        default: details.append(text("当前额度待核对", "Current quota unverified"))
        }
        return details
    }

    private func clean(_ value: Any?) -> String? {
        guard let raw = value as? String else { return nil }
        let line = raw.components(separatedBy: .controlCharacters).joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return nil }
        return String(line.prefix(96))
    }
}
