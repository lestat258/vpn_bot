#!/usr/bin/env python3
"""Read-only, multi-file release planner for the isolated vpn4us staging checkout.

Produces a repeatable inventory of candidate source-file changes; it NEVER copies,
removes, or executes production code. A plan is not approval to deploy.
Runtime data (.env, SQLite, certificates, credentials) are categorically excluded.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

STAGING = Path('/home/vpn4us-deploy/staging/repo')
PRODUCTION = Path('/opt/vpn-bot')
BRANCH = 'automation/bootstrap-2026-11-25'
ORIGIN = 'https://github.com/lestat258/vpn_bot.git'
MAX_FILE_SIZE = 1024 * 1024
ALLOWED_MODULE_DIRS = {'services', 'admin_modules', 'handlers'}
ALLOWED_STATIC_DIRS = {'templates', 'static'}
EXCLUDED_NAMES = {'.env', 'data.db', 'data.db-shm', 'data.db-wal'}
EXCLUDED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.pfx'}


class PlanBlocked(RuntimeError):
    pass


def allowed_source(path_str):
    """Allow only reviewed types/locations. Not a production deploy allowlist."""
    if not isinstance(path_str, str) or not path_str or '\\' in path_str or '\x00' in path_str:
        return False
    path = PurePosixPath(path_str)
    if path.is_absolute() or path_str != path.as_posix() or len(path.parts) > 5:
        return False
    if any(part in ('', '.', '..') or part.startswith('.') for part in path.parts):
        return False
    if path.name in EXCLUDED_NAMES or path.name.startswith('.env') or path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if len(path.parts) == 1:
        return path.suffix == '.py' and not path.name.startswith(('test_', 'apply_', 'patch_'))
    if path.parts[0] in ALLOWED_MODULE_DIRS:
        return path.suffix == '.py'
    if path.parts[0] == 'templates':
        return path.suffix.lower() in ('.html', '.jinja2')
    if path.parts[0] == 'static':
        return path.suffix.lower() in ('.css', '.js')
    return False


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def read_regular_file(root, relative):
    path = root
    if path.is_symlink() or not path.is_dir():
        raise PlanBlocked('Unexpected root path: ' + str(root))
    for component in PurePosixPath(relative).parts:
        path = path / component
        if path.is_symlink():
            raise PlanBlocked('Symlink in candidate path: ' + relative)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_SIZE:
        raise PlanBlocked('Unsupported file type or size: ' + relative)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PlanBlocked('Cannot read candidate: ' + relative) from exc
    if len(data) != info.st_size:
        raise PlanBlocked('Candidate changed while reading: ' + relative)
    return data


def git(*args):
    proc = subprocess.run(
        ['git', '-C', str(STAGING), *args], capture_output=True,
        timeout=30, check=False
    )
    if proc.returncode:
        raise PlanBlocked('Git check failed: ' + ' '.join(args[:2]))
    return proc.stdout


def collect_plan():
    if os.geteuid() == 0:
        raise PlanBlocked('Run as the unprivileged vpn4us-deploy user, not root')
    if not STAGING.is_dir() or not PRODUCTION.is_dir():
        raise PlanBlocked('Expected staging or production directory missing')
    if git('rev-parse', '--abbrev-ref', 'HEAD').decode().strip() != BRANCH:
        raise PlanBlocked('Unexpected staging branch')
    if git('remote', 'get-url', 'origin').decode().strip() != ORIGIN:
        raise PlanBlocked('Unexpected staging origin')
    if git('status', '--porcelain', '--untracked-files=no').strip():
        raise PlanBlocked('Staging working tree/index has modifications')
    commit = git('rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise PlanBlocked('Unexpected staging commit')
    if git('rev-parse', f'origin/{BRANCH}').decode().strip() != commit:
        raise PlanBlocked('Staging does not match its origin tracking branch')

    tracked = git('ls-files', '--stage', '-z').split(b'\0')
    entries = []
    for item in tracked:
        if not item:
            continue
        try:
            metadata, raw = item.split(b'\t', 1)
            mode, blob, stage = metadata.decode('ascii').split(' ')
            name = os.fsdecode(raw)
        except (ValueError, UnicodeError) as exc:
            raise PlanBlocked('Malformed Git index') from exc
        if not allowed_source(name):
            continue
        if mode not in ('100644', '100755') or stage != '0':
            raise PlanBlocked('Unexpected Git index entry: ' + name)
        entries.append((name, blob))

    changes = []
    same = 0
    blocked = 0
    for name, blob in sorted(entries):
        new_data = read_regular_file(STAGING, name)
        if new_data is None or git_blob_sha(new_data) != blob:
            raise PlanBlocked('Staging source differs from checked-in content: ' + name)
        previous = read_regular_file(PRODUCTION, name)
        after = digest(new_data)
        before = digest(previous) if previous is not None else None
        if before == after:
            same += 1
            continue
        changes.append({'path': name, 'action': 'new' if previous is None else 'replace',
                        'before_sha256': before, 'after_sha256': after})
    plan = {'commit': commit, 'changes': changes}
    plan_bytes = json.dumps(plan, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return plan, same, len(entries), digest(plan_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Only output structured plan')
    args = parser.parse_args()
    plan, same, total, fingerprint = collect_plan()
    if args.json:
        print(json.dumps({'plan_sha256': fingerprint, 'same': same, 'tracked_candidates': total,
                          **plan}, indent=2, ensure_ascii=True))
        return
    print('=== VPN4US READ-ONLY MULTI-FILE RELEASE PLAN ===')
    print('Staging commit:', plan['commit'])
    print('Considered source files:', total)
    print('Unchanged:', same)
    print('Changed/new:', len(plan['changes']))
    for change in plan['changes']:
        print(f"{change['action'].upper()}: {change['path']}")
        print(f"  production: {change['before_sha256'] or '(missing)'}")
        print(f"  staging:    {change['after_sha256']}")
    print('Plan fingerprint SHA256:', fingerprint)
    print('READ ONLY: nothing installed, backed up, deleted or restarted')
    print('This plan requires separate human review before any deployment')


if __name__ == '__main__':
    try:
        main()
    except (PlanBlocked, OSError, subprocess.TimeoutExpired) as exc:
        print('PLAN BLOCKED:', str(exc), file=sys.stderr)
        sys.exit(1)
