"""Durable balance operations. All monetary arithmetic is in integer kopecks.
Existing users.balance remains the compatibility projection in rubles.
Only explicitly registered new deposits are eligible; no historical replay.
"""
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_KOPECKS=10_000_000


def kopecks(value, *, positive=False, maximum=None):
    try:
        v=Decimal(str(value))
        if not v.is_finite() or v<0 or v!=v.quantize(Decimal("0.01")):
            raise ValueError("Invalid amount")
        n=int(v*100)
    except (InvalidOperation, TypeError, OverflowError):
        raise ValueError("Invalid amount") from None
    if (positive and n<=0) or (maximum is not None and n>maximum):
        raise ValueError("Invalid amount")
    return n


def rubles(n):
    return Decimal(n)/100


@contextmanager
def transaction(path):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            ensure_schema(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS balance_deposit_orders (
        payment_id TEXT PRIMARY KEY REFERENCES payments(payment_id),
        telegram_id INTEGER NOT NULL, amount_kopecks INTEGER NOT NULL CHECK(amount_kopecks>0),
        refunded_kopecks INTEGER NOT NULL DEFAULT 0 CHECK(refunded_kopecks>=0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS balance_operations (
        operation_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL,
        kind TEXT NOT NULL, delta_kopecks INTEGER NOT NULL,
        before_kopecks INTEGER NOT NULL CHECK(before_kopecks>=0),
        after_kopecks INTEGER NOT NULL CHECK(after_kopecks>=0),
        actor_id INTEGER, payment_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK(after_kopecks=before_kopecks+delta_kopecks))""")


def has_deposit_table(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='balance_deposit_orders'").fetchone())


def deposit_exists(db,payment_id):
    return has_deposit_table(db) and bool(db.execute(
        "SELECT 1 FROM balance_deposit_orders WHERE payment_id=?",(payment_id,)).fetchone())


def deposit_scope_sql(db,alias="p"):
    # alias is an internal fixed SQL identifier, not user input.
    return (f"EXISTS (SELECT 1 FROM balance_deposit_orders d WHERE d.payment_id={alias}.payment_id)"
            if has_deposit_table(db) else "0")


def is_registered(path,payment_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        return deposit_exists(db,payment_id)


def validate_deposit_amount(value):
    amount=kopecks(value,positive=True,maximum=MAX_KOPECKS)
    if amount<1000:
        raise ValueError("Minimum deposit is 10 rubles")
    return amount


def deposit_metadata(owner,amount):
    return {"telegram_id":str(owner),"tariff_id":"0","action":"deposit","type":"deposit",
            "amount":f"{rubles(amount):.2f}"}


def metadata_matches(owner,amount,meta):
    try:
        return (isinstance(meta,dict) and str(meta.get("telegram_id"))==str(owner)
            and str(meta.get("tariff_id"))=="0" and meta.get("action")=="deposit"
            and meta.get("type")=="deposit" and kopecks(meta.get("amount"))==amount)
    except ValueError:
        return False


def register_deposit(path,payment_id,owner,amount):
    if not isinstance(payment_id,str) or not payment_id or len(payment_id)>128:
        raise ValueError("Invalid payment id")
    if type(amount) is not int or not 1000<=amount<=MAX_KOPECKS:
        raise ValueError("Invalid deposit amount")
    with transaction(path) as db:
        user=db.execute("SELECT is_blocked FROM users WHERE telegram_id=?",(owner,)).fetchone()
        if not user or user[0]!=0:
            raise ValueError("User unavailable")
        old=db.execute("SELECT telegram_id,amount_kopecks FROM balance_deposit_orders WHERE payment_id=?",(payment_id,)).fetchone()
        if old:
            if tuple(old)!=(owner,amount):
                raise ValueError("Deposit identity mismatch")
            return
        db.execute("INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status) VALUES(?,?,0,?,'pending')",
                   (payment_id,owner,str(rubles(amount))))
        db.execute("INSERT INTO balance_deposit_orders(payment_id,telegram_id,amount_kopecks) VALUES(?,?,?)",
                   (payment_id,owner,amount))


def deposit_row(db,payment_id):
    row=db.execute("""SELECT d.telegram_id,d.amount_kopecks,d.refunded_kopecks,
        p.status,p.tariff_id,p.amount_rub,p.telegram_id AS payer
        FROM balance_deposit_orders d JOIN payments p USING(payment_id)
        WHERE d.payment_id=?""",(payment_id,)).fetchone()
    if not row or row["tariff_id"]!=0 or row["payer"]!=row["telegram_id"] or kopecks(row["amount_rub"])!=row["amount_kopecks"]:
        raise ValueError("Deposit order mismatch")
    return row


def balance_held(db,owner):
    return bool(db.execute("""SELECT 1 FROM balance_deposit_orders d
      JOIN payment_processing j USING(payment_id)
      WHERE d.telegram_id=? AND d.refunded_kopecks>0
        AND j.refund_state IN ('partial_review','full_review') LIMIT 1""",(owner,)).fetchone())


def apply_operation(db,operation_id,owner,kind,delta,*,actor=None,payment_id=None):
    old=db.execute("SELECT * FROM balance_operations WHERE operation_id=?",(operation_id,)).fetchone()
    if old:
        if (old["telegram_id"],old["kind"],old["delta_kopecks"],old["actor_id"],old["payment_id"])!=(owner,kind,delta,actor,payment_id):
            raise ValueError("Operation identity mismatch")
        return old["after_kopecks"],False
    row=db.execute("SELECT balance FROM users WHERE telegram_id=?",(owner,)).fetchone()
    if not row:
        raise ValueError("User not found")
    before=kopecks(row[0])
    after=before+delta
    if after<0 or (delta<0 and balance_held(db,owner)):
        raise ValueError("Insufficient or held balance")
    db.execute("UPDATE users SET balance=? WHERE telegram_id=?",(str(rubles(after)),owner))
    db.execute("""INSERT INTO balance_operations(operation_id,telegram_id,kind,delta_kopecks,
        before_kopecks,after_kopecks,actor_id,payment_id) VALUES(?,?,?,?,?,?,?,?)""",
        (operation_id,owner,kind,delta,before,after,actor,payment_id))
    return after,True


def admin_adjust(path,operation_id,owner,actor,amount,*,withdraw=False):
    n=kopecks(amount,positive=True,maximum=MAX_KOPECKS)
    with transaction(path) as db:
        return apply_operation(db,operation_id,owner,"admin_withdraw" if withdraw else "admin_credit",
                               -n if withdraw else n,actor=actor)


def credit_verified(path,payment_id,metadata):
    """Called only after provider verification, with zero verified refund."""
    with transaction(path) as db:
        row=deposit_row(db,payment_id)
        owner,amount=row["telegram_id"],row["amount_kopecks"]
        if not metadata_matches(owner,amount,metadata):
            raise ValueError("Deposit metadata mismatch")
        j=db.execute("SELECT * FROM payment_processing WHERE payment_id=?",(payment_id,)).fetchone()
        if row["refunded_kopecks"] or (j and j["refund_state"]!="none"):
            return "held",owner,None
        if row["status"]=="paid":
            op=db.execute("SELECT * FROM balance_operations WHERE operation_id=?",("deposit:"+payment_id,)).fetchone()
            if not op or op["telegram_id"]!=owner or op["delta_kopecks"]!=amount or op["kind"]!="deposit" or op["payment_id"]!=payment_id or not j or j["is_legacy"] or j["vpn_state"]!="done":
                raise ValueError("Paid deposit requires reconciliation")
            return "done",owner,op["after_kopecks"]
        if row["status"]!="pending" or j:
            raise ValueError("Unexpected deposit state")
        balance,_=apply_operation(db,"deposit:"+payment_id,owner,"deposit",amount,payment_id=payment_id)
        # Only new, verified cash deposits under an explicitly approved NPD
        # prepayment policy are eligible. A duplicate event never requeues history.
        tax=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('tax_enabled','tax_deposit_policy')"))
        tax_state="skipped"
        if tax.get("tax_enabled")=="true":
            tax_state="queued" if tax.get("tax_deposit_policy")=="npd_prepayment" else "needs_review"
        db.execute("""INSERT INTO payment_processing(payment_id,payment_state,vpn_state,tax_state)
            VALUES(?,'verified','done',?)""",(payment_id,tax_state))
        db.execute("UPDATE payments SET status='paid' WHERE payment_id=?",(payment_id,))
        return "credited",owner,balance


def record_deposit_change(path,payment_id,metadata,*,refund=None,canceled=False):
    """Verified provider event. Refunds hold spending pending human review;
    never silently remove funds or issue/cancel a tax receipt."""
    with transaction(path) as db:
        row=deposit_row(db,payment_id)
        if not metadata_matches(row["telegram_id"],row["amount_kopecks"],metadata):
            raise ValueError("Deposit metadata mismatch")
        j=db.execute("SELECT * FROM payment_processing WHERE payment_id=?",(payment_id,)).fetchone()
        if canceled:
            if row["status"]=="canceled":
                return "done"
            if row["status"]!="pending" or j or row["refunded_kopecks"]:
                raise ValueError("Canceled deposit requires reconciliation")
            db.execute("UPDATE payments SET status='canceled' WHERE payment_id=?",(payment_id,))
            db.execute("""INSERT INTO payment_processing(payment_id,payment_state,vpn_state,tax_state)
                VALUES(?,'canceled','not_started','skipped')""",(payment_id,))
            return "done"
        amount=kopecks(refund,positive=True)
        if amount>row["amount_kopecks"] or row["status"] not in ("paid","pending"):
            raise ValueError("Invalid refund")
        if amount<=row["refunded_kopecks"]:
            return "review"
        state="full_review" if amount==row["amount_kopecks"] else "partial_review"
        db.execute("UPDATE balance_deposit_orders SET refunded_kopecks=? WHERE payment_id=?",(amount,payment_id))
        if not j:
            db.execute("""INSERT INTO payment_processing(payment_id,payment_state,vpn_state,tax_state,refund_state)
                VALUES(?,'verified','not_started','skipped',?)""",(payment_id,state))
        else:
            db.execute("""UPDATE payment_processing SET refund_state=?,
                tax_state=CASE WHEN tax_state='queued' THEN 'needs_review' ELSE tax_state END,
                updated_at=CURRENT_TIMESTAMP WHERE payment_id=?""",(state,payment_id))
        return "review"
