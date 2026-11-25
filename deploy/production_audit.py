#!/usr/bin/env python3
"""Read-only candidate inventory for a FUTURE, manually approved production deployment.

Run as vpn4us-deploy. This script reads source files only, never opens .env/DB/key
files, never writes files, and never executes code from the production directory.
An inventory is NOT approval to deploy or restart services.
"""
import hashlib
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

STAGING = Path("/home/vpn4us-deploy/staging/repo")
PRODUCTION = Path("/opt/vpn-bot")
PROTECTED_NAMES = {".env", "data.db", "data.db-wal", "data.db-shm", "config.local.py"}
PROTECTED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".p12"}


def allowed(filename):
    """Conservative set of source files to review, NOT a deployment allowlist."""
    path = PurePosixPath(filename)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    if any(part in {"venv", ".venv", "node_modules", "__pycache__", "backups", "secrets"} for part in path.parts):
        return False
    if path.name in PROTECTED_NAMES or path.name.startswith(".env") or path.suffix.lower() in PROTECTED_SUFFIXES:
        return False
    if len(path.parts) == 1:
        return path.name == "requirements.txt" or (
            path.suffix == ".py" and
            not path.name.startswith(("test_", "apply_", "patch_"))
        )
    if path.parts[0] in {"services", "admin_modules", "handlers"}:
        return path.suffix == ".py"
    if path.parts[0] == "templates":
        return path.suffix in {".html", ".jinja2"}
    if path.parts[0] == "static":
        return path.suffix in {".css", ".js"}
    return False


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            h.update(chunk)
    return h.digest()


def regular_without_symlinks(path, root):
    current = root
    try:
        for component in path.relative_to(root).parts:
            current = current / component
            if current.is_symlink():
                return False
        return stat.S_ISREG(path.stat().st_mode)
    except (OSError, ValueError):
        return False


def show_service_status(name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name], capture_output=True, text=True, timeout=6
        )
        print(f"  {name}: {result.stdout.strip() or 'unknown'}")
    except (OSError, subprocess.TimeoutExpired):
        print(f"  {name}: unavailable")


def main():
    if pwd.getpwuid(os.geteuid()).pw_name != "vpn4us-deploy":
        raise RuntimeError("Run as vpn4us-deploy; root is not needed")
    if not (STAGING / ".git").is_dir() or not PRODUCTION.is_dir():
        raise RuntimeError("Expected staging checkout or production directory is missing")
    cmd = subprocess.run(
        ["git", "-C", str(STAGING), "ls-files", "-z"],
        capture_output=True, timeout=20, check=True
    )
    commit = subprocess.run(
        ["git", "-C", str(STAGING), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()
    entries = [os.fsdecode(entry) for entry in cmd.stdout.split(b"\0") if entry]
    candidates = sorted(name for name in entries if allowed(name))
    counts = {"same": 0, "changed": 0, "new": 0, "blocked": 0}
    differences = []

    for name in candidates:
        source = STAGING / name
        dest = PRODUCTION / name
        if not regular_without_symlinks(source, STAGING):
            counts["blocked"] += 1
            differences.append(("BLOCKED stage file", name))
            continue
        if not dest.exists() and not dest.is_symlink():
            counts["new"] += 1
            differences.append(("NEW", name))
            continue
        if not regular_without_symlinks(dest, PRODUCTION):
            counts["blocked"] += 1
            differences.append(("BLOCKED production path", name))
            continue
        try:
            equal = sha256_file(source) == sha256_file(dest)
        except (OSError, PermissionError):
            counts["blocked"] += 1
            differences.append(("BLOCKED unreadable", name))
            continue
        if equal:
            counts["same"] += 1
        else:
            counts["changed"] += 1
            differences.append(("CHANGED", name))

    print("=== VPN4US PRODUCTION READ-ONLY AUDIT ===")
    print("Staging commit:", commit)
    print("Staging files considered:", len(candidates))
    for name in ("same", "changed", "new", "blocked"):
        print(name + ":", counts[name])
    print("=== Differences (paths only, no contents) ===")
    for kind, name in differences:
        print(f"{kind}: {name}")
    print("=== Working services (read-only) ===")
    for name in ("vpn-bot", "vpn-admin", "vpn-webhook", "x-ui"):
        show_service_status(name)
    print("=== Access check (no sensitive files opened) ===")
    for name in (".env", "data.db"):
        p = PRODUCTION / name
        print(f"{name}: {'PROTECTED' if p.is_file() and not os.access(p, os.R_OK) else 'REVIEW NEEDED'}")
    print("DONE: audit only. No files copied, deleted or changed. No restarts.")
    print("NEXT: manually review diff and plan an explicit, backed-up deployment.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        print("AUDIT BLOCKED:", type(exc).__name__, str(exc)[:250], file=sys.stderr)
        sys.exit(1)
