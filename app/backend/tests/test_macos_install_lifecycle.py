"""Install/upgrade/uninstall checklist never targets the daily App or deletes data."""
import importlib.util
import json
import plistlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_macos_install_lifecycle.py"
spec = importlib.util.spec_from_file_location("check_macos_install_lifecycle", SCRIPT)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


def test_source_and_simulation_checks_pass_without_a_bundle(tmp_path):
    report = lifecycle.run_checks(None)
    assert report["did_not_use_daily_port"] is True
    assert report["did_not_replace_daily_app"] is True
    assert report["identity"]["bundle_id"] == "com.senrelay.desktop"
    assert report["identity"]["minimum_macos"] == "14.0"
    assert report["identity"]["daily_port"] == 8420
    failed = [item for item in report["checks"] if not item["ok"] and item["id"] != "bundle_inspect"]
    assert failed == []
    assert report["ok"] is True
    assert report["needs_clean_mac"]
    assert any("Application Support/ContextCore" in item for item in report["needs_clean_mac"])


def test_script_main_writes_json_and_does_not_require_a_bundle(tmp_path):
    out = tmp_path / "report.json"
    code = lifecycle.main(["--json-out", str(out)])
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["bundle"] is None


def test_missing_bundle_path_is_a_usage_error(tmp_path):
    assert lifecycle.main(["--bundle", str(tmp_path / "missing.app")]) == 2


def test_stale_bundle_below_macos_14_is_rejected(tmp_path):
    contents = tmp_path / "Senrelay.app" / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({
            "CFBundleIdentifier": "com.senrelay.desktop",
            "LSMinimumSystemVersion": "12.0",
        }, handle)
    report = lifecycle.run_checks(contents.parent)
    assert report["ok"] is False
    assert report["bundle"]["build_receipt"] is None
    failed = {item["id"] for item in report["checks"] if not item["ok"]}
    assert "bundle_minimum_macos" in failed
    assert "build_receipt" in failed
    assert any("LSMinimumSystemVersion 12.0" in item for item in report["needs_clean_mac"])


def test_install_macos_helper_clears_quarantine_and_installs(tmp_path):
    import os
    import subprocess
    import sys
    import pytest

    if sys.platform != "darwin":
        pytest.skip("macOS codesign/xattr test requires darwin")

    src_bundle = tmp_path / "src" / "Senrelay.app"
    macos_dir = src_bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    exe = macos_dir / "Senrelay"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    with (src_bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({
            "CFBundleName": "Senrelay",
            "CFBundleIdentifier": "com.senrelay.desktop",
            "CFBundleExecutable": "Senrelay",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "14.0",
        }, handle)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(src_bundle)], check=True)
    subprocess.run(
        ["xattr", "-w", "com.apple.quarantine", "0081;00000000;Safari;", str(src_bundle)],
        check=True,
    )
    target_apps = tmp_path / "Applications"
    helper = Path(__file__).resolve().parents[2] / "macos" / "install-macos.sh"
    res = subprocess.run(
        [str(helper), str(src_bundle)],
        env={**os.environ, "SENRELAY_INSTALL_DIR": str(target_apps)},
        capture_output=True,
        text=True,
        check=True,
    )
    installed = target_apps / "Senrelay.app"
    assert installed.is_dir(), res.stdout
    attrs = subprocess.run(["xattr", str(installed)], capture_output=True, text=True, check=True).stdout
    assert "com.apple.quarantine" not in attrs

