from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .models import CollectionItem, Listing, SyncConflict, SyncLog


class LedgerTestCase(TestCase):
    def setUp(self):
        call_command("seed_demo", stdout=StringIO())
        self.user = get_user_model().objects.create_user("gab", password="pw")
        self.client.force_login(self.user)


class PagesTest(LedgerTestCase):
    def test_login_required(self):
        self.client.logout()
        resp = self.client.get(reverse("ledger:collection"))
        self.assertRedirects(resp, f"{reverse('login')}?next=/")

    def test_main_pages_render(self):
        for name in ("collection", "sale", "sync", "item_create", "import_csv"):
            with self.subTest(name=name):
                resp = self.client.get(reverse(f"ledger:{name}"))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "theme/app.css")

    def test_collection_shows_stats_and_status(self):
        resp = self.client.get(reverse("ledger:collection"))
        self.assertContains(resp, "Ragavan, Nimble Pilferer")
        self.assertContains(resp, "Sync pending")
        self.assertEqual(resp.context["pending_changes"], 3)
        self.assertEqual(resp.context["stats"]["total"], 21)

    def test_collection_filters(self):
        resp = self.client.get(reverse("ledger:collection"), {"color": "B", "q": "seize"})
        names = [r["item"].card.name for r in resp.context["rows"]]
        self.assertEqual(names, ["Thoughtseize"])
        resp = self.client.get(reverse("ledger:collection"), {"set": "LTR", "cond": "NM"})
        self.assertEqual(len(resp.context["rows"]), 2)


class ItemTest(LedgerTestCase):
    def test_add_item_creates_card(self):
        resp = self.client.post(reverse("ledger:item_create"), {
            "name": "Lightning Bolt", "set_code": "m10", "type_line": "Instant", "color": "R",
            "condition": "NM", "language": "EN", "quantity": 4, "price": "1.50",
        })
        self.assertRedirects(resp, reverse("ledger:collection"))
        item = CollectionItem.objects.get(card__name="Lightning Bolt")
        self.assertEqual(item.card.set_code, "M10")
        self.assertEqual(item.quantity, 4)


class CsvTest(LedgerTestCase):
    def test_export_then_import_roundtrip(self):
        csv_bytes = self.client.get(reverse("ledger:export_csv")).content
        self.assertIn(b"name,set,type", csv_bytes)
        before = CollectionItem.objects.count()
        upload = SimpleUploadedFile("c.csv", csv_bytes + "Lightning Bolt,M10,Instant,R,NM,EN,0,4,1.50\n".encode())
        resp = self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertRedirects(resp, reverse("ledger:collection"))
        self.assertEqual(CollectionItem.objects.count(), before + 1)

    def test_import_rejects_bad_rows(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,condition\nBolt,M10,XX\n")
        resp = self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "invalid condition")
        self.assertFalse(CollectionItem.objects.filter(card__name="Bolt").exists())


class SaleSyncTest(LedgerTestCase):
    def test_publish_pending(self):
        self.client.post(reverse("ledger:publish"))
        self.assertFalse(Listing.objects.filter(status=Listing.Status.TO_PUBLISH).exists())
        self.assertTrue(SyncLog.objects.filter(message__contains="3 listings published").exists())

    def test_match_market_prices(self):
        self.client.post(reverse("ledger:match_prices"))
        solitude = Listing.objects.get(item__card__name="Solitude")
        self.assertEqual(solitude.price, Decimal("22.40"))
        self.assertEqual(solitude.status, Listing.Status.TO_PUBLISH)
        sold = Listing.objects.get(item__card__name="Endurance")
        self.assertEqual(sold.price, Decimal("11.00"))

    def test_edit_published_listing_requeues(self):
        item = CollectionItem.objects.get(card__name="Ragavan, Nimble Pilferer")
        self.client.post(reverse("ledger:listing_edit", args=[item.pk]), {
            "quantity": 1, "price": "45.00", "market_price": "46.50", "status": "published",
        })
        self.assertEqual(Listing.objects.get(item=item).status, Listing.Status.TO_PUBLISH)

    def test_listing_quantity_cannot_exceed_owned(self):
        item = CollectionItem.objects.get(card__name="The One Ring")
        resp = self.client.post(reverse("ledger:listing_edit", args=[item.pk]), {
            "quantity": 3, "price": "60", "status": "to_publish",
        })
        self.assertContains(resp, "You only own 1 copy.")

    def test_resolve_conflict_keep_remote_price(self):
        conflict = SyncConflict.objects.get(field="price")
        self.client.post(reverse("ledger:resolve", args=[conflict.pk]), {"keep": "remote"})
        conflict.refresh_from_db()
        self.assertEqual(conflict.resolution, "remote")
        self.assertEqual(conflict.item.listing.price, Decimal("0.70"))

    def test_sync_page_lists_open_conflicts(self):
        resp = self.client.get(reverse("ledger:sync"))
        self.assertEqual(len(resp.context["conflicts"]), 3)
        self.assertContains(resp, "Keep Ledger")
