from io import StringIO

from django.core.management import call_command

from . import catalog
from .models import Blueprint, Expansion
from .tests import LedgerTestCase


class CatalogTest(LedgerTestCase):
    def test_sync_expansions_mtg_only(self):
        self.assertEqual(catalog.sync_expansions(self.ct.client()), 4)
        self.assertFalse(Expansion.objects.filter(ct_id=99).exists())

    def test_ensure_blueprints_loads_once(self):
        client = self.ct.client()
        catalog.sync_expansions(client)
        neo = Expansion.objects.get(ct_id=11)
        catalog.ensure_blueprints(client, neo)
        catalog.ensure_blueprints(client, neo)
        self.assertEqual(self.ct.calls.count(("GET", "/blueprints/export")), 1)
        self.assertEqual(Blueprint.objects.filter(expansion__code="neo").count(), 2)
        showcase = Blueprint.objects.get(ct_id=1101)
        self.assertEqual((showcase.collector_number, showcase.version), ("356", "showcase"))

    def test_expansion_by_code_case_insensitive(self):
        catalog.sync_expansions(self.ct.client())
        self.assertEqual(catalog.expansion_by_code("NEO").ct_id, 11)
        self.assertIsNone(catalog.expansion_by_code("zzz"))

    def test_command_set(self):
        call_command("sync_catalog", "--set", "mh2", stdout=StringIO())
        self.assertEqual(Blueprint.objects.filter(expansion__code="mh2").count(), 4)
