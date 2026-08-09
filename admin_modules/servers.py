from fastapi.responses import RedirectResponse
"""Модуль управления серверами"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3
import hashlib
from datetime import datetime

from .auth import check_auth, log_admin_action
from encryption import encrypt
from server_manager import server_manager

servers_router = APIRouter(tags=["servers"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

def generate_short_email(telegram_id, username=None):
    """Генерация email для клиента"""
    if username:
        name_part = username[:15]
        name_part = ''.join(c for c in name_part if c.isalnum() or c == '_')
    else:
        name_part = str(telegram_id)
    if not name_part:
        name_part = str(telegram_id)
    hash_input = f"{telegram_id}_{datetime.now().timestamp()}"
    hash_short = hashlib.md5(hash_input.encode()).hexdigest()[:5]
    return f"user_{name_part}_{hash_short}"

@servers_router.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request):
    """Страница списка серверов"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, name, url, api_token, is_active, status, online_count, sub_url FROM servers')
    servers = c.fetchall()
    conn.close()

    return templates.TemplateResponse("servers.html", {"request": request, "servers": servers})

@servers_router.get("/servers/add", response_class=HTMLResponse)
async def add_server_page(request: Request):
    """Страница добавления сервера"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("add_server.html", {"request": request})

@servers_router.post("/servers/add")
async def add_server(request: Request,
                     name: str = Form(...),
                     url: str = Form(...),
                     api_token: str = Form(...),
                     sub_url: str = Form("")):
    """Добавление сервера"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    encrypted_token = encrypt(api_token)
    c.execute('INSERT INTO servers (name, url, api_token, is_active, status, sub_url) VALUES (?,?,?,?,?,?)',
              (name, url, encrypted_token, 1, 'pending', sub_url))
    conn.commit()
    conn.close()

    log_admin_action(812021055, 'add_server', f'Добавлен сервер: {name}')
    return RedirectResponse("/servers?added=1", status_code=302)

@servers_router.post("/servers/check/{server_id}")
async def check_server(request: Request, server_id: int):
    """Проверка сервера"""
    if not check_auth(request):
        return RedirectResponse("/")
    result = server_manager.check_server_health(server_id)
    log_admin_action(812021055, 'check_server', f'Проверка сервера ID: {server_id}, статус: {result["status"]}')
    return RedirectResponse(f"/servers?checked={server_id}&status={result['status']}", status_code=302)

@servers_router.post("/servers/delete/{server_id}")
async def delete_server(request: Request, server_id: int):
    """Удаление сервера"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT name FROM servers WHERE id = ?', (server_id,))
    server = c.fetchone()
    if server:
        c.execute('DELETE FROM servers WHERE id = ?', (server_id,))
        conn.commit()
        log_admin_action(812021055, 'delete_server', f'Удалён сервер: {server[0]} (ID: {server_id})')
    conn.close()
    return RedirectResponse("/servers?deleted=1", status_code=302)
