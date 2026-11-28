"""First completed, independently verified cash subscription purchase.

This module records bonus days, never changes a VPN subscription or cash balance.
Production activation requires both switches and a separate redemption/refund review.
No historical backfill.
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from referral import ReferralManager

LEVEL_DAYS = (30, 15, 5)


def table_exists(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def enabled(db):
    if os.environ.get("VPN4US_SAFE_REFERRAL_ACCRUAL") != "1" or not table_exists(db,"settings"):
        return False
    row=db.execute("SELECT value FROM settings WHERE key='referral_accrual_enabled'").fetchone()
    return bool(row and row[0]=="true")


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS referral_first_purchases(
        payer_id INTEGER PRIMARY KEY, payment_id TEXT NOT NULL UNIQUE,
        subscription_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('awarded','no_referrer','needs_review')),
        reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS referral_day_awards(
        payer_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
        level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
        days INTEGER NOT NULL CHECK(days>0),
        payment_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(payer_id,level), UNIQUE(payer_id,recipient_id),
        CHECK(payer_id<>recipient_id),
        FOREIGN KEY(payer_id) REFERENCES referral_first_purchases(payer_id))""")


def record_completed_purchase(db, payment_id):
    """Called in the SAME transaction as completion of a verified payment.
    No external calls; do not call for duplicate or historical delivery.
    """
    if not enabled(db):
        return "disabled"
    row=db.execute("""SELECT p.telegram_id,p.tariff_id,p.amount_rub,p.status,
        j.payment_state,j.vpn_state,j.refund_state,j.is_legacy,j.subscription_id
        FROM payments p JOIN payment_processing j USING(payment_id)
        WHERE p.payment_id=?""",(payment_id,)).fetchone()
    if not row:
        return "ineligible"
    payer,tariff,amount,status,verified,vpn,refund,legacy,sid=tuple(row)
    if not (tariff>0 and amount>0 and status=="paid" and verified=="verified"
            and vpn=="done" and not legacy and type(sid) is int and sid>0):
        return "ineligible"
    ensure_schema(db)
    if db.execute("SELECT 1 FROM referral_first_purchases WHERE payer_id=?",(payer,)).fetchone():
        return "already_recorded"
    # A prior real subscription payment counts even when it was refunded or
    # processed before this release. Trials and deposits are not purchases.
    if db.execute("""SELECT 1 FROM payments WHERE telegram_id=? AND payment_id<>?
        AND tariff_id>0 AND amount_rub>0 AND status='paid' LIMIT 1""",(payer,payment_id)).fetchone():
        return "previous_purchase"
    reason=""
    if refund!="none":
        reason="refund_present"
    elif db.execute("""SELECT 1 FROM payments p JOIN payment_processing j USING(payment_id)
        WHERE p.telegram_id=? AND p.payment_id<>? AND p.tariff_id>0 AND p.amount_rub>0
        AND j.payment_state='verified' AND p.status='pending'
        AND j.refund_state='none' LIMIT 1""",(payer,payment_id)).fetchone():
        reason="another_verified_purchase_unresolved"
    elif table_exists(db,"auto_renewal_operations") and db.execute(
        "SELECT 1 FROM auto_renewal_operations WHERE telegram_id=? LIMIT 1",(payer,)).fetchone():
        # The existing balance mixes cash and gifts. Never assume the source.
        reason="prior_balance_renewal_requires_review"
    chain=[]
    if not reason:
        if not db.execute("SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0",(payer,)).fetchone():
            reason="payer_unavailable"
        elif not db.execute("SELECT 1 FROM subscriptions WHERE id=? AND telegram_id=?",(sid,payer)).fetchone():
            reason="subscription_ownership_unconfirmed"
        else:
            chain,valid=ReferralManager._chain(db,payer)
            chain=chain[:3]
            if not valid:
                reason="invalid_referral_chain"
            elif any(not db.execute("SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0",(uid,)).fetchone() for uid in chain):
                reason="recipient_unavailable"
    state="needs_review" if reason else ("awarded" if chain else "no_referrer")
    db.execute("""INSERT INTO referral_first_purchases
        (payer_id,payment_id,subscription_id,state,reason) VALUES(?,?,?,?,?)""",
        (payer,payment_id,sid,state,reason))
    if state=="awarded":
        db.executemany("""INSERT INTO referral_day_awards
            (payer_id,recipient_id,level,days,payment_id) VALUES(?,?,?,?,?)""",
            [(payer,uid,level,LEVEL_DAYS[level-1],payment_id)
             for level,uid in enumerate(chain,1)])
    return state


def confirmed_days(path, recipient):
    """Read-only net eligible awards, not a redeemable balance.
    A recorded refund immediately excludes the entire affected award from
    this display. No automatic reassignment or historical replay.
    """
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        if not table_exists(db,"referral_day_awards"):
            return 0
        return int(db.execute("""SELECT COALESCE(SUM(a.days),0)
            FROM referral_day_awards a
            JOIN referral_first_purchases f ON f.payer_id=a.payer_id AND f.payment_id=a.payment_id
            JOIN payments p ON p.payment_id=a.payment_id AND p.telegram_id=a.payer_id
            JOIN payment_processing j ON j.payment_id=a.payment_id
            WHERE a.recipient_id=? AND f.state='awarded' AND p.status='paid'
              AND j.payment_state='verified' AND j.vpn_state='done'
              AND j.refund_state='none' AND j.is_legacy=0""",(recipient,)).fetchone()[0])
