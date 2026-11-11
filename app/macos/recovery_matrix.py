"""User-visible native recovery contract for Senrelay on macOS.

verify_native.py compares the rendered status label with these strings when
the app is actually run. Reading Senrelay.swift or verify_native.py is not
proof the window showed them.
"""

WEBVIEW_FAULTS = (
    {
        "zh": "页面加载失败，请点击“重新连接”重试。",
        "en": "The workspace could not load. Click Reconnect to retry.",
    },
    {
        "zh": "页面加载中断，请点击“重新连接”重试。",
        "en": "The workspace stopped loading. Click Reconnect to retry.",
    },
    {
        "zh": "页面进程已退出。请重新连接，已保存的项目数据仍在本机。",
        "en": "The workspace process stopped. Reconnect to reopen it. Saved project data remains on this Mac.",
    },
)

SCENARIOS = {
    "normal": {
        "kind": "webview_disconnect_reconnect",
        "faults": WEBVIEW_FAULTS,
        "retry_visible": True,
        "upgrade_visible": False,
    },
    "version-mismatch-recover": {
        "kind": "version_mismatch",
        "status": {
            "zh": "检测到本机正在运行旧版或其他版本的后台服务。点击“更新本机后台并重连”即可切换到当前 App 配套后台，已保存的项目与记忆数据不会丢失。",
            "en": "An older or different local service is currently running. Click “Update Local Service & Reconnect” to switch to this app’s bundled service without losing your saved projects or memories.",
        },
        "retry_visible": True,
        "upgrade_visible": True,
        "upgrade_title": {
            "zh": "更新本机后台并重连",
            "en": "Update Local Service & Reconnect",
        },
        "preserves_data": True,
    },
    "service-start-failure-recover": {
        "kind": "start_failure",
        "status": {
            "zh": "无法启动本机服务。请检查 App 是否完整，并查看本机服务日志。",
            "en": "The local service could not start. Check that the app bundle is complete and review the local service log.",
        },
        "retry_visible": True,
        "upgrade_visible": False,
    },
    "unreachable-port-recover": {
        "kind": "disconnect_reconnect",
        "status": {
            "zh": "本机服务未能就绪。请查看 ~/Library/Logs/Senrelay/service.log，然后点击“重新连接”重试。",
            "en": "The local service did not become ready. Review ~/Library/Logs/Senrelay/service.log, then click Reconnect.",
        },
        "retry_visible": True,
        "upgrade_visible": False,
    },
    "actual-webkit-termination": {
        "kind": "webview_process_exit",
        "status": WEBVIEW_FAULTS[2],
        "retry_visible": True,
        "upgrade_visible": False,
        "preserves_data": True,
    },
    "actual-service-interruption": {
        "kind": "disconnect_reconnect",
        "retry_visible": True,
        "upgrade_visible": False,
        "preserves_data": True,
        "online_cleared": True,
    },
}

REQUIRED_SCENARIO_IDS = (
    "normal",
    "version-mismatch-recover",
    "service-start-failure-recover",
    "unreachable-port-recover",
    "actual-webkit-termination",
    "actual-service-interruption",
)


def webview_fault_messages(language: str) -> list[str]:
    return [item[language] for item in WEBVIEW_FAULTS]


def scenario_status_text(scenario_id: str, language: str) -> str:
    status = SCENARIOS[scenario_id]["status"]
    return status[language]


def all_user_visible_strings() -> list[str]:
    seen: list[str] = []
    for item in WEBVIEW_FAULTS:
        seen.extend(item.values())
    for spec in SCENARIOS.values():
        status = spec.get("status")
        if isinstance(status, dict):
            seen.extend(status.values())
        title = spec.get("upgrade_title")
        if isinstance(title, dict):
            seen.extend(title.values())
    return seen
