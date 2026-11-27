"""Read-only local payment journal for the authenticated admin page."""
import re
from datetime import datetime, timezone, timedelta
import sqlite3
from contextlib import closing
from pathlib import Path
from refund_fulfillment import CUTOFF, LAST_HISTORICAL_ID
from balance_ledger import deposit_scope_sql

PAGE_SIZE = 50
FILTERS = {
    "all": "Все", "new": "Новые", "paid": "Оплаченные",
    "pending": "Ожидают", "canceled": "Отменённые",
    "refund": "Возвраты", "review": "Требуют сверки",
    "historical": "Исторические",
}
NEW = "(p.id > ? AND datetime(p.created_at) >= datetime(?) AND p.amount_rub > 0)"
REVIEW = """(j.vpn_state IN ('needs_review','processing')
 OR j.tax_state IN ('needs_review','processing')
 OR j.refund_state IN ('partial_review','full_review','full_processing'))"""
REFUND = "(j.refund_state IS NOT NULL AND j.refund_state <> 'none')"
WHERE = {
    "all": "1", "new": "is_new=1", "historical": "is_new=0",
    "paid": "status='paid'", "pending": "status='pending'",
    "canceled": "status='canceled'", "refund": "has_refund=1",
    "review": "needs_review=1",
}
LABELS = {
    "payment": {"returned":"Возвращён", "paid":"Оплачен", "pending":"Ожидает", "canceled":"Отменён"},
    "provider": {"verified":"Подтверждён", "pending":"Ожидает", "canceled":"Отменён"},
    "vpn": {"done":"Выполнено","not_started":"Не начато","processing":"В обработке","needs_review":"Требует сверки"},
    "tax": {"done":"Выдан","canceled":"Аннулирован","queued":"В очереди","processing":"В обработке",
            "needs_review":"Требует сверки","not_started":"Не начато","skipped":"Пропущен"},
    "refund": {"none":"Нет в журнале","partial_review":"Частичный · сверка","full_review":"Полный · сверка",
               "full_queued":"Полный · очередь","full_processing":"Полный · обработка","full_done":"Полный · завершён"},
}

def status_display(group, state):
    if state is None:
        return {"label":"Нет записи","style":"secondary"}
    label = LABELS[group].get(state, "Неизвестный статус")
    style = ("success" if state in ("paid","verified","done","full_done") else
             "danger" if state in ("needs_review","full_review","partial_review") else
             "warning" if state in ("pending","queued","processing","full_queued","full_processing") else
             "secondary")
    return {"label":label,"style":style}


def safe_receipt_link(url, uid):
    if not isinstance(url,str) or not isinstance(uid,str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
        return None
    if re.fullmatch(r"https://lknpd\.nalog\.ru/api/v1/receipt/[0-9]{12}/" + re.escape(uid) + r"/print", url):
        return url
    return None


def display_date(value, is_new):
    if not value:
        return "—"
    if not is_new:
        return str(value) + " (из базы)"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M:%S МСК")
    except (ValueError, TypeError):
        return str(value) + " (из базы)"


def load_journal(path, selected="all", page=1):
    selected = selected if selected in FILTERS else "all"
    try:
        page = max(1, int(page))
    except (TypeError,ValueError):
        page = 1
    cte = """WITH journal AS (
        SELECT p.id,p.amount_rub,p.status,p.created_at,
          u.first_name AS customer,CASE WHEN p.tariff_id=0 THEN 'Пополнение баланса' ELSE t.name END AS tariff,
          j.payment_state,j.vpn_state,j.tax_state,j.refund_state,
          j.subscription_id,j.receipt_uuid,j.receipt_url,
          CASE WHEN """ + NEW + """ THEN 1 ELSE 0 END AS is_new,
          CASE WHEN """ + REVIEW + """ THEN 1 ELSE 0 END AS needs_review,
          CASE WHEN """ + REFUND + """ THEN 1 ELSE 0 END AS has_refund
        FROM payments p LEFT JOIN users u ON u.telegram_id=p.telegram_id
          LEFT JOIN tariffs t ON t.id=p.tariff_id
          LEFT JOIN payment_processing j ON j.payment_id=p.payment_id
    ) """
    args = (LAST_HISTORICAL_ID,CUTOFF)
    predicate = WHERE[selected]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        cte=cte.replace(NEW,"("+NEW+" OR "+deposit_scope_sql(db)+")")
        stats = dict(db.execute(cte+"""
            SELECT COUNT(*) AS total,
              COALESCE(SUM(CASE WHEN status='paid' AND amount_rub>0 THEN amount_rub ELSE 0 END),0) AS gross,
              COALESCE(SUM(CASE WHEN refund_state='full_done' THEN amount_rub ELSE 0 END),0) AS completed_refunds,
              COUNT(CASE WHEN status='pending' THEN 1 END) AS pending,
              COUNT(CASE WHEN status='canceled' THEN 1 END) AS canceled,
              COUNT(CASE WHEN is_new=1 AND needs_review=1 THEN 1 END) AS review
            FROM journal
        """,args).fetchone())
        count=db.execute(cte+"SELECT COUNT(*) FROM journal WHERE "+predicate,args).fetchone()[0]
        pages=max(1,(count+PAGE_SIZE-1)//PAGE_SIZE)
        page=min(page,pages)
        rows=db.execute(cte+"SELECT * FROM journal WHERE "+predicate+
                        " ORDER BY id DESC LIMIT ? OFFSET ?",args+(PAGE_SIZE,(page-1)*PAGE_SIZE)).fetchall()
        items=[]
        for raw in rows:
            row=dict(raw)
            row["receipt_link"]=safe_receipt_link(row.pop("receipt_url"),row.pop("receipt_uuid"))
            for group,key in (("payment","status"),("provider","payment_state"),("vpn","vpn_state"),
                              ("tax","tax_state"),("refund","refund_state")):
                row[group+"_display"]=status_display(group,row[key])
            if row["refund_state"] == "full_done":
                row["payment_display"] = status_display("payment", "returned")
            row["display_date"] = display_date(row["created_at"], row["is_new"])
            items.append(row)
    return {"payments":items,"stats":stats,"filters":FILTERS,"selected":selected,
            "page":page,"pages":pages,"count":count}
