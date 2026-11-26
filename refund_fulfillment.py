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

LOG = logging.getLogger(__name__)
CUTOFF = "2026-11-26T12:15:00Z"
LAST_HISTORICAL_ID = 7


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result != result.quantize(Decimal("0.01")):
        raise ValueError("Invalid amount")
    return result


def eligible_row(db, payment_id):
    return db.execute("""
        SELECT * FROM payments WHERE payment_id=? AND id>?
          AND datetime(created_at)>=datetime(?) AND amount_rub>0
    """, (payment_id, LAST_HISTORICAL_ID, CUTOFF)).fetchone()


def scope(path, payment_id):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if eligible_row(db, payment_id):
            return "new"
        return "historical" if db.execute(
            "SELECT 1 FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone() else "unknown"


def record_canceled(path, payment_id):
    """Caller has independently verified canceled, unpaid and matching payment."""
    with transaction(path) as db:
        p = eligible_row(db, payment_id)
        if not p:
            return "ignored"
        if p["status"] == "canceled":
            return "done"
        if p["status"] != "pending" or db.execute(
            "SELECT 1 FROM payment_processing WHERE payment_id=?", (payment_id,)
        ).fetchone():
            return "needs_review"
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


async def cancel_and_confirm(client, receipt_uuid, amount, inn, reason):
    receipt = client.receipt()
    before = await receipt.json(receipt_uuid)
    if receipt_cancellation(before, receipt_uuid, amount, inn) is not None:
        raise RuntimeError("Receipt previously canceled; reconcile manually")
    income = client.income()
    disable_income_retry(income.http)
    # Do not repeat the POST, even if its response is lost.
    await income.cancel(receipt_uuid, reason)
    after = await receipt.json(receipt_uuid)
    cancellation = receipt_cancellation(after, receipt_uuid, amount, inn)
    if not isinstance(cancellation, dict) or not cancellation:
        raise RuntimeError("Unconfirmed cancellation result")
    # Unknown or conflicting reasons remain for manual reconciliation.
    if cancellation.get("comment") not in ("Возврат средств", "Возврат денежных средств"):
        raise RuntimeError("Cancellation reason requires reconciliation")


async def cancel_receipt(path, receipt_uuid, amount):
    """Authenticate, verify the receipt, cancel once and verify by a fresh GET."""
    from nalogo import Client
    from nalogo.dto.income import CancelCommentType
    inn, password = await asyncio.to_thread(credentials, path)
    public_receipt_url(inn, receipt_uuid)  # validate path components
    with tempfile.TemporaryDirectory(prefix="vpn4us-refund-") as tmp:
        client = Client(storage_path=str(Path(tmp) / "session.json"),
                        device_id=uuid.uuid4().hex, timeout=15.0)
        token = await client.create_new_access_token(inn, password)
        await client.authenticate(token)
        await cancel_and_confirm(client, receipt_uuid, amount, inn, CancelCommentType.REFUND)


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
            await asyncio.wait_for((canceler or cancel_receipt)(path, *claimed), 60)
            if not await asyncio.to_thread(finish_cancel, path, payment_id, True):
                raise RuntimeError("Refund claim lost")
        except (Exception, asyncio.CancelledError) as exc:
            await asyncio.to_thread(finish_cancel, path, payment_id, False)
            LOG.error("Refund requires manual reconciliation: %s", type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
