#!/bin/bash
set -e

echo "🚀 Установка VPN Bot..."
echo "=========================================="

# Проверка прав
if [ "$EUID" -ne 0 ]; then
    echo "❌ Запустите с root правами: sudo bash install.sh"
    exit 1
fi

# Определяем IP
IP=$(curl -s ifconfig.me || echo "localhost")

echo "📦 Установка зависимостей..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git sqlite3 curl ufw

# Создаём папку проекта
cd /opt
rm -rf vpn-bot
git clone https://github.com/lestat258/vpn_bot.git vpn-bot
cd vpn-bot

echo "🐍 Создание виртуального окружения..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "🗄️ Инициализация базы данных..."
python3 -c "
import sys
sys.path.insert(0, '/opt/vpn-bot')
from database import init_db
init_db()

import sqlite3
conn = sqlite3.connect('/opt/vpn-bot/data.db')
c = conn.cursor()

# Создаём таблицу settings если её нет
c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')

# Добавляем администратора
c.execute(\"INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_username', 'admin')\")
c.execute(\"INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_password', 'admin')\")
c.execute(\"INSERT OR REPLACE INTO settings (key, value) VALUES ('first_login', 'false')\")
c.execute(\"INSERT OR REPLACE INTO settings (key, value) VALUES ('bot_token', '')\")
c.execute(\"INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_id', '')\")

conn.commit()
conn.close()
print('✅ База данных настроена')
"

echo "⚙️ Настройка systemd..."
cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable vpn-bot vpn-admin 2>/dev/null || true
systemctl start vpn-bot vpn-admin

# Открываем порт 8000
ufw allow 8000/tcp 2>/dev/null || true

echo ""
echo "=========================================="
echo "✅ УСТАНОВКА ЗАВЕРШЕНА!"
echo "=========================================="
echo ""
echo "🌐 Веб-админка: http://$IP:8000"
echo "🔑 Логин: admin"
echo "🔑 Пароль: admin"
echo ""
echo "⚠️  После входа:"
echo "   1. Смените пароль администратора"
echo "   2. Введите токен бота в настройках"
echo "   3. Добавьте сервер 3X-UI"
echo "   4. Создайте тарифы"
echo ""
echo "📋 Команды:"
echo "   systemctl status vpn-bot vpn-admin"
echo "   tail -f /opt/vpn-bot/bot.log"
echo "=========================================="
