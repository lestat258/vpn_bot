"""Per-server fail-closed inbound selection. No network access or Xray side effects.

Version 1 intentionally controls selection only. VPNService must be integrated and
its multi-inbound fulfillment verified separately before real user activation.
"""
import json
import re
import sqlite3
from pathlib import Path
from contextlib import closing

GROUP_MARKER = re.compile(r'--([0-9]+)(?=--|$)')
VALID_MODES = ('group', 'manual', 'all')


class InboundPolicyError(ValueError):
    pass


def ensure_schema(db_path):
    with closing(sqlite3.connect(str(db_path), timeout=15)) as con, con:
        con.execute('''
            CREATE TABLE IF NOT EXISTS server_inbound_policies (
                server_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('group','manual','all')),
                group_number INTEGER,
                inbound_ids TEXT NOT NULL DEFAULT '[]',
                minimum_count INTEGER NOT NULL DEFAULT 1
                    CHECK(minimum_count BETWEEN 1 AND 50),
                allow_all INTEGER NOT NULL DEFAULT 0 CHECK(allow_all IN (0,1)),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
            )''')


def get_policy(db_path, server_id):
    with closing(sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)) as con:
        try:
            record = con.execute('''
                SELECT mode, group_number, inbound_ids, minimum_count, allow_all
                FROM server_inbound_policies WHERE server_id=?''', (int(server_id),)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc).lower():
                return None
            raise
    if not record:
        return None
    mode, group_number, serialized, minimum_count, allow_all = record
    try:
        ids = json.loads(serialized)
    except (ValueError, TypeError) as exc:
        raise InboundPolicyError('Некорректная конфигурация inbound') from exc
    return {
        'mode': mode, 'group_number': group_number,
        'inbound_ids': ids, 'minimum_count': minimum_count,
        'allow_all': bool(allow_all),
    }


def _id(value):
    if isinstance(value, bool):
        raise InboundPolicyError('Некорректный ID inbound')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InboundPolicyError('Некорректный ID inbound') from exc
    if result <= 0 or str(result) != str(value):
        raise InboundPolicyError('Некорректный ID inbound')
    return result


def validate_policy(policy):
    if not isinstance(policy, dict) or policy.get('mode') not in VALID_MODES:
        raise InboundPolicyError('Режим выбора inbound не задан')
    mode = policy['mode']
    try:
        minimum = int(policy.get('minimum_count', 1))
    except (ValueError, TypeError) as exc:
        raise InboundPolicyError('Некорректное минимальное количество inbound') from exc
    if minimum < 1 or minimum > 50:
        raise InboundPolicyError('Минимальное количество должно быть от 1 до 50')
    group = policy.get('group_number')
    ids = policy.get('inbound_ids', [])
    if not isinstance(ids, list):
        raise InboundPolicyError('Неверный список выбранных inbound')
    ids = [_id(i) for i in ids]
    if len(ids) != len(set(ids)):
        raise InboundPolicyError('Повторяющиеся inbound ID')
    if mode == 'group':
        try:
            group = int(group)
        except (ValueError, TypeError) as exc:
            raise InboundPolicyError('Номер группы не задан') from exc
        if group < 1 or group > 9999:
            raise InboundPolicyError('Номер группы: 1–9999')
    if mode == 'manual' and (not ids or len(ids) < minimum):
        raise InboundPolicyError('Выберите необходимое количество inbound')
    if mode == 'all' and policy.get('allow_all') is not True:
        raise InboundPolicyError('Режим «все» требует явного подтверждения')
    return {
        'mode': mode,
        'group_number': group if mode == 'group' else None,
        'inbound_ids': ids if mode == 'manual' else [],
        'minimum_count': minimum,
        'allow_all': mode == 'all' and policy.get('allow_all') is True,
    }


def select_matching_inbounds(policy, inbounds, *, include_disabled=False):
    """Policy-matching inbound IDs, independent of new-sales minimum.

    --! at the START of the inbound's 3x-ui remark excludes it from every
    mode (including explicit manual/all). When include_disabled=True this
    returns administratively eligible IDs even if temporarily disabled.
    Neither mode changes memberships; only VLESS is presently provisionable.
    """
    p = validate_policy(policy)
    if not isinstance(inbounds, list):
        raise InboundPolicyError('Панель не вернула список inbound')
    seen = set()
    selected = []
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise InboundPolicyError('Некорректный inbound в ответе панели')
        iid = _id(inbound.get('id'))
        if iid in seen:
            raise InboundPolicyError('Повторяющиеся ID в ответе 3x-ui')
        seen.add(iid)
        enabled = inbound.get('enable')
        if enabled is not True and enabled is not False:
            raise InboundPolicyError('Некорректное состояние inbound')
        remark = inbound.get('remark')
        if remark is None:
            remark = ''
        if not isinstance(remark, str):
            raise InboundPolicyError('Некорректный Remark inbound')
        if remark.startswith('--!'):
            continue
        if not include_disabled and enabled is not True:
            continue
        if p['mode'] == 'manual':
            eligible = iid in set(p['inbound_ids'])
        elif p['mode'] == 'all':
            eligible = True
        else:
            tag = inbound.get('tag') or ''
            eligible = (isinstance(tag, str) and
                        str(p['group_number']) in GROUP_MARKER.findall(tag))
        if eligible:
            selected.append(iid)
    return selected


def select_inbounds(policy, inbounds):
    """Strict ACTIVE selection for new subscriptions; enforce sales minimum."""
    p = validate_policy(policy)
    selected = select_matching_inbounds(p, inbounds, include_disabled=False)
    if p['mode'] == 'manual' and len(selected) != len(p['inbound_ids']):
        raise InboundPolicyError('Часть выбранных inbound отсутствует, исключена или отключена')
    if len(selected) < p['minimum_count']:
        raise InboundPolicyError('Недостаточно доступных inbound по выбранному правилу')
    return selected


def save_policy(db_path, server_id, policy):
    p = validate_policy(policy)
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=15)) as con, con:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        found = con.execute('SELECT 1 FROM servers WHERE id=?', (int(server_id),)).fetchone()
        if not found:
            raise InboundPolicyError('Сервер не найден')
        con.execute('''
            INSERT INTO server_inbound_policies
              (server_id, mode, group_number, inbound_ids, minimum_count, allow_all)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
              mode=excluded.mode, group_number=excluded.group_number,
              inbound_ids=excluded.inbound_ids, minimum_count=excluded.minimum_count,
              allow_all=excluded.allow_all, updated_at=CURRENT_TIMESTAMP''',
            (int(server_id), p['mode'], p['group_number'],
             json.dumps(p['inbound_ids']), p['minimum_count'], int(p['allow_all'])))
    return p


def select_for_server(db_path, server_id, current_inbounds):
    policy = get_policy(db_path, server_id)
    return select_inbounds(policy, current_inbounds)
