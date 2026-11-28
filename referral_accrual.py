"""Direct-referral achievement tiers; integer hundredths of a day.

Only freshly completed, verified cash subscription orders can earn awards.
No cash, VPN or receipt side effects. Redemption is deliberately disabled.
"""
import os
import sqlite3
import json
from contextlib import closing
from pathlib import Path
from referral import ReferralManager

DEFAULT_LEVELS=((1,"Новичок",10),(5,"Партнёр",20),(10,"Амбассадор",30),
                (25,"Эксперт",40),(50,"Легенда",50))


def table_exists(db,name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def setting(db,key):
    if not table_exists(db,"settings"):
        return None
    row=db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row[0] if row else None


def enabled(db):
    return os.environ.get("VPN4US_SAFE_REFERRAL_ACCRUAL")=="1" and setting(db,"referral_accrual_enabled")=="true"


def levels(db):
    raw=setting(db,"referral_levels")
    if raw is None:
        return DEFAULT_LEVELS
    return validate_levels(json.loads(raw))


def validate_levels(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=10:
        raise ValueError("Invalid referral levels")
    result=[]
    for row in rows:
        if not isinstance(row,list) or len(row)!=3:
            raise ValueError("Invalid referral level")
        threshold,name,rate=row
        if (type(threshold) is not int or not 1<=threshold<=100000
                or not isinstance(name,str) or not 1<=len(name.strip())<=40
                or type(rate) is not int or not 1<=rate<=100):
            raise ValueError("Invalid referral level values")
        if result and (threshold<=result[-1][0] or rate<result[-1][2]):
            raise ValueError("Nonmonotonic referral levels")
        result.append((threshold,name.strip(),rate))
    if result[0][0]!=1:
        raise ValueError("First level must start at one")
    return tuple(result)


def tier(rows,count):
    return next((r for r in reversed(rows) if count>=r[0]),(0,"Без уровня",0))


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS referral_purchase_awards(
        payment_id TEXT PRIMARY KEY, payer_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL, subscription_id INTEGER NOT NULL,
        duration_days INTEGER NOT NULL CHECK(duration_days>0),
        qualifying_count INTEGER NOT NULL CHECK(qualifying_count>0),
        level_name TEXT NOT NULL, rate_percent INTEGER NOT NULL CHECK(rate_percent BETWEEN 1 AND 100),
        centidays INTEGER NOT NULL CHECK(centidays=duration_days*rate_percent),
        action TEXT NOT NULL CHECK(action IN ('new_key','renew')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK(payer_id<>recipient_id))""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_referral_award_recipient
        ON referral_purchase_awards(recipient_id,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS referral_accrual_reviews(
        payment_id TEXT PRIMARY KEY, reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


VALID="""FROM referral_purchase_awards a
    JOIN payments p ON p.payment_id=a.payment_id AND p.telegram_id=a.payer_id
    JOIN payment_processing j ON j.payment_id=a.payment_id
    WHERE a.recipient_id=? AND p.status='paid' AND p.tariff_id>0 AND p.amount_rub>0
    AND j.payment_state='verified' AND j.vpn_state='done'
    AND j.is_legacy=0 AND j.refund_state='none'"""


def qualified(db,recipient):
    if not table_exists(db,"referral_purchase_awards"):
        return set()
    return {r[0] for r in db.execute("SELECT DISTINCT a.payer_id "+VALID,(recipient,))}


def record_completed_purchase(db,payment_id,duration_days,action):
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
    if db.execute("SELECT 1 FROM referral_purchase_awards WHERE payment_id=?",(payment_id,)).fetchone():
        return "already_recorded"
    if db.execute("SELECT 1 FROM referral_accrual_reviews WHERE payment_id=?",(payment_id,)).fetchone():
        return "needs_review"
    def hold(reason):
        db.execute("INSERT INTO referral_accrual_reviews(payment_id,reason) VALUES(?,?)",(payment_id,reason))
        return "needs_review"
    if type(duration_days) is not int or not 1<=duration_days<=3660 or action not in ("new_key","renew"):
        return hold("invalid_purchase_duration_or_action")
    if refund!="none":
        return hold("refund_present")
    if not db.execute("SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0",(payer,)).fetchone():
        return hold("payer_unavailable")
    chain,valid=ReferralManager._chain(db,payer)
    if not valid:
        return hold("invalid_referral_chain")
    if not chain:
        return "no_referrer"
    recipient=chain[0]
    if not db.execute("SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0",(recipient,)).fetchone():
        return hold("recipient_unavailable")
    if not db.execute("SELECT 1 FROM subscriptions WHERE id=? AND telegram_id=?",(sid,payer)).fetchone():
        return hold("subscription_ownership_unconfirmed")
    try:
        rows=levels(db)
    except (TypeError,ValueError):
        return hold("invalid_level_policy")
    count=len(qualified(db,recipient)|{payer})
    _,name,rate=tier(rows,count)
    db.execute("""INSERT INTO referral_purchase_awards
        (payment_id,payer_id,recipient_id,subscription_id,duration_days,qualifying_count,
        level_name,rate_percent,centidays,action) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (payment_id,payer,recipient,sid,duration_days,count,name,rate,duration_days*rate,action))
    return "awarded"


def snapshot(path,recipient):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.execute("BEGIN")
        rows=levels(db)
        count=len(qualified(db,recipient))
        current=tier(rows,count)
        next_level=next((r for r in rows if r[0]>count),None)
        invited=db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND telegram_id<>?",(recipient,recipient)).fetchone()[0]
        total=month=0
        history=[]
        if table_exists(db,"referral_purchase_awards"):
            total=db.execute("SELECT COALESCE(SUM(a.centidays),0) "+VALID,(recipient,)).fetchone()[0]
            month=db.execute("SELECT COALESCE(SUM(a.centidays),0) "+VALID+" AND a.created_at>=datetime('now','start of month')",(recipient,)).fetchone()[0]
            history=db.execute("""SELECT a.created_at,a.action,a.duration_days,a.rate_percent,a.centidays,
                CASE WHEN p.status='paid' AND j.payment_state='verified' AND j.vpn_state='done'
                  AND j.is_legacy=0 AND j.refund_state='none' THEN 1 ELSE 0 END
                FROM referral_purchase_awards a LEFT JOIN payments p ON p.payment_id=a.payment_id
                LEFT JOIN payment_processing j ON j.payment_id=a.payment_id
                WHERE a.recipient_id=? ORDER BY a.created_at DESC,a.payment_id DESC LIMIT 10""",(recipient,)).fetchall()
        return dict(invited=invited,paid=count,current=current,next=next_level,levels=rows,
                    centidays=total,month_centidays=month,history=history,enabled=enabled(db))


def confirmed_days(path,recipient):
    return snapshot(path,recipient)["centidays"]/100
