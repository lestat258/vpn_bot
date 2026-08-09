from fastapi.responses import RedirectResponse
"""Модуль управления платежами"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth

payments_router = APIRouter(tags=["payments"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@payments_router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    """Страница списка платежей"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''
        SELECT p.id, u.first_name, t.name, p.amount_rub, p.status, p.created_at
        FROM payments p
        JOIN users u ON u.telegram_id = p.telegram_id
        LEFT JOIN tariffs t ON t.id = p.tariff_id
        WHERE p.status = 'paid'
        ORDER BY p.created_at DESC LIMIT 100
    ''')
    payments = c.fetchall()

    c.execute('SELECT COUNT(*) FROM payments WHERE status = "paid"')
    total_payments = c.fetchone()[0]

    c.execute('SELECT SUM(amount_rub) FROM payments WHERE status = "paid"')
    total_amount = c.fetchone()[0] or 0

    c.execute('SELECT COUNT(*) FROM payments WHERE status = "paid"')
    paid_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM payments WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    conn.close()

    return templates.TemplateResponse("payments.html", {
        "request": request,
        "payments": payments,
        "total_payments": total_payments,
        "total_amount": total_amount,
        "paid_count": paid_count,
        "pending_count": pending_count
    })
