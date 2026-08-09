from fastapi.responses import RedirectResponse
"""Модуль настроек"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import check_auth, log_admin_action
from database import get_all_settings, set_setting

settings_router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@settings_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Страница настроек"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_all_settings()})

@settings_router.post("/settings")
async def save_settings(request: Request):
    """Сохранение настроек"""
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    for key in ['bot_token', 'yookassa_shop_id', 'yookassa_secret_key', 'admin_id']:
        if key in form:
            set_setting(key, form[key])
    log_admin_action(812021055, 'settings', 'Изменение настроек')
    return RedirectResponse("/settings?saved=1", status_code=302)

@settings_router.get("/tax-settings", response_class=HTMLResponse)
async def tax_settings_page(request: Request):
    """Страница настроек налогов"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("tax_settings.html", {"request": request, "settings": get_all_settings()})

@settings_router.post("/tax-settings")
async def save_tax_settings(request: Request):
    """Сохранение настроек налогов"""
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    for key in ['tax_enabled', 'tax_inn', 'tax_password', 'tax_description_template']:
        if key in form:
            set_setting(key, form[key])
    return RedirectResponse("/tax-settings?saved=1", status_code=302)

@settings_router.get("/bot-texts", response_class=HTMLResponse)
async def bot_texts_page(request: Request):
    """Страница текстов бота"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("bot_texts.html", {"request": request, "settings": get_all_settings()})

@settings_router.post("/bot-texts")
async def save_bot_texts(request: Request):
    """Сохранение текстов бота"""
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    for key in ['text_start', 'text_about', 'text_support', 'text_payment',
                'text_success', 'text_instruction', 'text_welcome',
                'text_install_vpn', 'text_android_instruction',
                'text_ios_instruction', 'text_windows_instruction', 'text_faq']:
        if key in form:
            set_setting(key, form[key])
    log_admin_action(812021055, 'edit_texts', 'Изменение текстов бота')
    return RedirectResponse("/bot-texts?saved=1", status_code=302)
