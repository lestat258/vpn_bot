"""Модуль безопасности (2FA, смена пароля, логи)"""
import sqlite3
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import check_auth, log_admin_action
from database import get_setting, set_setting, get_all_settings
from two_factor_auth import two_factor_auth

security_router = APIRouter(tags=["security"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

# ============ 2FA ============

@security_router.get("/2fa", response_class=HTMLResponse)
async def two_factor_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    status = two_factor_auth.get_status()
    return templates.TemplateResponse("two_factor.html", {
        "request": request,
        "status": status
    })

@security_router.post("/2fa/enable")
async def enable_2fa(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    data = await request.json()
    code = data.get('code')
    if two_factor_auth.verify_code(code):
        two_factor_auth.enable_2fa()
        log_admin_action(812021055, '2fa_enable', 'Включена 2FA')
        return {"success": True, "message": "2FA успешно включена"}
    else:
        return {"success": False, "message": "Неверный код"}

@security_router.post("/2fa/disable")
async def disable_2fa(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    data = await request.json()
    code = data.get('code')
    if two_factor_auth.verify_code(code):
        two_factor_auth.disable_2fa()
        log_admin_action(812021055, '2fa_disable', 'Выключена 2FA')
        return {"success": True, "message": "2FA успешно выключена"}
    else:
        return {"success": False, "message": "Неверный код"}

@security_router.get("/2fa/qr")
async def get_2fa_qr(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    username = get_setting('admin_username') or 'admin'
    qr_data = two_factor_auth.get_qr_code(username)
    return qr_data

@security_router.get("/2fa/status")
async def get_2fa_status(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    return two_factor_auth.get_status()

# ============ СМЕНА ЛОГИНА И ПАРОЛЯ ============

@security_router.get("/change-credentials", response_class=HTMLResponse)
async def change_credentials_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("change_credentials.html", {
        "request": request,
        "settings": get_all_settings()
    })

@security_router.post("/change-credentials")
async def change_credentials(request: Request,
                             current_password: str = Form(...),
                             new_username: str = Form(...),
                             new_password: str = Form(...),
                             confirm_password: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/")
    admin_pass = get_setting('admin_password') or 'admin'
    if current_password != admin_pass:
        return templates.TemplateResponse("change_credentials.html", {
            "request": request,
            "error": "❌ Неверный текущий пароль"
        })
    if not new_password or len(new_password) < 4:
        return templates.TemplateResponse("change_credentials.html", {
            "request": request,
            "error": "❌ Пароль должен содержать минимум 4 символа"
        })
    if new_password != confirm_password:
        return templates.TemplateResponse("change_credentials.html", {
            "request": request,
            "error": "❌ Пароли не совпадают"
        })
    if new_username:
        set_setting('admin_username', new_username)
    set_setting('admin_password', new_password)
    log_admin_action(812021055, 'change_credentials', f'Смена логина/пароля')
    return templates.TemplateResponse("change_credentials.html", {
        "request": request,
        "success": "✅ Логин и пароль успешно обновлены!"
    })

# ============ ЛОГИ АДМИНОВ ============

@security_router.get("/admin-logs", response_class=HTMLResponse)
async def admin_logs_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''SELECT id, admin_id, action, details, ip, created_at
                 FROM admin_logs ORDER BY created_at DESC LIMIT 100''')
    logs = c.fetchall()
    conn.close()
    
    return templates.TemplateResponse("admin_logs.html", {
        "request": request,
        "logs": logs
    })
