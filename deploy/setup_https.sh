#!/usr/bin/env bash
# Publish the already configured VPN4US services on a dedicated HTTPS hostname.
set -euo pipefail

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

[[ $EUID == 0 ]] || fail 'Запустите скрипт через sudo.'
admin_only=0
check_only=0
if [[ ${1:-} == --check-only ]]; then
    check_only=1
    shift
fi
if [[ ${1:-} == --admin-only ]]; then
    admin_only=1
    shift
fi
[[ $# == 2 ]] || fail 'Использование: sudo bash deploy/setup_https.sh [--check-only | --admin-only] DOMAIN ACME_EMAIL'
domain=${1,,}
https_port=${VPN4US_HTTPS_PORT:-443}
[[ $https_port == 443 || $https_port == 8443 ]] || fail 'Укажите порт HTTPS 443 или 8443.'
public_url="https://$domain"
if [[ $https_port == 8443 ]]; then public_url+=":8443"; fi
email=$2
[[ ${#domain} -le 253 && $domain =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] ||
    fail 'Укажите доменное имя без схемы, порта и пути.'
[[ $email =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] ||
    fail 'Укажите контактный email для выпуска сертификата.'
command -v apt-get >/dev/null || fail 'Этот шаг рассчитан на Debian или Ubuntu.'
command -v ss >/dev/null || fail 'Для проверки портов нужен пакет iproute2.'
getent ahostsv4 "$domain" >/dev/null || fail "Добавьте A-запись для $domain."

exec 9>/run/vpn4us-https.lock
flock -n 9 || fail 'Настройка HTTPS уже выполняется.'

sockets=$(ss -H -ltnp "( sport = :80 or sport = :$https_port )")
while IFS= read -r socket; do
    if [[ -n $socket && $socket != *'"nginx"'* ]]; then
        fail "Порт 80 или $https_port занят другим процессом. Проверьте, какая служба его использует."
    fi
done <<< "$sockets"

name="vpn4us-$domain"
site="/etc/nginx/sites-enabled/$name.conf"
root="/var/lib/vpn4us/acme/$domain"
lineage="/etc/letsencrypt/live/$name"
marker="# Managed by VPN4US HTTPS installer for $domain"
if [[ -L $site ]] || { [[ -e $site ]] && ! head -n 1 "$site" | grep -Fxq -- "$marker"; }; then
    fail "Путь $site уже занят существующей конфигурацией."
fi

if (( check_only )); then exit 0; fi

missing=()
for command_name in nginx certbot curl; do
    command -v "$command_name" >/dev/null || missing+=("$command_name")
done
if (( ${#missing[@]} )); then
    apt-get update
    apt-get install -y "${missing[@]}"
fi

# A dedicated hostname must not already belong to another enabled vhost.
nginx -T 2>/dev/null | awk -v domain="$domain" -v own="$site" '
    /^# configuration file / { current=$4; sub(/:$/, "", current) }
    {
        sub(/#.*/, "")
        if (current != own && ($1 == "server_name" || continued)) {
            start = continued ? 1 : 2
            continued = index($0, ";") == 0
            for (i=start; i<=NF; i++) {
                value=$i
                sub(/;$/, "", value)
                if (tolower(value) == domain) found=1
            }
        } else continued=0
    }
    END { exit found ? 1 : 0 }
' || fail "Имя $domain уже используется в Nginx или конфигурация Nginx содержит ошибку."

addresses=(http://127.0.0.1:8000/health)
if (( ! admin_only )); then addresses+=(http://127.0.0.1:8081/health); fi
for address in "${addresses[@]}"; do
    curl --noproxy '*' --fail --header "Host: $domain" --silent --show-error --max-time 5 --output /dev/null "$address" ||
        fail "Служба VPN4US недоступна по адресу $address."
done

install -d -m 0755 "$root/.well-known/acme-challenge"
tmp=$(mktemp -d)
had_site=0
changed=0
if [[ -f $site ]]; then
    cp -p -- "$site" "$tmp/previous"
    had_site=1
fi
cleanup() {
    local code=$?
    if (( code && changed )); then
        if (( had_site )); then cp -p -- "$tmp/previous" "$site"
        else rm -f -- "$site"
        fi
        nginx -t && systemctl reload nginx || true
    fi
    rm -rf -- "$tmp"
    return "$code"
}
trap cleanup EXIT

listen_v6=
listen_tls_v6=
if [[ -s /proc/net/if_inet6 ]]; then
    listen_v6='    listen [::]:80;'
    listen_tls_v6="    listen [::]:$https_port ssl;"
fi

write_http() {
    printf '%s\n' "$marker"
    cat <<EOF
server {
    listen 80;
$listen_v6
    server_name $domain;

    location ^~ /.well-known/acme-challenge/ {
        root $root;
        try_files \$uri =404;
    }

    location / {
        return 308 $public_url\$request_uri;
    }
}
EOF
}

reload_site() {
    install -m 0644 "$tmp/site" "$site"
    changed=1
    nginx -t
    if systemctl is-active --quiet nginx; then systemctl reload nginx
    else systemctl start nginx
    fi
}

if (( ! had_site )); then
    write_http > "$tmp/site"
    reload_site
fi

certbot certonly --webroot --webroot-path "$root" --cert-name "$name" \
    --domain "$domain" --email "$email" --agree-tos --non-interactive --keep-until-expiring \
    --deploy-hook 'nginx -t && systemctl reload nginx'

write_http > "$tmp/site"
cat >> "$tmp/site" <<EOF

server {
    listen $https_port ssl;
$listen_tls_v6
    server_name $domain;
    ssl_certificate $lineage/fullchain.pem;
    ssl_certificate_key $lineage/privkey.pem;

    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-For \$remote_addr;

    location = /webhook/ {
        proxy_pass http://127.0.0.1:8081;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
EOF
reload_site
systemctl enable --now certbot.timer
curl --noproxy '*' --resolve "$domain:$https_port:127.0.0.1" --fail --silent --show-error \
    --max-time 10 --output /dev/null "$public_url/"
if (( ! admin_only )); then
    status=$(curl --noproxy '*' --resolve "$domain:$https_port:127.0.0.1" --silent --show-error \
    --max-time 10 --output /dev/null --write-out '%{http_code}' "$public_url/webhook/")
    [[ $status == 405 ]] || fail "Webhook вернул HTTP $status на GET, ожидался HTTP 405."
fi
printf 'Админка VPN4US — %s/\n' "$public_url"
if (( ! admin_only )); then
    printf 'Уведомления ЮKassa — %s/webhook/\n' "$public_url"
fi
