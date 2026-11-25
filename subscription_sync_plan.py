"""Stage 6A: read-only planning for vpn4us-owned subscription inbound membership.

No API writes, no database changes. Zero subscriptions managed by this product -> zero client actions.
The plan is NOT an execution plan: identity, expiry, and 3x-ui write semantics
must be verified again immediately before any later mutating synchronizer is enabled.
"""
import json
import sqlite3
import uuid
from pathlib import Path
from inbound_policy import get_policy, validate_policy, select_matching_inbounds, InboundPolicyError


class SyncPlanningError(RuntimeError):
    pass


def select_existing(policy, inbounds):
    """Active eligible IDs for current clients, with no new-sales minimum.

    Disabled inbound membership is handled per owned client in build_plan():
    eligible disabled inbound IDs already tracked must be preserved but must
    NOT receive automatic client-add operations until re-enabled.
    """
    if not isinstance(inbounds, list) or not inbounds:
        raise SyncPlanningError('3x-ui did not return a valid inbound snapshot')
    try:
        return select_matching_inbounds(policy, inbounds, include_disabled=False)
    except InboundPolicyError as exc:
        raise SyncPlanningError('Invalid inbound policy or panel state') from exc


def index_inbounds(inbounds):
    """Returns {id: {email: client}}; fail closed on malformed panel data."""
    if not isinstance(inbounds, list) or not inbounds:
        raise SyncPlanningError('No inbound snapshot')
    result = {}
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise SyncPlanningError('Malformed inbound')
        try:
            iid = int(inbound['id'])
        except (KeyError, ValueError, TypeError) as exc:
            raise SyncPlanningError('Malformed inbound ID') from exc
        if iid <= 0 or iid in result or isinstance(inbound['id'], bool):
            raise SyncPlanningError('Duplicate/invalid inbound ID')
        settings = inbound.get('settings')
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (ValueError, TypeError) as exc:
                raise SyncPlanningError('Malformed inbound JSON') from exc
        if not isinstance(settings, dict):
            raise SyncPlanningError('Malformed inbound settings')
        raw_clients = settings.get('clients')
        if raw_clients is None and inbound.get('protocol') != 'vless':
            # Non-VLESS service inbounds can have no clients list.
            raw_clients = []
        if not isinstance(raw_clients, list):
            raise SyncPlanningError('No inbound client list for a VLESS inbound')
        clients = {}
        for client in raw_clients:
            if not isinstance(client, dict):
                raise SyncPlanningError('Malformed client record')
            email = client.get('email')
            if not isinstance(email, str) or not email.strip():
                continue
            if email in clients:
                raise SyncPlanningError('Duplicate client email')
            clients[email] = client
        result[iid] = clients
    return result


def _norm_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SyncPlanningError('Client UUID missing or invalid') from exc


