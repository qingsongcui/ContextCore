#!/usr/bin/env python3
"""Checks macOS package identity and uninstall-without-data-delete.

Supported matrix is macOS 14.0+ on arm64 only. This script does not launch the
daily App, does not connect to 127.0.0.1:8420, and does not delete
~/Library/Application Support/ContextCore.

A clean Mac later can rerun:

  python3 app/scripts/check_macos_install_lifecycle.py --bundle /path/Senrelay.app

This machine can rerun without a bundle:

  python3 app/scripts/check_macos_install_lifecycle.py
"""
from __future__ import annotations

import argparse
import json
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent
REPO_ROOT = APP_ROOT.parent
BACKEND_DIR = APP_ROOT / "backend"
MACOS_DIR = APP_ROOT / "macos"
DAILY_PORT = 8420
BUNDLE_ID = "com.senrelay.desktop"
MINIMUM_MACOS = "14.0"
SUPPORTED_ARCH = "arm64"
DATA_DIR_RELATIVE = "Library/Application Support/ContextCore"
LOGS_DIR_RELATIVE = "Library/Logs/Senrelay"

sys.path.insert(0, str(BACKEND_DIR))
from app import runtime  # noqa: E402


def log(message: str) -> None:
    print(f"[check_macos_install_lifecycle] {message}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source_bundle_id() -> str:
    text = _read(MACOS_DIR / "build.py")
    match = re.search(r"'CFBundleIdentifier':\s*'([^']+)'", text)
    if not match:
        raise RuntimeError("build.py does not declare CFBundleIdentifier")
    return match.group(1)


def _source_minimum_macos() -> str:
    text = _read(MACOS_DIR / "build.py")
    match = re.search(r"minimum_macos\s*=\s*'([^']+)'", text)
    if not match:
        raise RuntimeError("build.py does not declare minimum_macos")
    return match.group(1)


def inspect_source_identity() -> dict[str, Any]:
    swift = _read(MACOS_DIR / "Senrelay.swift")
    if "127.0.0.1:8420" not in swift:
        raise RuntimeError("Senrelay.swift no longer pins the daily loopback port")
    if DATA_DIR_RELATIVE not in swift:
        raise RuntimeError("Senrelay.swift no longer stores data under Application Support/ContextCore")
    if "Contents/Resources" in swift and "contextcore.db" in swift:
        raise RuntimeError("Senrelay.swift appears to keep a runtime database in the bundle")
    data_dir = str(runtime.DEFAULT_DATA_DIR_MACOS)
    if "Application Support" not in data_dir or "ContextCore" not in data_dir:
        raise RuntimeError("runtime default data dir is not Application Support/ContextCore")
    return {
        "bundle_id": _source_bundle_id(),
        "minimum_macos": _source_minimum_macos(),
        "architecture_claim": SUPPORTED_ARCH,
        "signature_claim": "ad-hoc",
        "notarized": False,
        "daily_port": DAILY_PORT,
        "data_dir": data_dir,
        "logs_dir": str(Path.home() / LOGS_DIR_RELATIVE),
    }


def inspect_bundle(bundle: Path) -> dict[str, Any]:
    info_path = bundle / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"bundle is missing Info.plist: {bundle}")
    info = plistlib.loads(info_path.read_bytes())
    binary = bundle / "Contents" / "MacOS" / "Senrelay"
    arch = ""
    if binary.is_file():
        probed = subprocess.run(["lipo", "-archs", str(binary)], capture_output=True, text=True, check=False)
        arch = (probed.stdout or probed.stderr).strip()
    resources = bundle / "Contents" / "Resources"
    bundled_db = sorted(path.name for path in resources.rglob("*.db")) if resources.is_dir() else []
    receipt_path = bundle.parent / "BUILD.json"
    build_receipt = None
    if receipt_path.is_file():
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        build_receipt = {
            "architecture": raw.get("architecture"),
            "minimum_macos": raw.get("minimum_macos"),
            "signature": raw.get("signature"),
            "notarized": raw.get("notarized"),
        }
    return {
        "path": str(bundle.resolve()),
        "bundle_id": info.get("CFBundleIdentifier"),
        "short_version": info.get("CFBundleShortVersionString"),
        "build_number": info.get("CFBundleVersion"),
        "minimum_macos": info.get("LSMinimumSystemVersion"),
        "architectures": arch,
        "bundled_sqlite": bundled_db,
        "build_receipt": build_receipt,
    }


