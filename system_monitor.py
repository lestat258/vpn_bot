"""Independent read-only probes and durable incident transitions.

Only dedicated monitor tables are written. No retries after ambiguous Telegram
delivery. CPU uses counters over the interval, not a momentary sample.
"""
import asyncio
import logging
import math
import time
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from dashboard_metrics import local_metrics, cpu_ticks, cpu_percent, probe_panels, read_db
from tax_fulfillment import transaction
from payment_admin_alerts import send

LOG = logging.getLogger(__name__)
INTERVAL = 60
MAX_GAP = 150
LIMITS = {"cpu": (90, 80), "memory": (85, 80), "disk": (85, 80)}
LABELS = {"cpu": "CPU сервера админки", "memory": "Память сервера админки", "disk": "Диск сервера админки"}


def init_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS system_monitor_state (
        key TEXT PRIMARY KEY, label TEXT NOT NULL, active INTEGER NOT NULL,
        bad_since REAL, bad_count INTEGER NOT NULL, good_count INTEGER NOT NULL,
        checked_at REAL NOT NULL, value TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS system_monitor_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, label TEXT NOT NULL,
        kind TEXT NOT NULL, value TEXT NOT NULL, occurred_at REAL NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'pending'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS system_monitor_heartbeat (
        id INTEGER PRIMARY KEY CHECK(id=1), checked_at REAL NOT NULL
    )""")


def classify(key, value):
    if key.startswith("panel:"):
        if value == "API доступен":
            return "good"
        if value == "Не подтверждён":
            return "bad"
        return "unknown"
    if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 100:
        return "unknown"
    high, low = LIMITS[key]
    if (value >= high if key == "disk" else value > high):
        return "bad"
    if value <= low:
        return "good"
    return "neutral"


def record_cycle(path, observations, *, now, checked_panel_ids=None):
    """A transaction records state and event atomically. Unknown never recovers."""
    with transaction(path) as db:
        init_tables(db)
        for key,(label,value) in observations.items():
            old = db.execute("SELECT * FROM system_monitor_state WHERE key=?",(key,)).fetchone()
            active = bool(old and old["active"])
            continuous = bool(old and 0 < now-old["checked_at"] <= MAX_GAP)
            bad_since = old["bad_since"] if continuous else None
            bad_count = old["bad_count"] if continuous else 0
            good_count = old["good_count"] if continuous else 0
            status = classify(key,value)
            kind = None
            if status == "bad":
                if bad_since is None:
                    bad_since = now
                bad_count += 1
                good_count = 0
                threshold = (bad_count >= 3 if key.startswith("panel:") else
                             True if key == "disk" else now-bad_since >= 300)
                if not active and threshold:
                    active,kind = True,"opened"
            elif status == "good":
                bad_since,bad_count = None,0
                good_count += 1
                if active and good_count >= 2:
                    active,kind = False,"recovered"
            else:
                bad_since,bad_count,good_count = None,0,0
            display = (str(value) if key.startswith("panel:") else
                       f"{value:.1f}%" if status != "unknown" else "Нет данных")
            db.execute("""INSERT INTO system_monitor_state(key,label,active,bad_since,bad_count,good_count,checked_at,value)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                label=excluded.label,active=excluded.active,bad_since=excluded.bad_since,
                bad_count=excluded.bad_count,good_count=excluded.good_count,
                checked_at=excluded.checked_at,value=excluded.value""",
                (key,label,int(active),bad_since,bad_count,good_count,now,display))
            if kind:
                db.execute("INSERT INTO system_monitor_events(key,label,kind,value,occurred_at) VALUES (?,?,?,?,?)",
                           (key,label,kind,display,now))
        # Disabled or deleted servers leave monitoring scope, not a fake recovery.
        if checked_panel_ids is not None:
            for row in db.execute("SELECT key,label,active FROM system_monitor_state WHERE key LIKE 'panel:%'").fetchall():
                if row["key"] not in checked_panel_ids:
                    if row["active"]:
                        db.execute("INSERT INTO system_monitor_events(key,label,kind,value,occurred_at,delivery) VALUES (?,?,?,?,?,'not_required')",
                                   (row["key"],row["label"],"removed","Исключён из мониторинга",now))
                    db.execute("DELETE FROM system_monitor_state WHERE key=?",(row["key"],))
        db.execute("INSERT INTO system_monitor_heartbeat(id,checked_at) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at",(now,))


def claim_event(path):
    with transaction(path) as db:
        init_tables(db)
        setting=db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
        if not setting or not str(setting[0]).isdigit() or int(setting[0]) <= 0:
            return None
        event=db.execute("SELECT * FROM system_monitor_events WHERE delivery='pending' ORDER BY id LIMIT 1").fetchone()
        if event is None:
            return None
        db.execute("UPDATE system_monitor_events SET delivery='attempted' WHERE id=?",(event["id"],))
        return int(setting[0]),dict(event)


def finish_event(path, event_id, state):
    with transaction(path) as db:
        db.execute("UPDATE system_monitor_events SET delivery=? WHERE id=? AND delivery='attempted'",(state,event_id))


async def deliver(path, *, sender=None):
    # Bound the batch; a claimed send is never replayed even after a restart.
    for _ in range(10):
        claimed=await asyncio.to_thread(claim_event,path)
        if claimed is None:
            break
        recipient,event=claimed
        headline="⚠️ Проблема" if event["kind"]=="opened" else "✅ Восстановление"
        message=(f"<b>VPN4US Admin — {headline}</b>\n"
                 f"{escape(event['label'])}: {escape(event['value'])}\n"
                 "Подробности: раздел «Мониторинг» в админке.")
        try:
            await asyncio.wait_for((sender or send)(recipient,message),20)
        except Exception as exc:
            LOG.warning("Monitor notification not confirmed: %s",type(exc).__name__)
            state="unconfirmed"
        else:
            state="sent"
        await asyncio.to_thread(finish_event,path,event["id"],state)


async def sample(path, previous=None):
    # The CPU observation is valid only across a recent contiguous interval.
    try:
        ticks=await asyncio.to_thread(cpu_ticks,Path("/proc"))
        stamp=time.monotonic()
        current=(stamp,ticks)
        cpu=cpu_percent(previous[1],ticks) if previous and 30 <= stamp-previous[0] <= MAX_GAP else None
    except (OSError,ValueError,IndexError):
        current,cpu=None,None
    local=await asyncio.to_thread(local_metrics)
    obs={key:(LABELS[key],cpu if key=="cpu" else local["percent"][key]) for key in LIMITS}
    keys=None
    try:
        result=await probe_panels(path)
        observed=set()
        for panel in result["panels"]:
            key="panel:"+str(panel["id"])
            observed.add(key)
            obs[key]=("API 3x-ui · "+panel["name"],panel["state"])
        if not result["truncated"]:
            keys=observed
    except Exception as exc:
        LOG.warning("Monitor API snapshot unavailable: %s",type(exc).__name__)
    # A failed global probe must break consecutive evidence, not indicate recovery.
    with closing(read_db(path)) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_monitor_state'").fetchone()
        if exists and keys is None:
            for row in db.execute("SELECT key,label FROM system_monitor_state WHERE key LIKE 'panel:%'"):
                obs.setdefault(row["key"],(row["label"],"Не проверен"))
    return current,obs,keys


async def monitor_worker(path):
    previous=None
    while True:
        started=time.monotonic()
        try:
            previous,obs,keys=await sample(path,previous)
            await asyncio.to_thread(record_cycle,path,obs,now=time.time(),checked_panel_ids=keys)
        except Exception as exc:
            previous=None
            LOG.error("Monitor sample failed: %s",type(exc).__name__)
        await asyncio.sleep(max(1,INTERVAL-(time.monotonic()-started)))


async def delivery_worker(path):
    while True:
        try:
            await deliver(path)
        except Exception as exc:
            LOG.error("Monitor delivery worker failed: %s",type(exc).__name__)
        await asyncio.sleep(30)


def history(path, now=None):
    now=time.time() if now is None else now
    result={"heartbeat":None,"fresh":False,"states":[],"events":[]}
    with closing(read_db(path)) as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_monitor_heartbeat'").fetchone():
            return result
        row=db.execute("SELECT checked_at FROM system_monitor_heartbeat WHERE id=1").fetchone()
        result["heartbeat"]=row[0] if row else None
        result["fresh"]=bool(row and 0 <= now-row[0] <= MAX_GAP)
        result["states"]=[dict(row) for row in db.execute("SELECT label,active,value,checked_at FROM system_monitor_state ORDER BY key")]
        result["events"]=[dict(row) for row in db.execute("SELECT label,kind,value,occurred_at,delivery FROM system_monitor_events ORDER BY id DESC LIMIT 50")]
    return result
