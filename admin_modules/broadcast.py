from fastapi.responses import RedirectResponse
"""Модуль рассылок"""
import asyncio
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth, log_admin_action

broadcast_router = APIRouter(tags=["broadcast"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

async def process_broadcast(broadcast_id):
    """Обработка рассылки в фоне"""
    from aiogram import Bot
    from database import get_setting
    from encryption import decrypt

    bot_token = get_setting('bot_token')
    if not bot_token:
        return

    try:
        bot_token = decrypt(bot_token)
    except:
        pass

    bot = Bot(token=bot_token)
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()

    c.execute('SELECT message, filter FROM broadcasts WHERE id = ?', (broadcast_id,))
    broadcast = c.fetchone()

    if not broadcast:
        conn.close()
        return

    message, filter_type = broadcast

    if filter_type == 'all':
        c.execute('SELECT telegram_id FROM users WHERE is_blocked = 0')
    elif filter_type == 'active':
        c.execute('''SELECT DISTINCT u.telegram_id FROM users u
                     JOIN subscriptions s ON u.telegram_id = s.telegram_id
                     WHERE s.is_active = 1 AND datetime(s.end_date) > datetime('now')
                     AND u.is_blocked = 0''')
    elif filter_type == 'expired':
        c.execute('''SELECT DISTINCT u.telegram_id FROM users u
                     JOIN subscriptions s ON u.telegram_id = s.telegram_id
                     WHERE s.is_active = 1 AND datetime(s.end_date) <= datetime('now')
                     AND u.is_blocked = 0''')
    else:
        c.execute('SELECT telegram_id FROM users WHERE is_blocked = 0')

    users = c.fetchall()
    conn.close()

    sent_count = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user[0], text=message, parse_mode="HTML")
            sent_count += 1
            await asyncio.sleep(0.1)
        except:
            pass

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''UPDATE broadcasts
                 SET sent_at = CURRENT_TIMESTAMP, status = 'sent', total_sent = ?
                 WHERE id = ?''', (sent_count, broadcast_id))
    conn.commit()
    conn.close()

    logging.info(f"📨 Рассылка #{broadcast_id} отправлена {sent_count} пользователям")

@broadcast_router.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    """Страница рассылок"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''SELECT id, message, filter, scheduled_at, sent_at, status, total_sent, created_at
                 FROM broadcasts ORDER BY created_at DESC LIMIT 50''')
    broadcasts = c.fetchall()
    conn.close()

    return templates.TemplateResponse("broadcast.html", {
        "request": request,
        "broadcasts": broadcasts
    })

@broadcast_router.post("/broadcast/send")
async def send_broadcast(request: Request):
    """Создание рассылки"""
    if not check_auth(request):
        return RedirectResponse("/")

    form = await request.form()
    message = form.get('message')
    filter_type = form.get('filter', 'all')

    if not message:
        return RedirectResponse("/broadcast?error=Введите текст сообщения", status_code=302)

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''INSERT INTO broadcasts (message, filter, status)
                 VALUES (?, ?, ?)''',
              (message, filter_type, 'pending'))
    broadcast_id = c.lastrowid
    conn.commit()
    conn.close()

    asyncio.create_task(process_broadcast(broadcast_id))

    log_admin_action(812021055, 'send_broadcast', f'Создана рассылка #{broadcast_id}, фильтр: {filter_type}')
    return RedirectResponse("/broadcast?sent=1", status_code=302)
