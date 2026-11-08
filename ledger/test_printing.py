from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from .catalog import normalize_number
from .models import Card
from .tests import LedgerTestCase


class PrintingModelTest(LedgerTestCase):
    def test_two_printings_same_name_coexist(self):
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(Card.objects.filter(name="Fable of the Mirror-Breaker").exclude(collector_number="").count(), 2)

    def test_same_set_number_rejected(self):
        Card.objects.create(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="141")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(name="Other", set_code="NEO", collector_number="141")

    def test_legacy_rows_still_unique_by_name(self):
        Card.objects.create(name="X", set_code="M10")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Card.objects.create(name="X", set_code="M10")

    def test_str_shows_number(self):
        card = Card(name="Fable of the Mirror-Breaker", set_code="NEO", collector_number="356")
        self.assertEqual(str(card), "Fable of the Mirror-Breaker (NEO #356)")


class PrintingFormCsvTest(LedgerTestCase):
    def test_form_with_number_targets_printing(self):
        resp = self.client.post(reverse("ledger:item_create"), {
            "name": "Fable of the Mirror-Breaker", "set_code": "neo", "collector_number": "0356",
            "color": "R", "condition": "NM", "language": "EN", "quantity": 1, "price": "10.00",
        })
        self.assertRedirects(resp, reverse("ledger:collection"))
        self.assertTrue(Card.objects.filter(set_code="NEO", collector_number="356").exists())

    def test_csv_number_column_roundtrip(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,number,quantity\nFable of the Mirror-Breaker,neo,141,2\n")
        self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertTrue(Card.objects.filter(name="Fable of the Mirror-Breaker", set_code="NEO",
                                            collector_number="141").exists())
        exported = self.client.get(reverse("ledger:export_csv")).content.decode()
        self.assertIn("price,number", exported.splitlines()[0])
        self.assertIn(",141", exported)

    def test_normalize_number(self):
        self.assertEqual(normalize_number("0187"), "187")
        self.assertEqual(normalize_number("012a"), "12A")
        self.assertEqual(normalize_number(" 7 "), "7")
        self.assertEqual(normalize_number(""), "")