def simulate_uninstall_preserves_data(selected_data: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="senrelay-install-lifecycle-") as temporary:
        root = Path(temporary)
        data_dir = selected_data if selected_data is not None else root / "data"
        data_dir.mkdir(exist_ok=True)
        marker = data_dir / "contextcore.db"
        extra = data_dir / "user-marker.txt"
        if selected_data is None:
            marker.write_text("synthetic-user-db", encoding="utf-8")
            extra.write_text("keep-me", encoding="utf-8")
        before = {path.relative_to(data_dir): path.read_bytes()
                  for path in data_dir.rglob("*") if path.is_file()}
        plist_path = root / "LaunchAgents" / "com.contextcore.service.plist"
        plist_path.parent.mkdir()
        plist_path.write_bytes(plistlib.dumps({
            "Label": runtime.DEFAULT_LABEL,
            "ProgramArguments": ["python3", "-m", "app.cli", "serve", "--data-dir", str(data_dir.resolve())],
            "EnvironmentVariables": {"CONTEXTCORE_DATA_DIR": str(data_dir.resolve())},
        }, fmt=plistlib.FMT_XML))
        with patch("sys.platform", "darwin"), patch("app.runtime.is_launchd_registered", return_value=False):
            result = runtime.uninstall_autostart(data_dir=data_dir, plist_path=plist_path)
        if not result.get("ok"):
            raise RuntimeError(f"uninstall_autostart failed: {result}")
        if plist_path.exists():
            raise RuntimeError("owned LaunchAgent plist was not removed")
        after = {path.relative_to(data_dir): path.read_bytes()
                 for path in data_dir.rglob("*") if path.is_file()}
        if after != before:
            raise RuntimeError("data directory files were changed by uninstall")
        return {
            "ok": True,
            "plist_removed": True,
            "data_dir": str(data_dir),
            "preserved": sorted(str(path) for path in before),
        }


def current_platform() -> dict[str, Any]:
    machine = platform.machine()
    mac_ver = platform.mac_ver()[0]
    supported = sys.platform == "darwin" and machine == SUPPORTED_ARCH and (
        not mac_ver or tuple(int(part) for part in mac_ver.split(".")[:1]) >= (14,)
    )
    return {
        "system": sys.platform,
        "mac_ver": mac_ver,
        "machine": machine,
        "supported_claim": f"macOS {MINIMUM_MACOS} {SUPPORTED_ARCH} only",
        "this_machine_matches_claim": supported,
    }


