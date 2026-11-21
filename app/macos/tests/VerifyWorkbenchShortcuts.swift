import Cocoa
import WebKit

@main
struct VerifyWorkbenchShortcuts {
    struct Failed: Error { let reason: String }

    @MainActor
    static func waitFor(_ label: String, _ condition: () -> Bool) async throws {
        for _ in 0..<150 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 40_000_000)
        }
        throw Failed(reason: "Timed out waiting for " + label)
    }

    @MainActor
    static func entry(_ title: String, in menu: NSMenu) throws -> NSMenuItem {
        guard let item = menu.item(withTitle: title) else { throw Failed(reason: title) }
        return item
    }

    @MainActor
    static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let origin = URL(string: CommandLine.arguments[1])!
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 560), configuration: config)
        let window = NSWindow(contentRect: web.bounds, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = web
        web.load(URLRequest(url: origin))
        try await waitFor("fixture page") { web.url != nil && !web.isLoading }
        guard (try await web.evaluateJavaScript("window.draft")) as? String == "retained" else { throw Failed(reason: "Fixture did not load") }

        var english = true
        var navigated: WorkbenchDestination?
        let shortcuts = WorkbenchShortcuts(
            text: { english ? $1 : $0 },
            read: { web.readWorkbenchRecord($0, origin: origin, completion: $1) },
            navigate: { destination in
                web.openWorkbenchDestination(destination, origin: origin) { _, success in
                    if success { navigated = destination }
                }
            }
        )
        shortcuts.menuWillOpen(shortcuts.menu)
        let favorites = try entry("Favorite agents", in: shortcuts.menu).submenu!
        try await waitFor("favorite readback") { favorites.item(withTitle: "Codex CLI") != nil }
        let recent = try entry("Recent agents", in: shortcuts.menu).submenu!
        guard recent.numberOfItems == 1, recent.item(withTitle: "No recent agents") != nil else { throw Failed(reason: "Empty recents") }
        let host = try entry("Codex CLI", in: favorites).submenu!
        shortcuts.menuWillOpen(host)
        try await waitFor("file status") { host.item(withTitle: "Direct · Configuration changed externally") != nil }
        _ = try entry("Example · test-model", in: host)
        host.performActionForItem(at: host.indexOfItem(withTitle: "Changes and recovery…"))
        try await waitFor("host deep link") { navigated == WorkbenchDestination(page: "agents", agent: "codex", section: "plans") }
        let loaded = try await web.evaluateJavaScript("[location.search, window.draft, window.visits]") as? [Any]
        guard loaded?[0] as? String == "?page=agents&agent=codex&section=plans",
              loaded?[1] as? String == "retained", loaded?[2] as? Int == 1 else { throw Failed(reason: "SPA history or draft lost") }

        let quotas = try entry("Account quotas", in: shortcuts.menu).submenu!
        shortcuts.menuWillOpen(quotas)
        try await waitFor("quota readback") { quotas.item(withTitle: "Codex") != nil }
        let reading = try entry("Codex", in: quotas).submenu!
        _ = try entry("Five hours 73% used", in: reading)
        _ = try entry("Current quota unverified", in: reading)
        guard reading.items.contains(where: { $0.title.hasPrefix("Last read ") }) else { throw Failed(reason: "Missing quota time") }
        let router = try entry("OpenRouter", in: quotas).submenu!
        _ = try entry("Key budget remaining · 1 USD", in: router)
        _ = try entry("Account balance · 10 USD", in: router)
        quotas.performActionForItem(at: quotas.indexOfItem(withTitle: "Open account quotas…"))
        try await waitFor("quota deep link") { navigated == WorkbenchDestination(page: "usage", section: "accounts") }
        _ = try await web.evaluateJavaScript("history.back()")
        try await waitFor("back navigation") { web.url?.query == "page=agents&agent=codex&section=plans" }

        english = false
        shortcuts.rebuild()
        _ = try entry("常用助手", in: shortcuts.menu)
        let go = NSMenu()
        shortcuts.addNavigationKeys(to: go)
        go.performActionForItem(at: go.indexOfItem(withTitle: "工作台"))
        try await waitFor("workbench home") { navigated == WorkbenchDestination(page: "home") }
        go.performActionForItem(at: go.indexOfItem(withTitle: "Agent 名片"))
        try await waitFor("agent card") { navigated == WorkbenchDestination(page: "personal", section: "profile") }
        go.performActionForItem(at: go.indexOfItem(withTitle: "全局资源库"))
        try await waitFor("global resource library") { navigated == WorkbenchDestination(page: "agents", section: "library") }
        guard try entry("所有助手", in: go).keyEquivalent == "2",
              try entry("账号额度", in: go).keyEquivalentModifierMask == [.option, .command]
        else { throw Failed(reason: "Keyboard shortcuts") }
        print("Native shortcuts verified")
        withExtendedLifetime(window) {}
    }
}
