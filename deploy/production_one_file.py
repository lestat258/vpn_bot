#!/usr/bin/env python3
"""Guarded, manual, ONE-FILE vpn4us source update. NEVER starts or edits 3x-ui.

  --check            read-only preflight
  --apply            backup + atomic replacement of one pinned .py file
  --rollback PATH    backup restore if the pinned updated file is still present

This tool is meant to be installed as a root-owned file after its Git blob hash
has been verified. Do not execute arbitrary scripts directly from staging as root.
"""
import argparse
import hashlib
import os
import pwd
import secrets
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STAGING = Path('/home/vpn4us-deploy/staging/repo/services/vpn_service.py')
PRODUCTION = Path('/opt/vpn-bot/services/vpn_service.py')
BACKUP_DIR = Path('/root/vpn4us-backups/source-deploy')
EXPECTED_CURRENT = '2fe91f903a74925c73a8999d776fd6ad8d04e321c2b09c6702cb40bb49384733'
EXPECTED_UPDATED = '4226fd190c2630458b10427259b4953a32cc6cd373f4b193605d400bd906c46c'
SERVICES = ('vpn-bot', 'vpn-admin', 'vpn-webhook')


class Blocked(RuntimeError):
    pass


def safe_regular(path, expected_uid=None):
    if path.is_symlink():
        raise Blocked('Symlink not permitted: ' + str(path))
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise Blocked('Missing file: ' + str(path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise Blocked('Non-regular file: ' + str(path))
    if expected_uid is not None and info.st_uid != expected_uid:
        raise Blocked('Unexpected file owner: ' + str(path))
    if info.st_size > 1_000_000:
        raise Blocked('Unexpected file size: ' + str(path))
    return info


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_checked(path, expected_uid=None):
    info = safe_regular(path, expected_uid)
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise Blocked('File changed while reading: ' + str(path))
    return data, info


def service_states():
    result = {}
    for svc in SERVICES:
        proc = subprocess.run(
            ['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=10
        )
        result[svc] = proc.stdout.strip() or 'unknown'
    return result


def ensure_services_stopped():
    states = service_states()
    for svc, state in states.items():
        print(f'{svc}: {state}')
    if any(state != 'inactive' for state in states.values()):
        raise Blocked('All three application services must be inactive for source replacement')


def ensure_directories():
    # Reject directory symlinks, including shared paths under user-writable staging.
    for path in (STAGING.parent, STAGING.parent.parent, PRODUCTION.parent, PRODUCTION.parent.parent):
        if path.is_symlink() or not path.is_dir():
            raise Blocked('Unexpected directory or symlink: ' + str(path))


def preflight():
    if os.geteuid() != 0:
        raise Blocked('Run from root SSH session')
    ensure_directories()
    deploy_uid = pwd.getpwnam('vpn4us-deploy').pw_uid
    source, _ = load_checked(STAGING, deploy_uid)
    target, metadata = load_checked(PRODUCTION, 0)
    try:
        compile(source.decode('utf-8'), str(STAGING), 'exec')
    except (SyntaxError, UnicodeError) as exc:
        raise Blocked('New source fails Python syntax check') from exc
    source_hash, target_hash = sha(source), sha(target)
    print('Staging source SHA256:', source_hash[:16])
    print('Production source SHA256:', target_hash[:16])
    if source_hash != EXPECTED_UPDATED:
        raise Blocked('Staging content differs from reviewed version; NO UPDATE')
    if target_hash not in (EXPECTED_CURRENT, EXPECTED_UPDATED):
        raise Blocked('Production differs from earlier reviewed snapshot; NO UPDATE')
    ensure_services_stopped()
    print('Eligible state:', 'already updated' if target_hash == EXPECTED_UPDATED else 'one-file update')
    return source, target, metadata, target_hash


def atomic_replace(data, dest, metadata):
    fd, tempname = tempfile.mkstemp(prefix='.vpn_service.', suffix='.tmp', dir=str(dest.parent))
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            os.fchown(file.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(file.fileno(), stat.S_IMODE(metadata.st_mode))
        os.replace(tempname, dest)
        dir_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)
    if sha(dest.read_bytes()) != sha(data):
        raise Blocked('Post-write hash mismatch. Reconcile manually; do not retry.')


def backup_original(data):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4)
    directory = BACKUP_DIR / label
    directory.mkdir(mode=0o700)
    target = directory / 'vpn_service.py'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    if sha(target.read_bytes()) != EXPECTED_CURRENT:
        raise Blocked('Backup verification failed; no production write attempted')
    return target


def apply():
    source, original, metadata, state = preflight()
    if state == EXPECTED_UPDATED:
        print('ALREADY APPLIED: no changes made')
        return
    backup_path = backup_original(original)
    print('Verified backup:', backup_path)
    # Reject a concurrent change before replacing the production code.
    if sha(load_checked(PRODUCTION, 0)[0]) != EXPECTED_CURRENT:
        raise Blocked('Production changed since backup; NO UPDATE')
    atomic_replace(source, PRODUCTION, metadata)
    print('APPLY SUCCESS: one source file replaced, no restart, no DB or 3x-ui modifications')
    print('Rollback backup:', backup_path)


def rollback(path):
    if os.geteuid() != 0:
        raise Blocked('Rollback requires root')
    ensure_directories()
    ensure_services_stopped()
    backup = Path(path)
    if backup.is_symlink() or backup.name != 'vpn_service.py':
        raise Blocked('Invalid backup path')
    # Backup must be beneath the dedicated root-owned backup directory.
    if backup.parent.parent != BACKUP_DIR or backup.parent.is_symlink():
        raise Blocked('Backup path outside dedicated source-deploy directory')
    data, _ = load_checked(backup, 0)
    if sha(data) != EXPECTED_CURRENT:
        raise Blocked('Backup contents do not match verified original')
    current, metadata = load_checked(PRODUCTION, 0)
    if sha(current) != EXPECTED_UPDATED:
        raise Blocked('Production has changed since this deployment; no automated rollback')
    atomic_replace(data, PRODUCTION, metadata)
    print('ROLLBACK SUCCESS: original source restored; no services restarted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', metavar='BACKUP_PATH')
    args = parser.parse_args()
    if args.check:
        preflight()
        print('CHECK ONLY: no files changed')
    elif args.apply:
        apply()
    else:
        rollback(args.rollback)


if __name__ == '__main__':
    try:
        main()
    except (Blocked, OSError, subprocess.SubprocessError) as exc:
        print('BLOCKED:', str(exc), file=sys.stderr)
        sys.exit(1)
