from celery import shared_task

from . import cardmarket


@shared_task(name="ledger.sync_cardmarket")
def sync_cardmarket():
    """Refresh the Cardmarket catalog and prices (outcome in the SyncLog)."""
    cardmarket.run_sync()
