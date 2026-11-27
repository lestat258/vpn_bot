"""Renewal through the identity-preserving 3x-ui v3.11 bulkAdjust API.
Never force enable. Direct panel-only blocks after expiry cannot be
distinguished from quota enforcement by this API version.
"""
import fcntl
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DAY_MS = 86400000


def renewal_days(old_ms, now_ms, paid_days):
    if type(paid_days) is not int or paid_days <= 0 or type(old_ms) is not int or old_ms <= 0:
        raise ValueError("Invalid renewal duration or expiry")
    return paid_days + (max(0, now_ms-old_ms)+DAY_MS-1)//DAY_MS


def client_state(api, email, uid, subid):
    reply=api._request("GET","/panel/api/clients/get/"+quote(email,safe=""))
    if not reply or reply.get("success") is not True:
        raise ValueError("Cannot read client")
    payload=reply["obj"]
    client=payload["client"]
    ids=payload.get("inboundIds")
    if (not uid or not subid or client.get("email")!=email
            or uid not in (client.get("uuid"),client.get("id"))
            or client.get("subId")!=subid or type(client.get("enable")) is not bool
            or type(client.get("expiryTime")) is not int
            or type(client.get("totalGB")) is not int or client["totalGB"]<0
            or not isinstance(ids,list) or not ids
            or any(type(i) is not int or i<=0 for i in ids) or len(set(ids))!=len(ids)):
        raise ValueError("Client identity or state mismatch")
    return dict(client),tuple(sorted(ids)),payload.get("usedTraffic")


def renew_subscription(path, subscription_id, paid_days, tariff_id, api_factory, *, clock=None):
    if type(subscription_id) is not int or subscription_id<=0 or type(paid_days) is not int or paid_days<=0:
        raise ValueError("Invalid subscription or duration")
    now_ms=clock or (lambda: time.time_ns()//1000000)
    lock_path=Path(path).with_name(f".renew-{subscription_id}.lock")
    with lock_path.open("a") as lock, closing(sqlite3.connect(path,timeout=15)) as db:
        fcntl.flock(lock,fcntl.LOCK_EX)
        row=db.execute("""
            SELECT s.end_date,s.xui_email,s.xui_client_uid,s.xui_subid,
                   v.url,v.api_token,s.is_active,s.managed_by_vpn4us,
                   u.is_blocked,v.is_active,s.server_id,s.telegram_id
              FROM subscriptions s JOIN servers v ON v.id=s.server_id
              JOIN users u ON u.telegram_id=s.telegram_id WHERE s.id=?
        """,(subscription_id,)).fetchone()
        if not row:
            raise ValueError("Subscription or owner missing")
        end,email,uid,subid,url,token,active,managed,blocked,server_active,server_id,owner=row
        if (active,managed,blocked,server_active)!=(1,1,0,1):
            raise ValueError("Subscription or owner administratively unavailable")
        expected=tuple(r[0] for r in db.execute("""
            SELECT inbound_id FROM subscription_inbound_memberships
             WHERE subscription_id=? AND server_id=? ORDER BY inbound_id
        """,(subscription_id,server_id)))
        api=api_factory(url=url,api_token=token)
        before,inbounds,used=client_state(api,email,uid,subid)
        old_ms=before["expiryTime"]
        local_end=datetime.fromisoformat(end)
        if old_ms<=0 or abs(old_ms-int(local_end.timestamp()*1000))>1 or inbounds!=expected:
            raise ValueError("Local and panel subscription require reconciliation")
        now=now_ms()
        if not before["enable"]:
            if old_ms>now:
                raise ValueError("Disabled unexpired client")
            total=before["totalGB"]
            if total and (type(used) is not int or not 0<=used<total):
                raise ValueError("Traffic depletion or unknown usage")
        adjustment=renewal_days(old_ms,now,paid_days)
        target_ms=old_ms+adjustment*DAY_MS
        reply=api._request("POST","/panel/api/clients/bulkAdjust",
                           {"emails":[email],"addDays":adjustment},retries=1)
        after,after_ids,_=client_state(api,email,uid,subid)
        if (not reply or reply.get("success") is not True
                or (reply.get("obj") or {}).get("adjusted")!=1
                or after["expiryTime"]!=target_ms or after["enable"] is not True
                or after_ids!=inbounds or after["totalGB"]!=before["totalGB"]):
            raise ValueError("Renewal not confirmed; manual reconciliation required")
        new_end=datetime.fromtimestamp(target_ms/1000,tz=local_end.tzinfo)
        with db:
            updated=db.execute("""
                UPDATE subscriptions SET end_date=?,tariff_id=COALESCE(?,tariff_id)
                 WHERE id=? AND end_date=? AND xui_client_uid=? AND xui_subid=?
                   AND server_id=? AND telegram_id=? AND is_active=1 AND managed_by_vpn4us=1
                   AND EXISTS(SELECT 1 FROM users WHERE telegram_id=? AND is_blocked=0)
                   AND EXISTS(SELECT 1 FROM servers WHERE id=? AND is_active=1)
            """,(new_end.isoformat(),tariff_id,subscription_id,end,uid,subid,
                 server_id,owner,owner,server_id))
            if updated.rowcount!=1:
                raise ValueError("Subscription changed during renewal; reconciliation required")
        return new_end


def expiry_presentation(end_date, *, now=None):
    end=datetime.fromisoformat(end_date)
    if now is None:
        now=datetime.now(end.tzinfo)
    seconds=(end-now).total_seconds()
    if seconds<=0:
        return False,end.strftime("%d.%m.%Y"),"Срок действия истёк"
    return True,end.strftime("%d.%m.%Y"),("Менее суток" if seconds<86400 else f"{int(seconds//86400)} дн.")
