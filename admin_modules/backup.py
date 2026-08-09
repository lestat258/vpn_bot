"""Модуль управления бэкапами"""
import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .auth import check_auth, log_admin_action
from database import get_all_settings, set_setting
from backup_manager import backup_manager

backup_router = APIRouter(tags=["backup"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@backup_router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    
    last_backup_time = 'Нет данных'
    backup_size = 'Нет данных'
    
    try:
        backups = backup_manager.get_backup_list()
        if backups:
            latest = backups[0]
            last_backup_time = latest.get('datetime', 'Нет данных')
            backup_size = f"{latest.get('size', 0) / 1024:.1f} KB"
    except Exception as e:
        print(f"Ошибка: {e}")
    
    return templates.TemplateResponse("backup.html", {
        "request": request,
        "last_backup_time": last_backup_time,
        "backup_size": backup_size
    })

@backup_router.post("/create")
async def create_backup_api(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    try:
        result = backup_manager.create_full_backup()
        if result["success"]:
            log_admin_action(812021055, 'create_backup', f'Создан бэкап: {result["timestamp"]}')
            return {"success": True, "message": "Бэкап создан", "timestamp": result["timestamp"], "size": result["size"]}
        else:
            return {"success": False, "message": result.get("error", "Ошибка создания")}
    except Exception as e:
        return {"success": False, "message": str(e)}

@backup_router.post("/create-full")
async def create_full_backup_api(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    try:
        result = backup_manager.create_full_project_backup()
        if result["success"]:
            log_admin_action(812021055, 'create_full_backup', f'Создан полный бэкап: {result["name"]}')
            return {
                "success": True,
                "message": "Полный бэкап создан",
                "timestamp": result["timestamp"],
                "size": result["size"],
                "name": result["name"]
            }
        else:
            return {"success": False, "message": result.get("error", "Ошибка создания")}
    except Exception as e:
        return {"success": False, "message": str(e)}

@backup_router.get("/backup/download-db")
async def download_db(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    log_admin_action(812021055, 'download_db', 'Скачивание БД')
    return FileResponse(
        path="/opt/vpn-bot/data.db",
        filename="vpn-bot-backup.db",
        media_type="application/octet-stream"
    )

@backup_router.get("/backup/download-settings")
async def download_settings(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    settings = get_all_settings()
    settings.pop('admin_password', None)
    log_admin_action(812021055, 'download_settings', 'Скачивание настроек')
    return PlainTextResponse(
        content=json.dumps(settings, indent=2, ensure_ascii=False),
        headers={"Content-Disposition": "attachment; filename=vpn-bot-settings.json"}
    )

@backup_router.post("/backup/restore")
async def restore_backup(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    form = await request.form()
    settings_json = form.get("settings_json")
    if settings_json:
        try:
            settings = json.loads(settings_json)
            for key, value in settings.items():
                set_setting(key, str(value))
            log_admin_action(812021055, 'restore_settings', 'Восстановление настроек')
            return RedirectResponse("/backup?restored=1", status_code=302)
        except:
            return RedirectResponse("/backup?error=1", status_code=302)
    return RedirectResponse("/backup", status_code=302)
