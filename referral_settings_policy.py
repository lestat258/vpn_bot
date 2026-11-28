"""Atomic, audited referral tier editing. Never enables financial features."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from referral_accrual import levels, validate_levels, setting, table_exists, enabled


class StalePolicy(ValueError):
    pass


def _revision(db):
    raw=setting(db,"referral_levels")
    last=db.execute("SELECT COALESCE(MAX(id),0) FROM referral_policy_history").fetchone()[0] if table_exists(db,"referral_policy_history") else 0
    return hashlib.sha256(json.dumps([raw,last],ensure_ascii=False).encode()).hexdigest()


def _view(db):
    error=False
    try:
        rows=levels(db)
    except (TypeError,ValueError):
        rows=()
        error=True
    history=[]
    if table_exists(db,"referral_policy_history"):
        history=db.execute("""SELECT id,created_at,old_policy,new_policy
             FROM referral_policy_history ORDER BY id DESC LIMIT 10""").fetchall()
    return dict(rows=rows,revision=_revision(db),invalid=error,
                requested=setting(db,"referral_accrual_enabled")=="true",
                admin_process_enabled=enabled(db),history=history)


def status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.execute("BEGIN")
        return _view(db)


def parse_form(form):
    rows=[]
    for i in range(10):
        keys=[f"{k}_{i}" for k in ("threshold","name","rate")]
        values=[form.get(k,"") for k in keys]
        if any(not isinstance(v,str) for v in values):
            raise ValueError("Некорректные поля уровня.")
        values=[v.strip() for v in values]
        if not any(values):
            continue
        threshold,name,rate=values
        if not (threshold.isascii() and threshold.isdecimal() and len(threshold)<=6
                and rate.isascii() and rate.isdecimal() and len(rate)<=3 and name):
            raise ValueError("Заполните название, порог и процент целыми числами.")
        rows.append([int(threshold),name,int(rate)])
    try:
        return validate_levels(rows)
    except (ValueError,TypeError):
        raise ValueError("От 1 до 10 уровней; первый порог 1, далее пороги строго возрастают, проценты 1–100 не убывают; названия 1–40 символов.") from None


def save(path,form):
    rows=parse_form(form)
    revision=form.get("revision")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=rw",uri=True,timeout=15)) as db,db:
        db.execute("BEGIN IMMEDIATE")
        if revision!=_revision(db):
            raise StalePolicy("Настройки изменились. Обновите страницу перед сохранением.")
        old=setting(db,"referral_levels")
        # Never change the activation flag or any existing awards.
        try:
            if rows==levels(db):
                return False
        except (TypeError,ValueError):
            pass
        new=json.dumps(rows,ensure_ascii=False,separators=(",",":"))
        db.execute("""CREATE TABLE IF NOT EXISTS referral_policy_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_policy TEXT,new_policy TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO referral_policy_history(old_policy,new_policy) VALUES(?,?)",(old,new))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('referral_levels',?)",(new,))
        return True
