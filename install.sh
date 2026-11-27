#!/usr/bin/env bash
# Bootstrap the web admin on a clean Debian or Ubuntu VPS.
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

[[ $EUID == 0 ]] || fail 'Запустите через sudo.'
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir=/opt/vpn4us
data_dir=/var/lib/vpn4us/data
unit=/etc/systemd/system/vpn-admin.service

[[ ! -e $app_dir && ! -e $data_dir && ! -e /opt/vpn-bot && ! -e $unit ]] ||
    fail 'Найдена существующая установка VPN4US. Этот установщик предназначен для чистой установки.'
if systemctl cat vpn-admin.service >/dev/null 2>&1 || id vpn4us >/dev/null 2>&1; then
    fail 'Учётная запись vpn4us или служба админки уже существует.'
fi

read -r -p 'Домен админки VPN4US: ' domain
domain=${domain,,}
read -r -p 'Email для выпуска сертификата: ' email
[[ ${#domain} -le 253 && $domain =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] ||
    fail 'Укажите доменное имя без схемы, порта и пути.'
[[ $email =~ ^[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,63}$ ]] ||
    fail 'Укажите контактный email для выпуска сертификата.'
getent ahostsv4 "$domain" >/dev/null || fail "Добавьте A-запись для $domain."

# The source checkout is required so installation uses exactly one revision.
git -C "$source_dir" cat-file -e HEAD:deploy/setup_https.sh ||
    fail 'Запустите install.sh из Git-репозитория с шагом настройки HTTPS.'

apt-get update
apt-get install -y python3 python3-venv curl iproute2
if [[ -n $(ss -H -ltn '( sport = :8000 )') ]]; then
    fail 'Локальный порт 8000 уже занят.'
fi

umask 022
install -d -m 0755 "$app_dir"
git -C "$source_dir" archive HEAD | tar -x --no-same-owner -C "$app_dir"
python3 -m venv "$app_dir/venv"
"$app_dir/venv/bin/python" -m pip install -r "$app_dir/requirements.txt"

useradd --system --user-group --no-create-home --shell /usr/sbin/nologin vpn4us
install -d -o vpn4us -g vpn4us -m 0700 "$data_dir"
cd "$app_dir"
initial_password=$(runuser -u vpn4us -- env VPN4US_DATA_DIR="$data_dir" "$app_dir/venv/bin/python" - <<'PY'
import secrets
import sys

from database import get_db, init_db

password = secrets.token_urlsafe(24)
init_db()
with get_db() as conn:
    for key, value in (
        ("admin_username", "admin"), ("admin_password", password), ("first_login", "true")
    ):
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
print(password, file=sys.stdout)
PY
)

cat > "$unit" <<EOF
[Unit]
Description=VPN4US Admin
After=network.target

[Service]
User=vpn4us
WorkingDirectory=$app_dir
UMask=0077
Environment=VPN4US_DATA_DIR=$data_dir
Environment=VPN4US_ADMIN_BIND=127.0.0.1
Environment=VPN4US_ADMIN_ALLOWED_HOSTS=$domain,localhost,127.0.0.1
ExecStart=$app_dir/venv/bin/python $app_dir/admin.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now vpn-admin.service
for attempt in {1..15}; do
    if curl --noproxy '*' --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
        break
    fi
    [[ $attempt -lt 15 ]] || fail 'Админка не запустилась. Проверьте журнал vpn-admin.service.'
    sleep 1
done

# Preserve the initial credentials for the operator even if ACME needs a retry.
printf 'Логин — admin\nВременный пароль — %s\n' "$initial_password"
bash deploy/setup_https.sh --admin-only "$domain" "$email"
printf '%s\n' 'Войдите в админку и смените временный пароль.'
