"""Offline-testable auto-renewal reservation and reconciliation journal.

The feature is OFF unless explicitly activated via both the process environment
and the existing administrator setting. No historical subscriptions are replayed.
"""
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from balance_ledger import MAX_KOPECKS, apply_operation, kopecks, transaction
from subscription_renewal import DAY_MS

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    subscription_id: int
    owner: int
    tariff_id: int
    end_date: str
    price: int
    days: int


def activated():
    return os.environ.get("VPN4US_SAFE_AUTORENEW") == "1"


def enabled(db):
    if not activated():
        return False
    row = db.execute("SELECT value FROM settings WHERE key='auto_renew_enabled'").fetchone()
    return row is not None and row[0] == "true"


def expiry_ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def due(end, now):
    return now-DAY_MS < expiry_ms(end) <= now


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS auto_renewal_operations(
        operation_id TEXT PRIMARY KEY, subscription_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL, tariff_id INTEGER NOT NULL,
        original_end TEXT NOT NULL, target_ms INTEGER NOT NULL,
        amount_kopecks INTEGER NOT NULL CHECK(amount_kopecks>0),
        duration_days INTEGER NOT NULL CHECK(duration_days>0),
        server_id INTEGER NOT NULL, client_uid TEXT NOT NULL, client_subid TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','needs_review','done')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(subscription_id,original_end))""")


def pending(db, subscription_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_renewal_operations'").fetchone():
        return False
    return bool(db.execute("""SELECT 1 FROM auto_renewal_operations
        WHERE subscription_id=? AND state IN ('reserved','needs_review') LIMIT 1""",
        (subscription_id,)).fetchone())


def open_payment(db, owner):
    # The historic payment table has no explicit subscription target. Prefer
    # skipping an otherwise eligible key to competing with any open VPN order.
    if db.execute("""SELECT 1 FROM payments p
        LEFT JOIN payment_processing j USING(payment_id)
        WHERE p.telegram_id=? AND p.tariff_id>0 AND
        (p.status='pending' OR j.vpn_state IN ('processing','needs_review')) LIMIT 1""",
        (owner,)).fetchone():
        return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_renewal_attempts'").fetchone():
        if db.execute("""SELECT 1 FROM manual_renewal_attempts
            WHERE telegram_id=? AND state='reserved' LIMIT 1""",(owner,)).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='promo_reservations'").fetchone():
        return bool(db.execute("""SELECT 1 FROM promo_reservations
            WHERE telegram_id=? AND action='renew' AND state IN ('reserved','attached') LIMIT 1""",
            (owner,)).fetchone())
    return False


def manual_attempt_pending(db, subscription_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_renewal_attempts'").fetchone():
        return False
    return bool(db.execute("""SELECT 1 FROM manual_renewal_attempts
        WHERE subscription_id=? AND state='reserved' LIMIT 1""",(subscription_id,)).fetchone())


def begin_manual_attempt(path, checkout_id, owner, subscription_id):
    """Called under the same subscription lock, before provider creation.

    An ambiguous provider call with no local payment row must still block
    automatic spending. This row never grants permission to retry a payment.
    """
    with transaction(path) as db:
        if manual_attempt_pending(db,subscription_id):
            raise ValueError("Manual renewal checkout awaits reconciliation")
        db.execute("""CREATE TABLE IF NOT EXISTS manual_renewal_attempts(
            checkout_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL,
            subscription_id INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','registered')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""INSERT INTO manual_renewal_attempts(checkout_id,telegram_id,subscription_id,state)
            VALUES(?,?,?,'reserved')""",(checkout_id,owner,subscription_id))


def finish_manual_attempt(path, checkout_id):
    with transaction(path) as db:
        row = db.execute("""UPDATE manual_renewal_attempts SET state='registered'
            WHERE checkout_id=? AND state='reserved'""",(checkout_id,))
        if row.rowcount != 1:
            raise ValueError("Manual renewal checkout requires reconciliation")


def candidates(path, now):
    if not activated():
        return []
    with closing(sqlite3.connect(path)) as db:
        if not enabled(db):
            return []
        rows = db.execute("""SELECT s.id,s.telegram_id,s.tariff_id,s.end_date,t.price_rub,t.duration_days
          FROM subscriptions s JOIN users u ON u.telegram_id=s.telegram_id
          JOIN tariffs t ON t.id=s.tariff_id JOIN servers v ON v.id=s.server_id
          WHERE s.is_active=1 AND s.managed_by_vpn4us=1 AND s.auto_renew=1
          AND u.is_blocked=0 AND v.is_active=1 AND t.is_active=1 AND t.price_rub>0
          AND t.duration_days>0 ORDER BY s.id""").fetchall()
        result = []
        for sid,owner,tid,end,price,days in rows:
            try:
                if type(days) is int and 0<days<=3660 and due(end,now) and not pending(db,sid) and not open_payment(db,owner):
                    result.append(Candidate(sid,owner,tid,end,kopecks(price,positive=True,maximum=MAX_KOPECKS),days))
            except (ValueError,TypeError,OverflowError):
                LOG.warning("Invalid auto renewal candidate %s",sid)
        return result


def reserve(path, candidate, *, now, target_ms, server_id, uid, subid, email, url, token, inbounds):
    """Recheck all financial and eligibility facts in one write transaction.

    Called only with the shared subscription renewal lock held, AFTER panel
    identity verification, BEFORE the single remote mutation.
    """
    c = candidate
    with transaction(path) as db:
        ensure_schema(db)
        if (not enabled(db) or not due(c.end_date,now) or pending(db,c.subscription_id)
                or open_payment(db,c.owner) or type(c.days) is not int or not 0<c.days<=3660):
            raise ValueError("Auto renewal disabled, not due or awaiting review")
        row = db.execute("""SELECT s.telegram_id,s.tariff_id,s.end_date,t.price_rub,t.duration_days,
                 s.server_id,s.xui_client_uid,s.xui_subid,s.xui_email,v.url,v.api_token
          FROM subscriptions s JOIN users u ON u.telegram_id=s.telegram_id
          JOIN tariffs t ON t.id=s.tariff_id JOIN servers v ON v.id=s.server_id
          WHERE s.id=? AND s.is_active=1 AND s.managed_by_vpn4us=1 AND s.auto_renew=1
          AND u.is_blocked=0 AND v.is_active=1 AND t.is_active=1""",(c.subscription_id,)).fetchone()
        if not row or (row[0],row[1],row[2],kopecks(row[3],positive=True,maximum=MAX_KOPECKS),
                       *tuple(row)[4:]) != (c.owner,c.tariff_id,c.end_date,c.price,c.days,server_id,uid,subid,email,url,token):
            raise ValueError("Auto renewal facts changed")
        actual = tuple(r[0] for r in db.execute("""SELECT inbound_id FROM subscription_inbound_memberships
            WHERE subscription_id=? AND server_id=? ORDER BY inbound_id""",(c.subscription_id,server_id)))
        if actual != inbounds:
            raise ValueError("Automatic renewal membership changed")
        operation_id = "auto-renew:" + sha256(f"{c.subscription_id}:{c.end_date}".encode()).hexdigest()
        db.execute("""INSERT INTO auto_renewal_operations(operation_id,subscription_id,telegram_id,
            tariff_id,original_end,target_ms,amount_kopecks,duration_days,server_id,client_uid,client_subid,state)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved')""",
            (operation_id,c.subscription_id,c.owner,c.tariff_id,c.end_date,target_ms,c.price,c.days,server_id,uid,subid))
        _,changed = apply_operation(db,operation_id,c.owner,"auto_renew_reserve",-c.price)
        if not changed:
            raise ValueError("Duplicate auto renewal reservation")
        return operation_id


def complete(db, operation_id, subscription_id, server_id, inbounds):
    # Same transaction as the local expiry update: neither can commit alone.
    actual = tuple(r[0] for r in db.execute("""SELECT inbound_id FROM subscription_inbound_memberships
        WHERE subscription_id=? AND server_id=? ORDER BY inbound_id""",(subscription_id,server_id)))
    if actual != inbounds:
        raise ValueError("Automatic renewal membership changed after panel update")
    row = db.execute("""UPDATE auto_renewal_operations SET state='done',updated_at=CURRENT_TIMESTAMP
        WHERE operation_id=? AND state='reserved'""",(operation_id,))
    if row.rowcount != 1:
        raise ValueError("Auto renewal reservation missing")


def review(path, operation_id):
    with closing(sqlite3.connect(path,timeout=15)) as db,db:
        db.execute("""UPDATE auto_renewal_operations SET state='needs_review',updated_at=CURRENT_TIMESTAMP
            WHERE operation_id=? AND state='reserved'""",(operation_id,))


def process_one(path, candidate, api_factory, *, clock=None):
    if not activated():
        raise ValueError("Automatic renewal not activated")
    with closing(sqlite3.connect(path)) as db:
        if not enabled(db):
            raise ValueError("Automatic renewal disabled")
    from subscription_renewal import renew_subscription
    return renew_subscription(path,candidate.subscription_id,candidate.days,candidate.tariff_id,
                              api_factory,clock=clock,auto=candidate)


def review_count(path):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_renewal_operations'").fetchone():
            return 0
        return db.execute("SELECT count(*) FROM auto_renewal_operations WHERE state IN ('reserved','needs_review')").fetchone()[0]
