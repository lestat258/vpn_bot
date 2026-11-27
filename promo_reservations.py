"""Durable discount reservations, without provider or VPN side effects.

Only explicitly registered checkout attempts participate. Never release an
ambiguous provider request by timeout. Integrators must attach before showing
a payment link and settle only from independently verified provider state.
"""
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime
from balance_ledger import kopecks, rubles


class ReservationBusy(RuntimeError):
    pass


class ReservationConflict(RuntimeError):
    pass


class ZeroPayable(ValueError):
    pass


def has_schema(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='promo_reservations'").fetchone())


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS promo_reservations (
        checkout_id TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL,
        tariff_id INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('new_key','renew')),
        subscription_id INTEGER,
        original_kopecks INTEGER NOT NULL CHECK(original_kopecks>0),
        discount_kopecks INTEGER NOT NULL CHECK(discount_kopecks>0 AND discount_kopecks<original_kopecks),
        kind TEXT NOT NULL CHECK(kind IN ('manual','reward')),
        manual_value TEXT,
        reward_id INTEGER,
        promocode_id INTEGER,
        payment_id TEXT UNIQUE REFERENCES payments(payment_id),
        state TEXT NOT NULL CHECK(state IN ('reserved','attached','consumed','released')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK((kind='manual' AND manual_value IS NOT NULL AND reward_id IS NULL AND promocode_id IS NULL)
           OR (kind='reward' AND manual_value IS NULL AND reward_id IS NOT NULL AND promocode_id IS NOT NULL)),
        CHECK((action='new_key' AND subscription_id IS NULL) OR (action='renew' AND subscription_id>0)),
        CHECK((state='reserved' AND payment_id IS NULL) OR (state<>'reserved' AND payment_id IS NOT NULL))
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_reservation_open
        ON promo_reservations(telegram_id) WHERE state IN ('reserved','attached')""")


@contextmanager
def transaction(path):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _positive_id(value):
    return type(value) is int and value>0


def _discount(price,percent,amount):
    try:
        p,a=Decimal(str(percent or 0)),Decimal(str(amount or 0))
        if not p.is_finite() or not a.is_finite() or not 0<=p<=100 or a<0:
            raise ValueError("Invalid discount")
        if p>0:
            return int((Decimal(price)*p/100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        return min(price,kopecks(a))
    except (InvalidOperation, TypeError, OverflowError):
        raise ValueError("Invalid discount") from None


def _unexpired(raw):
    try:
        expiry=datetime.fromisoformat(raw)
        return expiry>datetime.now(expiry.tzinfo)
    except (ValueError,TypeError):
        return False


def ensure_manual_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS manual_promo_grants (
        telegram_id INTEGER PRIMARY KEY, promocode_id INTEGER NOT NULL,
        value TEXT NOT NULL, valid_until TEXT)""")


def save_manual_grant(db,owner,promo_id,value,valid_until):
    """Called in the same transaction as the activation counter and setting."""
    ensure_manual_schema(db)
    db.execute("""INSERT INTO manual_promo_grants(telegram_id,promocode_id,value,valid_until)
        VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
        promocode_id=excluded.promocode_id,value=excluded.value,valid_until=excluded.valid_until""",
        (owner,promo_id,value,valid_until))


def expire_manual_grant(db,owner):
    """Only expire new grants with known deadlines. Legacy settings are preserved."""
    ensure_manual_schema(db)
    if has_open(db,owner):
        return False
    row=db.execute("SELECT value,valid_until FROM manual_promo_grants WHERE telegram_id=?",(owner,)).fetchone()
    if row is None or row[1] is None or _unexpired(row[1]):
        return False
    db.execute("UPDATE settings SET value='' WHERE key=? AND value=?",(f"promocode_user_{owner}",row[0]))
    db.execute("DELETE FROM manual_promo_grants WHERE telegram_id=?",(owner,))
    return True


def _select(db,owner,price):
    """Retain prior policy: first valid reward versus manual, larger wins.
    Equal discounts prefer the reward. Never combine discounts."""
    manual=db.execute("SELECT value FROM settings WHERE key=?",
                      (f"promocode_user_{owner}",)).fetchone()
    candidate=None
    for row in db.execute("""SELECT id,promocode_id,discount_percent,discount_amount,expires_at
                             FROM user_promocodes WHERE user_id=? AND is_used=0 ORDER BY id""",(owner,)):
        if not _unexpired(row["expires_at"]):
            continue
        discount=_discount(price,row["discount_percent"],row["discount_amount"])
        if discount:
            candidate=(discount,"reward",None,row["id"],row["promocode_id"])
            break
    if manual and manual[0]:
        parts=manual[0].split("|")
        if len(parts)!=2:
            raise ValueError("Invalid manual discount")
        discount=_discount(price,*parts)
        if discount and (candidate is None or discount>candidate[0]):
            candidate=(discount,"manual",manual[0],None,None)
    return candidate


def reserve(path,checkout_id,owner,tariff_id,price,*,action="new_key",subscription_id=None):
    """Return (row, newly_reserved). No discount -> (None, False).

    Replayed checkout IDs are returned but MUST NOT trigger another provider
    create. An outstanding reservation blocks competing discounted checkouts.
    The caller must not create a zero-payment order.
    """
    original=kopecks(price,positive=True)
    if (not isinstance(checkout_id,str) or not 1<=len(checkout_id)<=128
            or not _positive_id(owner) or not _positive_id(tariff_id)
            or not ((action=="new_key" and subscription_id is None)
                    or (action=="renew" and _positive_id(subscription_id)))):
        raise ValueError("Invalid checkout identity")
    with transaction(path) as db:
        ensure_schema(db)
        existing=db.execute("SELECT * FROM promo_reservations WHERE checkout_id=?",(checkout_id,)).fetchone()
        if existing:
            if tuple(existing[k] for k in ("telegram_id","tariff_id","original_kopecks","action","subscription_id"))!=(owner,tariff_id,original,action,subscription_id):
                raise ReservationConflict("Checkout identity mismatch")
            return dict(existing),False
        if has_open(db,owner):
            raise ReservationBusy("Previous discounted checkout is unresolved")
        expire_manual_grant(db,owner)
        candidate=_select(db,owner,original)
        if candidate is None:
            return None,False
        discount,kind,manual,reward,promo=candidate
        if discount>=original:
            raise ZeroPayable("Zero-price checkout requires separate policy")
        db.execute("""INSERT INTO promo_reservations
            (checkout_id,telegram_id,tariff_id,action,subscription_id,original_kopecks,
             discount_kopecks,kind,manual_value,reward_id,promocode_id,state)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved')""",
            (checkout_id,owner,tariff_id,action,subscription_id,original,discount,kind,manual,reward,promo))
        return dict(db.execute("SELECT * FROM promo_reservations WHERE checkout_id=?",(checkout_id,)).fetchone()),True


def has_open(db,owner):
    return has_schema(db) and bool(db.execute(
        "SELECT 1 FROM promo_reservations WHERE telegram_id=? AND state IN ('reserved','attached')",(owner,)).fetchone())


def registered(db,payment_id):
    return has_schema(db) and bool(db.execute("SELECT 1 FROM promo_reservations WHERE payment_id=?",(payment_id,)).fetchone())


def payable(row):
    return row["original_kopecks"]-row["discount_kopecks"]


def metadata_for(row):
    return {"promo_reservation_id":row["checkout_id"],"telegram_id":row["telegram_id"],
            "tariff_id":row["tariff_id"],"action":row["action"],
            "subscription_id":row["subscription_id"] if row["subscription_id"] is not None else ""}


def _match(row,metadata,amount):
    if not isinstance(metadata,dict) or kopecks(amount,positive=True)!=payable(row):
        raise ReservationConflict("Reservation amount mismatch")
    for key,value in metadata_for(row).items():
        actual=metadata.get(key)
        if key=="subscription_id" and value=="" and actual in (None,""):
            continue
        if str(actual)!=str(value):
            raise ReservationConflict("Reservation metadata mismatch")


def attach(path,checkout_id,payment_id,metadata,amount):
    """Atomically bind a known provider ID and insert its pending local order.
    On failure keep reservation held; never assume the provider did not create.
    """
    if not isinstance(payment_id,str) or not 1<=len(payment_id)<=128:
        raise ValueError("Invalid provider payment ID")
    with transaction(path) as db:
        row=db.execute("SELECT * FROM promo_reservations WHERE checkout_id=?",(checkout_id,)).fetchone()
        if not row:
            raise ReservationConflict("Missing reservation")
        _match(row,metadata,amount)
        if row["state"]!="reserved":
            if row["payment_id"]==payment_id:
                return False
            raise ReservationConflict("Checkout already bound")
        db.execute("INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status) VALUES(?,?,?,?,'pending')",
                   (payment_id,row["telegram_id"],row["tariff_id"],str(rubles(payable(row)))))
        db.execute("UPDATE promo_reservations SET payment_id=?,state='attached',updated_at=CURRENT_TIMESTAMP WHERE checkout_id=?",
                   (payment_id,checkout_id))
        return True


def settle(db,payment_id,metadata,amount,status):
    """In caller's BEGIN IMMEDIATE transaction, settle independently verified
    provider succeeded/canceled only. Caller also records canceled payment
    state in this transaction. Unknown legacy orders are untouched.
    Returns ignored, consumed, released, or done.
    """
    if not registered(db,payment_id):
        return "ignored"
    if status not in ("succeeded","canceled"):
        raise ValueError("Unverified terminal state")
    row=db.execute("SELECT * FROM promo_reservations WHERE payment_id=?",(payment_id,)).fetchone()
    _match(row,metadata,amount)
    pay=db.execute("SELECT telegram_id,tariff_id,amount_rub,status FROM payments WHERE payment_id=?",(payment_id,)).fetchone()
    allowed=("pending","paid") if status=="succeeded" else ("pending","canceled")
    if (not pay or pay["telegram_id"]!=row["telegram_id"] or pay["tariff_id"]!=row["tariff_id"]
            or kopecks(pay["amount_rub"])!=payable(row) or pay["status"] not in allowed):
        raise ReservationConflict("Local order mismatch")
    target="consumed" if status=="succeeded" else "released"
    if row["state"]==target:
        return "done"
    if row["state"]!="attached":
        raise ReservationConflict("Conflicting terminal state")
    if target=="consumed":
        owner=row["telegram_id"]
        if row["kind"]=="manual":
            cur=db.execute("UPDATE settings SET value='' WHERE key=? AND value=?",
                           (f"promocode_user_{owner}",row["manual_value"]))
            if cur.rowcount!=1:
                raise ReservationConflict("Manual discount changed while reserved")
            ensure_manual_schema(db)
            db.execute("DELETE FROM manual_promo_grants WHERE telegram_id=? AND value=?",(owner,row["manual_value"]))
        else:
            cur=db.execute("""UPDATE user_promocodes SET is_used=1,used_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=? AND promocode_id=? AND is_used=0""",
                (row["reward_id"],owner,row["promocode_id"]))
            if cur.rowcount!=1:
                raise ReservationConflict("Reward changed while reserved")
            db.execute("INSERT INTO promocode_uses(promocode_id,user_id,source) VALUES(?,?,?)",
                       (row["promocode_id"],owner,f"reservation:{row['checkout_id']}"))
    db.execute("UPDATE promo_reservations SET state=?,updated_at=CURRENT_TIMESTAMP WHERE checkout_id=?",
               (target,row["checkout_id"]))
    return target


def open_reservation(path,owner):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        if not has_schema(db):
            return None
        row=db.execute("SELECT * FROM promo_reservations WHERE telegram_id=? AND state IN ('reserved','attached')",(owner,)).fetchone()
        return dict(row) if row else None


def validate_binding(path,payment_id,metadata,amount):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        if not registered(db,payment_id):
            if metadata.get("promo_reservation_id"):
                raise ReservationConflict("Unregistered discount order")
            return False
        row=db.execute("SELECT * FROM promo_reservations WHERE payment_id=?",(payment_id,)).fetchone()
        _match(row,metadata,amount)
        return True


def settle_verified_success(path,payment_id,obj):
    """Called only after independent verification of provider success."""
    with transaction(path) as db:
        metadata=obj["metadata"]
        if not registered(db,payment_id):
            if metadata.get("promo_reservation_id"):
                raise ReservationConflict("Unregistered discount order")
            return "ignored"
        if obj.get("id")!=payment_id:
            raise ReservationConflict("Provider payment identity mismatch")
        return settle(db,payment_id,metadata,obj["amount_rub"],"succeeded")


def unbound_reservation(path,checkout_id):
    if not isinstance(checkout_id,str) or not 1<=len(checkout_id)<=128:
        return None
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        if not has_schema(db):
            return None
        row=db.execute("SELECT * FROM promo_reservations WHERE checkout_id=? AND state='reserved'",(checkout_id,)).fetchone()
        return dict(row) if row else None


def has_unbound(path):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        return has_schema(db) and bool(db.execute("SELECT 1 FROM promo_reservations WHERE state='reserved' LIMIT 1").fetchone())


def recover_verified_binding(path,payment_id,metadata,amount):
    """Only from authenticated provider response, never raw webhook data."""
    if not isinstance(metadata,dict):
        return False
    row=unbound_reservation(path,metadata.get("promo_reservation_id"))
    if row is None:
        return False
    _match(row,metadata,amount)
    attach(path,row["checkout_id"],payment_id,metadata,amount)
    return True
