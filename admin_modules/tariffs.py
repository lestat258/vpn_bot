from fastapi.responses import RedirectResponse
"""Модуль управления тарифами"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth, log_admin_action

tariffs_router = APIRouter(tags=["tariffs"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@tariffs_router.get("/tariffs", response_class=HTMLResponse)
async def tariffs_page(request: Request):
    """Страница списка тарифов"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days, traffic_gb, ip_limit, is_active FROM tariffs')
    tariffs = c.fetchall()
    conn.close()

    return templates.TemplateResponse("tariffs.html", {"request": request, "tariffs": tariffs})

@tariffs_router.get("/tariffs/add", response_class=HTMLResponse)
async def add_tariff_page(request: Request):
    """Страница добавления тарифа"""
    if not check_auth(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("add_tariff.html", {"request": request})

@tariffs_router.post("/tariffs/add")
async def add_tariff(request: Request, name: str = Form(...), price_rub: float = Form(...),
                     duration_days: int = Form(...), traffic_gb: float = Form(0),
                     ip_limit: int = Form(3)):
    """Добавление тарифа"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('INSERT INTO tariffs (name, price_rub, duration_days, traffic_gb, ip_limit) VALUES (?, ?, ?, ?, ?)',
              (name, price_rub, duration_days, traffic_gb, ip_limit))
    conn.commit()
    conn.close()

    log_admin_action(812021055, 'add_tariff', f'Добавлен тариф: {name}')
    return RedirectResponse("/tariffs", status_code=302)

@tariffs_router.get("/tariffs/edit/{tariff_id}", response_class=HTMLResponse)
async def edit_tariff_page(request: Request, tariff_id: int):
    """Страница редактирования тарифа"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, name, price_rub, duration_days, traffic_gb, ip_limit, is_active FROM tariffs WHERE id = ?', (tariff_id,))
    tariff = c.fetchone()
    conn.close()

    if not tariff:
        return RedirectResponse("/tariffs")

    return templates.TemplateResponse("edit_tariff.html", {"request": request, "tariff": tariff})

@tariffs_router.post("/tariffs/edit/{tariff_id}")
async def edit_tariff(request: Request, tariff_id: int,
                      name: str = Form(...),
                      price_rub: float = Form(...),
                      duration_days: int = Form(...),
                      traffic_gb: float = Form(0),
                      ip_limit: int = Form(3),
                      is_active: bool = Form(False)):
    """Редактирование тарифа"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''UPDATE tariffs
                 SET name = ?, price_rub = ?, duration_days = ?, traffic_gb = ?, ip_limit = ?, is_active = ?
                 WHERE id = ?''',
              (name, price_rub, duration_days, traffic_gb, ip_limit, 1 if is_active else 0, tariff_id))
    conn.commit()
    conn.close()

    log_admin_action(812021055, 'edit_tariff', f'Изменён тариф: {name} (ID: {tariff_id})')
    return RedirectResponse("/tariffs?edited=1", status_code=302)

@tariffs_router.post("/tariffs/delete/{tariff_id}")
async def delete_tariff(request: Request, tariff_id: int):
    """Удаление тарифа"""
    if not check_auth(request):
        return RedirectResponse("/")

    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE tariff_id = ? AND is_active = 1', (tariff_id,))
    count = c.fetchone()[0]

    if count > 0:
        conn.close()
        return RedirectResponse(f"/tariffs?error=Невозможно удалить тариф, есть {count} активных подписок", status_code=302)

    c.execute('DELETE FROM tariffs WHERE id = ?', (tariff_id,))
    conn.commit()
    conn.close()

    log_admin_action(812021055, 'delete_tariff', f'Удалён тариф ID: {tariff_id}')
    return RedirectResponse("/tariffs?deleted=1", status_code=302)
