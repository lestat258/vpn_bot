#!/bin/bash
# VPN Bot Installer
set -e

echo "🚀 Установка VPN Bot..."

# Проверка прав
if [ "$EUID" -ne 0 ]; then
    echo "❌ Запустите с root правами: sudo bash install.sh"
    exit 1
fi

# Установка зависимостей
apt-get update
apt-get install -y python3 python3-pip python3-venv git sqlite3 curl

# Клонирование проекта
cd /opt
rm -rf vpn-bot
git clone https://github.com/lestat258/vpn_bot.git vpn-bot
cd vpn-bot

# Создание виртуального окружения
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Инициализация БД
python3 -c "from database import init_db; init_db()"

# Настройка systemd
cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable vpn-bot vpn-admin vpn-webhook
systemctl start vpn-bot vpn-admin vpn-webhook

echo "✅ Установка завершена!"
echo "🌐 Веб-админка: http://$(curl -s ifconfig.me):8000"
echo "🔑 Логин: admin | Пароль: admin"
echo "⚠️  Смените пароль после первого входа!"
