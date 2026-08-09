"""Модуль управления пользователями"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3
from datetime import datetime

from .auth import check_auth, log_admin_action

users_router = APIRouter(tags=["users"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@users_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''
        SELECT u.id, u.telegram_id, u.username, u.first_name,
               CASE WHEN s.id IS NOT NULL AND datetime(s.end_date) > datetime('now') THEN 1 ELSE 0 END as has_active,
               s.end_date, u.is_blocked
        FROM users u
        LEFT JOIN subscriptions s ON u.telegram_id = s.telegram_id AND s.is_active = 1
        GROUP BY u.telegram_id
        ORDER BY u.created_at DESC LIMIT 100
    ''')
    users = c.fetchall()

    c.execute('SELECT COUNT(*) FROM users')
    total_users = c.fetchone()[0]

    c.execute('''SELECT COUNT(DISTINCT u.telegram_id) FROM users u
                 JOIN subscriptions s ON u.telegram_id = s.telegram_id
                 WHERE s.is_active = 1 AND datetime(s.end_date) > datetime('now')''')
    active_users = c.fetchone()[0]

    c.execute('''SELECT COUNT(*) FROM users
                 WHERE telegram_id NOT IN (SELECT DISTINCT telegram_id FROM subscriptions WHERE is_active = 1)''')
    inactive_users = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM users WHERE is_blocked = 1')
    blocked_users = c.fetchone()[0]
    conn.close()

    return templates.TemplateResponse("users.html", {
        "request": request,
        "users": users,
        "total_users": total_users,
        "active_users": active_users,
        "inactive_users": inactive_users,
        "blocked_users": blocked_users
    })

@users_router.post("/users/toggle-block/{telegram_id}")
async def toggle_block_user(request: Request, telegram_id: int):
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('UPDATE users SET is_blocked = NOT is_blocked WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    conn.close()

    log_admin_action(812021055, 'toggle_block', f'Изменен статус блокировки пользователя {telegram_id}')
    return RedirectResponse("/users", status_code=302)

@users_router.get("/users/{telegram_id}")
async def user_detail(request: Request, telegram_id: int):
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, telegram_id, username, first_name, created_at, is_blocked, balance FROM users WHERE telegram_id = ?', (telegram_id,))
    user = c.fetchone()

    if not user:
        conn.close()
        return RedirectResponse("/users")

    c.execute('''
        SELECT s.id, t.name, s.xui_email, s.end_date, s.is_active,
               s.xui_client_uid, t.price_rub, t.duration_days
        FROM subscriptions s JOIN tariffs t ON s.tariff_id = t.id
        WHERE s.telegram_id = ? ORDER BY s.end_date DESC
    ''', (telegram_id,))
    subscriptions = c.fetchall()

    c.execute('''
        SELECT id, amount_rub, status, created_at
        FROM payments WHERE telegram_id = ?
        ORDER BY created_at DESC LIMIT 20
    ''', (telegram_id,))
    payments = c.fetchall()

    c.execute('SELECT id, name, price_rub, duration_days FROM tariffs WHERE is_active = 1')
    tariffs = c.fetchall()
    conn.close()

    return templates.TemplateResponse("user_detail.html", {
        "request": request,
        "user": user,
        "subscriptions": subscriptions,
        "payments": payments,
        "tariffs": tariffs,
        "now": datetime.now().isoformat()
    })

# ============ ДОБАВЛЕНИЕ КЛЮЧА ============

@users_router.get("/users/{telegram_id}/add-key")
async def add_key_page(request: Request, telegram_id: int):
    if not check_auth(request):
        return RedirectResponse("/")
    
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days FROM tariffs WHERE is_active = 1')
    tariffs = c.fetchall()
    conn.close()
    
    return templates.TemplateResponse("add_key.html", {
        "request": request,
        "telegram_id": telegram_id,
        "tariffs": tariffs
    })

@users_router.post("/users/{telegram_id}/add-key")
async def add_key_submit(request: Request, telegram_id: int, 
                         tariff_id: int = Form(...),
                         days: int = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/")
    
    from services import vpn_service
    
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT name, traffic_gb, ip_limit FROM tariffs WHERE id = ?', (tariff_id,))
    tariff = c.fetchone()
    
    if not tariff:
        conn.close()
        return RedirectResponse(f"/users/{telegram_id}?error=Тариф не найден", status_code=302)
    
    tariff_name, traffic_gb, ip_limit = tariff
    
    result = await vpn_service.create_subscription(
        telegram_id=telegram_id,
        tariff_id=tariff_id,
        days=days,
        traffic_gb=traffic_gb or 0,
        ip_limit=ip_limit or 3
    )
    
    conn.close()
    
    if result['success']:
        log_admin_action(812021055, 'add_key', f'Добавлен ключ для {telegram_id}, тариф: {tariff_name}')
        return RedirectResponse(f"/users/{telegram_id}?success=Ключ добавлен", status_code=302)
    else:
        return RedirectResponse(f"/users/{telegram_id}?error={result.get('error', 'Ошибка создания')}", status_code=302)

# ============ УДАЛЕНИЕ КЛЮЧА ============

@users_router.post("/users/{telegram_id}/delete-key/{sub_id}")
async def user_delete_key(request: Request, telegram_id: int, sub_id: int):
    if not check_auth(request):
        return RedirectResponse("/")
    
    from xui_client import XUIClient
    from server_manager import server_manager
    
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT xui_email FROM subscriptions WHERE id = ? AND telegram_id = ?', (sub_id, telegram_id))
    sub = c.fetchone()
    
    if not sub:
        conn.close()
        return RedirectResponse(f"/users/{telegram_id}?error=Ключ не найден", status_code=302)
    
    xui_email = sub[0]
    
    try:
        servers = server_manager.get_active_servers()
        for server in servers:
            xui = XUIClient(url=server['url'], api_token=server['api_token'])
            xui.delete_client(xui_email)
            break
    except Exception as e:
        print(f"Ошибка удаления из 3X-UI: {e}")
    
    c.execute('DELETE FROM subscriptions WHERE id = ? AND telegram_id = ?', (sub_id, telegram_id))
    conn.commit()
    conn.close()
    
    log_admin_action(812021055, 'delete_key', f'Удалён ключ {sub_id} у пользователя {telegram_id}')
    return RedirectResponse(f"/users/{telegram_id}?success=Ключ удалён", status_code=302)
