from celery import shared_task

from . import cardmarket


@shared_task(name="ledger.sync_catalog")
def sync_catalog():
    """Refresh the CardTrader catalog and the Cardmarket prices (outcome in the SyncLog)."""
    cardmarket.run_sync()
