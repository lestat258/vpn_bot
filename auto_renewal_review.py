"""Read-only inspection and explicit operator reconciliation of auto-renewals.

A GET showing the old expiry is NOT proof that a timed-out POST cannot arrive
later. Refund requires an operator's independently verified finality evidence.
No remote mutation or payment-provider call is made here.
"""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from hashlib import sha256

from auto_renewal import ensure_review_schema, resolution_filter, expiry_ms
from balance_ledger import apply_operation, transaction
from subscription_renewal import client_state, renewal_lock


def _exists(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def pending_ids(path):
    with closing(sqlite3.connect(path)) as db:
        if not _exists(db, "auto_renewal_operations"):
            return []
        return [r[0] for r in db.execute(
            "SELECT operation_id FROM auto_renewal_operations WHERE state IN ('reserved','needs_review')"
            + resolution_filter(db) + " ORDER BY created_at,operation_id")]


def _facts(db, operation_id):
    db.row_factory=sqlite3.Row
    if not _exists(db,"auto_renewal_evidence"):
        raise ValueError("Original panel evidence missing")
    op=db.execute("SELECT * FROM auto_renewal_operations WHERE operation_id=?",(operation_id,)).fetchone()
    ev=db.execute("SELECT * FROM auto_renewal_evidence WHERE operation_id=?",(operation_id,)).fetchone()
    if not op or not ev or op["state"] not in ("reserved","needs_review"):
        raise ValueError("Operation or original evidence unavailable")
    if _exists(db,"auto_renewal_resolutions") and db.execute(
        "SELECT 1 FROM auto_renewal_resolutions WHERE operation_id=?",(operation_id,)).fetchone():
        raise ValueError("Already resolved")
    sub=db.execute("""SELECT s.telegram_id,s.tariff_id,s.end_date,s.server_id,s.xui_email,
        s.xui_client_uid,s.xui_subid,s.is_active,s.managed_by_vpn4us,v.url,v.api_token,
        v.is_active AS server_active,u.is_blocked
        FROM subscriptions s JOIN servers v ON v.id=s.server_id
        JOIN users u ON u.telegram_id=s.telegram_id WHERE s.id=?""",(op["subscription_id"],)).fetchone()
    if not sub or tuple(sub[k] for k in ("telegram_id","tariff_id","end_date","server_id","xui_client_uid","xui_subid")) != tuple(
        op[k] for k in ("telegram_id","tariff_id","original_end","server_id","client_uid","client_subid")):
        raise ValueError("Local subscription differs")
    if (sub["is_active"],sub["managed_by_vpn4us"],sub["server_active"],sub["is_blocked"])!=(1,1,1,0):
        raise ValueError("Administrative state changed")
    if ev["email"]!=sub["xui_email"] or ev["server_hash"]!=sha256(sub["url"].encode()).hexdigest():
        raise ValueError("Panel address or client email changed")
    ids=tuple(r[0] for r in db.execute("""SELECT inbound_id FROM subscription_inbound_memberships
        WHERE subscription_id=? AND server_id=? ORDER BY inbound_id""",(op["subscription_id"],op["server_id"])))
    if not ids or ids!=tuple(json.loads(ev["inbounds"])):
        raise ValueError("Membership changed")
    if abs(expiry_ms(op["original_end"])-ev["original_ms"])>1 or op["target_ms"]<=ev["original_ms"]:
        raise ValueError("Expiry evidence inconsistent")
    debit=db.execute("SELECT * FROM balance_operations WHERE operation_id=?",(operation_id,)).fetchone()
    if (not debit or debit["telegram_id"]!=op["telegram_id"] or debit["kind"]!="auto_renew_reserve"
        or debit["delta_kopecks"]!=-op["amount_kopecks"] or debit["actor_id"] is not None
        or debit["payment_id"] is not None or debit["before_kopecks"]+debit["delta_kopecks"]!=debit["after_kopecks"]):
        raise ValueError("Balance reservation inconsistent")
    return dict(op),dict(ev),dict(sub),ids


def _subscription_id(path, operation_id):
    with closing(sqlite3.connect(path)) as db:
        if not _exists(db,"auto_renewal_operations"):
            raise ValueError("Journal unavailable")
        row=db.execute("SELECT subscription_id FROM auto_renewal_operations WHERE operation_id=?",(operation_id,)).fetchone()
        if not row: raise ValueError("Operation unavailable")
        return row[0]


def _observe(facts, api_factory):
    op,ev,sub,ids=facts
    api=api_factory(url=sub["url"],api_token=sub["api_token"])
    panel,actual,_=client_state(api,ev["email"],op["client_uid"],op["client_subid"])
    expiry=panel["expiryTime"]
    status="unknown"
    if actual==ids and panel["totalGB"]==ev["total_gb"]:
        if expiry==op["target_ms"] and panel["enable"] is True:
            status="target_observed"
        elif expiry==ev["original_ms"] and int(panel["enable"])==ev["enabled"]:
            status="original_observed"
    return status,expiry


def inspect(path, operation_id, api_factory):
    sid=_subscription_id(path,operation_id)
    with renewal_lock(path,sid),closing(sqlite3.connect(path)) as db:
        facts=_facts(db,operation_id)
        status,observed=_observe(facts,api_factory)
        op=facts[0]
        return {"operation_id":operation_id,"subscription_id":sid,"amount_kopecks":op["amount_kopecks"],
                "status":status,"observed_ms":observed,"target_ms":op["target_ms"]}


def resolve(path, operation_id, outcome, actor, evidence, api_factory, *, verified_finality=False):
    """Explicit local-only decision. Caller must coordinate external panel edits.

    No refund on GET alone: the operator must establish final failure and
    attest that the original POST cannot still execute.
    """
    if outcome not in ("confirmed","refunded") or type(actor) is not int or actor<=0:
        raise ValueError("Invalid decision")
    if not isinstance(evidence,str) or not 20<=len(evidence.strip())<=500:
        raise ValueError("Document independent evidence (20-500 characters, no secrets)")
    if verified_finality is not True:
        raise ValueError("Independent evidence of final outcome required")
    sid=_subscription_id(path,operation_id)
    with renewal_lock(path,sid):
        with closing(sqlite3.connect(path)) as db:
            facts=_facts(db,operation_id)
            allowed=db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
            if not allowed or str(actor)!=allowed[0]:
                raise ValueError("Operator identity mismatch")
        status,observed=_observe(facts,api_factory)
        if status!=("target_observed" if outcome=="confirmed" else "original_observed"):
            raise ValueError("Panel outcome does not support decision")
        with transaction(path) as db:
            ensure_review_schema(db)
            allowed=db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
            if not allowed or str(actor)!=allowed[0] or _facts(db,operation_id)!=facts:
                raise ValueError("Local facts changed during inspection")
            op=facts[0]
            if outcome=="confirmed":
                local_end=datetime.fromisoformat(op["original_end"])
                new_end=datetime.fromtimestamp(op["target_ms"]/1000,tz=local_end.tzinfo).isoformat()
                db.execute("UPDATE subscriptions SET end_date=? WHERE id=?",(new_end,sid))
                db.execute("UPDATE auto_renewal_operations SET state='done',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                           (operation_id,))
            else:
                _,changed=apply_operation(db,"auto-refund:"+operation_id,op["telegram_id"],
                                         "auto_renew_refund",op["amount_kopecks"],actor=actor)
                if not changed:
                    raise ValueError("Duplicate refund")
                # Do not retry the same cycle or silently re-enable spending.
                db.execute("UPDATE subscriptions SET auto_renew=0 WHERE id=?",(sid,))
                db.execute("UPDATE auto_renewal_operations SET state='needs_review',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                           (operation_id,))
            db.execute("""INSERT INTO auto_renewal_resolutions(operation_id,outcome,actor_id,evidence,observed_ms)
                VALUES(?,?,?,?,?)""",(operation_id,outcome,actor,evidence.strip(),observed))
    return outcome


def main():
    from runtime_config import DB_PATH
    from xui_client import XUIClient
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("list")
    i=sub.add_parser("inspect")
    i.add_argument("operation_id")
    r=sub.add_parser("resolve")
    r.add_argument("operation_id")
    r.add_argument("outcome",choices=("confirmed","refunded"))
    r.add_argument("--actor",type=int,required=True)
    r.add_argument("--evidence",required=True)
    r.add_argument("--verified-finality",action="store_true")
    args=p.parse_args()
    if args.command=="list":
        result=pending_ids(DB_PATH)
    elif args.command=="inspect":
        result=inspect(DB_PATH,args.operation_id,XUIClient)
    else:
        result=resolve(DB_PATH,args.operation_id,args.outcome,args.actor,args.evidence,XUIClient,
                       verified_finality=args.verified_finality)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
