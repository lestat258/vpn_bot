from fastapi.responses import RedirectResponse
"""Модуль авторизации и аутентификации"""
import uuid
import logging
from fastapi import APIRouter, Request, Form, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi import APIRouter

from database import get_setting, set_setting
from two_factor_auth import two_factor_auth

# Создаем роутер
auth_router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")
logger = logging.getLogger(__name__)

# Хранилище сессий
sessions = {}

def check_auth(request: Request):
    """Проверка аутентификации"""
    public_paths = ['/', '/login', '/2fa/status', '/2fa/qr', '/favicon.ico', '/health']
    if request.url.path in public_paths:
        return True
    session_id = request.cookies.get("session_id")
    return sessions.get(session_id) is True

def log_admin_action(admin_id, action, details=None, ip=None):
    """Логирование действий администратора"""
    try:
        import sqlite3
        conn = sqlite3.connect('/opt/vpn-bot/data.db')
        c = conn.cursor()
        c.execute('INSERT INTO admin_logs (admin_id, action, details, ip) VALUES (?, ?, ?, ?)',
                  (admin_id, action, details, ip))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log admin action: {e}")

@auth_router.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа"""
    return templates.TemplateResponse("login.html", {"request": request})

@auth_router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Обработка входа"""
    admin_user = get_setting('admin_username') or 'admin'
    admin_pass = get_setting('admin_password') or 'admin'

    if username == admin_user and password == admin_pass:
        if two_factor_auth.is_enabled:
            form_data = await request.form()
            twofa_code = form_data.get('twofa_code', '')
            if not twofa_code or not two_factor_auth.verify_code(twofa_code):
                return templates.TemplateResponse("login.html", {
                    "request": request,
                    "error": "Неверный код 2FA"
                }, status_code=401)

        session_id = str(uuid.uuid4())
        sessions[session_id] = True

        client_ip = request.headers.get("X-Forwarded-For", request.client.host)
        log_admin_action(812021055, 'login', f'Успешный вход с IP: {client_ip}')

        if get_setting('first_login') == 'true':
            response = RedirectResponse("/change-password", status_code=302)
        else:
            response = RedirectResponse("/dashboard", status_code=302)

        response.set_cookie("session_id", session_id, httponly=True, secure=True, samesite="lax")
        return response

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "Неверный логин или пароль"
    }, status_code=401)

@auth_router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    """Страница смены пароля"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("change_password.html", {"request": request})

@auth_router.post("/change-password")
async def change_password(request: Request, new_password: str = Form(...)):
    """Смена пароля"""
    if not check_auth(request):
        return RedirectResponse("/")
    if len(new_password) < 4:
        return templates.TemplateResponse("change_password.html", {
            "request": request,
            "error": "Пароль должен содержать минимум 4 символа"
        })
    set_setting('admin_password', new_password)
    set_setting('first_login', 'false')
    log_admin_action(812021055, 'change_password', 'Смена пароля')
    return RedirectResponse("/dashboard", status_code=302)

@auth_router.get("/logout")
async def logout(request: Request):
    """Выход из системы"""
    session_id = request.cookies.get("session_id")
    if session_id:
        sessions.pop(session_id, None)
    log_admin_action(812021055, 'logout', 'Выход из админки')
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response

@auth_router.get("/health")
async def health_check():
    """Health check для мониторинга"""
    return {"status": "ok", "timestamp": "2026-07-24T17:45:00.000000"}
