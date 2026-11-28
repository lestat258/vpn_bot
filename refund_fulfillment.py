"""Verified cancellation/refund state machine. Historical orders are excluded.
Never request a monetary refund and never replay an ambiguous FNS cancellation.
"""
import asyncio
import logging
import sqlite3
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from tax_fulfillment import transaction, credentials, disable_income_retry, public_receipt_url
from balance_ledger import deposit_exists
from promo_reservations import registered as promo_registered, settle as settle_promo

LOG = logging.getLogger(__name__)
CUTOFF = "2026-11-26T12:15:00Z"
LAST_HISTORICAL_ID = 7


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result != result.quantize(Decimal("0.01")):
        raise ValueError("Invalid amount")
    return result


def eligible_row(db, payment_id):
    if deposit_exists(db,payment_id):
        return db.execute("SELECT * FROM payments WHERE payment_id=? AND tariff_id=0 AND amount_rub>0",(payment_id,)).fetchone()
    if promo_registered(db,payment_id):
        return db.execute("SELECT * FROM payments WHERE payment_id=? AND tariff_id>0 AND amount_rub>0",(payment_id,)).fetchone()
    return db.execute("""
        SELECT * FROM payments WHERE payment_id=? AND id>?
          AND datetime(created_at)>=datetime(?) AND amount_rub>0 AND tariff_id<>0
    """, (payment_id, LAST_HISTORICAL_ID, CUTOFF)).fetchone()


