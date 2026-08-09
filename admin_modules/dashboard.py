from fastapi.responses import RedirectResponse
"""Модуль дашборда"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth

dashboard_router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@dashboard_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Главная страница дашборда"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE is_active = 1')
    active_subs = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "paid"')
    payments_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM servers WHERE is_active = 1')
    servers_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "pending"')
    pending_count = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount_rub), 0) FROM payments WHERE status = "paid" AND DATE(created_at) = DATE("now")')
    today_revenue = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount_rub), 0) FROM payments WHERE status = "paid" AND created_at > datetime("now", "-7 days")')
    week_revenue = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount_rub), 0) FROM payments WHERE status = "paid" AND created_at > datetime("now", "-30 days")')
    month_revenue = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM users WHERE DATE(created_at) = DATE("now")')
    new_users_today = c.fetchone()[0]
    conn.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "users_count": users_count,
        "active_subs": active_subs,
        "payments_count": payments_count,
        "servers_count": servers_count,
        "pending_count": pending_count,
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "new_users_today": new_users_today
    })