def plan_subscription(subscription, tracked_ids, desired_ids, indexes, *, attachable_ids=None):
    """Pure, non-mutating plan for ONE explicitly owned subscription.

    subscription is a dict with id, xui_email, xui_client_uid, xui_subid.
    Plans removal only for inbound IDs explicitly recorded as bot-managed.
    """
    sid = int(subscription['id'])
    email = subscription['xui_email']
    if not isinstance(email, str) or not email:
        raise SyncPlanningError('Owned subscription has no email')
    uid = _norm_uuid(subscription['xui_client_uid'])
    subid = subscription['xui_subid']
    if not isinstance(subid, str) or not subid.strip():
        raise SyncPlanningError('Owned subscription has no confirmed SubID')
    if not isinstance(tracked_ids, set) or not tracked_ids:
        raise SyncPlanningError('Owned subscription lacks an initial managed inbound snapshot')
    desired = set(desired_ids)
    if not desired:
        raise SyncPlanningError('No desired active inbounds: do not mutate this client')
    current = {iid for iid, client_index in indexes.items() if email in client_index}
    for iid in current:
        client = indexes[iid][email]
        if _norm_uuid(client.get('id')) != uid or client.get('subId') != subid:
            raise SyncPlanningError('Identity conflict across inbound records')
        if client.get('enable') is not True:
            raise SyncPlanningError('Client record disabled in an inbound')
    unexpected = current - (tracked_ids | desired)
    if unexpected:
        raise SyncPlanningError('Client has unmanaged inbound membership: manual review')
    # Existing disabled inbound membership is retained; never try to add a
    # missing client to a disabled inbound based solely on its group marker.
    attachable = set(desired if attachable_ids is None else attachable_ids)
    if not attachable.issubset(desired):
        raise SyncPlanningError('Attachable inbounds are not a subset of desired membership')
    new = sorted((desired & attachable) - current)
    remove = sorted((tracked_ids - desired) & current)
    already_present_untracked = sorted((desired & current) - tracked_ids)
    stale_deleted = sorted(tracked_ids - set(indexes))
    return {
        'subscription_id': sid,
        'add': new,
        'remove': remove,
        'already_present_untracked': already_present_untracked,
        'prune_deleted_metadata': stale_deleted,
        'unchanged': sorted(current & desired),
        'retained_disabled': sorted((tracked_ids & desired) - attachable),
    }


def build_plan(db_path, server_id, inbounds):
    """Read-only DB and panel snapshot. Only our managed rows are examined."""
    db = sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        policy = get_policy(db_path, server_id)
        if policy is None:
            raise SyncPlanningError('No policy for server')
        desired = select_existing(policy, inbounds)
        try:
            eligible_any = set(select_matching_inbounds(policy, inbounds, include_disabled=True))
        except InboundPolicyError as exc:
            raise SyncPlanningError('Invalid inbound policy or panel state') from exc
        snapshot_by_id = {int(ib['id']): ib for ib in inbounds if isinstance(ib, dict)}
        if any(snapshot_by_id[i].get('protocol') != 'vless' for i in eligible_any):
            raise SyncPlanningError('Selected inbound protocol is not yet supported by this planner')
        index = index_inbounds(inbounds)
        rows = db.execute('''
          SELECT id, xui_email, xui_client_uid, xui_subid
          FROM subscriptions
          WHERE managed_by_vpn4us=1 AND server_id=? AND is_active=1
          ORDER BY id
        ''', (server_id,)).fetchall()
        output = []
        failed = 0
        for record in rows:
            sid = int(record['id'])
            tracked = {int(r[0]) for r in db.execute('''
                SELECT inbound_id FROM subscription_inbound_memberships
                WHERE subscription_id=? AND server_id=?
            ''', (sid, server_id))}
            try:
                # Only active eligible inbounds can receive new clients.
                # Retain disabled ones that were ALREADY managed by our bot,
                # provided group/manual/all rules still permit membership.
                held_disabled = tracked & (eligible_any - set(desired))
                per_client_desired = sorted(set(desired) | held_disabled)
                output.append(plan_subscription(
                    dict(record), tracked, per_client_desired, index,
                    attachable_ids=set(desired)
                ))
            except (SyncPlanningError, ValueError, TypeError, KeyError):
                failed += 1
        min_sales = validate_policy(policy)['minimum_count']
        return {
            'server_id': server_id,
            'policy_mode': policy['mode'],
            'desired_inbounds': desired,
            'below_sales_minimum': len(desired) < min_sales,
            'managed_subscriptions': len(rows),
            'safe_to_plan': len(output),
            'needs_manual_review': failed,
            'would_add': sum(len(row['add']) for row in output),
            'would_remove': sum(len(row['remove']) for row in output),
            'would_prune_deleted': sum(len(row['prune_deleted_metadata']) for row in output),
            'already_present_untracked': sum(len(row['already_present_untracked']) for row in output),
            'retained_disabled': sum(len(row['retained_disabled']) for row in output),
            'details': output,
        }
    finally:
        db.close()
