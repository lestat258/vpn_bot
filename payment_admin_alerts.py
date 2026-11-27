"""Alert on durable review/stalled states, never replay ambiguous Telegram delivery."""
import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from tax_fulfillment import transaction
from refund_fulfillment import CUTOFF, LAST_HISTORICAL_ID
from balance_ledger import deposit_scope_sql

LOG = logging.getLogger(__name__)
LABELS = {
    "payment": "Проверка оплаты",
    "vpn": "Выдача или продление подписки",
    "tax": "Формирование чека",
    "refund": "Обработка возврата",
}


def claim_alerts(path):
    # Explicit recipient only; no hard-coded fallback.
    with transaction(path) as db:
        row = db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
        if not row or not str(row[0]).isdigit() or int(row[0]) <= 0:
            return None, []
        recipient = int(row[0])
        db.execute("""CREATE TABLE IF NOT EXISTS payment_admin_alerts (
            order_id INTEGER NOT NULL, category TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'attempted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(order_id, category)
        )""")
        rows = db.execute("""
            SELECT p.id, j.payment_state, j.vpn_state, j.tax_state, j.refund_state,
                   COALESCE(datetime(j.updated_at)<=datetime('now','-15 minutes'),0) AS stale
              FROM payments p JOIN payment_processing j USING(payment_id)
             WHERE ((p.id>? AND datetime(p.created_at)>=datetime(?)) OR """+deposit_scope_sql(db)+""")
               AND p.amount_rub>0 AND j.is_legacy=0
             ORDER BY p.id
        """, (LAST_HISTORICAL_ID,CUTOFF)).fetchall()
        alerts = []
        for row in rows:
            categories = []
            if row["payment_state"] == "needs_review":
                categories.append("payment")
            if row["vpn_state"] == "needs_review" or (row["stale"] and row["vpn_state"] == "processing"):
                categories.append("vpn")
            if row["tax_state"] == "needs_review" or (row["stale"] and row["tax_state"] in ("queued","processing")):
                categories.append("tax")
            if row["refund_state"] in ("partial_review","full_review") or (row["stale"] and row["refund_state"] in ("full_queued","full_processing")):
                categories.append("refund")
            for category in categories:
                cur = db.execute("INSERT OR IGNORE INTO payment_admin_alerts(order_id,category) VALUES (?,?)",(row["id"],category))
                if cur.rowcount:
                    alerts.append((row["id"],category))
        return recipient, alerts


def finish(path,order_id,category,state):
    with transaction(path) as db:
        db.execute("UPDATE payment_admin_alerts SET state=? WHERE order_id=? AND category=? AND state='attempted'",
                   (state,order_id,category))


async def send(recipient, message):
    from payment_fulfillment import _telegram_notify
    await _telegram_notify(recipient,message,None,include_guide=False)


async def drain_alerts(path, *, sender=None):
    recipient, alerts = await asyncio.to_thread(claim_alerts,path)
    for order_id,category in alerts:
        try:
            await asyncio.wait_for((sender or send)(recipient,
                f"⚠️ VPN4US Admin\nЗаказ №{order_id}\n{LABELS[category]}: требуется внимание.\nОткройте раздел «Оплаты»."),30)
        except Exception as exc:
            LOG.warning("Admin alert delivery not confirmed: %s",type(exc).__name__)
            state = "unconfirmed"
        else:
            state = "sent"
        await asyncio.to_thread(finish,path,order_id,category,state)


async def alert_worker(path):
    while True:
        try:
            await drain_alerts(path)
        except Exception as exc:
            LOG.error("Admin alert worker error: %s",type(exc).__name__)
        await asyncio.sleep(30)
