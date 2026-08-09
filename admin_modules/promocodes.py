from fastapi.responses import RedirectResponse
"""Модуль управления промокодами и достижениями"""
import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3

from .auth import check_auth, log_admin_action

promocodes_router = APIRouter(tags=["promocodes"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@promocodes_router.get("/promocodes", response_class=HTMLResponse)
async def promocodes_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, code, discount_percent, discount_amount, valid_until, max_uses, used_count, trigger_type, is_active FROM promocodes WHERE trigger_type = "manual" ORDER BY created_at DESC')
    promocodes = c.fetchall()
    conn.close()
    return templates.TemplateResponse("promocodes.html", {"request": request, "promocodes": promocodes})

@promocodes_router.post("/promocodes/add")
async def add_promocode(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    code = form.get('code', '').strip().upper()
    if not code:
        return RedirectResponse("/promocodes?error=Введите код промокода", status_code=302)
    trigger_type = form.get('trigger_type', 'manual')
    trigger_params = {}
    if trigger_type != 'manual':
        target = form.get('trigger_target')
        if target:
            trigger_params['target_value'] = int(target)
        description = form.get('trigger_description', 'Достижение цели')
        trigger_params['description'] = description
    discount_percent = int(form.get('discount_percent', 0) or 0)
    discount_amount = float(form.get('discount_amount', 0) or 0)
    valid_until = form.get('valid_until') or None
    max_uses = int(form.get('max_uses', 1) or 1)
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute("""INSERT INTO promocodes
                 (code, discount_percent, discount_amount, valid_until, max_uses, trigger_type, trigger_params, is_active)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (code, discount_percent, discount_amount, valid_until, max_uses, trigger_type,
               json.dumps(trigger_params) if trigger_params else '{}', 1))
    conn.commit()
    conn.close()
    log_admin_action(812021055, 'add_promocode', f'Добавлен промокод: {code} (тип: {trigger_type})')
    return RedirectResponse("/promocodes?saved=1", status_code=302)

@promocodes_router.post("/promocodes/delete/{promocode_id}")
async def delete_promocode(request: Request, promocode_id: int):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT code FROM promocodes WHERE id = ?', (promocode_id,))
    promo = c.fetchone()
    if not promo:
        conn.close()
        return RedirectResponse("/promocodes?error=Промокод не найден", status_code=302)
    c.execute('DELETE FROM promocode_uses WHERE promocode_id = ?', (promocode_id,))
    c.execute('DELETE FROM promocodes WHERE id = ?', (promocode_id,))
    conn.commit()
    conn.close()
    log_admin_action(812021055, 'delete_promocode', f'Удалён промокод: {promo[0]} (ID: {promocode_id})')
    return RedirectResponse("/promocodes?deleted=1", status_code=302)

@promocodes_router.get("/promocodes/edit/{promocode_id}", response_class=HTMLResponse)
async def edit_promocode_page(request: Request, promocode_id: int):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT id, code, discount_percent, discount_amount, valid_until, max_uses, used_count, trigger_type, is_active, created_at FROM promocodes WHERE id = ?', (promocode_id,))
    promo = c.fetchone()
    conn.close()
    if not promo:
        return RedirectResponse("/promocodes?error=Промокод не найден", status_code=302)
    return templates.TemplateResponse("edit_promocode.html", {"request": request, "promo": promo, "saved": request.query_params.get('saved') == '1'})

@promocodes_router.post("/promocodes/edit/{promocode_id}")
async def edit_promocode(request: Request, promocode_id: int,
                         discount_percent: int = Form(...),
                         discount_amount: float = Form(...),
                         max_uses: int = Form(...),
                         valid_until: str = Form(None),
                         is_active: int = Form(...)):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT code FROM promocodes WHERE id = ?', (promocode_id,))
    promo = c.fetchone()
    if not promo:
        conn.close()
        return RedirectResponse("/promocodes?error=Промокод не найден", status_code=302)
    c.execute('''
        UPDATE promocodes
        SET discount_percent = ?,
            discount_amount = ?,
            max_uses = ?,
            valid_until = ?,
            is_active = ?
        WHERE id = ?
    ''', (discount_percent, discount_amount, max_uses, valid_until if valid_until else None, is_active, promocode_id))
    conn.commit()
    conn.close()
    log_admin_action(812021055, 'edit_promocode', f'Изменён промокод ID: {promocode_id}')
    return RedirectResponse(f"/promocodes/edit/{promocode_id}?saved=1", status_code=302)

# Достижения
@promocodes_router.get("/achievements", response_class=HTMLResponse)
async def achievements_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('''SELECT id, code, discount_percent, discount_amount, trigger_type, trigger_params, max_uses, used_count, is_active
                 FROM promocodes WHERE trigger_type != 'manual' ORDER BY trigger_type, id''')
    promocodes = c.fetchall()
    conn.close()
    grouped = {}
    stats = {'total': len(promocodes), 'active': 0, 'trigger_types': set()}
    for promo in promocodes:
        promo_id, code, discount_percent, discount_amount, trigger_type, trigger_params, max_uses, used_count, is_active = promo
        if is_active:
            stats['active'] += 1
        stats['trigger_types'].add(trigger_type)
        try:
            params = json.loads(trigger_params) if trigger_params else {}
            target = params.get('target_value', '?')
            description = params.get('description', '')
        except:
            target = '?'
            description = ''
        if trigger_type not in grouped:
            grouped[trigger_type] = []
        grouped[trigger_type].append({
            'id': promo_id,
            'code': code,
            'discount_percent': discount_percent,
            'discount_amount': discount_amount,
            'trigger_type': trigger_type,
            'target': target,
            'description': description,
            'max_uses': max_uses,
            'used_count': used_count,
            'is_active': is_active
        })
    stats['trigger_types'] = list(stats['trigger_types'])
    return templates.TemplateResponse("achievements.html", {"request": request, "grouped": grouped, "stats": stats})

@promocodes_router.post("/achievements/toggle/{promo_id}")
async def achievement_toggle(request: Request, promo_id: int):
    if not check_auth(request):
        return RedirectResponse("/")
    conn = sqlite3.connect('/opt/vpn-bot/data.db')
    c = conn.cursor()
    c.execute('SELECT is_active FROM promocodes WHERE id = ? AND trigger_type != "manual"', (promo_id,))
    promo = c.fetchone()
    if not promo:
        conn.close()
        return RedirectResponse("/achievements?error=Достижение не найдено", status_code=302)
    new_status = 0 if promo[0] else 1
    c.execute('UPDATE promocodes SET is_active = ? WHERE id = ?', (new_status, promo_id))
    conn.commit()
    conn.close()
    log_admin_action(812021055, 'achievement_toggle', f'Достижение ID {promo_id} {"включено" if new_status else "выключено"}')
    return RedirectResponse("/achievements?toggled=1", status_code=302)
