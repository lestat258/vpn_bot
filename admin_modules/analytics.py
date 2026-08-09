from fastapi.responses import RedirectResponse
"""Модуль аналитики"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth

analytics_router = APIRouter(tags=["analytics"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@analytics_router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    """Страница аналитики"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()

    c.execute('''SELECT DATE(created_at), COUNT(*)
                 FROM payments WHERE status = 'paid'
                 AND created_at > datetime('now', '-7 days')
                 GROUP BY DATE(created_at)''')
    payments_daily = c.fetchall()

    c.execute('''SELECT t.name, COUNT(*) as count
                 FROM subscriptions s JOIN tariffs t ON s.tariff_id = t.id
                 GROUP BY t.id ORDER BY count DESC''')
    top_tariffs = c.fetchall()

    c.execute('SELECT COUNT(*) FROM users')
    total_users = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM users WHERE is_blocked = 0')
    active_users = c.fetchone()[0]

    c.execute('SELECT COUNT(*), SUM(amount_rub) FROM payments WHERE status = "paid"')
    payments_stats = c.fetchone()
    conn.close()

    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "payments_daily": payments_daily,
        "top_tariffs": top_tariffs,
        "total_users": total_users,
        "active_users": active_users,
        "total_payments": payments_stats[0] or 0,
        "total_revenue": payments_stats[1] or 0
    })
