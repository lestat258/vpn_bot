#!/bin/bash
set -e
echo "🚀 Установка VPN Bot..."

if [ "$EUID" -ne 0 ]; then
    echo "❌ Запустите с root правами: sudo bash install.sh"
    exit 1
fi

apt-get update
apt-get install -y python3 python3-pip python3-venv git sqlite3 curl

cd /opt
rm -rf vpn-bot
git clone https://github.com/lestat258/vpn_bot.git vpn-bot
cd vpn-bot

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Инициализация БД с дефолтными настройками
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
c.execute(\"INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', '21232f297a57a5a743894a0e4a801fc3')\")
c.execute(\"INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_username', 'admin')\")
conn.commit()
conn.close()
print('✅ База данных инициализирована с дефолтными настройками')
"

cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable vpn-bot vpn-admin
systemctl start vpn-bot vpn-admin

IP=$(curl -s ifconfig.me)
echo "✅ Установка завершена!"
echo "🌐 Веб-админка: http://$IP:8000"
echo "🔑 Логин: admin | Пароль: admin"
echo ""
echo "⚠️  После входа в админку:"
echo "   1. Смените пароль администратора"
echo "   2. Введите токен бота в настройках"
echo "   3. Добавьте сервер 3X-UI"
echo "   4. Создайте тарифы"
