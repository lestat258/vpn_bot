from fastapi.responses import RedirectResponse
"""Модуль защиты от спама"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth, log_admin_action
from rate_limiter import rate_limiter

rate_limits_router = APIRouter(tags=["rate_limits"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@rate_limits_router.get("/rate-limits", response_class=HTMLResponse)
async def rate_limits_page(request: Request):
    """Страница управления защитой от спама"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''
        SELECT user_id, action_type, request_count, is_blocked, blocked_until
        FROM rate_limits ORDER BY last_request DESC LIMIT 100
    ''')
    limits = c.fetchall()
    c.execute('SELECT COUNT(DISTINCT user_id) FROM rate_limits WHERE is_blocked = 1')
    blocked_count = c.fetchone()[0]
    conn.close()

    return templates.TemplateResponse("rate_limits.html", {
        "request": request,
        "limits": limits,
        "blocked_count": blocked_count
    })

@rate_limits_router.post("/rate-limits/unblock/{user_id}")
async def rate_limit_unblock(request: Request, user_id: int):
    """Снятие блокировки с пользователя"""
    if not check_auth(request):
        return RedirectResponse("/")
    rate_limiter.reset_limits(user_id)
    log_admin_action(812021055, 'rate_unblock', f'Снята блокировка с пользователя {user_id}')
    return RedirectResponse("/rate-limits?unblocked=1", status_code=302)

@rate_limits_router.post("/rate-limits/reset/{user_id}")
async def rate_limit_reset(request: Request, user_id: int):
    """Сброс лимитов пользователя"""
    if not check_auth(request):
        return RedirectResponse("/")
    rate_limiter.reset_limits(user_id)
    log_admin_action(812021055, 'rate_reset', f'Сброшены лимиты пользователя {user_id}')
    return RedirectResponse("/rate-limits?reset=1", status_code=302)
