"""Crash-safe, fail-closed journal for NEW vpn4us VPN subscriptions.

No 3x-ui requests are made from this module. A committed 'prepared' record is
intentionally never auto-retried or auto-cleared: operators must reconcile it.
"""
import sqlite3
from contextlib import closing
from pathlib import Path


class IssueJournalError(RuntimeError):
    pass


DDL = '''
CREATE TABLE IF NOT EXISTS vpn4us_issue_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    telegram_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    tariff_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','verified','review')),
    subscription_id INTEGER,
    client_uuid TEXT,
    subid TEXT,
    issue_code TEXT,
    CHECK(state != 'verified' OR
          (subscription_id IS NOT NULL AND client_uuid IS NOT NULL AND subid IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vpn4us_issue_unresolved_user
    ON vpn4us_issue_journal(telegram_id)
    WHERE state IN ('prepared','review');
CREATE UNIQUE INDEX IF NOT EXISTS ux_vpn4us_issue_server_email
    ON vpn4us_issue_journal(server_id,email);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vpn4us_issue_subscription
    ON vpn4us_issue_journal(subscription_id)
    WHERE subscription_id IS NOT NULL;
'''


def ensure_schema(db):
    """Accepts an existing sqlite3.Connection. Called ONLY by installer."""
    db.executescript(DDL)


def _integer(value, name):
    if isinstance(value, bool):
        raise IssueJournalError('Invalid ' + name)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise IssueJournalError('Invalid ' + name) from None
    if result <= 0:
        raise IssueJournalError('Invalid ' + name)
    return result


def start_issue(db_path, telegram_id, server_id, email, tariff_id, *, allow_additional_key=False):
    """Commit journal BEFORE the one allowed /clients/add POST.

    A pending entry deliberately survives all API/DB failures until reviewed.
    It blocks retries for this user, even if a retry generates a new email.
    """
    telegram_id = _integer(telegram_id, 'telegram ID')
    server_id = _integer(server_id, 'server ID')
    tariff_id = _integer(tariff_id, 'tariff ID')
    if not isinstance(email, str) or not email or len(email) > 250 or email.strip() != email:
        raise IssueJournalError('Invalid client email')
    with closing(sqlite3.connect(str(db_path), timeout=20)) as db, db:
        db.execute('PRAGMA busy_timeout=20000')
        db.execute('BEGIN IMMEDIATE')
        # No writes when unexpectedly unconfigured: fail closed.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpn4us_issue_journal'").fetchone():
            raise IssueJournalError('Issue journal is not installed')
        if db.execute('''SELECT 1 FROM vpn4us_issue_journal WHERE telegram_id=?
                         AND state IN ('prepared','review') LIMIT 1''', (telegram_id,)).fetchone():
            raise IssueJournalError('Previous issue unresolved: reconcile before retry')
        if db.execute('''SELECT 1 FROM vpn4us_issue_journal WHERE server_id=? AND email=? LIMIT 1''',
                      (server_id,email)).fetchone():
            raise IssueJournalError('Client email already used in issue journal')
        if not allow_additional_key and db.execute('''SELECT 1 FROM subscriptions WHERE telegram_id=? AND managed_by_vpn4us=1
                         AND is_active=1 LIMIT 1''', (telegram_id,)).fetchone():
            raise IssueJournalError('Active subscription already exists: use renewal/recovery')
        if not db.execute('SELECT 1 FROM users WHERE telegram_id=?', (telegram_id,)).fetchone():
            raise IssueJournalError('User missing from database')
        if not db.execute('SELECT 1 FROM servers WHERE id=? AND is_active=1', (server_id,)).fetchone():
            raise IssueJournalError('Server disabled or missing')
        db.execute('''INSERT INTO vpn4us_issue_journal
            (telegram_id,server_id,email,tariff_id,state) VALUES (?,?,?,?,'prepared')''',
            (telegram_id,server_id,email,tariff_id))
        issue_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return issue_id


def finalize_issue(cursor, issue_id, server_id, telegram_id, email, sub_pk, client_uuid, subid):
    """Finalize in the SAME transaction as subscription INSERT and membership INSERT.

    This function deliberately does not commit. Caller MUST commit once.
    """
    issue_id = _integer(issue_id, 'issue ID')
    server_id = _integer(server_id, 'server ID')
    telegram_id = _integer(telegram_id, 'telegram ID')
    sub_pk = _integer(sub_pk, 'subscription ID')
    if not all(isinstance(x, str) and x.strip() for x in (email, client_uuid, subid)):
        raise IssueJournalError('Incomplete verified client identity')
    issue = cursor.execute('''SELECT telegram_id,server_id,email,state FROM vpn4us_issue_journal
                              WHERE id=?''', (issue_id,)).fetchone()
    if issue != (telegram_id,server_id,email,'prepared'):
        raise IssueJournalError('Issue identity/state changed before commit')
    sub = cursor.execute('''SELECT telegram_id,server_id,xui_email,xui_client_uid,xui_subid,managed_by_vpn4us
                            FROM subscriptions WHERE id=?''', (sub_pk,)).fetchone()
    if sub != (telegram_id,server_id,email,client_uuid,subid,1):
        raise IssueJournalError('Subscription and journal identities do not match')
    count = cursor.execute('''SELECT COUNT(*) FROM subscription_inbound_memberships
                              WHERE subscription_id=? AND server_id=?''', (sub_pk,server_id)).fetchone()[0]
    if count < 1:
        raise IssueJournalError('Subscription inbound memberships are empty')
    updated = cursor.execute('''UPDATE vpn4us_issue_journal SET
           state='verified',subscription_id=?,client_uuid=?,subid=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND state='prepared' ''',
           (sub_pk,client_uuid,subid,issue_id))
    if updated.rowcount != 1:
        raise IssueJournalError('Unable to finalize issue journal')
    return True


def pending_summary(db_path):
    """No personal identifiers or secrets in returned summary."""
    with closing(sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)) as db:
        return {state: count for state,count in db.execute('''SELECT state,COUNT(*)
                         FROM vpn4us_issue_journal GROUP BY state''')}
