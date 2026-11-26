"""Explicit read-only provider/receipt check, no payment or income mutations."""
import asyncio
import base64
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from refund_fulfillment import eligible_row, money, with_authenticated_client, receipt_cancellation, refund_confirmed


def order_snapshot(path, order_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        p=db.execute("SELECT payment_id FROM payments WHERE id=?", (order_id,)).fetchone()
        if not p:
            return None
        p=eligible_row(db,p["payment_id"])
        if not p:
            return None
        j=db.execute("SELECT * FROM payment_processing WHERE payment_id=?",(p["payment_id"],)).fetchone()
        receipts=db.execute("SELECT receipt_uuid,amount,user_id FROM tax_receipts WHERE payment_id=?",(p["payment_id"],)).fetchall()
        return dict(p), dict(j) if j else {}, [dict(r) for r in receipts]


def provider_credentials(path):
    """The settings form stores YooKassa keys in plaintext; support existing encrypted keys
    without importing encryption.py (which performs migration on import)."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        settings=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('yookassa_shop_id','yookassa_secret_key','encryption_key')"))
    shop=settings["yookassa_shop_id"]
    secret=settings["yookassa_secret_key"]
    if not isinstance(secret,str) or not re.fullmatch(r"(live|test)_[A-Za-z0-9_-]+",secret):
        from cryptography.fernet import Fernet
        secret=Fernet(base64.urlsafe_b64decode(settings["encryption_key"])).decrypt(base64.urlsafe_b64decode(secret)).decode()
    if not re.fullmatch(r"(live|test)_[A-Za-z0-9_-]+",secret) or not str(shop).isdigit():
        raise ValueError("Unsupported credentials")
    return str(shop),secret


async def provider_read(path, payment_id):
    import httpx
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",payment_id):
        raise ValueError("Invalid payment id")
    shop,secret=await asyncio.to_thread(provider_credentials,path)
    async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False) as client:
        response=await client.get("https://api.yookassa.ru/v3/payments/"+payment_id,auth=(shop,secret))
        response.raise_for_status()
        return response.json()


async def fiscal_read(path, uid, amount):
    async def action(client,inn):
        data=await asyncio.wait_for(client.receipt().json(uid),timeout=20)
        cancellation=receipt_cancellation(data,uid,amount,inn)
        if cancellation is None:
            return "active"
        return "refund" if refund_confirmed(cancellation) else "other_canceled"
    return await with_authenticated_client(path,uid,action)


def provider_state(p,data):
    if not isinstance(data,dict) or data.get("id")!=p["payment_id"]:
        raise ValueError("Identity")
    amount=data.get("amount") or {}
    meta=data.get("metadata") or {}
    if (amount.get("currency")!="RUB" or money(amount.get("value"))!=money(p["amount_rub"])
            or str(meta.get("telegram_id"))!=str(p["telegram_id"])
            or str(meta.get("tariff_id"))!=str(p["tariff_id"])):
        raise ValueError("Mismatch")
    state=data.get("status")
    if state not in ("pending","waiting_for_capture","succeeded","canceled"):
        raise ValueError("State")
    if data.get("paid") is not (state in ("waiting_for_capture","succeeded")):
        raise ValueError("Paid mismatch")
    refunded=data.get("refunded_amount")
    if refunded is None:
        if state=="succeeded":
            raise ValueError("Refund amount missing")
        return state,None
    if refunded.get("currency")!="RUB":
        raise ValueError("Currency")
    value=money(refunded.get("value"))
    if value>money(p["amount_rub"]):
        raise ValueError("Refund amount")
    return state,value


async def check_order(path,order_id,*,provider=None,fiscal=None):
    lines=[]
    def add(label,value):
        lines.append({"label":label,"value":value})
    result={"order_id":order_id,"lines":lines}
    snapshot=await asyncio.to_thread(order_snapshot,path,order_id)
    if not snapshot:
        add("Сверка","Заказ вне нового платёжного контура или не найден.")
        return result
    p,j,receipts=snapshot
    add("Локальный статус",p["status"])
    add("Локальный возврат",j.get("refund_state") or "Нет записи")
    add("Локальный чек",j.get("tax_state") or "Нет записи")
    try:
        data=await asyncio.wait_for((provider or provider_read)(path,p["payment_id"]),20)
        state,refunded=provider_state(p,data)
        add("ЮKassa",{"succeeded":"Оплачен","pending":"Ожидает","canceled":"Отменён","waiting_for_capture":"Ожидает подтверждения"}[state])
        add("Возвращено в ЮKassa",f"{refunded:.2f} ₽" if refunded is not None else "Не указано")
        expected={"paid":"succeeded","pending":"pending","canceled":"canceled"}.get(p["status"])
        if expected!=state:
            add("Оплата","Расхождение с локальным статусом")
        if refunded is not None and refunded>0 and j.get("refund_state") in (None,"none"):
            add("Возврат","Расхождение: возврат не отражён в журнале")
    except Exception:
        add("ЮKassa","Не удалось подтвердить данные")
    uid=j.get("receipt_uuid")
    if not uid:
        add("ФНС","Нет идентификатора чека. Отсутствие чека в ФНС не подтверждено.")
        return result
    try:
        if (len(receipts)!=1 or receipts[0]["receipt_uuid"]!=uid
                or money(receipts[0]["amount"])!=money(p["amount_rub"])
                or receipts[0]["user_id"]!=p["telegram_id"]):
            raise ValueError("Local receipt mismatch")
        state=await asyncio.wait_for((fiscal or fiscal_read)(path,uid,money(p["amount_rub"])),100)
        label={"active":"Действующий","refund":"Аннулирован: возврат средств","other_canceled":"Аннулирован: другая или неизвестная причина"}[state]
        add("ФНС",label)
        if (state=="active" and j.get("tax_state")=="canceled") or (state!="active" and j.get("tax_state")!="canceled"):
            add("Чек","Расхождение с локальным статусом")
    except Exception:
        add("ФНС","Не удалось подтвердить состояние чека")
    return result