def run_checks(bundle: Path | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def record(check_id: str, ok: bool, detail: str, needs_clean_mac: bool = False) -> None:
        checks.append({"id": check_id, "ok": ok, "detail": detail, "needs_clean_mac": needs_clean_mac})
        status = "ok" if ok else "fail"
        log(f"{status} {check_id}: {detail}")

    identity = inspect_source_identity()
    record("source_bundle_id", identity["bundle_id"] == BUNDLE_ID, identity["bundle_id"])
    record("source_minimum_macos", identity["minimum_macos"] == MINIMUM_MACOS, identity["minimum_macos"])
    record("source_daily_port_isolated", identity["daily_port"] == DAILY_PORT, f"daily App stays on {DAILY_PORT}; this check never uses it")
    record(
        "data_dir_outside_bundle",
        DATA_DIR_RELATIVE in identity["data_dir"] or "Application Support" in identity["data_dir"],
        identity["data_dir"],
    )
    try:
        simulation = simulate_uninstall_preserves_data()
        record("uninstall_simulation_preserves_data", True, json.dumps(simulation, ensure_ascii=False))
    except Exception as error:
        record("uninstall_simulation_preserves_data", False, str(error))

    bundle_info = None
    if bundle:
        try:
            bundle_info = inspect_bundle(bundle)
            record("bundle_id", bundle_info["bundle_id"] == BUNDLE_ID, str(bundle_info["bundle_id"]))
            record(
                "bundle_minimum_macos",
                bundle_info["minimum_macos"] == MINIMUM_MACOS,
                str(bundle_info["minimum_macos"]),
                needs_clean_mac=bundle_info["minimum_macos"] != MINIMUM_MACOS,
            )
            record(
                "bundle_architecture",
                SUPPORTED_ARCH in (bundle_info["architectures"] or ""),
                bundle_info["architectures"] or "lipo produced no architecture list",
            )
            record("bundle_has_no_user_db", bundle_info["bundled_sqlite"] == [], str(bundle_info["bundled_sqlite"]))
            receipt = bundle_info["build_receipt"]
            if receipt:
                record("receipt_architecture", receipt.get("architecture") == SUPPORTED_ARCH, str(receipt.get("architecture")))
                record("receipt_minimum_macos", receipt.get("minimum_macos") == MINIMUM_MACOS, str(receipt.get("minimum_macos")))
                record("receipt_not_notarized", receipt.get("notarized") is False, str(receipt.get("notarized")))
            else:
                record(
                    "build_receipt",
                    False,
                    "BUILD.json is missing next to the bundle. Rebuild before a clean-Mac install.",
                    needs_clean_mac=True,
                )
        except Exception as error:
            record("bundle_inspect", False, str(error), needs_clean_mac=True)
    else:
        record(
            "bundle_inspect",
            True,
            "no --bundle given; source and simulation checks ran. A clean Mac still has to install, upgrade, and uninstall a real ZIP.",
            needs_clean_mac=True,
        )

    needs_clean_mac = [
        "Download the ad-hoc arm64 ZIP on a Mac that has never installed Senrelay.",
        "Clear quarantine attributes with `xattr -cr Senrelay.app` (or `./install-macos.command`), confirm first launch, and verify that 8420 is used only by this App after install.",
        "Create a project and a memory, replace the App with a newer build in place, and read both records back.",
        "Move Senrelay.app to Trash without deleting ~/Library/Application Support/ContextCore, then confirm the records remain.",
        "Do not claim Intel, macOS older than 14.0, Developer ID, or notarization from this script.",
    ]
    if bundle_info and bundle_info.get("minimum_macos") != MINIMUM_MACOS:
        needs_clean_mac.insert(0, f"Rebuild the App. This bundle declares LSMinimumSystemVersion {bundle_info.get('minimum_macos')}; source requires {MINIMUM_MACOS}.")
    if bundle_info and not bundle_info.get("build_receipt"):
        needs_clean_mac.insert(0, "Rebuild with app/macos/build.py so BUILD.json and SHA256SUMS sit beside the App.")
    platform_info = current_platform()
    required = [item for item in checks if item["id"] != "bundle_inspect" or bundle is not None]
    return {
        "ok": all(item["ok"] for item in required),
        "platform": platform_info,
        "identity": identity,
        "bundle": bundle_info,
        "checks": checks,
        "needs_clean_mac": needs_clean_mac,
        "did_not_use_daily_port": True,
        "did_not_replace_daily_app": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Optional Senrelay.app to inspect read-only")
    parser.add_argument("--json-out", type=Path, help="Write the full report as JSON")
    args = parser.parse_args(argv)
    if args.bundle and not args.bundle.exists():
        print(f"bundle not found: {args.bundle}", file=sys.stderr)
        return 2
    report = run_checks(args.bundle)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": report["ok"],
        "platform": report["platform"],
        "needs_clean_mac": report["needs_clean_mac"],
        "failed": [item for item in report["checks"] if not item["ok"]],
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
