"""Finite command dispatch shared by both frozen service entry points."""
import sys


def run_hook(arguments):
    from app.integrations.constants import SUPPORTED_HOSTS
    from app.integrations import cc_hook
    events = set(cc_hook.EVENT_NAME_MAP) | set(cc_hook.EVENT_NAME_MAP.values())
    if len(arguments) != 2 or arguments[0] not in SUPPORTED_HOSTS or arguments[1] not in events:
        raise SystemExit('Usage: senrelay-service --hook HOST EVENT')
    original = sys.argv
    try:
        sys.argv = [original[0], *arguments]
        return cc_hook.main()
    finally:
        sys.argv = original
