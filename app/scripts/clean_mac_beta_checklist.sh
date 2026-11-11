#!/bin/zsh
# Clean-Mac Beta acceptance checklist runner.
# Does not replace the daily App. Prints steps and verifies local artifacts only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-}"
if [[ -z "$APP" ]]; then
  echo "Usage: $0 /absolute/path/to/Senrelay.app"
  echo "Then follow the printed clean-Mac steps on a separate Apple Silicon Mac."
  exit 2
fi
python3 "$ROOT/app/macos/verify_bundle.py" --bundle "$APP"
python3 - "$APP" <<'PY'
import json, hashlib, pathlib, sys
app = pathlib.Path(sys.argv[1])
build = app / "Contents" / "Resources" / "BUILD.json"
if not build.exists():
    # Some layouts keep BUILD.json beside the ZIP only.
    alt = list(app.parent.glob("BUILD.json"))
    build = alt[0] if alt else None
data = json.loads(build.read_text()) if build and build.exists() else {}
print("build_json", build)
print("version", data.get("version") or data.get("package_version"))
print("build_number", data.get("build_number"))
print("min_macos", data.get("minimum_macos") or data.get("minimum_system_version") or data.get("min_macos") or "check Info.plist")
print("arch", data.get("architecture") or data.get("arch"))
print("notarized", data.get("notarized"))
print("signing", data.get("signing") or data.get("signature"))
print("--- clean Mac steps ---")
for step in [
    "Copy the ZIP from a download URL, not from this developer checkout",
    "Confirm Gatekeeper prompt for an ad-hoc unnotarized build",
    "First launch: health ok, no developer Python/Node required",
    "Create a project and one memory; quit; reopen; data remains",
    "Upgrade from a previous Beta ZIP without deleting Application Support/ContextCore",
    "Interrupt copy mid-upgrade; recover to a runnable app",
    "Sleep/wake and reboot with the service still reachable or clearly recoverable",
    "Uninstall the .app only; user data remains until the user deletes it explicitly",
]:
    print("-", step)
PY
