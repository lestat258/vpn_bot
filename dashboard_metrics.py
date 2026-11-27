"""Read-only dashboard snapshots; no payment reconciliation or server mutations."""
import asyncio
import base64
import json
import math
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from refund_fulfillment import CUTOFF, LAST_HISTORICAL_ID

UNKNOWN = "Нет данных"
MSK = timezone(timedelta(hours=3))


def read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def cpu_ticks(proc):
    fields = (proc/"stat").read_text().splitlines()[0].split()
    if fields[0] != "cpu" or len(fields)<5:
        raise ValueError("CPU counters")
    # guest and guest_nice already included in user/nice.
    v = list(map(int, fields[1:9]))
    return sum(v), v[3]+(v[4] if len(v)>4 else 0)


def cpu_percent(before, after):
    total = after[0]-before[0]
    idle = after[1]-before[1]
    if total <= 0 or not 0 <= idle <= total:
        return None
    return round(100*(total-idle)/total,1)


def local_metrics(proc="/proc", disk_path="/opt/vpn-bot"):
    proc = Path(proc)
    data = dict.fromkeys(("cpu","memory","disk","uptime"),UNKNOWN)
    data["percent"] = dict.fromkeys(("cpu","memory","disk"))
    try:
        before = cpu_ticks(proc)
        time.sleep(.2)
        load = cpu_percent(before,cpu_ticks(proc))
        if load is not None:
            data["cpu"] = f"{load:.1f}% · {os.cpu_count() or 1} лог. CPU"
            data["percent"]["cpu"] = load
    except (OSError, ValueError, IndexError):
        pass
    try:
        info = {}
        for line in (proc/"meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("MemTotal","MemAvailable"):
                info[key] = int(value.split()[0])*1024
        total, available = info["MemTotal"],info["MemAvailable"]
        if 0 <= available <= total and total > 0:
            used = total-available
            data["memory"] = f"{used/2**30:.2f} / {total/2**30:.2f} ГиБ ({100*used/total:.0f}%)"
            data["percent"]["memory"] = round(100*used/total,1)
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        total, used, free = shutil.disk_usage(disk_path)
        if total > 0:
            data["disk"] = f"{used/2**30:.2f} / {total/2**30:.2f} ГиБ ({100*used/total:.0f}%)"
            data["percent"]["disk"] = round(100*used/total,1)
    except (OSError, ValueError):
        pass
    try:
        seconds = int(float((proc/"uptime").read_text().split()[0]))
        if seconds >= 0:
            days, rest = divmod(seconds,86400)
            hours, rest = divmod(rest,3600)
            data["uptime"] = f"{days} д. {hours} ч. {rest//60} мин."
    except (OSError, ValueError, IndexError, OverflowError):
        pass
    data["sampled_at"] = datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S МСК")
    return data


def dashboard_data(path, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MSK).date()
    def date_ago(n):
        return (today-timedelta(days=n)).isoformat()
    with closing(read_db(path)) as db:
        db.execute("BEGIN")
        def count(sql, args=()):
            return db.execute(sql,args).fetchone()[0]
        result = {
            "users_count": count("SELECT COUNT(*) FROM users"),
            "active_subs": count("SELECT COUNT(*) FROM subscriptions WHERE is_active=1 AND datetime(end_date)>datetime(?)",(now.isoformat(),)),
            "payments_count": count("SELECT COUNT(*) FROM payments WHERE status='paid' AND amount_rub>0"),
            "servers_count": count("SELECT COUNT(*) FROM servers"),
            "enabled_servers": count("SELECT COUNT(*) FROM servers WHERE is_active=1"),
            "pending_count": count("SELECT COUNT(*) FROM payments WHERE status='pending'"),
            "new_users_today": count("SELECT COUNT(*) FROM users WHERE date(created_at,'+3 hours')=?",(today.isoformat(),)),
        }
        # Cohorts by order creation date, not bank settlement or refund date.
        result["finance"] = []
        for label, days in (("Сегодня",0),("7 дней",6),("30 дней",29)):
            row = db.execute("""
                SELECT COALESCE(SUM(CASE WHEN p.status='paid' THEN p.amount_rub ELSE 0 END),0) AS gross,
                       COALESCE(SUM(CASE WHEN p.id>? AND datetime(p.created_at)>=datetime(?)
                         AND j.is_legacy=0 AND j.refund_state='full_done' THEN p.amount_rub ELSE 0 END),0) AS refunded
                FROM payments p LEFT JOIN payment_processing j USING(payment_id)
                WHERE p.amount_rub>0 AND date(p.created_at,'+3 hours') BETWEEN ? AND ?
            """,(LAST_HISTORICAL_ID,CUTOFF,date_ago(days),today.isoformat())).fetchone()
            result["finance"].append({"label":label,"gross":f"{row['gross']:.2f}","refunded":f"{row['refunded']:.2f}"})
        counts = dict(db.execute("""
            SELECT date(created_at,'+3 hours'), COUNT(*) FROM payments
            WHERE status='paid' AND amount_rub>0 AND date(created_at,'+3 hours') BETWEEN ? AND ?
            GROUP BY date(created_at,'+3 hours')
        """,(date_ago(6),today.isoformat())).fetchall())
        daily = [{"date":date_ago(i),"count":counts.get(date_ago(i),0)} for i in range(6,-1,-1)]
        peak = max(1,max(i["count"] for i in daily))
        for item in daily:
            item["height"] = round(100*item["count"]/peak)
        result["payments_daily"] = daily
        result["top_tariffs"] = [dict(row) for row in db.execute("""
            SELECT COALESCE(t.name,'Удалённый тариф') AS name, COUNT(*) AS count
            FROM subscriptions s LEFT JOIN tariffs t ON s.tariff_id=t.id
            WHERE s.is_active=1 AND datetime(s.end_date)>datetime(?)
            GROUP BY s.tariff_id,t.name ORDER BY count DESC,s.tariff_id LIMIT 10
        """,(now.isoformat(),))]
    return result


def server_targets(path):
    with closing(read_db(path)) as db:
        rows = [dict(row) for row in db.execute("SELECT id,name,url,api_token FROM servers WHERE is_active=1 ORDER BY id")]
        key = db.execute("SELECT value FROM settings WHERE key='encryption_key'").fetchone()
    return rows, key[0] if key else None


def token_clear(raw,key):
    # Match encryption.py's double-base64 format without importing its migration.
    if not isinstance(raw,str) or not raw.strip():
        raise ValueError("Missing token")
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        decoded = b""
    if decoded.startswith(b"gAAAA"):
        from cryptography.fernet import Fernet
        if not key:
            raise ValueError("Missing key")
        raw = Fernet(base64.urlsafe_b64decode(key)).decrypt(decoded).decode()
    elif raw.startswith(("gAAAA","ENC:","enc:")):
        raise ValueError("Unsupported token encoding")
    raw = raw.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw or len(raw)>4096 or not re.fullmatch(r"[!-~]+",raw):
        raise ValueError("Invalid token")
    return raw


async def probe_panel(client, row, key):
    try:
        parsed = urlsplit(row["url"])
        if (parsed.scheme!="https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return "Не проверен"
        token = token_clear(row["api_token"],key)
        async def fetch():
            async with client.stream("GET",row["url"].rstrip("/")+"/panel/api/inbounds/list",
                                     headers={"Authorization":"Bearer "+token,"Accept":"application/json"}) as resp:
                if resp.status_code != 200:
                    return "Не подтверждён"
                content=bytearray()
                async for part in resp.aiter_bytes():
                    content.extend(part)
                    if len(content)>8*1024*1024:
                        return "Не подтверждён"
                result=json.loads(content)
                if (isinstance(result,dict) and result.get("success") is True
                        and isinstance(result.get("obj"),list)):
                    return "API доступен"
                return "Не подтверждён"
        return await asyncio.wait_for(fetch(),8)
    except Exception:
        return "Не подтверждён"


async def probe_panel_metrics(client, row, key):
    """Read CPU, memory and disk for the host running this 3x-ui panel."""
    try:
        parsed = urlsplit(row["url"])
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return {"cpu": None, "memory": None, "disk": None}
        token = token_clear(row["api_token"], key)

        async def fetch():
            async with client.stream(
                "GET", row["url"].rstrip("/") + "/panel/api/server/status",
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    return {"cpu": None, "memory": None, "disk": None}
                content = bytearray()
                async for part in resp.aiter_bytes():
                    content.extend(part)
                    if len(content) > 1024 * 1024:
                        return {"cpu": None, "memory": None, "disk": None}
                result = json.loads(content)
                status = result.get("obj") if isinstance(result, dict) and result.get("success") is True else None
                if not isinstance(status, dict):
                    return {"cpu": None, "memory": None, "disk": None}

                def percent(section):
                    item = status.get(section)
                    if not isinstance(item, dict):
                        return None
                    current, total = item.get("current"), item.get("total")
                    if (isinstance(current, bool) or isinstance(total, bool)
                            or not isinstance(current, (int, float))
                            or not isinstance(total, (int, float))
                            or not math.isfinite(current) or not math.isfinite(total)
                            or total <= 0 or current < 0 or current > total):
                        return None
                    return round(100 * current / total, 1)

                cpu = status.get("cpu")
                if (isinstance(cpu, bool) or not isinstance(cpu, (int, float))
                        or not math.isfinite(cpu) or not 0 <= cpu <= 100):
                    cpu = None
                return {"cpu": cpu, "memory": percent("mem"), "disk": percent("disk")}

        return await asyncio.wait_for(fetch(), 6)
    except Exception:
        return {"cpu": None, "memory": None, "disk": None}


async def probe_panels(path):
    import httpx
    rows, key = await asyncio.to_thread(server_targets, path)
    limit = asyncio.Semaphore(32)
    async with httpx.AsyncClient(timeout=5, verify=True, follow_redirects=False, trust_env=False) as client:
        async def one(row):
            async with limit:
                state, metrics = await asyncio.gather(
                    probe_panel(client, row, key),
                    probe_panel_metrics(client, row, key),
                )
            return {"id": row["id"], "name": row["name"], "state": state, "metrics": metrics}
        statuses = await asyncio.gather(*(one(row) for row in rows))
    return {"panels": statuses, "truncated": False}
