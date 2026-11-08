from . import catalog
from .models import Blueprint, Expansion
from .recognition import Recognition, match
from .tests import LedgerTestCase

FABLE = "Fable of the Mirror-Breaker"


class MatchBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()
        catalog.sync_expansions(self.api)
        self.neo = Expansion.objects.get(code="neo")
        catalog.ensure_blueprints(self.api, self.neo)


class MatchTest(MatchBase):
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


class MatchReviewFixesTest(MatchBase):
    def test_locked_expansion_follows_other_printed_set(self):  # I2
        rec = Recognition(name="Counterspell", set_code="dmr", collector_number="45", language="EN")
        m = match(self.api, rec, self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1201))

    def test_name_only_across_loaded_sets_is_uncertain(self):  # I3
        m = match(self.api, Recognition(name=FABLE + "", collector_number="356", language="EN"), None)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual([b.ct_id for b in m.candidates], [1101])

    def test_unknown_codes_sync_expansions_once(self):  # I5
        match(self.api, Recognition(name="X", set_code="zz1", language="EN"), None)
        match(self.api, Recognition(name="Y", set_code="zz2", language="EN"), None)
        self.assertEqual(self.ct.calls.count(("GET", "/expansions")), 2)  # setUp + one throttled sync
