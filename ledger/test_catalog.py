from io import StringIO

from django.core.management import call_command

from . import catalog
from .models import Blueprint, Card, Category, CollectionItem, Expansion
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

    def test_ensure_blueprints_stores_the_large_image(self):
        self.ct.blueprints[11][0]["image"] = {
            "url": "/uploads/blueprints/image/1101/1101-x.jpg",
            "show": {"url": "/uploads/blueprints/image/1101/show_1101-x.jpg"},
            "preview": {"url": "/uploads/blueprints/image/1101/preview_1101-x.jpg"},
        }
        client = self.ct.client()
        catalog.sync_expansions(client)
        catalog.ensure_blueprints(client, Expansion.objects.get(ct_id=11))
        without, with_image = Blueprint.objects.filter(expansion__ct_id=11).order_by("ct_id")
        self.assertEqual(with_image.image_large_url,
                         "https://cardtrader.com/uploads/blueprints/image/1101/show_1101-x.jpg")
        self.assertEqual(without.image_large_url, "")

    def test_expansion_by_code_case_insensitive(self):
        catalog.sync_expansions(self.ct.client())
        self.assertEqual(catalog.expansion_by_code("NEO").ct_id, 11)
        self.assertIsNone(catalog.expansion_by_code("zzz"))

    def test_command_loads_category_names(self):
        call_command("sync_catalog", "--set", "dmu", stdout=StringIO())
        self.assertEqual(Category.objects.get(ct_id=4).name, "Booster Boxes")

    def test_command_set(self):
        call_command("sync_catalog", "--set", "mh2", stdout=StringIO())
        self.assertEqual(Blueprint.objects.filter(expansion__code="mh2").count(), 4)


class CategoryTest(LedgerTestCase):
    def test_sync_categories_upserts(self):
        client = self.ct.client()
        self.assertEqual(catalog.sync_categories(client), 3)
        self.ct.categories[1]["name"] = "Booster Box"
        catalog.sync_categories(client)
        self.assertEqual(Category.objects.count(), 3)
        self.assertEqual(Category.objects.get(ct_id=4).name, "Booster Box")

    def test_ensure_blueprints_stores_category_and_properties(self):
        client = self.ct.client()
        catalog.sync_expansions(client)
        catalog.ensure_blueprints(client, Expansion.objects.get(ct_id=13))
        box, mat, card = (Blueprint.objects.get(ct_id=i) for i in (1390, 1391, 1301))
        self.assertEqual(box.category.ct_id, 4)
        self.assertTrue(box.is_product)
        self.assertTrue(box.accepts("mtg_language"))
        self.assertFalse(box.accepts("condition"))
        self.assertFalse(mat.accepts("mtg_language"))
        self.assertFalse(card.is_product)
        self.assertEqual(card.editable_properties, ["condition", "mtg_language", "mtg_foil"])

    def test_unknown_category_is_created_without_name(self):
        self.ct.blueprints[13][0]["category_id"] = 77
        client = self.ct.client()
        catalog.sync_expansions(client)
        catalog.ensure_blueprints(client, Expansion.objects.get(ct_id=13))
        self.assertEqual(Blueprint.objects.get(ct_id=1301).category.name, "")

    def test_blueprint_without_category_is_a_card(self):
        bp = Blueprint(name="x")
        self.assertFalse(bp.is_product)
        self.assertTrue(bp.accepts("mtg_language"))


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
