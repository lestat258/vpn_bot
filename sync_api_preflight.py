#!/usr/bin/env python3
"""Stage 6C, API compatibility and owned-client scope preflight.

READ-ONLY. No API mutation methods and no SQLite writes. This tool does NOT
install or enable automatic synchronization.
"""
import argparse
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data.db'


class PreflightError(RuntimeError):
    pass


def normalize_path(value):
    if not isinstance(value, str) or not value.startswith('/'):
        raise PreflightError('Malformed OpenAPI path')
    return re.sub(r'(?::email|\{email\})', '{email}', value).rstrip('/')


# The 3x-ui v3.11 API has explicit per-client attach/detach methods.
REQUIRED_METHODS = {
    ('get', '/panel/api/clients/get/{email}'): 'fetch client',
    ('post', '/panel/api/clients/{email}/attach'): 'attach inbounds',
    ('post', '/panel/api/clients/{email}/detach'): 'detach inbounds',
}


def inspect_openapi(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('paths'), dict):
        raise PreflightError('Unexpected OpenAPI response structure')
    routes = set()
    for path, methods in spec['paths'].items():
        if not isinstance(methods, dict):
            continue
        normalized = normalize_path(path)
        for method in methods:
            if method.lower() in {'get', 'post', 'put', 'patch', 'delete'}:
                routes.add((method.lower(), normalized))
    return {description: (route in routes)
            for route, description in REQUIRED_METHODS.items()}


def validate_panel_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise PreflightError('Server URL must be a credential-free HTTPS URL')
    if parts.query or parts.fragment:
        raise PreflightError('Panel URL must not have query or fragment')
    return url.rstrip('/') + '/panel/api/openapi.json'


def load_servers(db_path, chosen_server):
    if not db_path.is_file():
        raise PreflightError('Database file missing')
    with sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True) as db:
        if chosen_server is None:
            rows = db.execute('''SELECT id, url, api_token FROM servers
                WHERE is_active=1 AND id IN (
                  SELECT server_id FROM server_inbound_policies
                ) ORDER BY id''').fetchall()
        else:
            rows = db.execute('''SELECT id, url, api_token FROM servers
                WHERE id=? AND is_active=1''', (chosen_server,)).fetchall()
        return rows


def owned_scope_status(db_path, server_id):
    with sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True) as db:
        owned = db.execute('''SELECT COUNT(*) FROM subscriptions
            WHERE managed_by_vpn4us=1 AND server_id=?''', (server_id,)).fetchone()[0]
        incomplete = db.execute('''SELECT COUNT(*) FROM subscriptions
            WHERE managed_by_vpn4us=1 AND server_id=? AND (
              xui_email IS NULL OR TRIM(xui_email)='' OR
              xui_client_uid IS NULL OR TRIM(xui_client_uid)='' OR
              xui_subid IS NULL OR TRIM(xui_subid)='' OR is_active IS NULL
            )''', (server_id,)).fetchone()[0]
        duplicate = db.execute('''SELECT COUNT(*) FROM (
          SELECT xui_email FROM subscriptions
          WHERE managed_by_vpn4us=1 AND server_id=?
          GROUP BY xui_email HAVING COUNT(*)>1
        )''', (server_id,)).fetchone()[0]
        missing_memberships = db.execute('''SELECT COUNT(*) FROM subscriptions s
          WHERE s.managed_by_vpn4us=1 AND s.server_id=? AND s.is_active=1
            AND NOT EXISTS (
              SELECT 1 FROM subscription_inbound_memberships m
              WHERE m.subscription_id=s.id AND m.server_id=s.server_id
            )''', (server_id,)).fetchone()[0]
        return {
            'owned': owned,
            'incomplete': incomplete,
            'duplicate': duplicate,
            'missing_memberships': missing_memberships,
        }


def main():
    ap = argparse.ArgumentParser(description='Read-only 3x-ui synchronization API preflight')
    ap.add_argument('--server-id', type=int, default=None)
    args = ap.parse_args()
    if args.server_id is not None and args.server_id < 1:
        ap.error('Server ID must be positive')

    from services.xui_client import XUIClient
    from subscription_sync_plan import build_plan

    try:
        rows = load_servers(DB, args.server_id)
    except (sqlite3.Error, PreflightError):
        raise SystemExit('STOP: unable to read configured servers') from None
    if not rows:
        raise SystemExit('STOP: no active servers with an inbound policy')

    print('=== Stage 6C: READ-ONLY 3x-ui preflight ===')
    for server_id, url, token in rows:
        print('\nServer ID:', server_id)
        xui = None
        try:
            api_url = validate_panel_url(url)
            scope = owned_scope_status(DB, server_id)
            print('vpn4us-owned subscriptions:', scope['owned'])
            print('Incomplete owned identities:', scope['incomplete'])
            print('Duplicate owned emails:', scope['duplicate'])
            print('Active owned rows without membership:', scope['missing_memberships'])
            if any(scope[k] for k in ('incomplete', 'duplicate', 'missing_memberships')):
                raise PreflightError('Owned-subscription inventory needs manual review')

            xui = XUIClient(url=url, api_token=token, verify_ssl=True)
            response = xui.session.get(api_url, timeout=12, verify=True, allow_redirects=False)
            print('OpenAPI HTTP status:', response.status_code)
            if response.status_code != 200:
                raise PreflightError('OpenAPI documentation not accessible')
            support = inspect_openapi(response.json())
            for method, ok in support.items():
                print('API', method + ':', 'YES' if ok else 'NO')
            if not all(support.values()):
                raise PreflightError('Required 3x-ui attach/detach API is not fully documented')
            inbounds = xui.get_inbounds()
            plan = build_plan(DB, server_id, inbounds)
            print('Policy inbound IDs:', plan['desired_inbounds'])
            print('Owned active subscriptions:', plan['managed_subscriptions'])
            print('Require manual review:', plan['needs_manual_review'])
            print('Would attach:', plan['would_add'])
            print('Would detach:', plan['would_remove'])
            print('Would prune deleted IDs:', plan['would_prune_deleted'])
            if plan['needs_manual_review']:
                raise PreflightError('Owned subscriptions require manual review')
            print('Preflight status: PASS')
        except Exception as exc:
            # No exception text: remote responses can contain sensitive details.
            print('Preflight status: STOP (' + type(exc).__name__ + ')')
            raise SystemExit(1) from None
        finally:
            if xui is not None:
                try:
                    xui.session.close()
                except Exception:
                    pass
    print('\nDONE: no changes to 3x-ui, SQLite, services, or subscriptions')


if __name__ == '__main__':
    main()
