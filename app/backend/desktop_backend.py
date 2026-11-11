import os
import threading
import time
import sys
from pathlib import Path


def monitor_parent_process() -> None:
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return
    while True:
        time.sleep(2)
        current_parent = os.getppid()
        if current_parent == 1 or current_parent != parent_pid:
            os._exit(0)


def main() -> None:
    if sys.argv[1:2] == ['--hook']:
        from app.integrations.hook_entry import run_hook
        return run_hook(sys.argv[2:])
    if sys.argv[1:]:
        raise SystemExit('Unsupported service arguments')
    data_dir = Path(os.getenv("CONTEXTCORE_DATA_DIR", Path.home() / "Library/Application Support/ContextCore"))
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CONTEXTCORE_DB", str(data_dir / "contextcore.db"))
    threading.Thread(target=monitor_parent_process, daemon=True).start()

    import uvicorn
    from app.database import create_schema, seed_data
    from app.main import app

    create_schema()
    seed_data()
    uvicorn.run(app, host="127.0.0.1", port=8420, log_level="warning")


if __name__ == "__main__":
    main()
