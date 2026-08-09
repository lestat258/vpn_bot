#!/usr/bin/env python3
"""
Админ-панель VPN Bot (МОДУЛЬНАЯ ВЕРСИЯ)
"""
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from database import init_db

# Импорт всех модулей админ-панели
from admin_modules import (
    auth_router,
    dashboard_router,
    users_router,
    servers_router,
    tariffs_router,
    payments_router,
    promocodes_router,
    broadcast_router,
    analytics_router,
    backup_router,
    settings_router,
    security_router,
    rate_limits_router,
)

app = FastAPI(title="VPN Bot Admin", version="2.0")
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bot.vpn4us.ru", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted Hosts
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["bot.vpn4us.ru", "localhost", "127.0.0.1"]
)

# ============================================================
# ПОДКЛЮЧЕНИЕ ВСЕХ РОУТЕРОВ
# ============================================================

app.include_router(auth_router)          # /, /login, /logout, /health
app.include_router(dashboard_router)     # /dashboard
app.include_router(users_router)         # /users, /users/{id}
app.include_router(servers_router)       # /servers, /servers/add, /servers/delete, /servers/check
app.include_router(tariffs_router)       # /tariffs, /tariffs/add, /tariffs/edit, /tariffs/delete
app.include_router(payments_router)      # /payments
app.include_router(promocodes_router)    # /promocodes, /achievements
app.include_router(broadcast_router)     # /broadcast
app.include_router(analytics_router)     # /analytics
app.include_router(backup_router)        # /backup
app.include_router(settings_router)      # /settings, /tax-settings, /bot-texts
app.include_router(security_router)      # /2fa, /change-credentials, /admin-logs
app.include_router(rate_limits_router)   # /rate-limits

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("🔐 Админ-панель: https://bot.vpn4us.ru")
    print("📊 Доступные страницы:")
    print("  - /dashboard - Дашборд")
    print("  - /users - Пользователи")
    print("  - /servers - Серверы")
    print("  - /tariffs - Тарифы")
    print("  - /payments - Оплаты")
    print("  - /promocodes - Промокоды")
    print("  - /achievements - Достижения")
    print("  - /broadcast - Рассылка")
    print("  - /analytics - Аналитика")
    print("  - /backup - Бэкап")
    print("  - /admin-logs - Логи")
    print("  - /rate-limits - Защита от спама")
    print("  - /2fa - 2FA")
    print("  - /change-credentials - Смена пароля")
    print("  - /tax-settings - Налоги")
    print("  - /bot-texts - Тексты бота")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
