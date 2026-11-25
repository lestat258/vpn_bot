"""Process *independently verified* YooKassa payments into VPN subscriptions.

Only call this from the webhook after fetching the payment from YooKassa API
and verifying succeeded/paid, amount, currency and metadata. This module never
verifies the original payment itself, never creates tax receipts and never
retries external effects whose outcomes may be uncertain.
"""
import asyncio
import logging
import sqlite3
from pathlib import Path

from payment_ledger import PaymentLedger

LOG = logging.getLogger(__name__)
DEFAULT_DB = '/opt/vpn-bot/data.db'


def _load_payment(db_path, payment_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('''
            SELECT p.telegram_id, p.tariff_id, t.name, t.duration_days,
                   t.traffic_gb, t.ip_limit
              FROM payments p JOIN tariffs t ON t.id=p.tariff_id
             WHERE p.payment_id=?
        ''', (payment_id,)).fetchone()
        return dict(row) if row else None


def _owned_subscription(db_path, sub_id, telegram_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        row = db.execute(
            'SELECT id FROM subscriptions WHERE id=? AND telegram_id=?',
            (sub_id, telegram_id),
        ).fetchone()
        return int(row[0]) if row else None


def _active_subscription(db_path, telegram_id):
    with sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True) as db:
        row = db.execute('''
            SELECT id FROM subscriptions WHERE telegram_id=? AND is_active=1
             ORDER BY end_date DESC, id DESC LIMIT 1
        ''', (telegram_id,)).fetchone()
        return int(row[0]) if row else None


def _strict_positive_int(value):
    if isinstance(value, bool):
        raise ValueError('Invalid subscription ID')
    result = int(value)
    if result <= 0 or str(result) != str(value):
        raise ValueError('Invalid subscription ID')
    return result


async def _telegram_notify(telegram_id, text):
    from aiogram import Bot
    from database import get_setting
    raw_token = get_setting('bot_token')
    if not raw_token:
        raise RuntimeError('Telegram bot token is not configured')
    try:
        from encryption import decrypt
        token = decrypt(raw_token)
    except Exception:
        token = raw_token
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
    finally:
        await bot.session.close()


async def process_verified_payment(payment_id, verified_data, *, db_path=DEFAULT_DB,
                                   vpn_api=None, notifier=None):
    """Return True only for completed/already-completed VPN fulfillment.

    `verified_data` MUST originate from the provider API, never directly from
    the incoming untrusted webhook JSON. Test with injected vpn_api/notifier.
    """
    if vpn_api is None:
        from services import vpn_service
        vpn_api = vpn_service
    if notifier is None:
        notifier = _telegram_notify

    try:
        obj = verified_data['object']
        metadata = obj['metadata']
        if not isinstance(metadata, dict) or str(obj['id']) != str(payment_id):
            LOG.error('Verified payment payload invalid')
            return False
        details = await asyncio.to_thread(_load_payment, db_path, payment_id)
        if not details:
            LOG.error('Payment/tariff not found for verified payment')
            return False
        telegram_id = int(details['telegram_id'])
        tariff_id = int(details['tariff_id'])
        if str(metadata.get('telegram_id')) != str(telegram_id) or str(metadata.get('tariff_id')) != str(tariff_id):
            LOG.error('Verified payment metadata does not match local order')
            return False
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        LOG.error('Invalid verified payment: %s', type(exc).__name__)
        return False

    ledger = PaymentLedger(db_path)
    state = await asyncio.to_thread(ledger.claim, payment_id)
    if state == 'done':
        return True
    if state != 'claimed':
        LOG.warning('Payment processing not started; ledger state=%s', state)
        return False

    try:
        duration = int(details['duration_days'])
        traffic = float(details['traffic_gb'] or 0)
        ip_limit = int(details['ip_limit'] or 3)
        action = metadata.get('action') or 'new_key'
        result = None
        sub_id = None
        link = None

        if action == 'new_key':
            result = await vpn_api.create_subscription(
                telegram_id=telegram_id, tariff_id=tariff_id,
                days=duration, traffic_gb=traffic, ip_limit=ip_limit,
                allow_additional_key=metadata.get("action") == "new_key",
            )
            if not isinstance(result, dict) or result.get('success') is not True:
                raise RuntimeError('VPN create returned failure or unknown result')
            sub_id = _strict_positive_int(result.get('subscription_id'))
            link = result.get('link')
        else:
            explicit_id = metadata.get('subscription_id')
            if explicit_id not in (None, ''):
                requested = _strict_positive_int(explicit_id)
                sub_id = await asyncio.to_thread(
                    _owned_subscription, db_path, requested, telegram_id
                )
                if sub_id is None:
                    raise RuntimeError('Requested subscription is not owned by payer')
            else:
                sub_id = await asyncio.to_thread(
                    _active_subscription, db_path, telegram_id
                )
            if sub_id is not None:
                result = await vpn_api.extend_subscription(
                    subscription_id=sub_id, days_to_add=duration, tariff_id=tariff_id
                )
                if not isinstance(result, dict) or result.get('success') is not True:
                    raise RuntimeError('VPN renewal returned failure or unknown result')
            else:
                result = await vpn_api.create_subscription(
                    telegram_id=telegram_id, tariff_id=tariff_id,
                    days=duration, traffic_gb=traffic, ip_limit=ip_limit,
                )
                if not isinstance(result, dict) or result.get('success') is not True:
                    raise RuntimeError('VPN create on renewal returned failure or unknown result')
                sub_id = _strict_positive_int(result.get('subscription_id'))
                link = result.get('link')

        # Commit completion BEFORE any Telegram notification. A notification
        # failure must never result in a second VPN subscription/renewal.
        await asyncio.to_thread(ledger.complete, payment_id, sub_id)

    except Exception as exc:
        LOG.error('VPN outcome requires reconciliation: %s', type(exc).__name__)
        try:
            await asyncio.to_thread(ledger.mark_review, payment_id, type(exc).__name__)
        except Exception:
            LOG.exception('Unable to mark payment for manual review')
        return False

    # Notification is a separate, best-effort effect (never repeat VPN).
    try:
        if not link:
            link = await vpn_api.get_subscription_link(sub_id)
        text = '✅ Оплата подтверждена. VPN-подписка активирована.'
        if link:
            text += '\n\nСсылка на подписку:\n' + str(link)
        await notifier(telegram_id, text)
    except Exception:
        LOG.warning('VPN delivered but user notification failed; requires follow-up')
    return True
