"""Filesystem and admin network settings shared by the VPN4US processes."""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
_data_dir = os.environ.get("VPN4US_DATA_DIR") or str(APP_DIR)
if not Path(_data_dir).is_absolute():
    raise ValueError("VPN4US_DATA_DIR must be an absolute path")
DATA_DIR = Path(_data_dir)
DB_PATH = str(DATA_DIR / "data.db")
BACKUP_DIR = str(DATA_DIR / "backups")
TEMPLATE_DIR = str(APP_DIR / "templates")


def _list_setting(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return [value.strip() for value in raw.split(",") if value.strip()]


ADMIN_ALLOWED_HOSTS = _list_setting(
    "VPN4US_ADMIN_ALLOWED_HOSTS",
    ["bot.vpn4us.ru", "admin.vpn4us.ru", "node1.vpn4us.ru", "localhost", "127.0.0.1", "31.77.142.92"],
)
ADMIN_CORS_ORIGINS = _list_setting(
    "VPN4US_ADMIN_CORS_ORIGINS",
    ["https://bot.vpn4us.ru", "http://localhost:8000", "http://127.0.0.1:8000"],
)
