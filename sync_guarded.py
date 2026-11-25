"""Stage 6D: guarded *one-action-per-run* 3x-ui inbound membership reconciliation.

Manual opt-in only; NO cron, NO timers, NO web UI mutations.
Use --dry-run for no writes. The explicit --apply path is intentionally disabled
until a distinct owned test subscription is provisioned and reviewed.
"""
import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from subscription_sync_plan import build_plan, index_inbounds

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data.db'
LOCKDIR = Path('/run/lock')
STOP_UNITS = ('vpn-bot', 'vpn-admin', 'vpn-webhook')

class SyncSafetyError(RuntimeError):
    pass


JOURNAL_DDL = '''
CREATE TABLE IF NOT EXISTS vpn4us_sync_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  server_id INTEGER NOT NULL,
  subscription_id INTEGER NOT NULL,
  inbound_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('attach','detach')),
  state TEXT NOT NULL CHECK(state IN ('prepared','verified','review')),
  issue_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vpn4us_sync_unresolved
ON vpn4us_sync_journal (server_id,subscription_id)
WHERE state IN ('prepared','review');
'''


def ensure_journal(db_path):
    with sqlite3.connect(db_path, timeout=15) as db:
        db.executescript(JOURNAL_DDL)


def validate_panel_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.query or p.fragment):
        raise SyncSafetyError('Panel must have a clean HTTPS URL')
    return url.rstrip('/')


