"""Independent fiscal state machine for new, independently verified payments.
Never repeat an ambiguous external income registration. Historical not_started
rows are deliberately not eligible; reconcile those with FNS separately.
"""
import asyncio
import base64
import logging
import sqlite3
import tempfile
import uuid
from datetime import datetime
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


def queue_verified(path, payment_id):
    """Only call following independent provider verification; never queue history."""
    with transaction(path) as db:
        db.execute("""
            INSERT OR IGNORE INTO payment_processing
              (payment_id, payment_state, vpn_state, tax_state)
            SELECT payment_id, 'verified', 'not_started', 'queued'
            FROM payments WHERE payment_id=? AND status='pending' AND amount_rub>0
        """, (payment_id,))


def claim(path, payment_id):
    with transaction(path) as db:
        row = db.execute("""
            SELECT p.telegram_id, p.amount_rub, p.status,
                   j.tax_state, j.receipt_uuid, j.payment_state,
                   j.refund_state, j.is_legacy
            FROM payments p JOIN payment_processing j USING(payment_id)
            WHERE p.payment_id=?
        """, (payment_id,)).fetchone()
        if (not row or row["status"] not in ("paid", "pending") or row["payment_state"] != "verified"
                or row["tax_state"] != "queued" or row["is_legacy"]
                or row["refund_state"] != "none" or row["amount_rub"] <= 0):
            return None
        settings = dict(db.execute("""
            SELECT key,value FROM settings WHERE key IN
              ('tax_enabled','tax_description_template')
        """))
        state = "processing"
        if row["receipt_uuid"] or db.execute(
            "SELECT 1 FROM tax_receipts WHERE payment_id=? LIMIT 1",
            (payment_id,)
        ).fetchone():
            state = "needs_review"
        elif settings.get("tax_enabled") != "true":
            state = "skipped"
        db.execute("""
            UPDATE payment_processing SET tax_state=?,
                updated_at=CURRENT_TIMESTAMP WHERE payment_id=?
        """, (state, payment_id))
        if state != "processing":
            return None
        return (int(row["telegram_id"]), Decimal(str(row["amount_rub"])),
                (settings.get("tax_description_template") or "Оплата VPN-подписки")[:255])


def complete(path, payment_id, user_id, amount, description, receipt_uuid, url):
    with transaction(path) as db:
        cur = db.execute("""
            UPDATE payment_processing SET tax_state='done', receipt_uuid=?,
                receipt_url=?, updated_at=CURRENT_TIMESTAMP
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
            UPDATE payment_processing SET tax_state='needs_review',
                updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=? AND tax_state='processing'
        """, (payment_id,))


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


def disable_income_retry(http):
    """nalogo 1.0.0 retries HTTP 401; never replay an income POST."""
    if not callable(getattr(http, "_handle_401_response", None)):
        raise RuntimeError("Unreviewed nalogo HTTP client")

    async def no_retry(*args, **kwargs):
        return None

    http._handle_401_response = no_retry


def validated_operation_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Missing verified capture timestamp")
    return value


async def issue(path, amount, description, operation_time):
    from nalogo import Client
    inn, password = await asyncio.to_thread(credentials, path)
    with tempfile.TemporaryDirectory(prefix="vpn4us-tax-") as tmp:
        client = Client(storage_path=str(Path(tmp) / "session.json"),
                        device_id=uuid.uuid4().hex, timeout=15.0)
        token = await client.create_new_access_token(inn, password)
        await client.authenticate(token)
        income = client.income()
        disable_income_retry(income.http)
        result = await income.create(name=description, amount=amount, quantity=1,
                                     operation_time=validated_operation_time(operation_time))
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
                              issuer=None, notifier=None, operation_time=None):
    claimed = await asyncio.to_thread(claim, db_path, payment_id)
    if claimed is None:
        return False
    user_id, amount, description = claimed
    try:
        call = (issuer(db_path, amount, description) if issuer else
                issue(db_path, amount, description, operation_time))
        receipt_uuid, url = await asyncio.wait_for(call, timeout=60)
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


class TaxReviewRequired(RuntimeError):
    """Provider data needs manual reconciliation before a receipt is issued."""


def queued_ids(path):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        return [row[0] for row in db.execute("""
            SELECT j.payment_id FROM payment_processing j JOIN payments p USING(payment_id)
            WHERE j.tax_state='queued' AND j.payment_state='verified'
              AND j.is_legacy=0 AND j.refund_state='none'
              AND p.status IN ('paid','pending') AND p.amount_rub>0
            ORDER BY j.created_at, j.payment_id
        """)]


def interrupted_count(path):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        return db.execute("""
            SELECT COUNT(*) FROM payment_processing
            WHERE tax_state IN ('processing','needs_review')
        """).fetchone()[0]


def hold_queued(path, payment_id):
    with transaction(path) as db:
        db.execute("""
            UPDATE payment_processing SET tax_state='needs_review',
                updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=? AND tax_state='queued'
        """, (payment_id,))


async def drain_queue(path, verifier, *, issuer=None, notifier=None):
    """Resume only work that was never claimed. Retry safe read-only lookups,
    never re-send an income POST after a crash or ambiguous outcome."""
    for payment_id in await asyncio.to_thread(queued_ids, path):
        try:
            operation_time = await verifier(payment_id)
            operation_time = validated_operation_time(operation_time)
        except TaxReviewRequired:
            await asyncio.to_thread(hold_queued, path, payment_id)
            LOG.warning("Fiscal queue entry requires provider reconciliation")
            continue
        except Exception as exc:
            LOG.warning("Fiscal provider verification deferred: %s", type(exc).__name__)
            continue
        try:
            await process_tax_receipt(payment_id, db_path=path,
                                      issuer=issuer, notifier=notifier,
                                      operation_time=operation_time)
        except Exception as exc:
            LOG.error("Fiscal queue entry unavailable: %s", type(exc).__name__)
