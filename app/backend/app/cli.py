"""
ContextCore Command Line Interface.

Provides local service operations:
- serve: Run background server on loopback with single worker and PID tracking.
- status: Check service state, PID, port, autostart preferences, and launchd registration.
- install: Generate launchd plist and register with macOS launchctl.
- uninstall: Unload from launchctl and remove plist file.
- stop: Gracefully stop running service with PID verification to avoid KeepAlive resurrection.
- generate-plist: Output XML plist content to stdout.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Sequence

from app import runtime


def is_loopback(host: str) -> bool:
    """Verify host address is strictly loopback (no remote bind allowed)."""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(normalized)
        return ip.is_loopback
    except ValueError:
        return False


def create_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="contextcore",
        description="ContextCore Background Service & Management CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available sub-commands")

    # serve command
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the ContextCore backend server on loopback (single worker)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface (must be loopback, default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=runtime.DEFAULT_PORT,
        help="Port to listen on (default: 8420)",
    )
    serve_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory (sets process-local CONTEXTCORE_DATA_DIR)",
    )
    serve_parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="Uvicorn log level (default: info)",
    )
    serve_parser.add_argument(
        "--no-static",
        action="store_true",
        help="Do not mount frontend static distribution even if present",
    )
    serve_parser.add_argument("--frontend-dir", default=None, help="Explicit built frontend directory; pinned into the service data directory at startup")

    # status command
    status_parser = subparsers.add_parser("status", help="Show service and autostart status")
    status_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Output status as structured JSON",
    )

    # install command
    install_parser = subparsers.add_parser("install", help="Install macOS launchd autostart plist")
    install_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )
    install_parser.add_argument(
        "--python-path",
        default=None,
        help="Explicit path to Python interpreter",
    )
    install_parser.add_argument(
        "--port",
        type=int,
        default=runtime.DEFAULT_PORT,
        help="Port for the service (default: 8420)",
    )
    install_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the service (default: 127.0.0.1)",
    )
    install_parser.add_argument(
        "--no-load",
        action="store_true",
        help="Create plist file without loading into launchctl",
    )

    # uninstall command
    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall macOS launchd autostart plist")
    uninstall_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )

    # stop command
    stop_parser = subparsers.add_parser("stop", help="Stop running ContextCore service")
    stop_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )
    stop_parser.add_argument(
        "--port",
        type=int,
        default=runtime.DEFAULT_PORT,
        help="Service port for --stop-port-owner / --require-port-free (default: 8420)",
    )
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Timeout in seconds to wait for shutdown (default: 5.0)",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Force terminate using SIGKILL if graceful shutdown times out",
    )
    stop_parser.add_argument(
        "--stop-port-owner",
        action="store_true",
        help="Stop a current-user ContextCore listener bound to --port when no PID file exists in --data-dir",
    )
    stop_parser.add_argument(
        "--require-port-free",
        action="store_true",
        help="Return non-zero if --port remains occupied after stop",
    )

    # generate-plist command
    plist_parser = subparsers.add_parser("generate-plist", help="Print generated launchd plist to stdout")
    plist_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )
    plist_parser.add_argument(
        "--python-path",
        default=None,
        help="Explicit path to Python interpreter",
    )
    plist_parser.add_argument(
        "--port",
        type=int,
        default=runtime.DEFAULT_PORT,
        help="Port for the service (default: 8420)",
    )
    plist_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the service (default: 127.0.0.1)",
    )

    # backup command
    backup_parser = subparsers.add_parser("backup", help="Create an integrity-verified whole-database SQLite backup")
    backup_parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to ContextCore data directory",
    )
    backup_parser.add_argument(
        "--output",
        default=None,
        help="Destination file or directory path for the backup",
    )
    backup_parser.add_argument(
        "--json",
        action="store_true",
        help="Output backup receipt as structured JSON",
    )

    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    """Execute the 'serve' command."""
    if not is_loopback(args.host):
        print(
            f"Security error: '{args.host}' is not a loopback address. "
            "ContextCore service strictly requires loopback binding (127.0.0.1 / localhost).",
            file=sys.stderr,
        )
        return 1

    selected_data = Path(args.data_dir or os.environ.get('CONTEXTCORE_DATA_DIR') or runtime.get_default_data_dir()).expanduser()
    from app.private_storage import check_directory_path, private_directory
    try:
        check_directory_path(selected_data)
    except OSError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    resolved_dir = runtime.resolve_data_dir(args.data_dir)
    try:
        pinned_release = runtime.verified_serving_release(
            resolved_dir, Path(__file__).resolve().parent.parent,
            frontend_dir=Path(args.frontend_dir).expanduser() if getattr(args, 'frontend_dir', None) else None,
            release_id=os.environ.get('CONTEXTCORE_RELEASE_ID'),
        )
    except ValueError as exc:
        print(f"Error: Published service verification failed: {exc}", file=sys.stderr)
        return 1
    if pinned_release and not getattr(args, 'frontend_dir', None):
        args.frontend_dir = pinned_release['frontend_dir']
    if not args.no_static and getattr(args, "frontend_dir", None):
        frontend = Path(args.frontend_dir).expanduser()
        if not frontend.is_dir() or not (frontend / "index.html").is_file():
            print("Error: Explicit frontend distribution is unavailable", file=sys.stderr)
            return 1
    private_directory(resolved_dir)

    # Set process-local environment variables for config.py and SQLite database
    os.environ["CONTEXTCORE_DATA_DIR"] = str(resolved_dir)
    os.environ["CONTEXTCORE_DB"] = str(resolved_dir / "contextcore.db")
    os.environ["CONTEXTCORE_LISTEN_URL"] = f"http://127.0.0.1:{args.port}"

    # Check if another verified instance is already running
    running, active_pid = runtime.is_service_running(resolved_dir)
    if running:
        print(
            f"Error: ContextCore service is already running (PID: {active_pid}). "
            "Stop the running instance first with 'contextcore stop'.",
            file=sys.stderr,
        )
        return 1

    bind_host = args.host.strip().lower()
    if bind_host == "localhost":
        bind_host = "127.0.0.1"
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    # Bind before importing the application: a failed startup must not open or
    # migrate the database, start jobs, or replace the service PID.
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((bind_host, args.port))
            listener.listen(128)
        except (OSError, OverflowError) as exc:
            print(f"Error: Could not bind local service: {exc}", file=sys.stderr)
            return 1
        return _serve_reserved(args, resolved_dir, pinned_release, listener)


def _serve_reserved(args: argparse.Namespace, resolved_dir: Path,
                    pinned_release: dict | None, listener: socket.socket) -> int:
    # Optional static hosting for production frontend if dist exists
    if pinned_release:
        from app.main import app
        app.state.release = {
            'release_id': pinned_release['release_id'], 'api_contract': 2,
            'frontend_sha256': pinned_release['frontend_sha256'],
        }
    if not args.no_static:
        is_explicit = bool(getattr(args, "frontend_dir", None))
        if is_explicit:
            frontend_dist = Path(args.frontend_dir).expanduser().resolve()
        else:
            # Source development/workspace discovery matches vite outDir (dist-staging)
            # Stale frontend/dist is intentionally ignored without explicit configuration
            frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist-staging"

        if frontend_dist.exists() and frontend_dist.is_dir() and (frontend_dist / "index.html").is_file():
            try:
                from fastapi.staticfiles import StaticFiles
                from app.main import app

                # Mount at root only if not already mounted
                has_root_mount = any(
                    getattr(route, "path", None) == "" or getattr(route, "path", None) == "/"
                    for route in getattr(app, "routes", [])
                )
                if not has_root_mount:
                    from app.frontend_release import snapshot_frontend, PinnedStaticFiles
                    release = snapshot_frontend(frontend_dist, resolved_dir)
                    app.state.release = {
                        "release_id": pinned_release['release_id'] if pinned_release else "development",
                        "api_contract": 2,
                        "frontend_sha256": release.name,
                    }
                    app.mount("/", PinnedStaticFiles(directory=release, html=True), name="frontend_dist")
            except Exception as e:
                print(f"Warning: Could not mount frontend static distribution: {e}", file=sys.stderr)
                if is_explicit:
                    return 1
        elif is_explicit:
            print("Error: Explicit frontend distribution is unavailable", file=sys.stderr)
            return 1

    import uvicorn

    server_config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=1,
        log_level=args.log_level,
        loop="auto",
        timeout_graceful_shutdown=3.0,
    )
    server = uvicorn.Server(server_config)

    # Clean signal handling ensuring clean exit code 0
    def handle_exit_signal(signum: int, frame: Any) -> None:
        server.should_exit = True
        try:
            from sse_starlette.sse import AppStatus
            AppStatus.should_exit = True
        except ImportError:
            pass
        try:
            from app.event_bus import notify_shutdown
            notify_shutdown()
        except Exception:
            pass

    original_server_handle_exit = server.handle_exit

    def server_exit_wrapper(sig: int, frame: Any) -> None:
        try:
            from sse_starlette.sse import AppStatus
            AppStatus.should_exit = True
        except ImportError:
            pass
        try:
            from app.event_bus import notify_shutdown
            notify_shutdown()
        except Exception:
            pass
        original_server_handle_exit(sig, frame)

    server.handle_exit = server_exit_wrapper

    try:
        signal.signal(signal.SIGTERM, handle_exit_signal)
        signal.signal(signal.SIGINT, handle_exit_signal)
    except (ValueError, OSError):
        pass

    # Record PID with identity metadata before starting
    runtime.write_pid_file(
        pid=os.getpid(),
        port=args.port,
        host=args.host,
        data_dir=resolved_dir,
    )
    # Clear any stale shutdown receipt from prior run
    runtime.clear_shutdown_receipt(resolved_dir)

    try:
        from sse_starlette.sse import AppStatus
        AppStatus.should_exit = False
    except ImportError:
        pass
    try:
        from app.event_bus import reset_shutdown
        reset_shutdown()
    except Exception:
        pass

    try:
        server.run(sockets=[listener])
    finally:
        try:
            from sse_starlette.sse import AppStatus
            AppStatus.should_exit = False
        except ImportError:
            pass
        try:
            from app.event_bus import reset_shutdown
            reset_shutdown()
        except Exception:
            pass
        current_pid = runtime.read_pid_file(resolved_dir)
        if current_pid and current_pid.get("pid") == os.getpid():
            runtime.remove_pid_file(resolved_dir)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Execute the 'status' command."""
    status = runtime.autostart_status(args.data_dir)
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0

    print("ContextCore Service Status:")
    print(f"  Platform:            {status['platform']}")
    print(f"  Service Running:     {'YES' if status['running'] else 'NO'}" + (f" (PID: {status['pid']}, Port: {status['port']})" if status['running'] else ""))
    print(f"  Autostart Desired:   {'YES' if status['desired'] else 'NO'}")
    print(f"  Launchd Installed:   {'YES' if status['installed'] else 'NO'}")
    print(f"  Launchd Registered:  {'YES' if status['registered'] else 'NO'}")
    if status.get("label_plist_present") and not status.get("plist_owned_by_data_dir"):
        print("  Label Plist Present: YES")
        print(f"  Plist Ownership:     {status.get('plist_ownership')}")
        print(f"  Label Registered:    {'YES' if status.get('label_registered') else 'NO'}")
    if status.get("stop_status"):
        print(f"  Shutdown Status:     {status['stop_status'].upper()}" + (f" (Error: {status['stop_error']})" if status.get("stop_error") else ""))
    print(f"  Data Directory:      {status['data_dir']}")
    print(f"  Plist Path:          {status['plist_path']}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Execute the 'install' command."""
    res = runtime.install_autostart(
        data_dir=args.data_dir,
        python_path=args.python_path,
        port=args.port,
        host=args.host,
        register=not args.no_load,
    )
    if res.get("ok"):
        print("ContextCore launchd autostart successfully installed:")
        print(f"  Plist Path:  {res.get('plist_path')}")
        print(f"  Registered:  {'YES' if res.get('registered') else 'NO (created without load)'}")
        return 0

    print(f"Error installing autostart: {res.get('error')}", file=sys.stderr)
    return 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Execute the 'uninstall' command."""
    res = runtime.uninstall_autostart(data_dir=args.data_dir)
    if res.get("ok"):
        print("ContextCore launchd autostart successfully uninstalled:")
        print(f"  Plist Path:  {res.get('plist_path')}")
        print(f"  Unloaded:    {'YES' if res.get('unloaded') else 'NO'}")
        return 0

    print(f"Error uninstalling autostart: {res.get('error')}", file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    """Execute the 'stop' command."""
    stop_port = getattr(args, "port", runtime.DEFAULT_PORT)
    stop_owner = bool(getattr(args, "stop_port_owner", False))
    require_free = bool(getattr(args, "require_port_free", False))
    if stop_owner:
        res = runtime.stop_service(
            data_dir=args.data_dir,
            timeout=args.timeout,
            force=args.force,
            port=stop_port,
            stop_port_owner=True,
        )
    else:
        res = runtime.stop_service(data_dir=args.data_dir, timeout=args.timeout, force=args.force)
    if not res.get("ok"):
        msg = res.get("error") or res.get("message") or "Service stop failed."
        print(msg, file=sys.stderr)
        return 1
    if require_free and runtime.is_port_occupied(stop_port):
        owners = runtime.find_current_user_port_owners(stop_port)
        owner_hint = f" (ContextCore listener PID {', '.join(str(p) for p in owners)})" if owners else ""
        print(f"Port {stop_port} is still occupied after stop{owner_hint}.", file=sys.stderr)
        return 1
    msg = res.get("message") or "Service stop completed."
    print(msg, file=sys.stdout)
    return 0

def cmd_generate_plist(args: argparse.Namespace) -> int:
    """Execute the 'generate-plist' command."""
    content = runtime.generate_launchd_plist(
        data_dir=args.data_dir,
        python_path=args.python_path,
        port=args.port,
        host=args.host,
    )
    print(content)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Execute the 'backup' command."""
    from app.database import backup_database
    db_path = None
    if getattr(args, "data_dir", None):
        resolved_dir = runtime.resolve_data_dir(args.data_dir)
        db_path = resolved_dir / "contextcore.db"
    try:
        res = backup_database(destination=getattr(args, "output", None), db_path=db_path)
    except Exception as exc:
        print(f"Error creating database backup: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("ContextCore database backup created:")
        print(f"  Backup Path: {res.get('backup_path')}")
        print(f"  Size Bytes:  {res.get('size_bytes')}")
        print(f"  SHA-256:     {res.get('sha256')}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = create_parser()
    args = parser.parse_args(argv)

    handlers = {
        "serve": cmd_serve,
        "status": cmd_status,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "stop": cmd_stop,
        "generate-plist": cmd_generate_plist,
        "backup": cmd_backup,
    }

    handler = handlers.get(args.command)
    if not handler:
        parser.print_help(sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