def fetch_server(db_path, server_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        row = db.execute('''SELECT id,url,api_token,is_active FROM servers WHERE id=?''',
                         (server_id,)).fetchone()
    if not row or row[3] != 1:
        raise SyncSafetyError('Server is missing or disabled')
    return (validate_panel_url(row[1]), row[2])


def select_one(plan, subscription_id):
    """Pick ONE action from the existing verified planner, fail closed on ambiguity."""
    if plan.get('needs_manual_review', 0):
        raise SyncSafetyError('Some owned subscriptions require manual review')
    if plan.get('safe_to_plan', 0) != plan.get('managed_subscriptions', 0):
        raise SyncSafetyError('Not all owned clients can be planned')
    matches = [d for d in plan.get('details', [])
               if d.get('subscription_id') == subscription_id]
    if len(matches) != 1:
        raise SyncSafetyError('Subscription is not active and exclusively owned')
    detail = matches[0]
    if detail['already_present_untracked'] or detail['prune_deleted_metadata']:
        raise SyncSafetyError('Membership metadata requires separate manual reconciliation')
    if detail['add']:
        return 'attach', detail['add'][0]
    if detail['remove']:
        if not (set(detail['unchanged']) & set(plan['desired_inbounds'])):
            raise SyncSafetyError('Refusing to remove the last active desired client connection')
        return 'detach', detail['remove'][0]
    return None


def owned_row(db_path, server_id, subscription_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT id,xui_email,xui_client_uid,xui_subid FROM subscriptions
            WHERE id=? AND server_id=? AND managed_by_vpn4us=1 AND is_active=1''',
            (subscription_id, server_id)).fetchall()
        if len(rows) != 1:
            raise SyncSafetyError('Active vpn4us-owned subscription not found')
        row = dict(rows[0])
        repeated = db.execute('''SELECT COUNT(*) FROM subscriptions
            WHERE server_id=? AND xui_email=? AND managed_by_vpn4us=1''',
            (server_id,row['xui_email'])).fetchone()[0]
    if repeated != 1:
        raise SyncSafetyError('Duplicate owned email')
    if not row['xui_email'] or not row['xui_subid']:
        raise SyncSafetyError('Incomplete identity')
    try:
        row['xui_client_uid'] = str(uuid.UUID(str(row['xui_client_uid'])))
    except (ValueError, TypeError, AttributeError):
        raise SyncSafetyError('Invalid owned UUID') from None
    return row


def memberships(inbounds, row):
    indexed = index_inbounds(inbounds)
    email = row['xui_email']
    present = set()
    for iid, clients in indexed.items():
        client = clients.get(email)
        if client is None:
            continue
        try:
            actual_uuid = str(uuid.UUID(str(client.get('id'))))
        except (TypeError, ValueError, AttributeError):
            raise SyncSafetyError('Invalid client UUID from panel') from None
        if actual_uuid != row['xui_client_uid'] or client.get('subId') != row['xui_subid']:
            raise SyncSafetyError('Client identity conflict')
        if client.get('enable') is not True:
            raise SyncSafetyError('Owned client disabled in 3x-ui')
        present.add(iid)
    return present


def validate_effect(action, iid, before, after):
    """No unexpected additions/removals, even if the API reported success."""
    if action == 'attach':
        return iid not in before and after == before | {iid}
    if action == 'detach':
        return iid in before and after == before - {iid}
    raise SyncSafetyError('Unknown action')


def journal_unresolved(db_path, server_id, subscription_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        exists = db.execute('''SELECT 1 FROM vpn4us_sync_journal
            WHERE server_id=? AND subscription_id=? AND state IN ('prepared','review')
            LIMIT 1''', (server_id,subscription_id)).fetchone()
        return bool(exists)


def begin_action(db_path, server_id, subscription_id, action, iid, expected):
    with sqlite3.connect(db_path, timeout=20) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT xui_email,xui_client_uid,xui_subid
           FROM subscriptions WHERE id=? AND server_id=? AND managed_by_vpn4us=1 AND is_active=1''',
            (subscription_id,server_id)).fetchone()
        if row != expected:
            raise SyncSafetyError('Owned identity changed since panel verification')
        has_unresolved = db.execute('''SELECT 1 FROM vpn4us_sync_journal
            WHERE server_id=? AND subscription_id=? AND state IN ('prepared','review')''',
            (server_id,subscription_id)).fetchone()
        if has_unresolved:
            raise SyncSafetyError('Previous ambiguous action requires manual review')
        db.execute('''INSERT INTO vpn4us_sync_journal
            (server_id,subscription_id,inbound_id,action,state)
            VALUES (?,?,?,?, 'prepared')''', (server_id,subscription_id,iid,action))
        jid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return jid


def mark_review(db_path, journal_id, issue_code):
    """Sanitized reason only. If this DB update fails, 'prepared' also blocks retries."""
    if issue_code not in {'api_failed','post_read_failed','membership_unexpected','database_failed'}:
        issue_code = 'database_failed'
    with sqlite3.connect(db_path, timeout=20) as db:
        db.execute('''UPDATE vpn4us_sync_journal SET state='review',issue_code=?,
            finished_at=CURRENT_TIMESTAMP WHERE id=? AND state='prepared' ''',
            (issue_code, journal_id))


def confirm_action(db_path, jid, server_id, subscription_id, action, iid, row):
    with sqlite3.connect(db_path, timeout=20) as db:
        db.execute('BEGIN IMMEDIATE')
        same = db.execute('''SELECT xui_email,xui_client_uid,xui_subid
            FROM subscriptions WHERE id=? AND server_id=? AND managed_by_vpn4us=1 AND is_active=1''',
            (subscription_id,server_id)).fetchone()
        if same != (row['xui_email'],row['xui_client_uid'],row['xui_subid']):
            raise SyncSafetyError('Identity changed before metadata commit')
        journal = db.execute('''SELECT state,action,inbound_id FROM vpn4us_sync_journal
            WHERE id=? AND server_id=? AND subscription_id=?''',
            (jid,server_id,subscription_id)).fetchone()
        if journal != ('prepared', action, iid):
            raise SyncSafetyError('Journal state changed')
        if action == 'attach':
            db.execute('''INSERT INTO subscription_inbound_memberships
                (subscription_id,server_id,inbound_id) VALUES(?,?,?)''',
                (subscription_id,server_id,iid))
        else:
            deleted = db.execute('''DELETE FROM subscription_inbound_memberships
                WHERE subscription_id=? AND server_id=? AND inbound_id=?''',
                (subscription_id,server_id,iid))
            if deleted.rowcount != 1:
                raise SyncSafetyError('Missing tracked membership during detach')
        updated = db.execute('''UPDATE vpn4us_sync_journal SET state='verified',
            finished_at=CURRENT_TIMESTAMP WHERE id=? AND state='prepared' ''', (jid,))
        if updated.rowcount != 1:
            raise SyncSafetyError('Unable to finalize journal')


def api_change(client, base_url, email, action, iid):
    """At most one API POST. Response status alone is never sufficient proof."""
    endpoint = '/panel/api/clients/' + quote(email, safe='') + '/' + action
    result = client.session.post(base_url + endpoint,
        json={'inboundIds':[iid]}, timeout=12, verify=True, allow_redirects=False)
    if result.status_code != 200:
        return False
    try:
        obj = result.json()
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get('success') is True


@contextmanager
def exclusive_server_lock(server_id):
    # Used only by the opt-in write path. Independent server IDs can be reviewed.
    path = LOCKDIR / f'vpn4us-stage6d-server-{server_id}.lock'
    fd = os.open(path, os.O_CREAT|os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def require_services_stopped():
    for unit in STOP_UNITS:
        status = subprocess.run(['systemctl','is-active','--quiet',unit],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if status.returncode == 0:
            raise SyncSafetyError('Service is running: '+unit)


def run_one(db_path, server_id, subscription_id, *, apply=False, xui_factory=None):
    if apply:
        require_services_stopped()
    row = owned_row(db_path,server_id,subscription_id)
    url,token = fetch_server(db_path,server_id)
    if xui_factory is None:
        from services.xui_client import XUIClient
        xui_factory = XUIClient
    xui = xui_factory(url=url,api_token=token,verify_ssl=True)
    try:
        before_snapshot = xui.get_inbounds()
        plan = build_plan(db_path,server_id,before_snapshot)
        op = select_one(plan,subscription_id)
        if op is None:
            return 'NO_CHANGE'
        action,iid = op
        print('Planned:',action,'inbound',iid,'for subscription ID',subscription_id)
        if not apply:
            return 'DRY_RUN_ONLY'
        # At this point only the explicitly requested and verified owned client
        # is eligible for any write. Disallow replay of all uncertain actions.
        if journal_unresolved(db_path,server_id,subscription_id):
            raise SyncSafetyError('Unresolved journal entry: manual review required')
        # Revalidate exact plan and identity from FRESH snapshot immediately
        # before marking the attempt. No blind retries after any network issue.
        fresh = xui.get_inbounds()
        if select_one(build_plan(db_path,server_id,fresh),subscription_id) != op:
            raise SyncSafetyError('Policy or inbound membership changed during inspection')
        latest = owned_row(db_path,server_id,subscription_id)
        if latest != row:
            raise SyncSafetyError('Owned record changed since inspection')
        present = memberships(fresh,row)
        if action == 'attach' and iid in present:
            raise SyncSafetyError('Target already attached')
        if action == 'detach' and iid not in present:
            raise SyncSafetyError('Target already detached')
        raw_expected = (row['xui_email'],row['xui_client_uid'],row['xui_subid'])
        jid = begin_action(db_path,server_id,subscription_id,action,iid,raw_expected)
        api_ok = False
        try:
            api_ok = api_change(xui,url,row['xui_email'],action,iid)
        except Exception:
            pass
        # Always re-read after a response, including failure. Never retry a POST.
        try:
            after = xui.get_inbounds()
            now = memberships(after,row)
        except Exception:
            mark_review(db_path,jid,'post_read_failed')
            return 'REVIEW_POST_READ_FAILED'
        if not api_ok:
            mark_review(db_path,jid,'api_failed')
            return 'REVIEW_API_FAILED'
        if not validate_effect(action,iid,present,now):
            mark_review(db_path,jid,'membership_unexpected')
            return 'REVIEW_MEMBERSHIP_UNEXPECTED'
        try:
            confirm_action(db_path,jid,server_id,subscription_id,action,iid,row)
        except Exception:
            try:
                mark_review(db_path,jid,'database_failed')
            except Exception:
                pass
            return 'REVIEW_DB_FAILED'
        return 'VERIFIED_ONE_ACTION'
    finally:
        try: xui.session.close()
        except Exception: pass


def main():
    parser=argparse.ArgumentParser(description='Stage6D guarded single-client inbound synchronizer')
    parser.add_argument('--server-id',type=int,required=True)
    parser.add_argument('--subscription-id',type=int,required=True)
    parser.add_argument('--apply',action='store_true',help='One remote mutation; disabled until separate authorization')
    args=parser.parse_args()
    if args.server_id < 1 or args.subscription_id < 1:
        parser.error('Positive server and subscription IDs required')
    # Gate cannot be overridden by command-line switches; the next step
    # deliberately requires reviewing tests on a real OWNED canary first.
    if args.apply:
        raise SystemExit('STAGE6D SAFE MODE: API writes are not enabled in this release')
    try:
        result = run_one(DB,args.server_id,args.subscription_id,apply=False)
        print('Result:',result)
        print('DONE: no changes to 3x-ui or SQLite')
    except Exception as exc:
        print('STOP:',type(exc).__name__)
        raise SystemExit(1) from None

if __name__=='__main__':
    main()
