#!/usr/bin/env python3
"""Read-only audit of bot-owned subscriptions vs per-server inbound policies."""
from runtime_config import DB_PATH
import argparse
from pathlib import Path
import sqlite3
from subscription_sync_plan import build_plan
from services.xui_client import XUIClient

DB = Path(DB_PATH)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server-id', type=int, default=None,
                        help='Optional numeric server ID; omit to check all configured servers')
    args = parser.parse_args()
    with sqlite3.connect(f'file:{DB.resolve()}?mode=ro', uri=True) as db:
        if args.server_id is None:
            rows = db.execute('''SELECT id,url,api_token FROM servers WHERE is_active=1
              AND id IN (SELECT server_id FROM server_inbound_policies) ORDER BY id''').fetchall()
        else:
            rows = db.execute('''SELECT id,url,api_token FROM servers WHERE id=?''',
                              (args.server_id,)).fetchall()
    if not rows:
        print('No configured active servers to inspect')
        return
    print('=== Stage 6A: READ-ONLY inbound synchronization plan ===')
    for server_id, url, token in rows:
        print('\nServer ID:', server_id)
        xui = None
        try:
            xui = XUIClient(url=url, api_token=token, verify_ssl=True)
            inbounds = xui.get_inbounds()
            plan = build_plan(DB, server_id, inbounds)
            print('Selected inbound IDs:', plan['desired_inbounds'])
            print('Below new-sales minimum:', plan['below_sales_minimum'])
            print('Owned active subscriptions:', plan['managed_subscriptions'])
            print('Safe-to-plan subscriptions:', plan['safe_to_plan'])
            print('Require manual review:', plan['needs_manual_review'])
            print('Would attach:', plan['would_add'])
            print('Would detach:', plan['would_remove'])
            print('Tracked disabled memberships preserved:', plan['retained_disabled'])
            print('Would prune deleted inbound IDs:', plan['would_prune_deleted'])
            print('Already present but not recorded:', plan['already_present_untracked'])
        except Exception as exc:
            # Avoid printing remote response bodies, URLs, emails, keys.
            print('STOP: unable to build plan:', type(exc).__name__)
            raise SystemExit(1) from None
        finally:
            if xui is not None:
                try:
                    xui.session.close()
                except Exception:
                    pass
    print('\nDONE: no writes to database or 3x-ui')


if __name__ == '__main__':
    main()
