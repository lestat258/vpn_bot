import asyncio
import hmac
import os
import secrets
import subprocess

from runtime_config import DATA_DIR, TEMPLATE_DIR, DB_PATH
from tax_settings_policy import save as save_fiscal, status as fiscal_status
from fastapi.responses import RedirectResponse
"""Модуль настроек"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import check_auth, log_admin_action
from database import get_all_settings, get_db, set_setting

settings_router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory=TEMPLATE_DIR)

_activation_key = secrets.token_bytes(32)
_secret_fields = {"bot_token", "yookassa_secret_key"}


def activation_token(request):
    return hmac.digest(_activation_key, request.cookies["session_id"].encode(), "sha256").hex()


def activation_errors(settings):
    missing = []
    if settings.get("first_login") == "true":
        missing.append("Смените временный пароль администратора.")
    for key, label in (
        ("bot_token", "токен Telegram"),
        ("admin_id", "Telegram ID администратора"),
        ("yookassa_shop_id", "Shop ID ЮKassa"),
        ("yookassa_secret_key", "секретный ключ ЮKassa"),
    ):
        value = (settings.get(key) or "").strip()
        if not value or (key == "admin_id" and not (value.isascii() and value.isdecimal() and int(value) > 0)):
            missing.append(f"Укажите {label}.")
    if settings.get("tax_enabled") == "true" and not all(
        (settings.get(key) or "").strip() for key in ("tax_inn", "tax_password")
    ):
        missing.append("Заполните данные ФНС в разделе «Налоги».")
    with get_db() as db:
        servers = db.execute(
            "SELECT url FROM servers WHERE is_active=1 AND trim(api_token) != ''"
        ).fetchall()
        tariff = db.execute(
            "SELECT 1 FROM tariffs WHERE is_active=1 AND duration_days>0 AND price_rub>0"
        ).fetchone()
    if not any(row[0].startswith("https://") for row in servers):
        missing.append("Добавьте активную панель 3x-ui с HTTPS и API-токеном.")
    if not tariff:
        missing.append("Добавьте активный платный тариф.")
    return missing


def service_status():
    try:
        states = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "vpn-bot.service", "vpn-webhook.service"],
            capture_output=True, text=True, timeout=3,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return "Статус служб недоступен."
    if states == ["active", "active"]:
        return "Обе службы запущены."
    if "failed" in states:
        return "Одна из служб завершилась с ошибкой. Подробности в журнале systemd."
    if (DATA_DIR / "services.enabled").exists():
        return "Запуск запрошен. Обновите страницу через несколько секунд."
    return "Службы ожидают запуска."


@settings_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Страница настроек"""
    if not check_auth(request):
        return RedirectResponse("/")
    settings = get_all_settings()
    public_url = os.environ.get("VPN4US_ACTIVATION_URL")
    context = {
        "request": request,
        "settings": {key: value for key, value in settings.items() if key not in _secret_fields},
        "saved_secrets": {key for key in _secret_fields if settings.get(key)},
    }
    if public_url:
        context.update(
            activation_token=activation_token(request),
            activation_errors=activation_errors(settings),
            service_status=await asyncio.to_thread(service_status),
            webhook_url=public_url.rstrip("/") + "/webhook/",
        )
    return templates.TemplateResponse("settings.html", context, headers={"Cache-Control": "no-store"})


@settings_router.post("/settings/activate")
async def activate_services(request: Request, csrf: str = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/", status_code=303)
    if not os.environ.get("VPN4US_ACTIVATION_URL"):
        return HTMLResponse("Запуск из админки недоступен.", status_code=404)
    if not hmac.compare_digest(activation_token(request).encode(), csrf.encode()):
        return HTMLResponse("Обновите страницу настроек.", status_code=403)
    if activation_errors(get_all_settings()):
        return RedirectResponse("/settings", status_code=303)
    (DATA_DIR / "services.enabled").touch(mode=0o600, exist_ok=True)
    return RedirectResponse("/settings", status_code=303)

@settings_router.post("/settings")
async def save_settings(request: Request):
    """Сохранение настроек"""
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    for key in ['bot_token', 'yookassa_shop_id', 'yookassa_secret_key', 'admin_id']:
        if key in form and (key not in _secret_fields or form[key].strip()):
            set_setting(key, form[key])
    log_admin_action(812021055, 'settings', 'Изменение настроек')
    return RedirectResponse("/settings?saved=1", status_code=302)

@settings_router.get("/tax-settings", response_class=HTMLResponse)
async def tax_settings_page(request: Request):
    """Страница настроек налогов"""
    if not check_auth(request):
        return RedirectResponse("/")
    settings = get_all_settings()
    return templates.TemplateResponse("tax_settings.html", {
        "request": request,
        "settings": {k:settings.get(k,"") for k in ("tax_enabled","tax_inn","tax_description_template")},
        "fiscal": await asyncio.to_thread(fiscal_status,DB_PATH),
        "has_password": bool(settings.get("tax_password")),
        "csrf": activation_token(request),
    }, headers={"Cache-Control":"no-store"})

@settings_router.post("/tax-settings")
async def save_tax_settings(request: Request):
    """Сохранение настроек налогов"""
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    if not hmac.compare_digest(activation_token(request).encode(), str(form.get("csrf","")).encode()):
        return HTMLResponse("Обновите страницу настроек.",status_code=403)
    try:
        await asyncio.to_thread(save_fiscal,DB_PATH,form)
    except ValueError as exc:
        from html import escape
        return HTMLResponse(escape(str(exc)),status_code=400,headers={"Cache-Control":"no-store"})
    return RedirectResponse("/tax-settings?saved=1", status_code=302)

@settings_router.get("/referral-settings", response_class=HTMLResponse)
async def referral_settings_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    from referral_settings_policy import status
    policy=await asyncio.to_thread(status,DB_PATH)
    return templates.TemplateResponse("referral_settings.html", {
        "request":request,"policy":policy,"csrf":activation_token(request),
    },headers={"Cache-Control":"no-store"})


@settings_router.post("/referral-settings")
async def save_referral_settings(request: Request):
    if not check_auth(request):
        return RedirectResponse("/",status_code=303)
    form=await request.form()
    if not hmac.compare_digest(activation_token(request).encode(),str(form.get("csrf","")).encode()):
        return HTMLResponse("Обновите страницу настроек.",status_code=403,headers={"Cache-Control":"no-store"})
    from referral_settings_policy import save, StalePolicy
    from html import escape
    try:
        await asyncio.to_thread(save,DB_PATH,form)
    except StalePolicy as exc:
        return HTMLResponse(escape(str(exc)),status_code=409,headers={"Cache-Control":"no-store"})
    except ValueError as exc:
        return HTMLResponse(escape(str(exc)),status_code=400,headers={"Cache-Control":"no-store"})
    return RedirectResponse("/referral-settings?saved=1",status_code=303)


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
