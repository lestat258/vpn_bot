"""Independent fiscal state machine for new, completed VPN payments.
Never repeat an ambiguous external income registration. Historical not_started
rows are deliberately not eligible; reconcile those with FNS separately.
"""
import asyncio
import base64
import logging
import sqlite3
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

LOG = logging.getLogger(__name__)


@contextmanager
def transaction(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def claim(path, payment_id):
    with transaction(path) as db:
        row = db.execute("""
            SELECT p.telegram_id, p.amount_rub, p.status,
                   j.tax_state, j.receipt_uuid, j.vpn_state,
                   j.refund_state, j.is_legacy
            FROM payments p JOIN payment_processing j USING(payment_id)
            WHERE p.payment_id=?
        """, (payment_id,)).fetchone()
        if (not row or row["status"] != "paid" or row["vpn_state"] != "done"
                or row["tax_state"] != "queued" or row["is_legacy"]
                or row["refund_state"] != "none" or row["amount_rub"] <= 0):
            return None
        settings = dict(db.execute("""
            SELECT key,value FROM settings WHERE key IN
              ('tax_enabled','tax_description_template')
        """))
        state = "processing"
        reason = None
        if row["receipt_uuid"] or db.execute(
            "SELECT 1 FROM tax_receipts WHERE payment_id=? LIMIT 1",
            (payment_id,)
        ).fetchone():
            state, reason = "needs_review", "tax: existing receipt"
        elif settings.get("tax_enabled") != "true":
            state = "skipped"
        db.execute("""
            UPDATE payment_processing SET tax_state=?, last_error=?,
                updated_at=CURRENT_TIMESTAMP WHERE payment_id=?
        """, (state, reason, payment_id))
        if state != "processing":
            return None
        return (int(row["telegram_id"]), Decimal(str(row["amount_rub"])),
                (settings.get("tax_description_template") or "Оплата VPN-подписки")[:255])


def complete(path, payment_id, user_id, amount, description, receipt_uuid, url):
    with transaction(path) as db:
        cur = db.execute("""
            UPDATE payment_processing SET tax_state='done', receipt_uuid=?,
                receipt_url=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=? AND tax_state='processing'
        """, (receipt_uuid, url, payment_id))
        if cur.rowcount != 1:
            raise RuntimeError("Receipt claim lost")
        db.execute("""
            INSERT INTO tax_receipts
              (payment_id, receipt_uuid, receipt_url, amount, description, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (payment_id, receipt_uuid, url, str(amount), description, user_id))


def review(path, payment_id, error_name):
    with transaction(path) as db:
        db.execute("""
            UPDATE payment_processing SET tax_state='needs_review', last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=? AND tax_state='processing'
        """, ("tax: " + error_name[:100], payment_id))


def credentials(path):
    from cryptography.fernet import Fernet
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        values = dict(db.execute("""
            SELECT key,value FROM settings WHERE key IN
              ('tax_inn','tax_password','encryption_key')
        """))
    inn = values["tax_inn"]
    if not (len(inn) == 12 and inn.isascii() and inn.isdigit()):
        raise ValueError("Invalid taxpayer identifier")
    cipher = Fernet(base64.urlsafe_b64decode(values["encryption_key"]))
    password = cipher.decrypt(base64.urlsafe_b64decode(values["tax_password"])).decode()
    return inn, password


async def issue(path, amount, description):
    from nalogo import Client
    inn, password = await asyncio.to_thread(credentials, path)
    with tempfile.TemporaryDirectory(prefix="vpn4us-tax-") as tmp:
        client = Client(storage_path=str(Path(tmp) / "session.json"),
                        device_id="vpn4us-tax-receipts", timeout=15.0)
        token = await client.create_new_access_token(inn, password)
        await client.authenticate(token)
        result = await client.income().create(name=description, amount=amount, quantity=1)
        receipt_uuid = result.get("approvedReceiptUuid")
        if not isinstance(receipt_uuid, str) or not receipt_uuid:
            raise RuntimeError("Missing receipt UUID")
        # The result is committed locally before any Telegram notification.
        url = client.receipt().print_url(receipt_uuid)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("Invalid receipt URL")
        return receipt_uuid, url


async def notify(user_id, url):
    from html import escape
    from payment_fulfillment import _telegram_notify
    await _telegram_notify(user_id,
                           '🧾 <b>Чек об оплате</b>\n<a href="' + escape(url, quote=True) + '">Открыть чек</a>',
                           None)


async def process_tax_receipt(payment_id, *, db_path="/opt/vpn-bot/data.db",
                              issuer=None, notifier=None):
    claimed = await asyncio.to_thread(claim, db_path, payment_id)
    if claimed is None:
        return False
    user_id, amount, description = claimed
    try:
        receipt_uuid, url = await asyncio.wait_for(
            (issuer or issue)(db_path, amount, description), timeout=60)
        await asyncio.to_thread(complete, db_path, payment_id, user_id,
                                amount, description, receipt_uuid, url)
    except (Exception, asyncio.CancelledError) as exc:
        try:
            await asyncio.to_thread(review, db_path, payment_id, type(exc).__name__)
        except Exception:
            LOG.error("Unable to persist fiscal review state")
        LOG.error("Fiscal outcome requires reconciliation: %s", type(exc).__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return False
    try:
        await (notifier or notify)(user_id, url)
    except Exception as exc:
        LOG.warning("Receipt stored but Telegram delivery failed: %s", type(exc).__name__)
    return True
