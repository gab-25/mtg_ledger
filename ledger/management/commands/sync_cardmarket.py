from django.core.management.base import BaseCommand

from ledger import tasks


class Command(BaseCommand):
    help = "Queue the Cardmarket catalog and price sync on the Celery worker."

    def handle(self, *args, **options):
        result = tasks.sync_cardmarket.delay()
        self.stdout.write(f"Cardmarket sync queued (task {result.id})")
