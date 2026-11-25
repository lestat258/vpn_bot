#!/usr/bin/env python3
"""Manual, source-only VPN4US deployment with an auditable, reversible manifest.

Production invocation:
  --plan
  --apply --commit SHA40 --plan-sha256 SHA256 --approve SOURCE-ONLY
  --rollback BACKUP_ID --approve SOURCE-ONLY

All writes are limited to reviewed source paths in /opt/vpn-bot.
No database, environment, keys, package installation, process starts, or x-ui changes.
Install this script as a root-owned executable after verifying its Git blob hash.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

STAGE = Path('/home/vpn4us-deploy/staging/repo')
PROD = Path('/opt/vpn-bot')
BACKUPS = Path('/root/vpn4us-backups/multifile')
BRANCH = 'automation/bootstrap-2026-11-25'
ORIGIN = 'https://github.com/lestat258/vpn_bot.git'
SERVICES = ('vpn-bot', 'vpn-admin', 'vpn-webhook')
MAX_SIZE = 1024 * 1024


class Blocked(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def allow_path(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        return False
    p = PurePosixPath(name)
    if p.is_absolute() or p.as_posix() != name or len(p.parts) > 5:
        return False
    if any(x in ('.', '..') or x.startswith('.') for x in p.parts):
        return False
    if p.name.startswith('.env') or p.suffix.lower() in (
        '.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.pfx'
    ):
        return False
    if len(p.parts) == 1:
        return p.suffix == '.py' and not p.name.startswith(('test_', 'apply_', 'patch_'))
    if p.parts[0] in ('services', 'admin_modules', 'handlers'):
        return p.suffix == '.py'
    if p.parts[0] == 'templates':
        return p.suffix.lower() in ('.html', '.jinja2')
    if p.parts[0] == 'static':
        return p.suffix.lower() in ('.css', '.js')
    return False


def valid_dir(path, owner=None):
    if path.is_symlink() or not path.is_dir():
        raise Blocked('Unsafe directory: ' + str(path))
    info = path.stat()
    if owner is not None and info.st_uid != owner:
        raise Blocked('Unexpected directory owner: ' + str(path))
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Blocked('Writable-by-group/other directory: ' + str(path))


def verify_parent_dirs(root, name, *, owner=None):
    valid_dir(root, owner)
    parent = root
    for component in PurePosixPath(name).parts[:-1]:
        parent = parent / component
        valid_dir(parent, owner)
    return parent


def load_file(root, name, *, expected_owner=None, allow_missing=False):
    parent = verify_parent_dirs(root, name, owner=expected_owner)
    path = parent / PurePosixPath(name).parts[-1]
    if path.is_symlink():
        raise Blocked('Symlink: ' + name)
    try:
        info = path.stat()
    except FileNotFoundError:
        if allow_missing:
            return None, None
        raise Blocked('Missing file: ' + name) from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SIZE:
        raise Blocked('Unexpected file type/size: ' + name)
    if expected_owner is not None and info.st_uid != expected_owner:
        raise Blocked('Unexpected file owner: ' + name)
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH):
        raise Blocked('Unsafe file permissions: ' + name)
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise Blocked('File size changed during read: ' + name)
    return data, info


def as_deploy_git(*args):
    command = ['runuser', '-u', 'vpn4us-deploy', '--',
               'git', '-C', str(STAGE), *args]
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode:
        raise Blocked('Git stage verification failed: ' + ' '.join(args[:2]))
    return result.stdout


def staging_manifest():
    deploy_uid = pwd.getpwnam('vpn4us-deploy').pw_uid
    valid_dir(STAGE, deploy_uid)
    if as_deploy_git('rev-parse', '--abbrev-ref', 'HEAD').decode().strip() != BRANCH:
        raise Blocked('Unexpected branch')
    if as_deploy_git('remote', 'get-url', 'origin').decode().strip() != ORIGIN:
        raise Blocked('Unexpected Git origin')
    if as_deploy_git('status', '--porcelain', '--untracked-files=no').strip():
        raise Blocked('Staging checkout has modifications')
    commit = as_deploy_git('rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise Blocked('Invalid commit')
    if as_deploy_git('rev-parse', f'origin/{BRANCH}').decode().strip() != commit:
        raise Blocked('Staging behind origin')
    # Verified, root-owned staging-pull also confirms GitHub CI for this commit.
    verified = subprocess.run(
        ['runuser', '-u', 'vpn4us-deploy', '--', '/usr/bin/python3',
         '/usr/local/libexec/vpn4us-staging-pull.py', '--check'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=145
    )
    if verified.returncode:
        raise Blocked('Staging has not passed both approved CI checks')

    files = {}
    for item in as_deploy_git('ls-files', '--stage', '-z').split(b'\0'):
        if not item:
            continue
        try:
            metadata, raw = item.split(b'\t', 1)
            mode, blob, stage = metadata.decode('ascii').split()
            name = raw.decode('utf-8')
        except (ValueError, UnicodeDecodeError) as exc:
            raise Blocked('Malformed git index') from exc
        if not allow_path(name):
            continue
        if mode not in ('100644', '100755') or stage != '0':
            raise Blocked('Unexpected tracked file type: ' + name)
        data, info = load_file(STAGE, name, expected_owner=deploy_uid)
        if git_sha(data) != blob:
            raise Blocked('Staging file does not match its Git blob: ' + name)
        files[name] = data
    return commit, files


def make_plan(commit, files, root, *, owner=None):
    changed = []
    unchanged = 0
    for name, data in sorted(files.items()):
        if not allow_path(name):
            raise Blocked('Prohibited deployment path')
        previous, info = load_file(root, name, expected_owner=owner, allow_missing=True)
        before = sha256(previous) if previous is not None else None
        after = sha256(data)
        if before == after:
            unchanged += 1
            continue
        changed.append({'path': name, 'action': 'replace' if previous is not None else 'new',
                        'before_sha256': before, 'after_sha256': after})
    plan = {'commit': commit, 'changes': changed}
    fingerprint = sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=True).encode())
    return plan, fingerprint, unchanged


def service_gate():
    for svc in SERVICES:
        result = subprocess.run(
            ['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=10
        )
        state = result.stdout.strip()
        if state != 'inactive':
            raise Blocked('App service is not inactive: ' + svc + ' (' + state + ')')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, info=None):
    # Explicitly invoked only on pre-reviewed source file paths or root-private backup paths.
    fd, tmp = tempfile.mkstemp(prefix='.vpn4us-deploy-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            if info is None:
                os.fchown(out.fileno(), 0, 0)
                os.fchmod(out.fileno(), 0o644)
            else:
                os.fchown(out.fileno(), info.st_uid, info.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_manifest(folder, manifest):
    raw = json.dumps(manifest, sort_keys=True, indent=2).encode()
    tmp = folder / 'manifest.tmp'
    if tmp.exists() or tmp.is_symlink():
        raise Blocked('Unexpected temporary manifest')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, folder / 'manifest.json')
        sync_dir(folder)
    finally:
        if tmp.exists():
            tmp.unlink()


def backup_name():
    return (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            + '-' + secrets.token_hex(5))


def secure_backup_root():
    if BACKUPS.exists() or BACKUPS.is_symlink():
        valid_dir(BACKUPS, 0)
    else:
        # Do not overwrite an existing directory with a symlink.
        valid_dir(BACKUPS.parent, 0)
        BACKUPS.mkdir(mode=0o700)
    if BACKUPS.stat().st_mode & 0o077:
        raise Blocked('Backup directory permissions are too broad')


@contextlib.contextmanager
def deployment_lock():
    secure_backup_root()
    lock = BACKUPS / '.lock'
    if lock.is_symlink():
        raise Blocked('Unsafe deployment lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != 0:
            raise Blocked('Unexpected deployment lock owner')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_backups(folder, changes, root, owner=0):
    for change in changes:
        name = change['path']
        if change['action'] == 'new':
            continue
        original, _ = load_file(root, name, expected_owner=owner)
        if sha256(original) != change['before_sha256']:
            raise Blocked('Production changed before backup: ' + name)
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with dest.open('xb') as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(dest, 0o600)
        if sha256(dest.read_bytes()) != change['before_sha256']:
            raise Blocked('Backup integrity failure: ' + name)
    sync_dir(folder)


def check_release_paths(changes, root, owner=0):
    for change in changes:
        current, _ = load_file(root, change['path'], expected_owner=owner, allow_missing=True)
        actual = sha256(current) if current is not None else None
        if actual != change['before_sha256']:
            raise Blocked('Production diverged from approved plan: ' + change['path'])


def apply_to_root(root, changes, data_by_name, folder, *, owner=0):
    # The manifest and all backups must already be durable BEFORE calling this.
    check_release_paths(changes, root, owner)
    for change in changes:
        name = change['path']
        current, metadata = load_file(root, name, expected_owner=owner, allow_missing=True)
        actual = sha256(current) if current is not None else None
        if actual != change['before_sha256']:
            raise Blocked('Concurrent modification detected: ' + name)
        data = data_by_name[name]
        if sha256(data) != change['after_sha256']:
            raise Blocked('Source bytes changed after approval: ' + name)
        target = root / name
        atomic_write(target, data, metadata)
        if sha256(load_file(root, name, expected_owner=owner)[0]) != change['after_sha256']:
            raise Blocked('Post-install verification failed: ' + name)


def rollback_root(root, changes, folder, *, owner=0):
    # Preflight ALL files before modifying any. Accept only before/after states,
    # for recovery from interruption; never overwrite unrelated post-release edits.
    for change in changes:
        name = change['path']
        current, _ = load_file(root, name, expected_owner=owner, allow_missing=True)
        actual = sha256(current) if current is not None else None
        if actual not in (change['before_sha256'], change['after_sha256']):
            raise Blocked('Cannot safely roll back modified file: ' + name)
        if change['action'] != 'new':
            backup = folder / name
            if backup.is_symlink():
                raise Blocked('Unsafe backup symlink')
            contents = backup.read_bytes()
            if sha256(contents) != change['before_sha256']:
                raise Blocked('Backup checksum mismatch: ' + name)
    for change in reversed(changes):
        name = change['path']
        current, info = load_file(root, name, expected_owner=owner, allow_missing=True)
        actual = sha256(current) if current is not None else None
        if actual == change['before_sha256']:
            continue
        dest = root / name
        if change['action'] == 'new':
            dest.unlink()
            sync_dir(dest.parent)
        else:
            data = (folder / name).read_bytes()
            atomic_write(dest, data, info)
            if sha256(load_file(root, name, expected_owner=owner)[0]) != change['before_sha256']:
                raise Blocked('Rollback post-check failed: ' + name)


def run_apply(args):
    service_gate()
    commit, files = staging_manifest()
    plan, fingerprint, same = make_plan(commit, files, PROD, owner=0)
    if args.commit != commit or args.plan_sha256 != fingerprint:
        raise Blocked('Explicit commit or plan fingerprint mismatch')
    changes = plan['changes']
    if not changes:
        print('NO CHANGES: production already matches staging')
        return
    if len(changes) > 50:
        raise Blocked('Overly broad deployment: manual review required')
    with deployment_lock():
        service_gate()
        check_release_paths(changes, PROD)
        name = backup_name()
        folder = BACKUPS / name
        folder.mkdir(mode=0o700)
        manifest = {'schema': 1, 'status': 'prepared', 'commit': commit,
                    'plan_sha256': fingerprint, 'changes': changes}
        write_backups(folder, changes, PROD)
        write_manifest(folder, manifest)
        manifest['status'] = 'applying'
        write_manifest(folder, manifest)
        try:
            apply_to_root(PROD, changes, files, folder)
        except Exception:
            print('APPLY INTERRUPTED: attempting safe rollback', file=sys.stderr)
            try:
                rollback_root(PROD, changes, folder)
                manifest['status'] = 'rolled_back'
                write_manifest(folder, manifest)
                print('ROLLBACK COMPLETED: original source files restored', file=sys.stderr)
            except Exception as exc:
                print('ROLLBACK BLOCKED; retain backup and investigate: ' + str(exc),
                      file=sys.stderr)
            raise
        manifest['status'] = 'applied'
        write_manifest(folder, manifest)
        print('APPLY SUCCESS: source files only; no database / service modifications')
        print('Backup ID:', name)
        print('Files updated:', len(changes))


def load_manifest(backup_id):
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{10}', backup_id):
        raise Blocked('Invalid backup identifier')
    folder = BACKUPS / backup_id
    valid_dir(folder, 0)
    data, info = load_file(folder, 'manifest.json', expected_owner=0)
    if len(data) > 256000:
        raise Blocked('Oversized manifest')
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise Blocked('Invalid manifest JSON') from exc
    if manifest.get('schema') != 1 or manifest.get('status') not in (
        'prepared', 'applying', 'applied', 'rolled_back'
    ):
        raise Blocked('Unsupported backup manifest')
    changes = manifest.get('changes')
    if not isinstance(changes, list) or len(changes) > 50:
        raise Blocked('Invalid manifest changes')
    for change in changes:
        if not isinstance(change, dict) or not allow_path(change.get('path')):
            raise Blocked('Invalid manifest path')
        if change.get('action') not in ('new', 'replace'):
            raise Blocked('Invalid manifest action')
        if not re.fullmatch(r'[0-9a-f]{64}', str(change.get('after_sha256'))):
            raise Blocked('Invalid new checksum')
        old = change.get('before_sha256')
        if old is not None and not re.fullmatch(r'[0-9a-f]{64}', str(old)):
            raise Blocked('Invalid old checksum')
        if (change['action'] == 'new') != (old is None):
            raise Blocked('Manifest action/checksum conflict')
    return folder, manifest


def run_rollback(args):
    service_gate()
    with deployment_lock():
        folder, manifest = load_manifest(args.backup_id)
        if manifest['status'] == 'rolled_back':
            print('ALREADY ROLLED BACK: no changes')
            return
        service_gate()
        rollback_root(PROD, manifest['changes'], folder)
        manifest['status'] = 'rolled_back'
        write_manifest(folder, manifest)
        print('ROLLBACK SUCCESS: verified original source restored')
        print('No services started or databases changed')


def run_plan():
    commit, files = staging_manifest()
    plan, fingerprint, same = make_plan(commit, files, PROD, owner=0)
    print('=== VPN4US MULTI-FILE DEPLOY PREFLIGHT (READ ONLY) ===')
    print('Staging commit:', commit)
    print('Eligible source files:', len(files))
    print('Unchanged:', same)
    print('Changed/new:', len(plan['changes']))
    for change in plan['changes']:
        print(change['action'].upper() + ': ' + change['path'])
    print('Approved plan SHA256:', fingerprint)
    print('Source only: .env, databases, credentials, deploy tooling and tests excluded')
    print('No source files copied, no processes restarted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', dest='backup_id')
    parser.add_argument('--commit')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--approve')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Blocked('Run installed root-owned tool as root')
    if args.plan:
        if args.approve or args.commit or args.plan_sha256:
            raise Blocked('Unexpected approval flags in read-only mode')
        run_plan()
    elif args.apply:
        if args.approve != 'SOURCE-ONLY' or not re.fullmatch('[0-9a-f]{40}', args.commit or '') or (
            not re.fullmatch('[0-9a-f]{64}', args.plan_sha256 or '')
        ):
            raise Blocked('Explicit SHA, fingerprint, and SOURCE-ONLY approval required')
        run_apply(args)
    else:
        if args.approve != 'SOURCE-ONLY' or args.commit or args.plan_sha256:
            raise Blocked('Explicit SOURCE-ONLY rollback approval required')
        run_rollback(args)


if __name__ == '__main__':
    try:
        main()
    except (Blocked, OSError, subprocess.SubprocessError, ValueError) as exc:
        print('DEPLOY BLOCKED:', str(exc), file=sys.stderr)
        sys.exit(1)
