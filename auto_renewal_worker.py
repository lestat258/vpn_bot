"""Single supervised auto-renewal task, hosted by the webhook process."""
import asyncio
import logging

from auto_renewal import activated, review_count

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 300


async def cycle(path, manager):
    # Reconciliation remains visible regardless of the new-charge gate.
    pending = await asyncio.to_thread(review_count,path)
    if pending:
        logger.warning("Automatic renewal: %s operations require operator reconciliation",pending)
    if activated():
        await manager.process_renewals()


async def renewal_worker(path, *, interval=INTERVAL_SECONDS, manager=None):
    if manager is None:
        from auto_renew import AutoRenewManager
        manager=AutoRenewManager(db_path=path)
    logger.info("Auto renewal scheduler started: interval=%s, activation=%s",interval,activated())
    try:
        while True:
            try:
                await cycle(path,manager)
            except Exception as exc:
                logger.error("Automatic renewal cycle unavailable: %s",type(exc).__name__)
            await asyncio.sleep(interval)
    finally:
        if manager.bot is not None:
            await manager.bot.session.close()
