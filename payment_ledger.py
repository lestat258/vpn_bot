"""SQLite state machine for exactly-once *claims*, not guaranteed exactly-once external effects.

Call claim() only after independent YooKassa verification.  Never automatically
retry an externally attempted VPN action whose outcome is uncertain.
"""
from runtime_config import DB_PATH
import sqlite3
from contextlib import contextmanager


class PaymentLedger:
    def __init__(self, path=DB_PATH):
        self.path = path

    @contextmanager
    def _tx(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def claim(self, payment_id):
        """Returns claimed, done, held, missing, or legacy."""
        with self._tx() as db:
            pay = db.execute(
                'SELECT status, amount_rub FROM payments WHERE payment_id=?',
                (payment_id,)
            ).fetchone()
            if pay is None:
                return 'missing'
            if pay['amount_rub'] is None or pay['amount_rub'] <= 0:
                return 'held'  # Free trials do not pass through YooKassa.
            existing = db.execute('''
                SELECT is_legacy, vpn_state, refund_state
                FROM payment_processing WHERE payment_id=?
            ''', (payment_id,)).fetchone()
            if existing and existing['is_legacy']:
                return 'legacy'
            if existing and existing['vpn_state'] == 'done':
                return 'done'
            if pay['status'] != 'pending':
                return 'held'

            db.execute('''
                INSERT OR IGNORE INTO payment_processing
                  (payment_id, payment_state, vpn_state, tax_state)
                VALUES (?, 'verified', 'not_started', 'queued')
            ''', (payment_id,))
            state = db.execute('''
                SELECT is_legacy, vpn_state, refund_state
                FROM payment_processing WHERE payment_id=?
            ''', (payment_id,)).fetchone()
            if state['is_legacy']:
                return 'legacy'
            if state['vpn_state'] == 'done':
                return 'done'
            if state['refund_state'] != 'none':
                return 'held'
            if state['vpn_state'] != 'not_started':
                return 'held'  # processing or needs_review: never retry blindly.
            db.execute('''
                UPDATE payment_processing
                   SET payment_state='verified', vpn_state='processing',
                       updated_at=CURRENT_TIMESTAMP
                 WHERE payment_id=? AND vpn_state='not_started'
            ''', (payment_id,))
            return 'claimed'

    def complete(self, payment_id, subscription_id, *, duration_days=None, action=None):
        """Commit successful VPN effect. Raises if payment is not claimed."""
        if not isinstance(subscription_id, int) or subscription_id <= 0:
            raise ValueError('Missing positive subscription_id')
        with self._tx() as db:
            status = db.execute(
                'SELECT status FROM payments WHERE payment_id=?', (payment_id,)
            ).fetchone()
            if not status or status['status'] != 'pending':
                raise RuntimeError('Payment no longer pending; manual review required')
            cur = db.execute('''
                UPDATE payment_processing
                   SET vpn_state='done', subscription_id=?, last_error=NULL,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE payment_id=? AND is_legacy=0 AND vpn_state='processing'
            ''', (subscription_id, payment_id))
            if cur.rowcount != 1:
                raise RuntimeError('Invalid VPN state; manual review required')
            db.execute(
                "UPDATE payments SET status='paid' WHERE payment_id=?",
                (payment_id,)
            )
            from referral_accrual import record_completed_purchase
            record_completed_purchase(db, payment_id, duration_days, action)

    def mark_review(self, payment_id, reason):
        """Retain uncertain outcome for manual reconciliation. No auto-retry."""
        with self._tx() as db:
            db.execute('''
                UPDATE payment_processing
                   SET vpn_state='needs_review', last_error=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE payment_id=? AND vpn_state='processing'
            ''', (str(reason)[:300], payment_id))
