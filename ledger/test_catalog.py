from io import StringIO

from django.core.management import call_command

from . import catalog
from .models import Blueprint, Card, CollectionItem, Expansion
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


class MergeOwnersTest(LedgerTestCase):
    def test_merge_keeps_owners_apart(self):
        source = Card.objects.create(name="Bolt", set_code="M10")
        target = Card.objects.create(name="Lightning Bolt", set_code="M10", collector_number="146")
        mine = CollectionItem.objects.create(owner=self.user, card=source, condition="NM", language="EN", quantity=2)
        theirs = CollectionItem.objects.create(owner=self.other, card=target, condition="NM", language="EN", quantity=5)
        self.assertTrue(catalog.merge_cards(source, target))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.card_id, mine.owner, mine.quantity), (target.pk, self.user, 2))
        self.assertEqual((theirs.owner, theirs.quantity), (self.other, 5))
        self.assertEqual(CollectionItem.objects.filter(card=target).count(), 2)
