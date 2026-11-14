from django.core.management.base import BaseCommand, CommandError

from ledger import catalog, services, tasks


class Command(BaseCommand):
    help = (
        "Queue the full catalog sync (CardTrader expansions, categories and blueprints, owned cards prices) on the "
        "Celery worker; with --set, reload the blueprints of one expansion right away instead."
    )

    def add_arguments(self, parser):
        parser.add_argument("--set", dest="set_code", help="Reload now the blueprints of one expansion (code)")

    def handle(self, *args, set_code=None, **options):
        if not set_code:
            result = tasks.sync_catalog.delay()
            self.stdout.write(f"Catalog sync queued (task {result.id})")
            return
        with services.service_client() as client:
            catalog.sync_expansions(client)
            catalog.sync_categories(client)
            expansion = catalog.expansion_by_code(set_code)
            if expansion is None:
                raise CommandError(f"Unknown expansion '{set_code}'")
            n = catalog.ensure_blueprints(client, expansion, force=True)
            self.stdout.write(f"{expansion}: {n} blueprints")
