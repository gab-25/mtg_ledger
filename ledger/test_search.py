from django.db import connection
from django.test import TestCase

from .search import normalize_all

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

    def test_normalize_all_uses_the_sql_function(self):
        self.assertEqual(normalize_all(["Sœur Weiß", "Lim-Dûl"]), ["soeur weiss", "lim dul"])
        self.assertEqual(normalize_all([]), [])
