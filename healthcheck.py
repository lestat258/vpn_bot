#!/usr/bin/env python3
"""
Healthcheck сервер для мониторинга состояния бота
"""

from fastapi import FastAPI
from datetime import datetime
import sqlite3
import logging

app = FastAPI(title="VPN Bot Health Check")

@app.get("/health")
async def health():
    """Проверка состояния всех компонентов"""
    status = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "components": {}
    }

    # Проверка базы данных
    try:
        conn = sqlite3.connect('/opt/vpn-bot/data.db')
        c = conn.cursor()
        c.execute('SELECT 1')
        conn.close()
        status["components"]["database"] = "ok"
    except Exception as e:
        status["components"]["database"] = f"error: {str(e)}"
        status["status"] = "degraded"

    # Проверка бота (через systemd)
    try:
        import subprocess
        result = subprocess.run(['systemctl', 'is-active', 'vpn-bot'], capture_output=True, text=True)
        if result.stdout.strip() == 'active':
            status["components"]["bot"] = "ok"
        else:
            status["components"]["bot"] = f"inactive: {result.stdout.strip()}"
            status["status"] = "degraded"
    except Exception as e:
        status["components"]["bot"] = f"error: {str(e)}"

    # Проверка админки
    try:
        import subprocess
        result = subprocess.run(['systemctl', 'is-active', 'vpn-admin'], capture_output=True, text=True)
        if result.stdout.strip() == 'active':
            status["components"]["admin"] = "ok"
        else:
            status["components"]["admin"] = f"inactive: {result.stdout.strip()}"
            status["status"] = "degraded"
    except Exception as e:
        status["components"]["admin"] = f"error: {str(e)}"

    return status

@app.get("/health/db")
async def health_db():
    """Проверка только базы данных"""
    try:
        conn = sqlite3.connect('/opt/vpn-bot/data.db')
        c = conn.cursor()
        c.execute('SELECT 1')
        conn.close()
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    logging.info("🚀 Healthcheck server started on port 8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
