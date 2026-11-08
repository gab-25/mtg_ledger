from . import catalog
from .models import Blueprint, Expansion
from .recognition import Recognition, match
from .tests import LedgerTestCase

FABLE = "Fable of the Mirror-Breaker"


class MatchTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()
        catalog.sync_expansions(self.api)
        self.neo = Expansion.objects.get(code="neo")
        catalog.ensure_blueprints(self.api, self.neo)

    def test_number_in_locked_expansion(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="0356", language="IT"), self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1101))

    def test_name_only_with_versions_is_ambiguous(self):
        m = match(self.api, Recognition(name=FABLE, language="EN"), self.neo)
        self.assertEqual(m.reason, "ambiguous")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.ct_id for b in m.candidates], [1100, 1101])

    def test_all_expansions_lazy_loads_set(self):
        rec = Recognition(name="Sheoldred, the Apocalypse", set_code="dmu", collector_number="107", language="EN")
        m = match(self.api, rec, None)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1301))
        self.assertTrue(Blueprint.objects.filter(expansion__code="dmu").exists())

    def test_unknown_set_code_not_found(self):
        m = match(self.api, Recognition(name="Nothing", set_code="zzz", language="EN"), None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))

    def test_name_number_mismatch_is_ambiguous(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="999", language="EN"), self.neo)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual({b.ct_id for b in m.candidates}, {1100, 1101})

    def test_split_card_front_face_matches(self):
        Blueprint.objects.create(ct_id=1199, expansion=self.neo, name="Fire // Ice", collector_number="400")
        m = match(self.api, Recognition(name="Fire", language="EN"), self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1199))

    def test_unreadable_language_needs_confirmation(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="356", language=""), self.neo)
        self.assertEqual(m.reason, "language")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.ct_id for b in m.candidates], [1101])