def scope(path, payment_id):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if eligible_row(db, payment_id):
            return "new"
        return "historical" if db.execute(
            "SELECT 1 FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone() else "unknown"


def record_canceled(path, payment_id, *, metadata=None, amount=None):
    """Caller has independently verified canceled, unpaid and matching payment."""
    with transaction(path) as db:
        p = eligible_row(db, payment_id)
        if not p:
            return "ignored"
        if p["status"] == "canceled":
            if promo_registered(db,payment_id):
                settle_promo(db,payment_id,metadata,amount,"canceled")
            return "done"
        if p["status"] != "pending" or db.execute(
            "SELECT 1 FROM payment_processing WHERE payment_id=?", (payment_id,)
        ).fetchone():
            return "needs_review"
        if promo_registered(db,payment_id):
            settle_promo(db,payment_id,metadata,amount,"canceled")
        db.execute("UPDATE payments SET status='canceled' WHERE payment_id=?", (payment_id,))
        db.execute("""
            INSERT INTO payment_processing(payment_id,payment_state,vpn_state,tax_state)
            VALUES (?,'canceled','not_started','skipped')
        """, (payment_id,))
        return "done"


def record_refund(path, payment_id, aggregate):
    """Caller verified succeeded payment and provider's cumulative refunded amount."""
    aggregate = money(aggregate)
    with transaction(path) as db:
        p = eligible_row(db, payment_id)
        if not p:
            return "ignored"
        amount = money(p["amount_rub"])
        if not 0 < aggregate <= amount or p["status"] not in ("paid", "pending"):
            raise ValueError("Invalid refund/payment state")
        db.execute("""
            INSERT OR IGNORE INTO payment_processing
                (payment_id,payment_state,vpn_state,tax_state)
            VALUES (?,'verified','not_started','not_started')
        """, (payment_id,))
        j = db.execute("SELECT * FROM payment_processing WHERE payment_id=?", (payment_id,)).fetchone()
        state = j["refund_state"]
        if j["is_legacy"] or j["payment_state"] != "verified":
            return "needs_review"
        if state != "none":
            # Any prior partial/manual handling is ambiguous; no automated second effect.
            if state == "partial_review" and aggregate == amount:
                state = "full_review"
                db.execute("UPDATE payment_processing SET refund_state=?,updated_at=CURRENT_TIMESTAMP WHERE payment_id=?",
                           (state, payment_id))
            return state
        if aggregate < amount:
            state = "partial_review"
        elif j["tax_state"] == "processing":
            state = "full_queued"
        elif j["tax_state"] == "done" and j["receipt_uuid"]:
            state = "full_queued"
        else:
            state = "full_review"
        db.execute("""
            UPDATE payment_processing SET refund_state=?,updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=?
        """, (state, payment_id))
        # Preserve paid status and subscription history. No automatic VPN revocation.
        return state


def pending_ids(path):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        return [r[0] for r in db.execute("""
            SELECT j.payment_id FROM payment_processing j JOIN payments p USING(payment_id)
            WHERE j.refund_state='full_queued' AND j.is_legacy=0
              AND p.id>? AND datetime(p.created_at)>=datetime(?)
            ORDER BY p.id
        """, (LAST_HISTORICAL_ID, CUTOFF))]


def claim_cancel(path, payment_id):
    with transaction(path) as db:
        p = eligible_row(db, payment_id)
        if not p:
            return None
        j = db.execute("SELECT * FROM payment_processing WHERE payment_id=?", (payment_id,)).fetchone()
        if not j or j["refund_state"] != "full_queued" or j["is_legacy"]:
            return None
        if j["tax_state"] == "processing":
            return None
        rows = db.execute("SELECT receipt_uuid,amount,user_id FROM tax_receipts WHERE payment_id=?",
                          (payment_id,)).fetchall()
        if (j["payment_state"] != "verified" or j["tax_state"] != "done"
                or not j["receipt_uuid"] or len(rows) != 1
                or rows[0]["receipt_uuid"] != j["receipt_uuid"]
                or money(rows[0]["amount"]) != money(p["amount_rub"])
                or rows[0]["user_id"] != p["telegram_id"]):
            db.execute("UPDATE payment_processing SET refund_state='full_review',updated_at=CURRENT_TIMESTAMP WHERE payment_id=?", (payment_id,))
            return None
        db.execute("UPDATE payment_processing SET refund_state='full_processing',updated_at=CURRENT_TIMESTAMP WHERE payment_id=?", (payment_id,))
        return j["receipt_uuid"], money(p["amount_rub"])


def finish_cancel(path, payment_id, success):
    with transaction(path) as db:
        if not eligible_row(db, payment_id):
            return False
        if success:
            result = db.execute("""
                UPDATE payment_processing SET refund_state='full_done',tax_state='canceled',
                    updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND refund_state='full_processing' AND tax_state='done'
            """, (payment_id,))
        else:
            result = db.execute("""
                UPDATE payment_processing SET refund_state='full_review',updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND refund_state='full_processing'
            """, (payment_id,))
        return result.rowcount == 1


def receipt_cancellation(data, receipt_uuid, amount, inn):
    """Read the authenticated receipt, not the unreliable POST response."""
    if (not isinstance(data, dict) or data.get("receiptId") != receipt_uuid
            or str(data.get("inn")) != inn
            or money(data.get("totalAmount")) != amount
            or "cancellationInfo" not in data):
        raise RuntimeError("Receipt identity or cancellation state mismatch")
    return data["cancellationInfo"]


def refund_confirmed(cancellation):
    return isinstance(cancellation, dict) and cancellation.get("comment") in (
        "Возврат средств", "Возврат денежных средств"
    )


async def read_confirmation(receipt, receipt_uuid, amount, inn, *, delays=(0, 2, 5)):
    """Retry only the authenticated GET, never a mutation."""
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            data = await asyncio.wait_for(receipt.json(receipt_uuid), timeout=35)
            cancellation = receipt_cancellation(data, receipt_uuid, amount, inn)
        except Exception as exc:
            LOG.warning("Refund stage=confirm_get error=%s", type(exc).__name__)
            continue
        if refund_confirmed(cancellation):
            LOG.info("Refund stage=confirm_get result=confirmed")
            return True
        if cancellation is not None:
            LOG.warning("Refund stage=confirm_get result=unknown_reason")
            return False
        LOG.info("Refund stage=confirm_get result=active")
    return False


async def cancel_and_confirm(client, receipt_uuid, amount, inn, reason, *, delays=(0, 2, 5)):
    LOG.info("Refund stage=precheck_get started")
    receipt = client.receipt()
    try:
        before = await asyncio.wait_for(receipt.json(receipt_uuid), timeout=35)
        if receipt_cancellation(before, receipt_uuid, amount, inn) is not None:
            raise RuntimeError("Receipt previously canceled; reconcile manually")
    except Exception as exc:
        LOG.warning("Refund stage=precheck_get error=%s", type(exc).__name__)
        raise
    LOG.info("Refund stage=precheck_get result=active")
    income = client.income()
    disable_income_retry(income.http)
    # nalogo 1.0.0 passes this per-request timeout into httpx.
    # The outer deadline also bounds the complete POST.
    income.http.timeout = 90.0
    LOG.info("Refund stage=cancel_post started")
    try:
        await asyncio.wait_for(income.cancel(receipt_uuid, reason), timeout=95)
        LOG.info("Refund stage=cancel_post response_received")
    except Exception as exc:
        # A failed response is not proof that the server did not mutate.
        LOG.warning("Refund stage=cancel_post error=%s; read-only reconciliation", type(exc).__name__)
    if not await read_confirmation(receipt, receipt_uuid, amount, inn, delays=delays):
        raise RuntimeError("Unconfirmed cancellation result")


async def with_authenticated_client(path, receipt_uuid, action):
    from nalogo import Client
    inn, password = await asyncio.to_thread(credentials, path)
    public_receipt_url(inn, receipt_uuid)
    with tempfile.TemporaryDirectory(prefix="vpn4us-refund-") as tmp:
        client = Client(storage_path=str(Path(tmp) / "session.json"),
                        device_id=uuid.uuid4().hex, timeout=30.0)
        stage = "token"
        try:
            LOG.info("Refund stage=token started")
            token = await asyncio.wait_for(client.create_new_access_token(inn, password), timeout=35)
            LOG.info("Refund stage=token result=ok")
            stage = "authenticate"
            await asyncio.wait_for(client.authenticate(token), timeout=35)
            LOG.info("Refund stage=authenticate result=ok")
        except Exception as exc:
            LOG.warning("Refund stage=%s error=%s", stage, type(exc).__name__)
            raise
        return await action(client, inn)


async def cancel_receipt(path, receipt_uuid, amount):
    from nalogo.dto.income import CancelCommentType

    async def action(client, inn):
        await cancel_and_confirm(client, receipt_uuid, amount, inn, CancelCommentType.REFUND)

    await with_authenticated_client(path, receipt_uuid, action)


async def check_receipt_canceled(path, receipt_uuid, amount):
    """Read-only reconciliation, including after worker restart."""
    async def action(client, inn):
        return await read_confirmation(client.receipt(), receipt_uuid, amount, inn, delays=(0,))

    return await with_authenticated_client(path, receipt_uuid, action)


def review_receipt(path, payment_id):
    """Only eligible, fully refunded review/processing rows with one matching receipt."""
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        p = eligible_row(db, payment_id)
        if not p:
            return None
        j = db.execute("SELECT * FROM payment_processing WHERE payment_id=?", (payment_id,)).fetchone()
        if (not j or j["refund_state"] not in ("full_review", "full_processing")
                or j["tax_state"] != "done" or j["payment_state"] != "verified"
                or j["is_legacy"] or not j["receipt_uuid"]):
            return None
        rows = db.execute("SELECT receipt_uuid,amount,user_id FROM tax_receipts WHERE payment_id=?",
                          (payment_id,)).fetchall()
        if (len(rows) != 1 or rows[0]["receipt_uuid"] != j["receipt_uuid"]
                or money(rows[0]["amount"]) != money(p["amount_rub"])
                or rows[0]["user_id"] != p["telegram_id"]):
            return None
        return j["receipt_uuid"], money(p["amount_rub"])


def finish_reconciliation(path, payment_id, receipt_uuid):
    with transaction(path) as db:
        if not eligible_row(db, payment_id):
            return False
        cur = db.execute("""
            UPDATE payment_processing SET refund_state='full_done',tax_state='canceled',
                updated_at=CURRENT_TIMESTAMP
            WHERE payment_id=? AND receipt_uuid=? AND tax_state='done'
              AND payment_state='verified' AND is_legacy=0
              AND refund_state IN ('full_review','full_processing')
        """, (payment_id, receipt_uuid))
        return cur.rowcount == 1


async def reconcile_refund(path, payment_id, verifier, *, checker=None):
    """Never sends POST, never queues a refund for another attempt."""
    try:
        claimed = await asyncio.to_thread(review_receipt, path, payment_id)
        if not claimed or not await verifier(payment_id):
            return False
        if not await asyncio.wait_for((checker or check_receipt_canceled)(path, *claimed), timeout=120):
            return False
        return await asyncio.to_thread(finish_reconciliation, path, payment_id, claimed[0])
    except Exception as exc:
        LOG.warning("Refund stage=reconcile error=%s", type(exc).__name__)
        return False


async def drain_refunds(path, verifier, *, canceler=None):
    for payment_id in await asyncio.to_thread(pending_ids, path):
        try:
            # Independent read-only verification before any external fiscal effect.
            if not await verifier(payment_id):
                continue
            claimed = await asyncio.to_thread(claim_cancel, path, payment_id)
            if claimed is None:
                continue
        except Exception as exc:
            LOG.warning("Refund verification deferred: %s", type(exc).__name__)
            continue
        try:
            await asyncio.wait_for((canceler or cancel_receipt)(path, *claimed), 330)
            if not await asyncio.to_thread(finish_cancel, path, payment_id, True):
                raise RuntimeError("Refund claim lost")
        except (Exception, asyncio.CancelledError) as exc:
            await asyncio.to_thread(finish_cancel, path, payment_id, False)
            LOG.error("Refund requires manual reconciliation: %s", type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
