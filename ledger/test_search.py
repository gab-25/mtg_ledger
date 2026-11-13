from django.db import connection
from django.test import TestCase

CASES = [
    ("Lim-Dûl the Necromancer", "lim dul the necromancer"),
    ("Urza's Saga", "urzas saga"),
    ("Æther Vial", "aether vial"),
    ("Fire // Ice", "fire ice"),
]


class SearchTextSqlTest(TestCase):
    def test_normalises_names(self):
        with connection.cursor() as cursor:
            for name, expected in CASES:
                cursor.execute("SELECT ledger_search_text(%s)", [name])
                self.assertEqual(cursor.fetchone()[0], expected, name)
