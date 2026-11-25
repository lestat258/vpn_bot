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
    c.execute("SELECT id, name, url, CASE WHEN LENGTH(api_token)>0 THEN 'configured' ELSE '' END AS api_token, is_active, status, online_count, sub_url FROM servers")
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


# vpn4us stage3: editor routes and protected token handling
@servers_router.get('/servers/edit/{server_id}', response_class=HTMLResponse)
async def edit_server_page(request: Request, server_id: int):
    """Authenticated server editor; never echo stored API tokens to HTML."""
    from pathlib import Path
    import secrets
    from fastapi import HTTPException
    if not check_auth(request):
        return RedirectResponse('/login', status_code=303)

    database_file = Path(__file__).resolve().parents[1] / 'data.db'
    conn = sqlite3.connect(str(database_file))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT id, name, url, sub_url FROM servers WHERE id=?',
            (server_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Server not found')
        server = dict(row)
    finally:
        conn.close()

    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        'edit_server_safe.html',
        {'request': request, 'server': server, 'csrf': csrf},
    )
    response.set_cookie(
        'vpn_bot_edit_csrf', csrf, httponly=True,
        secure=request.url.scheme == 'https',
        samesite='strict', max_age=600, path='/',
    )
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@servers_router.post('/servers/edit/{server_id}')
async def edit_server(
    request: Request, server_id: int, name: str=Form(...),
    url: str=Form(...), api_token: str=Form(''), sub_url: str=Form(''),
    csrf: str=Form(...),
):
    """Update a server, preserving the previous token if field is empty."""
    import secrets
    import logging
    from pathlib import Path
    from urllib.parse import urlsplit
    from fastapi import HTTPException
    if not check_auth(request):
        return RedirectResponse('/login', status_code=303)

    cookie_csrf = request.cookies.get('vpn_bot_edit_csrf', '')
    if not cookie_csrf or not secrets.compare_digest(cookie_csrf, csrf):
        raise HTTPException(status_code=403, detail='CSRF validation failed')

    name = name.strip()
    url = url.strip().rstrip('/')
    sub_url = sub_url.strip()
    new_token = api_token.strip()
    if new_token.lower().startswith('bearer '):
        new_token = new_token[7:].strip()
    if not name or len(name) > 120 or len(url) > 2048 or len(sub_url) > 2048:
        raise HTTPException(status_code=400, detail='Invalid form fields')
    if len(new_token) > 4096:
        raise HTTPException(status_code=400, detail='Invalid token length')

    def check_https(value):
        try:
            parsed = urlsplit(value)
            # The server URL may contain a nonstandard panel base path.
            return (
                parsed.scheme == 'https' and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        except ValueError:
            return False

    if not check_https(url) or (sub_url and not check_https(sub_url)):
        raise HTTPException(status_code=400, detail='Only HTTPS URLs are supported')

    database_file = Path(__file__).resolve().parents[1] / 'data.db'
    conn = sqlite3.connect(str(database_file), timeout=15)
    try:
        with conn:
            exists = conn.execute(
                'SELECT 1 FROM servers WHERE id=?', (server_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail='Server not found')
            if new_token:
                encrypted_token = encrypt(new_token)
                conn.execute(
                    "UPDATE servers SET name=?, url=?, sub_url=?, api_token=?, status='unknown' WHERE id=?",
                    (name, url, sub_url, encrypted_token, server_id),
                )
            else:
                conn.execute(
                    "UPDATE servers SET name=?, url=?, sub_url=?, status='unknown' WHERE id=?",
                    (name, url, sub_url, server_id),
                )
    finally:
        conn.close()

    # No credentials or URLs in admin audit log.
    try:
        from database import get_setting
        admin_id = int(get_setting('admin_id') or 0)
        if admin_id > 0:
            log_admin_action(admin_id, 'edit_server', f'server_id={server_id}')
    except Exception:
        logging.getLogger(__name__).warning('Server updated; audit logging failed')
    response = RedirectResponse('/servers?edited=1', status_code=303)
    response.delete_cookie('vpn_bot_edit_csrf', path='/')
    return response


# vpn4us stage4: per-server inbound policy routes
async def _stage4_load_inbounds(server):
    """Fetch via server's configured auth, bounded by XUIClient request timeout."""
    import asyncio
    from services.xui_client import XUIClient
    def load():
        xui = XUIClient(url=server['url'], api_token=server['api_token'], verify_ssl=True)
        try:
            return xui.get_inbounds()
        finally:
            xui.session.close()
    try:
        records = await asyncio.to_thread(load)
        if not isinstance(records, list) or not records:
            return []
        result = []
        for i in records:
            if not isinstance(i, dict):
                continue
            try:
                ident = int(i['id'])
            except (KeyError, ValueError, TypeError):
                continue
            if ident <= 0:
                continue
            result.append({
                'id': ident, 'tag': str(i.get('tag') or ''),
                'protocol': str(i.get('protocol') or ''),
                'enabled': i.get('enable') is True,
            })
        return result
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Unable to fetch inbound list for server id=%s', server['id'])
        return []


@servers_router.get('/servers/inbounds/{server_id}', response_class=HTMLResponse)
async def stage4_inbounds_page(request: Request, server_id: int):
    import secrets
    from pathlib import Path
    from fastapi import HTTPException
    from inbound_policy import get_policy
    if not check_auth(request):
        return RedirectResponse('/login', status_code=303)
    db_path = Path(__file__).resolve().parents[1] / 'data.db'
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute('SELECT id,name,url,api_token FROM servers WHERE id=?',
                          (server_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Server not found')
        server = dict(row)
    finally:
        con.close()
    policy = get_policy(db_path, server_id) or {
        'mode': 'group', 'group_number': 1,
        'inbound_ids': [], 'minimum_count': 1, 'allow_all': False,
    }
    inbounds = await _stage4_load_inbounds(server)
    csrf = secrets.token_urlsafe(32)
    safe_server = {'id': server['id'], 'name': server['name']}
    resp = templates.TemplateResponse('server_inbounds.html', {
        'request': request, 'server': safe_server,
        'policy': policy, 'inbounds': inbounds,
        'csrf': csrf, 'error': None,
        'success': request.query_params.get('saved') == '1',
    })
    resp.set_cookie('vpn_bot_inbound_csrf', csrf, httponly=True,
                    secure=request.url.scheme == 'https',
                    samesite='strict', max_age=600, path='/')
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'same-origin'
    return resp


@servers_router.post('/servers/inbounds/{server_id}')
async def stage4_save_inbounds(
    request: Request, server_id: int, csrf: str=Form(...),
    mode: str=Form(...), group_number: str=Form('1'),
    minimum_count: int=Form(1), selected_ids: list[str]=Form([]),
    allow_all: str=Form(''),
):
    import secrets
    from pathlib import Path
    from fastapi import HTTPException
    from inbound_policy import InboundPolicyError, select_inbounds, save_policy
    if not check_auth(request):
        return RedirectResponse('/login', status_code=303)
    cookie = request.cookies.get('vpn_bot_inbound_csrf', '')
    if not cookie or not secrets.compare_digest(cookie, csrf):
        raise HTTPException(status_code=403, detail='Invalid CSRF token')
    db_path = Path(__file__).resolve().parents[1] / 'data.db'
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute('SELECT id,name,url,api_token FROM servers WHERE id=?',
                          (server_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Server not found')
        server = dict(row)
    finally:
        con.close()
    policy = {
        'mode': mode, 'group_number': group_number,
        'inbound_ids': selected_ids, 'minimum_count': minimum_count,
        'allow_all': allow_all == 'yes',
    }
    inbounds = await _stage4_load_inbounds(server)
    if not inbounds:
        raise HTTPException(status_code=503, detail='3x-ui inbound list unavailable; policy not saved')
    try:
        selected = select_inbounds(policy, [
            {'id': i['id'], 'tag': i['tag'], 'enable': i['enabled']}
            for i in inbounds
        ])
        saved = save_policy(db_path, server_id, policy)
    except (InboundPolicyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        from database import get_setting
        uid = int(get_setting('admin_id') or 0)
        if uid > 0:
            log_admin_action(uid, 'update_inbound_policy',
                             f'server_id={server_id}, mode={saved["mode"]}, count={len(selected)}')
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Policy saved; audit log failed')
    response = RedirectResponse(f'/servers/inbounds/{server_id}?saved=1', status_code=303)
    response.delete_cookie('vpn_bot_inbound_csrf', path='/')
    return response
