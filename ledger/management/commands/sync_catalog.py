from django.core.management.base import BaseCommand, CommandError

from ledger import catalog, services
from ledger.models import Expansion


class Command(BaseCommand):
    help = "Refresh the local CardTrader catalog (expansions, and blueprints with --set or --all)."

    def add_arguments(self, parser):
        parser.add_argument("--set", dest="set_code", help="Reload the blueprints of one expansion (code)")
        parser.add_argument("--all", action="store_true", help="Reload the blueprints of every expansion")

    def handle(self, *args, set_code=None, all=False, **options):
        with services.service_client() as client:
            count = catalog.sync_expansions(client)
            self.stdout.write(f"{count} expansions")
            if set_code:
                expansion = catalog.expansion_by_code(set_code)
                if expansion is None:
                    raise CommandError(f"Unknown expansion '{set_code}'")
                targets = [expansion]
            elif all:
                targets = list(Expansion.objects.all())
            else:
                targets = []
            for expansion in targets:
                n = catalog.ensure_blueprints(client, expansion, force=True)
                self.stdout.write(f"{expansion}: {n} blueprints")
