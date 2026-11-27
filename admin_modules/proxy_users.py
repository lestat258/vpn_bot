"""Manage the configured Squid NCSA credentials file."""
from runtime_config import TEMPLATE_DIR
import fcntl
import hmac
import logging
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import check_auth

proxy_router = APIRouter(prefix="/proxy-users")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
logger = logging.getLogger(__name__)
csrf_key = secrets.token_bytes(32)
valid_username = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def csrf_token(request):
    return hmac.digest(csrf_key, request.cookies["session_id"].encode(), "sha256").hex()


def credentials_path():
    configured = os.environ.get("VPN4US_SQUID_PASSWD")
    return Path(configured) if configured == "/etc/squid/passwd" else None


def read_credentials(path):
    # Refuse links and non-regular files. Never return hashes to the browser.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Credentials must be in a regular file")
        entries = file.read().splitlines(keepends=True)
    return metadata, entries


def matching_entries(entries, username):
    return [entry for entry in entries if entry.partition(b":")[0] == username.encode()
            and b":" in entry]


def hash_password(username, password):
    if not 12 <= len(password.encode()) <= 72 or any(c in password for c in "\r\n\0"):
        raise HTTPException(400, "Пароль должен занимать от 12 до 72 байт, без переносов строк.")
    result = subprocess.run(
        ["/usr/bin/htpasswd", "-niB", "--", username],
        input=(password + "\n").encode(),
        capture_output=True, check=True, timeout=10,
    )
    entry = result.stdout.strip()
    if not entry.startswith(username.encode() + b":$2") or b"\n" in entry:
        raise OSError("Unexpected htpasswd output")
    return entry + b"\n"


def change_credentials(path, username, password):
    if password is not None and not valid_username.fullmatch(username):
        raise HTTPException(400, "Логин может содержать 1–64 символа: A–Z, a–z, 0–9, точку, дефис и подчёркивание.")
    with os.fdopen(os.open(path.with_name(path.name + ".vpn4us.lock"),
                          os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "rb+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata, entries = read_credentials(path)
        matches = matching_entries(entries, username)
        if password is None:
            if not matches:
                raise HTTPException(404, "Прокси-пользователь не найден.")
            entries = [entry for entry in entries if entry not in matches]
        else:
            if matches:
                raise HTTPException(409, "Прокси-пользователь с таким логином уже есть.")
            if entries and not entries[-1].endswith(b"\n"):
                entries.append(b"\n")
            entries.append(hash_password(username, password))

        fd, temporary = tempfile.mkstemp(prefix=".vpn4us-credentials-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchown(file.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(file.fileno(), stat.S_IMODE(metadata.st_mode))
                file.writelines(entries)
                file.flush()
                # The NCSA helper checks mtime with one-second resolution.
                modified = max(time.time_ns(), (int(metadata.st_mtime) + 1) * 10**9)
                os.utime(file.fileno(), ns=(metadata.st_atime_ns, modified))
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


@proxy_router.get("")
def list_proxy_users(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    path = credentials_path()
    usernames = []
    if path is not None:
        try:
            _, entries = read_credentials(path)
            usernames = sorted({line.partition(b":")[0].decode(errors="replace")
                                for line in entries
                                if b":" in line and not line.lstrip().startswith(b"#")})
        except OSError:
            logger.exception("Could not read Squid credentials")
            raise HTTPException(503, "Файл пользователей Squid недоступен.")
    return templates.TemplateResponse("proxy_users.html", {
        "request": request, "enabled": path is not None, "usernames": usernames,
        "csrf": csrf_token(request),
    }, headers={"Cache-Control": "no-store"})


@proxy_router.post("/add")
@proxy_router.post("/delete")
def update_proxy_users(request: Request, username: str = Form(...),
                       csrf: str = Form(...), password: str = Form("")):
    if not check_auth(request):
        raise HTTPException(401, "Войдите в админку.")
    if not hmac.compare_digest(csrf_token(request).encode(), csrf.encode()):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    path = credentials_path()
    if path is None:
        raise HTTPException(503, "Управление пользователями Squid не настроено.")
    deleting = request.url.path.endswith("/delete")
    try:
        change_credentials(path, username, None if deleting else password)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not update Squid credentials")
        raise HTTPException(503, "Изменение файла пользователей Squid не выполнено.")
    logger.info("Squid account %s: %s", "deleted" if deleting else "added", username)
    return RedirectResponse("/proxy-users", status_code=303)
