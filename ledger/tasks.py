from celery import shared_task

from . import catalog_sync


@shared_task(name="ledger.sync_catalog")
def sync_catalog():
    """Refresh the CardTrader catalog and the prices of owned cards (outcome in the SyncLog)."""
    catalog_sync.run_sync()
