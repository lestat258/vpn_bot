"""Local tax settings only. Never register income or requeue historical payments."""
import base64
import sqlite3
from contextlib import closing
from pathlib import Path

from encryption import cipher_from_stored_key, unwrap_token


def ready(values):
    inn=values.get("tax_inn") or ""
    if len(inn)!=12 or not inn.isascii() or not inn.isdecimal():
        return False
    try:
        token=unwrap_token(values.get("tax_password"))
        # The current fiscal worker requires the canonical double-base64 format.
        raw=values["tax_password"]
        if not token or not raw.startswith("Z0FBQU"):
            return False
        return bool(cipher_from_stored_key(values["encryption_key"]).decrypt(token).decode().strip())
    except Exception:
        return False


def _settings(db):
    return dict(db.execute("SELECT key,value FROM settings WHERE key IN "
        "('tax_enabled','tax_inn','tax_password','tax_description_template','encryption_key')"))


def save(path, form, *, toggle=False):
    """Atomic local save, explicit opt-in; absent checkbox means false."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=rw",uri=True,timeout=15)) as db,db:
        db.execute("BEGIN IMMEDIATE")
        old=_settings(db)
        values=dict(old)
        if toggle:
            enable=old.get("tax_enabled")!="true"
        else:
            raw=form.get("tax_enabled")
            if raw not in (None,"on","true","false","off"):
                raise ValueError("Некорректное значение переключателя.")
            enable=raw in ("on","true")
            if "tax_inn" in form:
                inn=form["tax_inn"].strip()
                if inn and (len(inn)!=12 or not inn.isascii() or not inn.isdecimal()):
                    raise ValueError("ИНН должен содержать 12 цифр.")
                values["tax_inn"]=inn
            if form.get("tax_password"):
                password=form["tax_password"]
                if not password.strip() or len(password)>1024:
                    raise ValueError("Некорректный пароль.")
                try:
                    cipher=cipher_from_stored_key(old["encryption_key"])
                    values["tax_password"]=base64.urlsafe_b64encode(cipher.encrypt(password.encode())).decode()
                except Exception:
                    raise ValueError("Не удалось зашифровать пароль. Проверьте ключ шифрования.") from None
            if "tax_description_template" in form:
                description=form["tax_description_template"].strip()
                if not description or len(description)>255:
                    raise ValueError("Укажите описание чека (1–255 символов).")
                values["tax_description_template"]=description
        if enable and not ready(values):
            raise ValueError("Для включения укажите ИНН и пароль в веб-админке.")
        values["tax_enabled"]="true" if enable else "false"
        for key in ("tax_enabled","tax_inn","tax_password","tax_description_template"):
            if key in values and values[key]!=old.get(key):
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(key,values[key]))
        return enable


def status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        values=_settings(db)
        return {"enabled":values.get("tax_enabled")=="true",
                "legacy_flag":values.get("tax_enabled") not in (None,"true","false"),
                "credentials_ready":ready(values)}
