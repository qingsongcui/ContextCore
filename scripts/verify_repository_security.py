#!/usr/bin/env python3
"""Fail-closed public dependency audits and bounded repository hygiene checks.

Requires Python 3.11+, npm, uv and uvx. Python audit input is the exact universal
runtime graph exported from uv.lock (including platform-specific variants), not
an independently resolved requirements.txt. pip-audit runs in an isolated tool
environment against public PyPI only; no package/advisory exemptions apply.

The custom hygiene detector is intentionally bounded: known credential shapes
and prohibited runtime/key files, in current tracked/nonignored untracked files
and all reachable Git source blobs. It is not comprehensive secret detection or
security certification. Generated directories are excluded, never tests/QA.
Scanner errors, shallow history, unsupported lock exports and oversized source
blobs fail closed. Credential bytes and raw scanner diagnostics are never logged.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
PIP_AUDIT_VERSION = "2.10.1"
MAX_SOURCE_BYTES = 16 * 1024 * 1024
GENERATED_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", "dist", "dist-staging", "build",
})
SUSPICIOUS_EXTENSIONS = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".kdbx", ".sqlite", ".sqlite3", ".db",
    ".shm", ".wal", ".pid", ".pyc",
})
SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |DSA |EC |PGP )?PRIVATE KEY-----"), "private-key header"),
    (re.compile(r"(?:api[_-]?key|secret[_-]?key|access[_-]?token)[\"']?\s*[:=]\s*['\"][A-Za-z0-9_./+\-=]{24,}['\"]", re.IGNORECASE), "literal credential assignment"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), "OpenAI-style token"),
    (re.compile(r"\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{24,}"), "Slack token"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access-key ID"),
)
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9][A-Za-z0-9_.+!-]*)")

# One reviewed false positive, not a tests-directory or credential-prefix
# exemption. The provider-check fixture injects a synthetic resolve_provider
# value and routes requests through httpx.MockTransport (test_provider_checks.py
# lines 20-35). Main confirmed this sentinel on 2026-10-25, including historical
# blob 1a86da6b3e4276d73483074074ded1f99febf6fa. Only the exact credential-assignment
# match at this exact path is exempt; any changed/additional credential is scanned.
# Store only its SHA-256 fingerprint, never log matched bytes.
REVIEWED_SYNTHETIC_MATCHES = {
    ("app/backend/tests/test_provider_checks.py", "literal credential assignment"):
        frozenset({"c3f5161d279fd4dfec1dd1821aec923b5324770871aa12c0de077126aa07ade3"}),
}


def log(message: str) -> None:
    print(f"[verify_security] {message}")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def public_audit_environment() -> dict[str, str]:
    # Do not inherit private index credentials, injected pip-audit options or uv
    # configuration. Neither audit contacts a private registry or loads .netrc.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "UV_", "PIP_AUDIT_"))}
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_INDEX_URL": "https://pypi.org/simple",
                "UV_DEFAULT_INDEX": "https://pypi.org/simple", "NETRC": os.devnull,
                "UV_PYTHON_DOWNLOADS": "never", "UV_KEYRING_PROVIDER": "disabled"})
    return env


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                          text=True, check=False, timeout=timeout)


def frontend_report_is_clean(report: object, returncode: int) -> bool:
    """Accept only a complete npm v2 audit report with a successful exit status."""
    if returncode != 0 or not isinstance(report, dict) or "error" in report:
        return False
    if report.get("auditReportVersion") != 2 or not isinstance(report.get("vulnerabilities"), dict):
        return False
    metadata = report.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("dependencies"), dict):
        return False
    dependencies = metadata["dependencies"]
    dependency_fields = ("prod", "dev", "optional", "peer", "peerOptional", "total")
    if any(type(dependencies.get(key)) is not int or dependencies[key] < 0 for key in dependency_fields):
        return False
    if dependencies["total"] == 0:
        return False
    counts = metadata.get("vulnerabilities")
    fields = ("info", "low", "moderate", "high", "critical", "total")
    if not isinstance(counts, dict) or any(type(counts.get(key)) is not int or counts[key] < 0 for key in fields):
        return False
    if sum(counts[key] for key in fields[:-1]) != counts["total"]:
        return False
    # No advisory allowlist: a successful audit means no reported vulnerabilities.
    return counts["total"] == 0 and report["vulnerabilities"] == {}


def check_frontend_audit() -> bool:
    log("Auditing locked frontend dependencies against the official npm registry...")
    try:
        proc = run(["npm", "audit", "--registry=https://registry.npmjs.org", "--json"],
                   cwd=REPO_ROOT / "app/frontend", timeout=180)
        report = json.loads(proc.stdout)
        if not frontend_report_is_clean(report, proc.returncode):
            log("  FAIL/UNKNOWN: npm audit did not return a complete, successful zero-advisory report.")
            return False
    except (OSError, subprocess.SubprocessError, ValueError):
        log("  UNKNOWN: npm audit unavailable, timed out or returned invalid JSON (diagnostics withheld).")
        return False
    log("  Frontend audit: complete successful report; zero known advisories.")
    return True


def runtime_pins(export: str) -> set[tuple[str, str]]:
    """Convert uv's universal export into a union of all exact runtime variants.

    Markers are deliberately broadened, not evaluated on the scanner host: a
    macOS-only package must also be audited on Linux CI. No package is installed.
    Unsupported/VCS/local/unpinned exports fail rather than disappear from scope.
    """
    pins = set()
    for line in export.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = line.split(";", 1)[0].strip()
        match = PIN.fullmatch(requirement)
        if match is None:
            raise ValueError("Unsupported runtime export entry")
        pins.add((normalize_name(match[1]), match[2]))
    if not pins:
        raise ValueError("Empty runtime export")
    return pins


def python_report_is_clean(report: object, returncode: int,
                           expected: set[tuple[str, str]]) -> bool:
    """Reject advisory, skipped, malformed, partial and unsuccessful scans."""
    if returncode != 0 or not isinstance(report, dict) or "error" in report:
        return False
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies or report.get("fixes") != []:
        return False
    observed = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or "skip_reason" in dependency or "error" in dependency:
            return False
        name, version = dependency.get("name"), dependency.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            return False
        if dependency.get("vulns") != []:
            return False
        pin = (normalize_name(name), version)
        if pin in observed:
            return False
        observed.add(pin)
    return bool(expected) and observed == expected


def check_python_security() -> bool:
    log(f"Auditing canonical locked runtime graph with isolated pip-audit=={PIP_AUDIT_VERSION}...")
    backend = REPO_ROOT / "app/backend"
    env = public_audit_environment()
    try:
        export = run(["uv", "--no-config", "export", "--locked", "--no-dev", "--no-default-groups",
                      "--no-emit-project", "--no-hashes", "--no-annotate", "--no-header",
                      "--format", "requirements.txt"], cwd=backend, env=env)
        if export.returncode != 0:
            log("  UNKNOWN: canonical runtime lock export failed; no alternate resolution accepted.")
            return False
        pins = runtime_pins(export.stdout)
        with tempfile.TemporaryDirectory(prefix="contextcore-public-audit-") as temporary:
            requirements = Path(temporary) / "runtime.txt"
            requirements.write_text("".join(f"{name}=={version}\n" for name, version in sorted(pins)))
            # APIs: https://github.com/pypa/pip-audit/tree/v2.10.1
            # --disable-pip consumes the already locked graph without installing
            # the application or resolving a different set of transitive packages.
            proc = run(["uvx", "--no-config", "--isolated", "--default-index", "https://pypi.org/simple",
                        "--from", f"pip-audit=={PIP_AUDIT_VERSION}", "pip-audit",
                        "--requirement", str(requirements), "--no-deps", "--disable-pip", "--strict",
                        "--vulnerability-service", "pypi", "--format", "json",
                        "--progress-spinner", "off", "--desc", "off"],
                       cwd=backend, env=env, timeout=300)
            report = json.loads(proc.stdout)
            if not python_report_is_clean(report, proc.returncode, pins):
                # Only identifiers from validated report fields, never dependency
                # descriptions or raw command output (which may contain secrets).
                log("  FAIL/UNKNOWN: Python advisories, incomplete coverage, skipped packages or scanner failure.")
                return False
    except (OSError, subprocess.SubprocessError, ValueError):
        log("  UNKNOWN: Python audit unavailable, unsupported or malformed (diagnostics withheld).")
        return False
    log(f"  Python audit: all {len(pins)} locked runtime variants covered; zero known advisories.")
    return True


def generated_path(relative: str) -> bool:
    return any(part in GENERATED_PARTS or part.endswith(".egg-info") for part in PurePosixPath(relative).parts)


def prohibited_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (path.suffix.lower() in SUSPICIOUS_EXTENSIONS
            or path.name.endswith(("-wal", "-shm"))
            or (path.name.startswith(".env") and path.name != ".env.example")
            or any(part in {".contextcore", "workspace", ".slim"} for part in path.parts)
            or path.name in {"current-release.json", "credentials.json", "auth.json"})


def content_findings(content: bytes, relative: str) -> list[tuple[int, str]]:
    # Latin-1 is lossless for bytes and retains ASCII patterns even in binary
    # blobs. No file type, tests, QA or example directories are exempted.
    text = content.decode("latin-1")
    findings = []
    for pattern, description in SECRET_PATTERNS:
        reviewed_hashes = REVIEWED_SYNTHETIC_MATCHES.get((relative, description), ())
        for match in pattern.finditer(text):
            if reviewed_hashes and hashlib.sha256(match.group().encode("latin-1")).hexdigest() in reviewed_hashes:
                continue
            findings.append((text.count("\n", 0, match.start()) + 1, description))
    return findings


def inspect_source(relative: str, content: bytes, location: str) -> bool:
    clean = True
    if prohibited_path(relative):
        log(f"  FAIL: prohibited runtime/key artifact at {location}.")
        clean = False
    for line, description in content_findings(content, relative):
        log(f"  FAIL: {description} at {location}, line {line} (matched content withheld).")
        clean = False
    return clean


class HygieneError(ValueError):
    """A scanner-authored, safe diagnostic; never raw subprocess/OS text."""


def safe_relative_location(relative: str) -> str:
    # Preserve actionable relative locations while removing known credential
    # shapes even if embedded in filenames; JSON escaping prevents log injection.
    for pattern, _ in SECRET_PATTERNS:
        relative = pattern.sub("[REDACTED]", relative)
    return json.dumps(relative, ensure_ascii=True)


def git(*args: str, timeout: int = 120) -> bytes:
    operation = args[0]
    object_context = f" object {args[-1]}" if re.fullmatch(r"[0-9a-f]{40,64}", args[-1]) else ""
    try:
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                              check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HygieneError(f"git-timeout: {operation}{object_context}") from None
    except OSError:
        raise HygieneError(f"git-unavailable: {operation}{object_context}") from None
    if proc.returncode:
        raise HygieneError(f"git-command-failed: {operation}{object_context} (exit {proc.returncode})")
    return proc.stdout


def source_has_symlink(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or ".." in parts:
        raise HygieneError(f"unsafe-source-path: {safe_relative_location(relative)}")
    current = REPO_ROOT
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def check_repository_hygiene() -> bool:
    log("Bounded credential/artifact scan: current sources plus all reachable Git blobs; not certification.")
    try:
        history_state = git("rev-parse", "--is-shallow-repository").strip()
        if history_state == b"true":
            raise HygieneError("shallow-history: full reachable history is required; obtain complete history from the approved public origin (CI uses fetch-depth: 0)")
        if history_state != b"false":
            raise HygieneError("invalid-history-status: Git did not return a supported repository history state")
        paths = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""}
        clean = True
        scanned = 0
        for raw_path in sorted(paths):
            relative = os.fsdecode(raw_path)
            if generated_path(relative):
                continue
            path = REPO_ROOT / relative
            location = f"worktree source {safe_relative_location(relative)}"
            try:
                if source_has_symlink(relative):
                    log(f"  UNKNOWN: source-symlink: {location}; source was not followed.")
                    clean = False
                    continue
                if not path.exists():  # Deletions remain covered by history.
                    continue
                if not path.is_file():
                    raise HygieneError(f"unsupported-source-type: {location}")
                size = path.stat().st_size
                if size > MAX_SOURCE_BYTES:
                    raise HygieneError(f"oversized-source: {location} ({size} bytes; limit {MAX_SOURCE_BYTES})")
                content = path.read_bytes()
            except OSError:
                raise HygieneError(f"source-unreadable: {location}") from None
            scanned += 1
            clean = inspect_source(relative, content, location) and clean

        # --all includes branch/tag history, including removed files. Report
        # object hashes and redacted/escaped paths, never matched credential bytes.
        objects = git("rev-list", "--objects", "--all").splitlines()
        history_count = 0
        seen = set()
        for entry in objects:
            oid, separator, raw_path = entry.partition(b" ")
            if not separator or oid in seen:
                continue  # Commits and root trees have no path.
            relative = os.fsdecode(raw_path)
            if generated_path(relative):
                continue
            object_id = oid.decode("ascii")
            if git("cat-file", "-t", object_id).strip() != b"blob":
                continue
            seen.add(oid)
            size = int(git("cat-file", "-s", object_id))
            location = f"Git blob {object_id}, path {safe_relative_location(relative)}"
            if size > MAX_SOURCE_BYTES:
                raise HygieneError(f"oversized-history-blob: {location} ({size} bytes; limit {MAX_SOURCE_BYTES})")
            history_count += 1
            clean = inspect_source(relative, git("cat-file", "blob", object_id), location) and clean
        if not history_count:
            raise HygieneError("empty-history-scan: no reachable source blobs were scanned")
    except HygieneError as exc:
        log(f"  UNKNOWN: {exc}")
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        log("  UNKNOWN: unsupported-inventory-data or source-IO error (raw diagnostics withheld).")
        return False
    log(f"  Bounded hygiene coverage: {scanned} current source files, {history_count} reachable source blobs.")
    return clean


def main() -> int:
    results = [check_frontend_audit(), check_python_security(), check_repository_hygiene()]
    log("Private disclosure: follow SECURITY.md; never post credentials or private reproductions publicly.")
    if not all(results):
        log("Security gates FAILED or UNKNOWN; release is blocked.")
        return 1
    log("Required bounded security gates completed successfully; not comprehensive security certification.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
