"""Provider integration for durable discount reservations."""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import closing
from balance_ledger import kopecks,rubles
from promo_reservations import (
    reserve,attach,payable,metadata_for,open_reservation,validate_binding,
    ReservationConflict,ReservationBusy,
)


def request_key(bot_id,callback_id):
    return hashlib.sha256(f"vpn4us-checkout:{bot_id}:{callback_id}".encode()).hexdigest()


def provider_fields(payment,metadata,amount):
    actual=getattr(payment,"metadata",None)
    if not isinstance(actual,dict):
        raise ReservationConflict("Provider metadata unavailable")
    for k,v in metadata.items():
        if k=="subscription_id" and v in (None,"") and actual.get(k) in (None,""):
            continue
        if str(actual.get(k))!=str(v):
            raise ReservationConflict("Provider metadata mismatch")
    if not metadata.get("promo_reservation_id") and actual.get("promo_reservation_id"):
        raise ReservationConflict("Unexpected discount reservation")
    if payment.amount.currency!="RUB" or kopecks(payment.amount.value,positive=True)!=amount:
        raise ReservationConflict("Provider amount mismatch")
    pid=getattr(payment,"id",None)
    if not isinstance(pid,str) or not pid or len(pid)>128:
        raise ReservationConflict("Provider ID unavailable")
    return pid


def payment_link(payment):
    url=getattr(getattr(payment,"confirmation",None),"confirmation_url",None)
    return url if isinstance(url,str) and url.startswith("https://") else None


def create_checkout(path,provider,checkout_id,owner,tariff_id,name,price,username,*,action="new_key",subscription_id=None,full_price_token=None):
    if action == "renew":
        from subscription_renewal import renewal_lock
        from auto_renewal import pending
        with renewal_lock(path,subscription_id):
            with closing(sqlite3.connect(path)) as db:
                if pending(db,subscription_id):
                    raise ReservationBusy("Automatic renewal awaits reconciliation")
            return _create_checkout(path,provider,checkout_id,owner,tariff_id,name,price,username,
                                    action=action,subscription_id=subscription_id,full_price_token=full_price_token)
    return _create_checkout(path,provider,checkout_id,owner,tariff_id,name,price,username,
                            action=action,subscription_id=subscription_id,full_price_token=full_price_token)


def _create_checkout(path,provider,checkout_id,owner,tariff_id,name,price,username,*,action,subscription_id,full_price_token):
    row,fresh=reserve(path,checkout_id,owner,tariff_id,price,action=action,subscription_id=subscription_id,full_price_token=full_price_token)
    if row and not fresh:
        raise ReservationBusy("Already attempted")
    original=kopecks(price,positive=True)
    amount=payable(row) if row else original
    discount=original-amount
    metadata=metadata_for(row) if row else {
        "telegram_id":owner,"tariff_id":tariff_id,"action":action,
        "subscription_id":subscription_id if subscription_id is not None else "",
    }
    metadata={k:str(v) for k,v in metadata.items()}
    payload={
        "amount":{"value":f"{rubles(amount):.2f}","currency":"RUB"},
        "confirmation":{"type":"redirect","return_url":f"https://t.me/{username}"},
        "capture":True,
        "description":f"VPN {name}"[:128],
        "metadata":metadata,
    }
    if action == "renew":
        from auto_renewal import begin_manual_attempt, finish_manual_attempt
        begin_manual_attempt(path,checkout_id,owner,subscription_id)
    payment=provider.create(payload,checkout_id)
    pid=provider_fields(payment,metadata,amount)
    if row:
        attach(path,checkout_id,pid,payment.metadata,payment.amount.value)
    else:
        with closing(sqlite3.connect(path,timeout=15)) as db,db:
            db.execute("BEGIN IMMEDIATE")
            existing=db.execute("SELECT telegram_id,tariff_id,amount_rub,status FROM payments WHERE payment_id=?",(pid,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO payments(payment_id,telegram_id,tariff_id,amount_rub,status) VALUES(?,?,?,?,'pending')",
                    (pid,owner,tariff_id,str(rubles(amount))))
            elif existing[:2]!=(owner,tariff_id) or kopecks(existing[2])!=amount or existing[3]!="pending":
                raise ReservationConflict("Existing payment differs")
    if action == "renew":
        finish_manual_attempt(path,checkout_id)
    return amount,discount,payment_link(payment)


def recover_from_list(path,provider,row,*,max_pages=3):
    """Bounded read-only search. Never release a reservation based on absence.
    Require a unique exact marker and independently fetch the candidate.
    """
    created=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
    params={"created_at.gte":(created-timedelta(minutes=5)).isoformat().replace("+00:00","Z"),"limit":100}
    matches=set()
    cursors=set()
    for _ in range(max_pages):
        page=provider.list(dict(params))
        for item in page.items:
            meta=getattr(item,"metadata",None)
            if isinstance(meta,dict) and meta.get("promo_reservation_id")==row["checkout_id"]:
                matches.add(provider_fields(item,metadata_for(row),payable(row)))
        cursor=page.next_cursor
        if not cursor:
            break
        if cursor in cursors:
            return None
        cursors.add(cursor)
        params["cursor"]=cursor
    else:
        return None
    if len(matches)!=1:
        return None
    pid=next(iter(matches))
    payment=provider.find_one(pid)
    if provider_fields(payment,metadata_for(row),payable(row))!=pid:
        raise ReservationConflict("Wrong provider payment")
    if not ((payment.status=="pending" and payment.paid is False)
            or (payment.status=="succeeded" and payment.paid is True)
            or (payment.status=="canceled" and payment.paid is False and getattr(payment,"captured_at",None) is None)):
        return None
    attach(path,row["checkout_id"],pid,payment.metadata,payment.amount.value)
    return payment


def check_open_checkout(path,provider,owner):
    """Read only the owner's attached provider order, except verified cancellation."""
    row=open_reservation(path,owner)
    if not row:
        return "none",None,None
    if row["state"]=="reserved":
        if not callable(getattr(provider,"list",None)):
            return "review",payable(row),None
        payment=recover_from_list(path,provider,row)
        if payment is None:
            return "review",payable(row),None
        pid=payment.id
    else:
        pid=row["payment_id"]
        payment=provider.find_one(pid)
    if provider_fields(payment,metadata_for(row),payable(row))!=pid:
        raise ReservationConflict("Wrong provider payment")
    validate_binding(path,pid,payment.metadata,payment.amount.value)
    status=payment.status
    if status=="canceled" and payment.paid is False and getattr(payment,"captured_at",None) is None:
        from refund_fulfillment import record_canceled
        result=record_canceled(path,pid,metadata=payment.metadata,amount=payment.amount.value)
        return ("canceled" if result=="done" else "review"),payable(row),None
    if status=="pending" and payment.paid is False:
        return "pending",payable(row),payment_link(payment)
    if status=="succeeded" and payment.paid is True:
        return "paid",payable(row),None
    return "review",payable(row),None
